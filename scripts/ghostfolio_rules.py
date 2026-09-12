#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定层 —— 按资产类别分别采用公开量化项目的交易判定条件，不做自造规则或阈值。

**设计原则（Bob 2026-09-12 拍板）**：
> 不要自己建立规则和判定，就按量化算法的判定来定买入卖出持有。
> 美股的判定用 QuantConnect 的规则，加密货币和美股的判定规则分开。

**为什么分开**（已核实官方文档）：
  - freqtrade 官方 README：「a free and open source **crypto** trading bot」——
    支持列表全是加密交易所，**完全不含美股**，其策略参数是按加密市场结构调的。
  - Lean 官方文档：「LEAN works on Equities, Forex, Options, Futures, Indexes,
    Crypto, and CFD Assets」—— 覆盖美股与加密等九类资产。

所以：
  美股/ETF（QQQ VOO SCHD SMH SIVR SGOV）→ QuantConnect Lean 的官方 Alpha 模型
  加密货币（BTC ETH BNB）              → freqtrade 的策略判定

═══ 美股：QuantConnect/Lean 官方 Alpha 模型（21,588 ⭐）═══
 ① RsiAlphaModel.cs — GetState() 状态机
    rsi>70 → TrippedHigh（卖）；rsi<30 → TrippedLow（买）；
    TrippedLow 且 rsi>35 → Middle；TrippedHigh 且 rsi<65 → Middle；否则保持
    周期默认 14（构造函数）
 ② EmaCrossAlphaModel.cs — fast/slow EMA 位置（构造函数默认 fast=12, slow=26）
 ③ MacdAlphaModel.cs — normalizedSignal = MACD.Signal / Price，
    与常量 BounceThresholdPercent = 0.01 比较（第 38 行）；周期默认 12/26/9

═══ 加密货币：freqtrade 社区策略 **三个** 多数表决 ═══
   （freqtrade/freqtrade-strategies 的 berlinguyinca 目录；源码内均无自定义参数、
     三套逻辑不同源 —— 多数表决才名副其实。
     v10.2 修：本行此前只写 AdxSmas，与实际 CRYPTO_SOURCES 三条不符，会误导维护者。）

   AdxSmas.py（趋势跟随）
     买入：ADX(14) > 25 且 SMA(3) 上穿 SMA(6)
     卖出：ADX(14) < 25 且 SMA(6) 上穿 SMA(3)
   BbandRsi.py（均值回归）
     买入：RSI < 30 且 收盘价 < 布林下轨；卖出：RSI > 70
     ⚠️ 布林带用**典型价** (H+L+C)/3 —— 官方源码是 qtpylib.typical_price()
   AwesomeMacd.py（动量双确认）
     买入：MACD > 0 且 AO > 0 且 AO 上穿 0；卖出：MACD < 0 且 AO < 0 且 AO 下穿 0

   三条都是穿越/条件型状态机：触发后保持到反向触发为止，各自带 signal_age_days。
   报告的年龄只取**与最终多数同向**的票里最近那次触发。
   （注：freqtrade 官方模板 sample_strategy.py 条件过苛，实测 9 只一年 0 触发，不采用）

