#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则策略完整回测 —— 回答「这套判定规则真能赚钱吗」。

与旧的 ghostfolio_eval.py 的区别：旧脚本评价的是**已废弃的自研打分**（连续分数，测 IC/分层收益）；
本脚本评价的是**当前生产在用的规则**（状态型策略，必须做完整交易回放，不能只看 IC）。

方法（无前视偏差）：
  1. 一次性算好全部指标（指标都是因果的，不含未来信息）
  2. 逐根 bar 回放：只喂到第 i 根，得判定 → 第 i+1 根开盘（这里用收盘近似）成交
  3. 判定为「买入」→ 满仓持有多头；「卖出/持有」→ 空仓
  4. 计费：双边 0.1% 手续费 + 0.05% 滑点（加密按 0.2% + 0.1%）
  5. 对标基准：同期买入并持有

输出：年化收益 / 最大回撤 / 夏普 / 胜率 / 盈亏比 / 交易次数 / 换手 / 相对买入持有的超额。

用法：
    python3 ghostfolio_rule_eval.py                # 全部标的，按资产类别汇总
    python3 ghostfolio_rule_eval.py --json         # 机器可读
    python3 ghostfolio_rule_eval.py --selfcheck
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghostfolio_rules as gr  # noqa: E402

WARMUP = 260          # 指标预热（SMA200 + 状态机需要足够历史）
COST = {"us_equity": (0.0010, 0.0005), "crypto": (0.0020, 0.0010)}   # (手续费, 滑点) 单边
# 成本敏感性实验用（评审建议）：0 = 理想化，0.5 = 乐观，1 = 当前假设，2 = 保守。
# 诊断价值：若 0 成本仍显著跑输 → 问题不是成本，而是规则的市场暴露不足或方向错；
#            若 0 成本接近基准、实际成本后明显落后 → 问题是 turnover 过高。
COST_MULT = 1.0
TRADING_DAYS = {"us_equity": 252, "crypto": 365}


def _votes_at(ind, i, is_crypto):
    """只用前 i+1 根数据做判定（防止前视）→ 各策略原始票，**失败票已剔除**。

    走生产的 raw_votes：之前裸调 _fn()，策略一异常就炸掉整轮回测，
    且失败票若返回 None 会被 Counter 计数、被 scaled 算进分母 —— 与生产分叉。
    """
    sub = {k: (v.iloc[:i + 1] if isinstance(v, pd.Series) else v) for k, v in ind.items()}
    votes, _errs = gr.raw_votes(sub, is_crypto)
    return [v for v in votes if v is not None]


def _majority_at(ind, i, is_crypto):
    """多数票（无共识 → HOLD）。"""
    vs = _votes_at(ind, i, is_crypto)
    if not vs:
        return gr.HOLD
    from collections import Counter
    c = Counter(vs)
    top = max(c.values())
    win = [k for k, v in c.items() if v == top]
    return win[0] if len(win) == 1 else gr.HOLD


# 仓位映射的两种口径（产品选择，不是实现细节 —— 必须写进结论前提）：
#   long_flat  ：买入=满仓，卖出/**持有**=空仓。HOLD 被当成「清仓」。
#   long_short ：买入=满仓，卖出=**做空**，持有=空仓。
# Lean 的 Alpha 模型输出 InsightDirection.Down，在 Lean 官方框架里配合
# PortfolioConstructionModel 通常是要建**空头**仓位，而不是「清仓躺平」。
# 因此 long_flat 对美股侧是真实的口径偏离：短期技术面一翻空就把你从长期向上
# 的 ETF 里踢到现金上。用 long_short 跑一遍可分辨：跑输是「模型没用」还是
# 「多空被压成了多空仓」。
#   hold_keep  ：买入=满仓，卖出=空仓，**持有=保持昨日仓位**（不再清仓）。
# HOLD 占判定的大头，「HOLD 当空仓」会把大量时间晾在现金上 —— 这是 Grok 指出的
# 第三种口径，用来区分「规则本身没用」和「HOLD 被当成清仓」。
#   scaled     ：仓位 = 支持买入的票数 / 总有效票（1/3、2/3、3/3）。
# 前三者都是二元开关：「2买1卖」和「3买0卖」都给满仓，丢掉了确信度差异。
# 这是评审 Q3 的补充实验 —— 如果结论对仓位缩放也成立，就更能说明问题不在映射上。
POSITION_MODES = ("long_flat", "long_short", "hold_keep", "scaled")


def replay(hist, is_crypto, mode="long_flat"):
    """返回 positions。positions[i] 表示第 i 根收盘后持有的仓位。

    mode="long_flat"  → 取值 {0, 1}
    mode="long_short" → 取值 {-1, 0, 1}
    mode="hold_keep"  → 取值 {0, 1}，但 HOLD 时保持上一根仓位
    """
    assert mode in POSITION_MODES, f"未知仓位模式 {mode}"
    ind = gr.compute_indicators(hist)
    close = ind["close"]
    n = len(close)
    pos = np.zeros(n)
    for i in range(WARMUP, n):
        if mode == "scaled":
            # 净票数比例，不是「支持买入的票数比例」。
            # 原口径 `buy/total` 下「1买2卖」仍是 1/3 多头仓位，与「多数偏空」直觉不符
            # （评审指出）。改为 (buy-sell)/total 再 clip 到 [0,1]：多数偏空即 0 仓。
            _vs = _votes_at(ind, i, is_crypto)
            pos[i] = (max(0.0, (_vs.count(gr.BUY) - _vs.count(gr.SELL)) / len(_vs))
                      if _vs else 0.0)
            continue
        v = _majority_at(ind, i, is_crypto)
        # 第 i 根的判定 → 第 i+1 根生效（pos[i] 表示「第 i 根收盘时的持仓」）
        if v == gr.BUY:
            pos[i] = 1.0
        elif v == gr.SELL:
            pos[i] = -1.0 if mode == "long_short" else 0.0
        elif mode == "hold_keep":
            pos[i] = pos[i - 1] if i > 0 else 0.0    # 持有 = 不动
        else:
            pos[i] = 0.0
    pos = np.concatenate([[0.0], pos[:-1]])        # 右移一根，消除前视
    return pos


def metrics(close, pos, is_crypto, prev_pos=0.0):
    close = pd.Series(close).reset_index(drop=True)
    pos = pd.Series(pos).reset_index(drop=True)
    ret = close.pct_change().fillna(0.0)
    fee, slip = COST["crypto" if is_crypto else "us_equity"]
    fee, slip = fee * COST_MULT, slip * COST_MULT     # 成本敏感性实验用，默认 1.0
    # 分段统计时首根的 pos.diff() 是 NaN；原来的 fillna(|pos[0]|) 会把「期间开始就已持有」
    # 误当成一次新建仓 → 每段重复收一次开仓成本（评审 #6）。用上一根的真实仓位来补。
    turn = pos.diff()
    turn.iloc[0] = pos.iloc[0] - prev_pos
    turn = turn.abs()
    cost = turn * (fee + slip)
    strat = pos * ret - cost
    bench = ret

    td = TRADING_DAYS["crypto" if is_crypto else "us_equity"]
    ann = (1 + strat.clip(-0.99, None)).prod() ** (td / max(1, len(strat))) - 1
    bann = (1 + bench.clip(-0.99, None)).prod() ** (td / max(1, len(bench))) - 1
    vol = strat.std() * np.sqrt(td)
    sharpe = (strat.mean() * td) / vol if vol else 0.0
    eq = (1 + strat.clip(-0.99, None)).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())

    # 每笔交易（持仓段）收益
    trades, i = [], 0
    p = pos.to_numpy()
    while i < len(p):
        if p[i] != 0:                       # 多头(1)或空头(-1)持仓段都算一笔
            j = i
            while j + 1 < len(p) and p[j + 1] == p[i]:
                j += 1
            seg = strat.iloc[i:j + 1]
            trades.append(float((1 + seg).prod() - 1))
            i = j + 1
        else:
            i += 1
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    return {
        "bars": int(len(strat)),
        "annual_return": round(float(ann), 4),
        "bench_annual_return": round(float(bann), 4),
        "excess": round(float(ann - bann), 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(float(sharpe), 3),
        "trades": len(trades),
        "turnover_per_year": round(float(turn.sum() / max(1, len(turn)) * td), 2),
        "holding_period_holding_period_win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_win": round(float(np.mean(wins)), 4) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 4) if losses else None,
        "total_cost": round(float(cost.sum()), 4),
        "exposure": round(float(pos.mean()), 3),
    }