零新依赖：指标全部用 pandas/numpy 实现。
"""
import numpy as np
import pandas as pd

BUY, SELL, HOLD = "买入", "卖出", "持有"

# ─────────────────────────── 指标 ───────────────────────────

def _sma(s, n):
    return s.rolling(n).mean()


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    """Wilder RSI（与 Lean/freqtrade 的 Wilder 实现同口径）"""
    d = close.diff()
    au = d.clip(lower=0.0).ewm(alpha=1 / n, adjust=False).mean()
    ad = (-d).clip(lower=0.0).ewm(alpha=1 / n, adjust=False).mean()
    out = 100 - 100 / (1 + au / ad)        # ad=0 且 au>0 → inf → 100
    return out.where(~((au == 0) & (ad == 0)), 50.0)


def adx(high, low, close, n=14):
    """Wilder ADX（freqtrade AdxSmas 用的 ta.ADX，timeperiod=14）"""
    up, dn = high.diff(), -low.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = close.shift(1)
    tr = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(mdm, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _bollinger(close, n=20, k=2.0):
    """布林带：中轨 SMA(n)，上下轨 ±k 个标准差（BbandRsi / Low_BB 策略需要）"""
    mid = _sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def _awesome_oscillator(high, low):
    """Awesome Oscillator = SMA(median price, 5) − SMA(median price, 34)
    median price = (High + Low) / 2    —— qtpylib.awesome_oscillator 的标准定义"""
    mp = (high + low) / 2
    return _sma(mp, 5) - _sma(mp, 34)


def compute_indicators(h):
    c = h["Close"]
    hi = h["High"] if "High" in h.columns else c
    lo = h["Low"] if "Low" in h.columns else c
    _b_low, _b_mid, _b_up = _bollinger(c, 20, 2.0)
    # BbandRsi 官方源码用的是**典型价**上行情的布林带（qtpylib.typical_price = (H+L+C)/3），
    # 不是收盘价。两者轨道有系统性差异，且用收盘价会造成「收盘价既参与算轨道又拿去比较」的自指。
    _tp = (hi + lo + c) / 3.0
    _t_low, _t_mid, _t_up = _bollinger(_tp, 20, 2.0)
    return {
        "close": c,
        "volume": h["Volume"] if "Volume" in h.columns else pd.Series(0.0, index=c.index),
        "rsi14": rsi(c, 14),
        "ema12": _ema(c, 12), "ema26": _ema(c, 26),
        "sma3": _sma(c, 3), "sma6": _sma(c, 6),
        "adx14": adx(hi, lo, c, 14),
        "boll_lower": _b_low, "boll_mid": _b_mid, "boll_upper": _b_up,
        "boll_lower_tp": _t_low, "boll_mid_tp": _t_mid, "boll_upper_tp": _t_up,
        "ao": _awesome_oscillator(hi, lo),
        "macd": _ema(c, 12) - _ema(c, 26),
    }


def _last(x):
    s = x if isinstance(x, pd.Series) else pd.Series(x)
    s = s.dropna()
    return float(s.iloc[-1]) if len(s) else None


# ═══════════════ 美股：Lean 官方 Alpha 模型 ═══════════════

def lean_rsi(ind):
    """RsiAlphaModel.cs GetState() 完整状态机（30 进 / 35 出，70 进 / 65 出）"""
    s = ind["rsi14"].dropna()
    if len(s) < 2:
        return HOLD
    state = "Middle"
    for v in s.to_numpy():
        if v > 70:
            state = "TrippedHigh"
        elif v < 30:
            state = "TrippedLow"
        elif state == "TrippedLow" and v > 35:
            state = "Middle"
        elif state == "TrippedHigh" and v < 65:
            state = "Middle"
    return {"TrippedLow": BUY, "TrippedHigh": SELL}.get(state, HOLD)


def lean_ema(ind):
    """EmaCrossAlphaModel.cs：EMA(12) 在 EMA(26) 上方 → Up，反之为 Down"""
    f, sl = _last(ind["ema12"]), _last(ind["ema26"])
    if f is None or sl is None:
        return HOLD
    return BUY if f > sl else SELL


def lean_macd(ind):
    """MacdAlphaModel.cs：MACD.Signal / Price 与 ±BounceThresholdPercent(0.01) 比较"""
    c = ind["close"]
    sig = _ema(_ema(c, 12) - _ema(c, 26), 9)
    s, px = _last(sig), _last(c)
    if s is None or not px:
        return HOLD
    n = s / px
    if n > 0.01:
        return BUY
    if n < -0.01:
        return SELL
    return HOLD


US_SOURCES = [
    ("Lean RSI 模型", "QuantConnect/Lean · RsiAlphaModel.cs",
     "RSI 状态机：30进/35出 买，70进/65出 卖", lean_rsi),
    ("Lean EMA 交叉", "QuantConnect/Lean · EmaCrossAlphaModel.cs",
     "EMA(12) 与 EMA(26) 的位置", lean_ema),
    ("Lean MACD 模型", "QuantConnect/Lean · MacdAlphaModel.cs",
     "MACD信号/价格 与 ±1% 比较", lean_macd),
]


# ═══════════════ 加密货币：freqtrade AdxSmas ═══════════════

def ft_adxsmas_detail(ind):
    """返回 dict：state / n_buy / n_sell / last_signal_date / signal_age_days

    为什么要 signal_age：AdxSmas 是**穿越型状态机** —— 只在均线穿越那一根发信号，
    之后一直保持该状态。所以「买入」可能代表 3 天前的信号，也可能代表 90 天前的。
    不给年龄，读者会把「买入」误读成「今天刚出的买入信号」。
    （对比：美股的 Lean 三模型是**当前状态**判定，每天重算，不存在年龄问题。）
    """
    df = pd.DataFrame({"a": ind["adx14"], "s": ind["sma3"], "l": ind["sma6"]}).dropna()
    if len(df) < 2:
        return {"state": HOLD, "n_buy": 0, "n_sell": 0,
                "last_signal_date": None, "signal_age_days": None,
                # 单位是**日线根数**，不是自然日也不是交易日（评审 Perplexity #8）：
            # 加密 7×24 连续交易，一根日线 = 一天。
            "signal_age_unit": "daily_bar"}
    a = df["a"].to_numpy(); s = df["s"].to_numpy(); l = df["l"].to_numpy()
    state, n_buy, n_sell, last_i = HOLD, 0, 0, None
    for i in range(1, len(df)):
        if a[i] > 25 and s[i - 1] <= l[i - 1] and s[i] > l[i]:
            state, n_buy, last_i = BUY, n_buy + 1, i
        elif a[i] < 25 and l[i - 1] <= s[i - 1] and l[i] > s[i]:
            state, n_sell, last_i = SELL, n_sell + 1, i
    last_date = df.index[last_i] if last_i is not None else None
    age = (len(df) - 1 - last_i) if last_i is not None else None
    # index 可能是 Timestamp（真实数据）也可能是整数（自检用的合成序列）
    if last_date is not None and hasattr(last_date, "strftime"):
        last_str = last_date.strftime("%Y-%m-%d")
    elif last_date is not None:
        last_str = str(last_date)
    else:
        last_str = None
    return {"state": state, "n_buy": n_buy, "n_sell": n_sell,
            "last_signal_date": last_str,
            "signal_age_days": (int(age) if age is not None else None),
            "signal_age_unit": "daily_bar"}


def _detail(df, state, last_i, n_buy, n_sell):
    """把扫描结果打包成统一结构（含最后信号日期与年龄）。
    index 可能是 Timestamp（真实数据）也可能是整数（自检用合成序列），两种都兼容。"""
    last_date = df.index[last_i] if last_i is not None else None
    age = (len(df) - 1 - last_i) if last_i is not None else None
    if last_date is not None and hasattr(last_date, "strftime"):
        last_str = last_date.strftime("%Y-%m-%d")
    elif last_date is not None:
        last_str = str(last_date)
    else:
        last_str = None
    return {"state": state, "n_buy": n_buy, "n_sell": n_sell,
            "last_signal_date": last_str,
            "signal_age_days": (int(age) if age is not None else None),
            "signal_age_unit": "daily_bar"}


def _empty_detail():
    return {"state": HOLD, "n_buy": 0, "n_sell": 0,
            "last_signal_date": None, "signal_age_days": None,
            "signal_age_unit": "daily_bar"}


def _scan_state(df, buy_at, sell_at):
    """通用状态机扫描 —— 三个 freqtrade 策略共用。

    逐根判定，返回 (state, last_signal_index, n_buy, n_sell)。
    buy_at(i)/sell_at(i) 以当前下标为参数，可自行看 i-1 来表达「穿越」语义。
    「状态延续」= 触发后保持，直到反向条件成立 —— 与 freqtrade 机器人
    「持仓到 exit 信号」的行为一致（区别见 ft_*_detail 的说明）。
    """
    state, n_buy, n_sell, last_i = HOLD, 0, 0, None
    for i in range(1, len(df)):
        if buy_at(i):
            state, n_buy, last_i = BUY, n_buy + 1, i
        elif sell_at(i):
            state, n_sell, last_i = SELL, n_sell + 1, i
    return state, last_i, n_buy, n_sell


def ft_adxsmas_signals(ind):
    """兼容旧签名：返回 (状态, 买入数, 卖出数)。新代码请用 ft_adxsmas_detail()。"""
    d = ft_adxsmas_detail(ind)
    return d["state"], d["n_buy"], d["n_sell"]


def ft_bbandrsi_detail(ind):
    """freqtrade-strategies / berlinguyinca / BbandRsi.py —— 均值回归型

    源码：
      买入（enter_long）：RSI < 30 且 收盘价 < 布林下轨
      卖出（exit_long） ：RSI > 70
    无自定义参数（源码里没有 IntParameter/DecimalParameter）。

    ⚠️ 布林带必须用**典型价** (H+L+C)/3 —— 官方源码是
    `qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)`。
    用收盘价算轨道会有系统性差异，且造成「收盘价既参与算轨道、又拿去比较」的自指。
    """
    df = pd.DataFrame({"rsi": ind["rsi14"], "c": ind["close"],
                       "bl": ind["boll_lower_tp"]}).dropna()
    if len(df) < 2:
        return _empty_detail()
    r = df["rsi"].to_numpy(); c = df["c"].to_numpy(); bl = df["bl"].to_numpy()
    st, li, nb, ns = _scan_state(
        df,
        lambda i: r[i] < 30 and c[i] < bl[i],
        lambda i: r[i] > 70,
    )
    return _detail(df, st, li, nb, ns)


def ft_bbandrsi(ind):
    """投票用：只要状态。"""
    return ft_bbandrsi_detail(ind)["state"]


def ft_awesomemacd_detail(ind):
    """freqtrade-strategies / berlinguyinca / AwesomeMacd.py —— 动量双确认

    源码：
      买入：MACD > 0 且 AO > 0 且 AO 上穿 0（ao.shift() < 0 表示前一根还在 0 下方）
      卖出：MACD < 0 且 AO < 0 且 AO 下穿 0
    AO = Awesome Oscillator（SMA(中位价,5) − SMA(中位价,34)）。
    无自定义参数。与 AdxSmas 互补：它用振荡器族，AdxSmas 用均线族。
    """
    df = pd.DataFrame({"m": ind["macd"], "ao": ind["ao"]}).dropna()
    if len(df) < 2:
        return _empty_detail()
    m = df["m"].to_numpy(); ao = df["ao"].to_numpy()
    st, li, nb, ns = _scan_state(
        df,
        lambda i: m[i] > 0 and ao[i] > 0 and ao[i - 1] < 0,
        lambda i: m[i] < 0 and ao[i] < 0 and ao[i - 1] > 0,
    )
    return _detail(df, st, li, nb, ns)


def ft_awesomemacd(ind):
    """投票用：只要状态。"""
    return ft_awesomemacd_detail(ind)["state"]


def ft_adxsmas(ind):
    """投票用：只要状态。"""
    return ft_adxsmas_signals(ind)[0]


CRYPTO_SOURCES = [
    ("freqtrade AdxSmas", "freqtrade/freqtrade-strategies · AdxSmas.py",
     "ADX>25 且 SMA(3) 上穿 SMA(6)", ft_adxsmas),
    ("freqtrade BbandRsi", "freqtrade/freqtrade-strategies · BbandRsi.py",
     "RSI<30 且收盘<布林下轨 买；RSI>70 卖", ft_bbandrsi),
    ("freqtrade AwesomeMacd", "freqtrade/freqtrade-strategies · AwesomeMacd.py",
     "MACD>0 且 AO 上穿 0 买；反之卖", ft_awesomemacd),
]
# 说明：三条都是 freqtrade 社区策略、**源码里没有自定义参数**（无 IntParameter 等），
# 且逻辑互补 —— AdxSmas 趋势跟随 / BbandRsi 均值回归 / AwesomeMacd 动量双确认。
# 这样加密侧的「多数票」才名副其实（此前只有 1 票，等于没有表决）。


# ─────────────────────────── 判定入口 ───────────────────────────

def _combine(verdicts):
    """多数票。无多数（三方各一票）→ 持有（无共识，不给操作结论）。

    不设「买优先」之类的人为倾向 —— 那是自造规则。

    ⚠️ vote 为 None 表示该策略**计算失败**，直接不参与投票（total 只数有效票）。
    此前把异常吞成 HOLD，会让「指标算错」和「策略真的看持有」在报告里长得一模一样 ——
    错误信息被伪装成正常交易信号。
    """
    valid = [v for v in verdicts if v["vote"] is not None]
    failed = len(verdicts) - len(valid)
    buy = sum(1 for v in valid if v["vote"] == BUY)
    sell = sum(1 for v in valid if v["vote"] == SELL)
    hold = len(valid) - buy - sell
    counts = {BUY: buy, SELL: sell, HOLD: hold}
    top = max(counts.values()) if valid else 0
    winners = [k for k, v in counts.items() if v == top] if valid else []
    if not valid:
        majority = None            # 全失败 → 不给判定，而不是默认「持有」
    else:
        majority = winners[0] if len(winners) == 1 else HOLD
    return {"verdicts": verdicts, "buy": buy, "sell": sell, "hold": hold,
            "majority": majority, "tie": len(winners) > 1,
            "total": len(valid), "failed": failed}


# 加密侧三个策略各自的 detail 函数（用于取信号年龄）
_CRYPTO_DETAIL_FNS = {
    "freqtrade AdxSmas": ft_adxsmas_detail,
    "freqtrade BbandRsi": ft_bbandrsi_detail,
    "freqtrade AwesomeMacd": ft_awesomemacd_detail,
}


def raw_votes(ind, is_crypto=False):
    """各策略原始票。策略抛异常 → 该票 None 并带回错误文本。

    生产 evaluate 与回测 rule_eval 共用这一个函数 —— 之前回测自己裸调 _fn()，
    策略一异常就把整轮回测炸掉、失败票还会被算进分母，与生产语义分叉。
    """
    sources = CRYPTO_SOURCES if is_crypto else US_SOURCES
    votes, errors = [], []
    for _name, _src, _detail, fn in sources:
        try:
            votes.append(fn(ind))
            errors.append(None)
        except Exception as e:
            votes.append(None)
            errors.append(f"{type(e).__name__}: {e}")
    return votes, errors


def evaluate(hist, is_crypto=False):
    """美股走 Lean 三模型（RSI 状态机 / EMA 交叉 / MACD），加密走 freqtrade 三策略。
    两套完全分开，各自多数表决。
    """
    sources = CRYPTO_SOURCES if is_crypto else US_SOURCES
    try:
        ind = compute_indicators(hist)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "majority": None,
                "verdicts": [], "buy": 0, "sell": 0, "hold": 0, "tie": False,
                "total": 0, "failed": 0}
    _votes, _errs = raw_votes(ind, is_crypto)   # 失败票记 None，不投票，如实登记
    verdicts = [{"source": name, "from": src, "detail": detail, "vote": v, "error": e}
                for (name, src, detail, _fn), v, e in zip(sources, _votes, _errs)]
    out = _combine(verdicts)
    out["asset_class"] = "crypto" if is_crypto else "us_equity"

    # ── 信号年龄 ──
    # 加密侧三条都是**穿越型状态机**：只在触发那根改状态，之后保持到反向触发。
    # 因此「买入」可能代表 3 天前的信号，也可能代表 90 天前的 —— 必须标年龄。
    # 美股侧：Lean 的 RSI 模型**同样是粘性状态机**（RSI 在 30~35 死区里晃时
    # 「买入」会一直挂着，直到 RSI 回到 35 以上才转 Middle），EMA 交叉与 MACD
    # 才是真正的「当天重算」。
    # ponytail: 美股侧目前只标注语义、不给年龄 —— Lean 的 RSI 状态机年龄需要
    # 复刻 GetState() 的分支历史，收益不大；等真有人被误导再加。
    if is_crypto:
        per = {}
        for _nm, _, _, _fn in CRYPTO_SOURCES:
            _detf = _CRYPTO_DETAIL_FNS.get(_nm)
            if _detf is None:
                continue
            try:
                per[_nm] = _detf(ind)
            except Exception:
                per[_nm] = {"state": None, "signal_age_days": None,
                            "last_signal_date": None}
        out["strategy_ages"] = {
            k: {"state": v["state"], "signal_age_days": v["signal_age_days"],
                "last_signal_date": v["last_signal_date"]}
            for k, v in per.items()}
        # 聚合年龄：只统计**与最终多数同向**的票里最近的那次触发。
        # 此前用 min(ages) 且只取 AdxSmas —— 多数可能是另外两条策略达成的，
        # 挂上 AdxSmas 的年龄等于张冠李戴。
        _maj = out.get("majority")
        # ⚠️ 年龄和日期必须取自**同一条**策略。分开取会矛盾：
        # min(年龄)=最近一次触发，而 min(ISO 日期字符串)=最旧的日期。
        # 实测 BTC age=9（AdxSmas 9 天前）却报 last_signal_date=2026-08-20（23 天前，来自 AwesomeMacd）。
        _same = [v for v in per.values()
                 if v.get("state") == _maj and v.get("signal_age_days") is not None]
        if _maj in (BUY, SELL) and _same:
            _best = min(_same, key=lambda v: v["signal_age_days"])
            out["signal_age_days"] = _best["signal_age_days"]
            out["last_signal_date"] = _best.get("last_signal_date")
            out["age_basis"] = "驱动当前多数判定的策略中最近一次触发"
        else:
            out["signal_age_days"] = None
            out["last_signal_date"] = None
            out["age_basis"] = "无多数共识，不给年龄"
        out["signal_semantics"] = "state_machine"
    else:
        out["signal_semantics"] = "lean_current_state"
        out["age_note"] = "EMA/MACD 为当天重算；RSI 模型为粘性状态机，未标年龄"
    out["source_project"] = ("freqtrade/freqtrade-strategies" if is_crypto
                             else "QuantConnect/Lean")
    return out


def _selfcheck():
    n = 500
    up = pd.Series(np.linspace(100, 400, n))
    hu = pd.DataFrame({"Close": up, "High": up * 1.005, "Low": up * 0.995,
                       "Volume": np.full(n, 1e6)})
    dn = pd.Series(np.linspace(400, 100, n))
    hd = pd.DataFrame({"Close": dn, "High": dn * 1.005, "Low": dn * 0.995,
                       "Volume": np.full(n, 1e6)})

    # 美股：Lean 三模型
    ru, rd = evaluate(hu), evaluate(hd)
    assert len(ru["verdicts"]) == len(US_SOURCES), "美股应为 3 个 Lean 模型"
    assert rd["majority"] == SELL, f"单边下跌应判卖出：{rd['majority']}"
    assert ru["buy"] >= 1, f"单边上涨应有买入票：{ru['verdicts']}"

    # 加密：AdxSmas 是「穿越型」策略 —— 直接喂构造好的指标序列测策略逻辑本身，
    # 比通过合成价格间接测可靠（光滑价格没有回踩，穿越点可能一个都不出现）。
    ind = {
        #  idx:     0    1    2    3    4    5
        "adx14": pd.Series([10.0, 30, 30, 20, 20, 20]),
        "sma3":  pd.Series([1.0, 1, 3, 3, 3, 0]),
        "sma6":  pd.Series([2.0, 2, 2, 1, 1, 3]),
    }
    # i=2（adx30>25，sma3 上穿 sma6）→ 买入；i=5（adx20<25，sma6 上穿 sma3）→ 卖出
    st_all, nb, ns = ft_adxsmas_signals(ind)
    assert nb == 1, f"应识别出 1 个买入信号，实际 {nb}"
    assert ns == 1, f"应识别出 1 个卖出信号，实际 {ns}"
    assert st_all == SELL, f"最后一个信号是卖出，状态应为卖出：{st_all}"
    ind_buy = {k: v.iloc[:4] for k, v in ind.items()}     # 截到 i=3：最后信号是买入
    assert ft_adxsmas_signals(ind_buy)[0] == BUY, "截断后状态应为买入"

    # 真实形态（趋势 + 回踩）应能触发买入
    t = np.arange(600, dtype=float)
    up_noisy = 100 + t * 0.35 + 6 * np.sin(t * 2 * np.pi / 25)
    a = np.asarray(up_noisy, dtype=float)
    h_noisy = pd.DataFrame({"Close": a, "High": a * 1.006, "Low": a * 0.994,
                            "Volume": np.full(len(a), 1e6)})
    _, u_buy, _ = ft_adxsmas_signals(compute_indicators(h_noisy))
    assert u_buy > 0, f"带回踩的上涨序列应产生买入信号，实际 {u_buy}"

    cu = evaluate(h_noisy, is_crypto=True)
    assert len(cu["verdicts"]) == len(CRYPTO_SOURCES), f"加密应有 {len(CRYPTO_SOURCES)} 个策略"
    assert [v["source"] for v in cu["verdicts"]] == [s[0] for s in CRYPTO_SOURCES], "加密策略名单不符"
    assert cu["majority"] in (BUY, SELL, HOLD), cu["majority"]
    assert cu["signal_semantics"] == "state_machine" and "signal_age_days" in cu, "加密侧应带 signal_age"
    assert ru["signal_semantics"] == "lean_current_state", "美股侧语义标注不符"
    # 三家各自也要能跑出结果（不能有异常静默降级成 HOLD）
    for _nm, _, _, _fn in CRYPTO_SOURCES:
        _ind2 = compute_indicators(h_noisy)
        assert _fn(_ind2) in (BUY, SELL, HOLD), f"{_nm} 返回值非法"

    # ── 评审提出的边界用例 ──
    # ① 三个加密策略**各自**都要有独立年龄（此前只有 AdxSmas 有）
    assert set(cu["strategy_ages"]) == {n for n, _, _, _ in CRYPTO_SOURCES}, \
        f"三个策略都应各自报年龄，实际 {list(cu['strategy_ages'])}"
    # ② 无多数共识时不给年龄（避免拿某一票的年龄冒充整体）
    if cu["majority"] not in (BUY, SELL):
        assert cu["signal_age_days"] is None, "无多数共识却给了年龄"
    # ③ 有年龄时必须来自与多数同向的策略
    if cu["signal_age_days"] is not None:
        _same = [v["signal_age_days"] for v in cu["strategy_ages"].values()
                 if v["state"] == cu["majority"]]
        assert cu["signal_age_days"] == min(_same), "年龄不是同向票里最近的触发"

    # ④ 策略抛异常 → 不投票，而不是伪装成「持有」
    _orig0 = CRYPTO_SOURCES[0]
    try:
        CRYPTO_SOURCES[0] = (CRYPTO_SOURCES[0][0], CRYPTO_SOURCES[0][1],
                             CRYPTO_SOURCES[0][2],
                             lambda ind: (_ for _ in ()).throw(RuntimeError("boom")))
        _ce = evaluate(h_noisy, is_crypto=True)
        _v0 = _ce["verdicts"][0]
        assert _v0["vote"] is None and "boom" in (_v0["error"] or ""), "异常应记为 None + error"
        assert _ce["failed"] == 1 and _ce["total"] == 2, "失败票不应计入总票数"
        assert _ce["hold"] == 0 or _ce["hold"] <= 1, "失败票不应变成持有票"
    finally:
        CRYPTO_SOURCES[0] = _orig0

    # ⑤ 无 OHLC 列（只有 Close）时不能崩
    _c_only = pd.DataFrame({"Close": np.linspace(100, 200, 300)})
    _rc = evaluate(_c_only)
    assert _rc["majority"] in (BUY, SELL, HOLD, None), "缺 High/Low 时应优雅降级"

    # ⑥ data 太短不能崩
    _tiny = hu.head(5)
    assert evaluate(_tiny)["total"] == 0 or evaluate(_tiny)["majority"] is not None

    # ⑦ BbandRsi 必须用**典型价**布林（官方 qtpylib.typical_price）
    _ind3 = compute_indicators(h_noisy)
    assert "boll_lower_tp" in _ind3, "缺少典型价布林带"
    assert not _ind3["boll_lower_tp"].equals(_ind3["boll_lower"]), \
        "典型价布林带与收盘价布林带不应相同（说明确实换了输入）"
    # ⑧ 年龄与日期必须同源（评审：min(age)=最近 而 min(ISO日期)=最旧 → 两字段会矛盾）
    if cu["signal_age_days"] is not None:
        _cand = {v["last_signal_date"] for v in cu["strategy_ages"].values()
                 if v["state"] == cu["majority"]
                 and v["signal_age_days"] == cu["signal_age_days"]}
        assert cu["last_signal_date"] in _cand, \
            (f"年龄({cu['signal_age_days']}天)对应的日期应是 {_cand}，"
             f"实际却报了 {cu['last_signal_date']} —— 年龄和日期不是同一条策略")

    # ⑨ 三条策略**全部**异常 → majority=None，报告不得当成「持有」
    _all = CRYPTO_SOURCES[:]
    try:
        for _k in range(len(CRYPTO_SOURCES)):
            CRYPTO_SOURCES[_k] = (CRYPTO_SOURCES[_k][0], CRYPTO_SOURCES[_k][1],
                                  CRYPTO_SOURCES[_k][2],
                                  lambda ind: (_ for _ in ()).throw(RuntimeError("all-boom")))
        _ae = evaluate(h_noisy, is_crypto=True)
        assert _ae["majority"] is None, "全失败应给 None，不能默认「持有」"
        assert _ae["total"] == 0 and _ae["failed"] == 3, "全失败时有效票应为 0"
    finally:
        for _k in range(len(CRYPTO_SOURCES)):
            CRYPTO_SOURCES[_k] = _all[_k]

    print(f"  加密三策略判定: " + " / ".join(
        f"{v['source'].split()[-1]}={v['vote']}" for v in cu["verdicts"]) + f" → {cu['majority']}")

    # 两套不混用
    assert evaluate(hu)["source_project"] != evaluate(hu, is_crypto=True)["source_project"]

    # 指标健全性
    ind = compute_indicators(hu)
    assert 0 <= _last(ind["rsi14"]) <= 100, "RSI 越界"
    assert _last(ind["adx14"]) >= 0, "ADX 为负"

    print(f"✅ rules 自检通过（美股：上涨 {ru['buy']}买{ru['sell']}卖 → {ru['majority']}，"
          f"下跌 → {rd['majority']}｜加密：AdxSmas 逻辑 1买1卖、真实形态 {u_buy} 个买入信号 → {cu['majority']}"
          f"｜三策略各自年龄 {len(cu['strategy_ages'])}/3、异常不投票、典型价布林）")


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        import yfinance as yf
        _CR = {"BTC-USD", "ETH-USD", "BNB-USD"}
        for tk in (sys.argv[1:] or ["QQQ", "BTC-USD"]):
            h = yf.Ticker(tk).history(period="3y", auto_adjust=True)
            r = evaluate(h, is_crypto=tk in _CR)
            print(f"\n── {tk}  [{r.get('source_project')}]  "
                  f"{r['buy']}买/{r['sell']}卖/{r['hold']}持 → {r['majority']}"
                  + ("  ⚠️无共识" if r.get("tie") else ""))
            for v in r["verdicts"]:
                print(f"     {v['vote']:<3} {v['source']:<20} {v['detail']}")