# 样本外切分：在**全样本上回放**（状态机需要连续预热），但**收益按时间段切片统计**。
# 目的：检验 −10% 这个结论是不是全样本过拟合的产物。
PERIODS = [
    ("训练期 2021-2024", "2000-01-01", "2024-12-31"),
    ("验证期 2025",      "2025-01-01", "2025-12-31"),
    ("留出期 2026+",     "2026-01-01", "2099-12-31"),
]


def period_excess(h, pos, is_crypto, periods=None):
    """返回 {期间名: {年化, 基准年化, 超额, 交易数, 仓位占用}}

    注：h.index 已在 _load_history 里归一化为「无时区日期」，可直接比较。
    """
    close = pd.Series(h["Close"].to_numpy(), index=h.index).sort_index()
    p = pd.Series(pos, index=close.index)
    out = {}
    for name, a, b in (periods or PERIODS):
        sel = (close.index >= pd.Timestamp(a)) & (close.index <= pd.Timestamp(b))
        if sel.sum() < 40:                 # 样本太短不给结论
            out[name] = {"error": f"样本仅 {int(sel.sum())} 根，太短"}
            continue
        c2, p2 = close[sel].reset_index(drop=True), p[sel].reset_index(drop=True)
        # 该段第一根之前一根的持仓（0.0 = 段外空仓）→ 跨段持仓不再被重复计费
        _idx = np.flatnonzero(np.asarray(sel))   # sel 是 numpy 布尔数组，没有 .to_numpy()
        _prev = float(p.iloc[_idx[0] - 1]) if _idx[0] > 0 else 0.0
        m = metrics(c2, p2, is_crypto, prev_pos=_prev)
        out[name] = {"annual_return": m["annual_return"],
                     "bench_annual_return": m["bench_annual_return"],
                     "excess": m["excess"], "trades": m["trades"],
                     "exposure": m["exposure"], "bars": int(sel.sum())}
    return out


def run_split(symbols=None, period="5y", mode="long_flat"):
    """按训练/验证/留出三段分别统计超额收益"""
    CR = {"BTC-USD", "ETH-USD", "BNB-USD"}
    if symbols is None:
        symbols = ["QQQ", "VOO", "SCHD", "SMH", "SIVR", "SGOV",
                   "BTC-USD", "ETH-USD", "BNB-USD"]
    out = {}
    import time
    for tk in symbols:
        try:
            h = _load_history(tk, period)
            is_c = tk in CR
            pos = replay(h, is_c, mode)
            out[tk] = period_excess(h, pos, is_c)
            out[tk]["_class"] = "crypto" if is_c else "us_equity"
        except Exception as e:
            out[tk] = {"error": f"{type(e).__name__}: {e}"}
        time.sleep(0.5)
    return out


def render_split(res):
    L = ["# 规则策略 —— 样本外切分检验", "",
         "> 回放仍在**全样本**上跑（状态机需连续预热），但**超额收益按时间段切片统计**。",
         "> 目的：检验「跑不赢买入持有」是不是全样本过拟合的产物。", ""]
    names = [p[0] for p in PERIODS]
    L += ["| 标的 | " + " | ".join(names) + " |",
          "|---|" + "---|" * len(names)]
    for tk in sorted(res, key=lambda x: 0 if res[x].get("_class") == "us_equity" else 1):
        row = res[tk]
        if "error" in row:
            L.append(f"| {tk} | " + " | ".join([f"取不到"] * len(names)) + " |")
            continue
        cells = []
        for n in names:
            v = row.get(n) or {}
            cells.append("n/a" if "error" in v else f"**{v['excess']:+.1%}**")
        L.append(f"| {tk} | " + " | ".join(cells) + " |")
    # 分类汇总
    for cls, lab in (("us_equity", "美股/ETF"), ("crypto", "加密货币")):
        L += ["", f"**{lab} 各期平均超额**"]
        for n in names:
            vals = [(res[t].get(n) or {}).get("excess") for t in res
                    if res[t].get("_class") == cls and "error" not in res[t]
                    and "error" not in (res[t].get(n) or {})]
            vals = [v for v in vals if v is not None]
            if vals:
                L.append(f"- {n}：**{sum(vals)/len(vals):+.1%}**（{len(vals)} 只）")
    return "\n".join(L)


def _load_history(tk, period="5y", cache_dir=None, retries=3):
    """取历史价格：优先本地缓存，缺了才联网（带退避重试）。

    为什么要缓存：反复跑回测会触发 yfinance 限流（实测连续几次后全部返回空）。
    缓存到 /opt/data/cache/eval_prices/，之后重跑不联网。
    """
    import time
    cd = Path(cache_dir or (Path(__file__).resolve().parent.parent / "cache" / "eval_prices"))
    cd.mkdir(parents=True, exist_ok=True)
    f = cd / f"{tk.replace('^', '_').replace('/', '_')}_{period}.csv"
    min_rows = WARMUP + 60

    def _norm(df):
        """把 index 统一成「无时区日期」——
        yfinance 的 index 带时区，且夏令时/冬令时偏移不同（-04:00 / -05:00），
        直接 to_datetime 会报 Mixed timezones。"""
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(None).normalize()
        return df.sort_index()

    if f.exists():
        try:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            if len(df) >= min_rows:
                return _norm(df)
        except Exception:
            pass
    import yfinance as yf
    last = None
    for k in range(retries):
        try:
            h = yf.Ticker(tk).history(period=period, auto_adjust=True)
            if h is not None and len(h) >= min_rows:
                h = _norm(h)
                h.to_csv(f)
                return h
            last = f"返回 {0 if h is None else len(h)} 根"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (k + 1))          # 退避：3s / 6s / 9s
    raise RuntimeError(f"取不到历史（{last}）")


def run(symbols=None, period="5y", mode="long_flat"):
    CR = {"BTC-USD", "ETH-USD", "BNB-USD"}
    if symbols is None:
        symbols = ["QQQ", "VOO", "SCHD", "SMH", "SIVR", "SGOV",
                   "BTC-USD", "ETH-USD", "BNB-USD"]
    out = {}
    import time
    for tk in symbols:
        try:
            h = _load_history(tk, period)
        except Exception as e:
            out[tk] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if len(h) < WARMUP + 60:
            out[tk] = {"error": f"历史不足（{len(h)} 根 < {WARMUP + 60}）"}
            continue
        is_c = tk in CR
        try:
            pos = replay(h, is_c, mode)
            out[tk] = metrics(h["Close"], pos, is_c)
            out[tk]["asset_class"] = "crypto" if is_c else "us_equity"
        except Exception as e:
            out[tk] = {"error": f"{type(e).__name__}: {e}"}
        time.sleep(1.0)                  # 温和限速，避免触发 yfinance 429
    return out


def render(res):
    L = ["# 规则策略完整回测", "",
         f"> 回放方式：逐根 bar 只用历史数据判定 → 次日生效（无前视）；"
         f"买入=满仓持有多头，卖出/持有=空仓。",
         f"> 交易成本：美股 0.10%+0.05% 单边，加密 0.20%+0.10% 单边。预热 {WARMUP} 根。", "",
         "| 标的 | 规则年化 | 买入持有 | 超额 | 最大回撤 | 夏普 | 交易数 | 胜率 | 仓位占用 |",
         "|---|---|---|---|---|---|---|---|---|"]
    ok = {k: v for k, v in res.items() if "error" not in v}
    for tk in sorted(ok, key=lambda x: -ok[x]["excess"]):
        v = ok[tk]
        L.append(f"| {tk} | {v['annual_return']:+.1%} | {v['bench_annual_return']:+.1%} "
                 f"| **{v['excess']:+.1%}** | {v['max_drawdown']:.1%} | {v['sharpe']:.2f} "
                 f"| {v['trades']} | {(v['holding_period_holding_period_win_rate'] or 0):.0%} | {v['exposure']:.0%} |")
    errs = {k: v["error"] for k, v in res.items() if "error" in v}
    if errs:
        L += ["", "未跑成功："] + [f"- {k}：{e}" for k, e in errs.items()]
    for cls, label in (("us_equity", "美股/ETF"), ("crypto", "加密货币")):
        grp = [v for v in ok.values() if v.get("asset_class") == cls]
        if grp:
            ex = np.mean([v["excess"] for v in grp])
            L += ["", f"**{label} 平均超额收益：{ex:+.1%}**（{len(grp)} 只）"]
    return "\n".join(L)


def _selfcheck():
    """构造「先跌后涨」的行情：卖出信号应避免下跌段，买入信号应吃到上涨段"""
    n = 900
    down = np.linspace(300, 150, 400)
    up = np.linspace(150, 400, 500)
    a = np.concatenate([down, up])
    h = pd.DataFrame({"Close": a, "High": a * 1.006, "Low": a * 0.994,
                      "Volume": np.full(n, 1e6)})
    pos = replay(h, is_crypto=False)
    assert len(pos) == n, "仓位序列长度不符"
    assert set(np.unique(pos)) <= {0.0, 1.0}, "long_flat 下仓位只能是 0/1"
    # 做空模式：允许 -1，且必须在「先跌」段里真的出现过空头（否则模式没生效）
    pos_s = replay(h, is_crypto=False, mode="long_short")
    assert set(np.unique(pos_s)) <= {-1.0, 0.0, 1.0}, "long_short 下仓位只能取 -1/0/1"
    assert (pos_s == -1).any(), "先跌后涨的行情里应出现过空头仓位"
    assert len(pos_s) == n and not np.array_equal(pos, pos_s), "两种模式结果不应完全相同"
    _seen = set()
    # period_excess 必须真跑一遍 —— 上一轮只测了 run()，
    # 结果 period_excess 里把 numpy 布尔数组当 Series 处理（调 .to_numpy()）而出错，
    # 却一路通过自检，直到 --split 实测「全部取不到」才暴露。
    # 它需要带日期索引的行情（按时间段切片），所以单独构造一条。
    _n2 = 1500
    _idx2 = pd.date_range("2021-01-04", periods=_n2, freq="B")
    _a2 = np.concatenate([np.linspace(200, 120, 750), np.linspace(120, 260, 750)])
    _hd = pd.DataFrame({"Close": _a2, "High": _a2 * 1.006, "Low": _a2 * 0.994,
                        "Volume": np.full(_n2, 1e6)}, index=_idx2)
    _pe = period_excess(_hd, replay(_hd, False), False)
    assert _pe, "period_excess 没返回任何时间段"
    assert any("excess" in v for v in _pe.values()), f"period_excess 全部取不到: {_pe}"
    for _m in POSITION_MODES:
        _p = replay(h, False, _m)
        _seen.add(tuple(np.unique(_p)))
        _mm = metrics(h["Close"], _p, False)
        assert -1 < _mm["annual_return"] < 1000, f"{_m} 年化异常"
    assert len(_seen) >= 2, "三种仓位口径不应给出完全相同的结果"
    # hold_keep 必须在 HOLD 段里真的「保持」住仓位（否则模式没生效）
    _pk = replay(h, False, "hold_keep")
    assert _pk.sum() >= replay(h, False, "long_flat").sum(), "hold_keep 持仓时间不应少于 long_flat"
    assert pos.sum() > 0, "至少应有一段持仓"
    m = metrics(h["Close"], pos, False)
    assert -1 < m["annual_return"] < 100, f"年化收益异常: {m['annual_return']}"
    assert m["max_drawdown"] <= 0, "最大回撤应为非正数"
    assert m["trades"] >= 0 and m["exposure"] <= 1
    # 前视检查：把最后一根的价格抬高 10 倍，历史仓位不应变化
    h2 = h.copy(); h2.loc[h2.index[-1], "Close"] *= 10
    pos2 = replay(h2, is_crypto=False)
    assert np.array_equal(pos[:-1], pos2[:-1]), "存在前视偏差：改末根影响了历史仓位"
    # ── 回测 / 生产 规则一致性断言 ──
    # 最容易出的错：生产已经换成新版规则（比如加密从 1 票变 3 票），回测还在用旧的。
    # 两边都从 gr.CRYPTO_SOURCES / gr.US_SOURCES 取名单，这里把契约钉死。
    assert [s[0] for s in gr.CRYPTO_SOURCES] == [
        "freqtrade AdxSmas", "freqtrade BbandRsi", "freqtrade AwesomeMacd"], \
        f"回测与生产约定的加密策略名单不符：{[s[0] for s in gr.CRYPTO_SOURCES]}"
    assert len(gr.CRYPTO_SOURCES) == 3, "加密侧应为三策略多数表决（不是单策略）"
    assert len(gr.US_SOURCES) == 3, "美股侧应为 Lean 三模型"
    # 回测必须真的用「多数表决」，而不是只取第一条策略
    _h = pd.DataFrame({"Close": np.linspace(100, 200, WARMUP + 60),
                       "High": np.linspace(100, 200, WARMUP + 60) * 1.005,
                       "Low": np.linspace(100, 200, WARMUP + 60) * 0.995,
                       "Volume": np.full(WARMUP + 60, 1e6)})
    _prod = gr.evaluate(_h, is_crypto=True)
    assert len(_prod["verdicts"]) == len(gr.CRYPTO_SOURCES), \
        "生产判定票数与策略名单不一致"
    assert _prod.get("strategy_ages") is not None, \
        "生产判定应给出三策略各自年龄（回测/报告都依赖它）"
    # 生产异常口径：失败票 vote=None，不算持有
    for _v in _prod["verdicts"]:
        assert _v["vote"] in (gr.BUY, gr.SELL, gr.HOLD, None), "票值非法"

    print(f"✅ rule_eval 自检通过（回放仓位合法、指标健全、无前视偏差、"
          f"{len(POSITION_MODES)} 种仓位口径、生产/回测规则一致；"
          f"样例年化 {m['annual_return']:+.1%} / "
          f"回撤 {m['max_drawdown']:.1%} / {m['trades']} 笔）")


if __name__ == "__main__":
    # --cost-mult 0|0.5|1|2 —— 成本敏感性实验，默认 1（不改变既有结论）
    globals()["COST_MULT"] = 1.0
    for _a in sys.argv:
        if _a.startswith("--cost-mult"):
            globals()["COST_MULT"] = float(_a.split("=", 1)[1] if "=" in _a
                                           else sys.argv[sys.argv.index(_a) + 1])

    if "--selfcheck" in sys.argv:
        _selfcheck()
    elif "--split" in sys.argv:
        mode = ("long_short" if "--short" in sys.argv
                else "hold_keep" if "--keep" in sys.argv
                else "scaled" if "--scaled" in sys.argv else "long_flat")
        res = run_split(mode=mode)
        if "--json" in sys.argv:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(render_split(res))
    else:
        mode = ("long_short" if "--short" in sys.argv
                else "hold_keep" if "--keep" in sys.argv
                else "scaled" if "--scaled" in sys.argv else "long_flat")
        res = run(mode=mode)
        if "--json" in sys.argv:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(render(res))
