#!/usr/bin/env python3
"""Ghostfolio 数据契约测试。版本不再手写在文件里 —— 见下方 CUR_SCHEMA。

为什么需要它：这个项目已经发生过**三次静默失败**（配置路径返回 `{}`、
数据质量分母自指、`config_version` 写错位置导致加密标的全丢），
共同的形态是「产出看起来正常」。四处分散的 --selfcheck 覆盖的是**各自的算法**，
但没有一处检查**最终 JSON 的字段语义是否自洽**。

本文件补的就是这一层：对最终产物做不变量断言，不碰网络。

用法：
    python3 ghostfolio_contract_test.py                          # 只用合成 fixture
    python3 ghostfolio_contract_test.py --json /tmp/gf_report.json  # 额外校验真实产物

退出码：0 = 全部通过；1 = 有失败
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ALLOWED_SIGNALS = {"买入", "卖出", "持有"}
REQUIRED_TOP = ["positions", "tech", "signals", "portfolio_risk", "data_quality", "alerts",
                "deltas", "news", "ml_predictions", "llm_analyst_prompts", "config_version",
                "severity", "exit_hint", "failure_codes", "schema_version",
                "generated_at", "provenance"]
SEVERITIES = {"ok", "degraded", "critical"}
ALLOWED_ASSET_TYPES = {"equity", "crypto", "special"}

# 失败码闭合集（v10.2）。调度侧按码分流，所以拼错的码必须当场被拒 ——
# 否则 `degrade` / `degraded` 这类笔误会把上层带进未知状态（评审 ChatGPT #2）。
ALLOWED_FAILURE_CODES = {
    "PORTFOLIO_INCOMPLETE", "MARKET_DATA_MISSING", "MARKET_DATA_PARTIAL",
    "FUNDAMENTALS_DEGRADED", "ML_UNAVAILABLE", "SOURCE_FAILED",
}

# 决策类字段：产物里**根本不该存在**（评审 ChatGPT #2 第一层「结构性禁止」）。
# 黑名单只能事后抓措辞（「现在适合进场」就绕过去了），schema 里没有这些键
# 才是根本防线。
STRUCT_FORBIDDEN = {"recommendation", "action", "target_weight", "entry_price",
                    "stop_loss", "take_profit", "suggested_action"}

# 报告里禁止出现的「裸操作指令」词（评审 Grok 建议）——
# 判定层的原生枚举是 买入/卖出/持有，但**渲染层**必须翻译成偏多/偏空/中性。
# 出现下面这些单独用法，说明报告又变回了交易信号板。
REPORT_BANNED = ["建议买入", "建议卖出", "该买入", "该卖出", "止损", "加仓", "减仓",
                 "🟢买", "🔴卖", "⚪持", "强烈买入"]
# 允许出现「买入/卖出」的上下文（脚注里解释语义、变更记录）
REPORT_ALLOW_CTX = ["语义不同", "粘性状态机", "变更", "已废弃", "死引用", "不再", "只产出三值"]
# 操作语义 allow-list（v10.3，评审 ChatGPT P2）。报告只允许：观察 / 状态变化 /
# 风险提示 / 数据异常。下面这些词一律不得出现，除非处在解释性上下文里。
_ACTION_WORDS = ["加仓", "减仓", "建仓", "补仓", "清仓", "调仓", "再平衡",
                 "抄底", "追高", "止损", "止盈", "进场", "出场", "入场", "离场",
                 "满仓", "空仓", "择时操作", "风险收益比不值得"]
# 允许出现的上下文：否定句、方法论说明、变更记录、回测结论转述
_ACTION_ALLOW_CTX = ["不代表", "不构成", "不得", "不要", "无需", "禁止", "已废弃",
                     "不含", "不是", "只允许", "无正权重解", "规则层已被回测证明",
                     "语义", "变更记录", "死引用", "不再", "回测", "口径",
                     # 首次实跑抓到的两处假阳性（都是解释性文本，不是操作指令）：
                     #   市场情绪 = 63（贪婪）… 越高越容易追高
                     #   如 `RsiAlphaModel.cs` 的进出场条件、`BbandRsi.py` 的 RSI<30 买入
                     "源码", "越容易", "进出场条件", "术语速查", "白话解释"]

# fixture 的版本号一律从这里取 —— v10.5 的教训：手写的 "10.2"/"10.3" 一直没人升，
# 于是白名单漂了、契约测试还绿着。绑到代码常量就不可能再漂。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghostfolio_collect as _gc      # noqa: E402  （本文件其余地方是函数内局部导入）
CUR_SCHEMA = _gc.SCHEMA_VERSION
# 旧版存在、现已删除的字段 —— 出现即说明语义漂移又发生了
DEAD_FIELDS = ["attribution", "confidence_intervals", "alpha_models",
               "scoring", "legacy_signal_mapping", "signal_schema",
               "confidence", "reliability"]


def check_contract(d, label, problems):
    """对一份产物做全部不变量检查。problems 里直接追加问题描述。"""
    def bad(msg):
        problems.append(f"[{label}] {msg}")

    # ── ① 必需顶层键 ──
    for k in REQUIRED_TOP:
        if k not in d:
            bad(f"缺少顶层键 `{k}`")

    # ── ② 已删除字段不得复活（语义漂移哨兵）──
    for k in DEAD_FIELDS:
        if k in d:
            bad(f"出现已删除字段 `{k}` —— 自研评分体系残留又回来了")

    # ── ③ signals 不变量 ──
    for sym, v in (d.get("signals") or {}).items():
        if not isinstance(v, dict):
            bad(f"signals.{sym} 不是对象")
            continue
        total = v.get("total")
        # 特殊资产：applicable=False → 必须没有投票
        if v.get("applicable") is False:
            if total:
                bad(f"signals.{sym} applicable=False 却有 total={total}（特殊资产不该有投票）")
            if v.get("majority"):
                bad(f"signals.{sym} applicable=False 却有 majority={v['majority']}")
            # 状态必须存在。v10.2 真实发生过：状态值来自配置的 signal 键，删掉配置键
            # 就变成 None，还触发一次假的「状态变化」告警（持有→None）。
            # 状态是事实层，不能存在「配置改空就没了」的可能。
            if not v.get("signal"):
                bad(f"signals.{sym} applicable=False 但 signal 为空 —— "
                    f"固定资产的状态必须由代码给出，不能依赖配置")
            continue
        # 一般资产：票数自洽
        b, s_, h = v.get("buy"), v.get("sell"), v.get("hold")
        if total is not None and None not in (b, s_, h):
            if b + s_ + h != total:
                bad(f"signals.{sym} 票数不自洽: {b}+{s_}+{h} != total {total}")
        sig = v.get("signal")
        if sig is not None and sig not in ALLOWED_SIGNALS:
            bad(f"signals.{sym}.signal={sig!r} 不在 {ALLOWED_SIGNALS}"
                f"（若要写「多数偏多」这类措辞，那是报告层的事，不该写进产物）")
        # 年龄与日期必须同源且自洽
        age, lsd = v.get("signal_age_days"), v.get("last_signal_date")
        if age is not None:
            if age < 0:
                bad(f"signals.{sym}.signal_age_days={age} 为负")
            if not lsd:
                bad(f"signals.{sym} 有年龄却无 last_signal_date（不同源）")
        # 三条策略各自的年龄结构
        sa = v.get("strategy_ages")
        if sa is not None:
            for nm, sv in sa.items():
                if sv.get("state") is not None and sv.get("signal_age_days") is None:
                    bad(f"signals.{sym}.strategy_ages[{nm}] 有状态却无年龄")

    # ── ④ tech.rules 与 signals 不得双轨 ──
    # 这是 v9.9 那类「假票数」bug 的根源：tech 里有 rules、signals 里却说不适用。
    for sym, t in (d.get("tech") or {}).items():
        if not isinstance(t, dict):
            continue
        rr = t.get("rules") or {}
        sig = (d.get("signals") or {}).get(sym) or {}
        if rr.get("applicable") is False and rr.get("total"):
            bad(f"tech.{sym}.rules applicable=False 却有 total={rr['total']}")
        if sig.get("applicable") is False and rr and rr.get("applicable") is not False:
            bad(f"{sym}：signals 说不适用，tech.rules 却仍在跑规则 —— 双轨又出现了")

    # ── ④-2 基本面分母只能数「适用标的」（ETF ∩ 持仓），不是持仓总数 ──
    # 教训：分母写 9 会让「6 只 ETF 全成功」也判 FUNDAMENTALS_DEGRADED，
    # 正常日报天天 degraded —— 这个信号就废了。
    _dqf = d.get("data_quality") or {}
    _fval = _dqf.get("基本面")
    if isinstance(_fval, str) and "/" in _fval:
        try:
            _fh, _fe = _fval.replace("fail(", "").replace(")", "").split("/")
            _fh, _fe = int(_fh), int(_fe)
        except ValueError:
            _fh = _fe = None
        if _fe is not None:
            _etf_exp = len([s for s in (d.get("positions") or {}) if s in _gc.ETF_TICKERS])
            if _fe != _etf_exp:
                bad(f"基本面分母 {_fe} != 适用标的数 {_etf_exp}（ETF ∩ 持仓）—— "
                    "加密没有基本面，不能进分母")
            elif ("FUNDAMENTALS_DEGRADED" in (d.get("failure_codes") or [])) != (_fh < _fe):
                bad(f"基本面 {_fh}/{_fe} 与 FUNDAMENTALS_DEGRADED 不自洽")

    # ── ⑤ data_quality 的分母必须来自独立期望值 ──
    # 教训：分母取自产出本身时，「丢了 3 个持仓」会被算成「6 个全成功」。
    dq = d.get("data_quality") or {}
    n_pos = len(d.get("positions") or {})
    # 基本面例外：只有 ETF 适用，分母由下方专项断言管
    for key in ("行情", "ML"):
        val = dq.get(key)
        if not isinstance(val, str) or "/" not in val:
            continue
        try:
            have, exp = val.replace("fail(", "").replace(")", "").split("/")
            have, exp = int(have), int(exp)
        except ValueError:
            continue
        if exp != n_pos:
            bad(f"data_quality.{key}={val} 的分母不是持仓数 {n_pos} —— 分母可能又自指了")
        if have > exp:
            bad(f"data_quality.{key}={val} 分子大于分母")

    # ── ⑥ 组合风险层（新增，可选但字段要自洽）──
    rk = (d.get("portfolio_risk") or {}).get("risk") or {}
    if rk:
        ac = rk.get("asset_class") or {}
        if ac and abs(sum(ac.values()) - 1.0) > 0.02:
            bad(f"portfolio_risk.risk.asset_class 合计 {sum(ac.values()):.3f} 不是 1")
        rc = rk.get("risk_contribution") or {}
        if rc and abs(sum(rc.values()) - 1.0) > 0.02:
            bad(f"risk_contribution 合计 {sum(rc.values()):.3f} 不是 1")
        co = rk.get("correlation") or {}
        if co and not (-1.0 <= co.get("max_value", 0) <= 1.0):
            bad(f"相关系数 {co.get('max_value')} 超出 [-1,1]")

    # ── ⑦ 时间一致性（评审 ChatGPT #1）──
    # 任何「最近信号日期」都不能晚于报告生成时刻 —— 那是未来数据。
    import re as _re
    gen = None
    m = _re.search(r"(\d{4}-\d{2}-\d{2})", str(d.get("timestamp") or ""))
    if m:
        gen = m.group(1)
    if gen:
        for sym, v in (d.get("signals") or {}).items():
            lsd = (v or {}).get("last_signal_date")
            if lsd and str(lsd)[:10] > gen:
                bad(f"signals.{sym}.last_signal_date={lsd} 晚于生成时间 {gen}（未来数据）")

    # ── ⑧ 权重不变量（评审 ChatGPT #3）──
    w = (d.get("portfolio_risk") or {}).get("weights") or {}
    if w:
        for k, val in w.items():
            if not isinstance(val, (int, float)) or not (0 <= val <= 1):
                bad(f"weights.{k}={val} 不在 [0,1]")
        if abs(sum(w.values()) - 1.0) > 0.02:
            bad(f"weights 合计 {sum(w.values()):.3f} 偏离 1 超过 2%")

    # ── ⑨ 风险贡献：合计≈1 且禁 NaN/Inf（评审 ChatGPT #4）──
    rk2 = (d.get("portfolio_risk") or {}).get("risk") or {}
    rc = rk2.get("risk_contribution_90d") or rk2.get("risk_contribution")
    if rc:
        import math
        vals = list(rc.values())
        if any((not isinstance(x, (int, float))) or math.isnan(x) or math.isinf(x) for x in vals):
            bad(f"risk_contribution 含 NaN/Inf/非数字: {rc}")
        else:
            tot = sum(vals)
            if abs(tot - 1.0) > 0.02:
                bad(f"risk_contribution 合计 {tot:.3f} 偏离 1 超过 2%")

    # ── ⑩ 局部失败不得伪装成功（评审 ChatGPT #6 + 历史三次静默失效）──
    # 分母必须来自独立期望值；缺了哪些标要能说清。
    n_pos2 = len(d.get("positions") or {})
    n_tech = len(d.get("tech") or {})
    if n_pos2 and n_tech < n_pos2:
        missing = sorted(set(d.get("positions") or {}) - set(d.get("tech") or {}))
        if not d.get("missing_assets"):
            bad(f"丢了 {n_pos2 - n_tech} 个标的（{missing}）却没有 missing_assets 字段说明")
    if d.get("severity") is not None and d["severity"] not in SEVERITIES:
        bad(f"severity={d['severity']!r} 不在 {SEVERITIES}")

    # ── ⑪ 状态变化必须是 Change Detection 语义，不得夹带建议 ──
    for sym, v in ((d.get("deltas") or {}).get("state_changes") or {}).items():
        for k in ("previous_state", "current_state", "changed"):
            if k not in v:
                bad(f"deltas.state_changes.{sym} 缺字段 `{k}`")
        if v.get("changed") and v.get("previous_state") == v.get("current_state"):
            bad(f"deltas.state_changes.{sym} changed=True 但前后状态相同")
        if v.get("changed") is False and v.get("previous_state") != v.get("current_state"):
            bad(f"deltas.state_changes.{sym} changed=False 但前后状态不同"
                f"（{v.get('previous_state')} vs {v.get('current_state')}）—— 不要交给渲染层去推")

    # ── ⑭ 固定档资产不得进入状态迁移（v10.3，评审 ChatGPT P0）──
    # 只断「applicable=False 必须有 signal」不够：SGOV 若 previous=持有 / current=买入，
    # 仍然能过。事实层常量根本不该走规则状态迁移 —— 否则以后改状态映射又会出假告警。
    for sym, v in ((d.get("deltas") or {}).get("state_changes") or {}).items():
        sg = (d.get("signals") or {}).get(sym) or {}
        if sg.get("applicable") is False:
            bad(f"deltas.state_changes.{sym} 是固定档资产（applicable=False）却进入了"
                f"状态迁移 —— 它的状态是事实常量，不是规则投票结果")

    # ── ⑮ 风险宇宙完整性（v10.3，评审 ChatGPT P0）──
    # 只算「有收益序列的标的」时，字段若仍叫「组合风险贡献」，读者会默认是 9/9。
    rk3 = (d.get("portfolio_risk") or {}).get("risk") or {}
    if rk3.get("risk_contribution_90d"):
        ru = rk3.get("risk_universe")
        if not isinstance(ru, dict):
            bad("有 risk_contribution_90d 却没有 risk_universe —— "
                "无法判断这是全组合风险还是子组合风险")
        else:
            for k in ("expected_assets", "used_assets", "complete", "missing_assets"):
                if k not in ru:
                    bad(f"risk_universe 缺字段 `{k}`")
            exp_, used_ = ru.get("expected_assets"), ru.get("used_assets")
            if isinstance(exp_, int) and isinstance(used_, int):
                if used_ > exp_:
                    bad(f"risk_universe.used_assets={used_} 大于 expected_assets={exp_}")
                if ru.get("complete") is True and used_ != exp_:
                    bad(f"risk_universe 说 complete=True 但只用了 {used_}/{exp_} 个标的"
                        f"（{ru.get('missing_assets')}）—— 别让渲染层去推")
                if ru.get("complete") is False and used_ == exp_:
                    bad(f"risk_universe 说 complete=False 但用满了 {used_}/{exp_} 个标的")

    # ── ⑯ ML 缺失掩码必须与特征一起产出（v10.3，评审 ChatGPT P1）──
    # 缺值填 0 和「真实值就是 0」在产物里必须能区分，否则没法回看历史预测的可信度。
    mf = d.get("ml_features")
    if isinstance(mf, dict) and mf:
        if "ml_features_missing" not in d:
            bad("有 ml_features 却没有 ml_features_missing —— "
                "缺值填 0 与真实 0 无法区分（missing_out 又变成死参数了）")
        else:
            mm = d.get("ml_features_missing")
            if not isinstance(mm, dict):
                bad(f"ml_features_missing 不是对象：{type(mm).__name__}")
            elif set(mm) - set(mf):
                bad(f"ml_features_missing 含未参与特征工程的标的 {sorted(set(mm) - set(mf))}")
            # 形状稳定性：源整个挂掉时键被省掉 → 向量从 26 个静默缩到 10 个，
            # 而模型按 FEATURE_COLS 名字取值（缺的补 0），于是「少了 16 个特征」
            # 在产物里毫无痕迹。键集必须在所有标的间一致。
            _ks = {s: tuple(sorted(v)) for s, v in mf.items() if isinstance(v, dict)}
            if len(set(_ks.values())) > 1:
                _g = {}
                for s, k in _ks.items():
                    _g.setdefault(k, []).append(s)
                bad(f"ml_features 键集在不同标的间不一致（特征向量静默缩水）: "
                    f"{[v[:3] for v in _g.values()]}")

    # ── ⑫ schema / 产源 / 失败码（v10.2，评审 ChatGPT #1 #2 #3 #11）──
    sv = d.get("schema_version")
    if sv is not None:
        if sv != d.get("config_version"):
            bad(f"schema_version={sv!r} 与 config_version={d.get('config_version')!r} 不一致 "
                f"—— 字段结构版本与配置版本漂移了")
        prov = d.get("provenance") or {}
        for k in ("schema_version", "config_version", "code_version", "code_fingerprint"):
            if k not in prov:
                bad(f"provenance 缺字段 `{k}`")
        if prov.get("schema_version") not in (None, sv):
            bad(f"provenance.schema_version={prov.get('schema_version')!r} 与顶层 {sv!r} 不一致")
        fp = prov.get("code_fingerprint")
        if fp is not None and not re.fullmatch(r"[0-9a-f]{12}", str(fp)):
            bad(f"provenance.code_fingerprint={fp!r} 不是 12 位十六进制")

    # 失败码闭环：ok 不得有码，非 ok 必须有码。
    # 否则「哪儿坏了」又变成不可说 —— 上层只知道坏了，不知道坏在哪。
    codes = d.get("failure_codes")
    if codes is not None:
        if not isinstance(codes, list):
            bad(f"failure_codes 不是数组：{type(codes).__name__}")
        else:
            unknown = [c for c in codes if c not in ALLOWED_FAILURE_CODES]
            if unknown:
                bad(f"failure_codes 含未登记代码 {unknown}")
            if d.get("severity") == "ok" and codes:
                bad(f"severity=ok 却有失败码 {codes}")
            if d.get("severity") in ("degraded", "critical") and not codes:
                bad(f"severity={d['severity']} 却没有失败码 —— 上层不知道坏在哪")

    # exit_hint 不得与 severity 静默脱档（v10.1 里 critical 配的就是 degraded）。
    eh = d.get("exit_hint")
    if eh is not None and eh not in SEVERITIES:
        bad(f"exit_hint={eh!r} 不在 {SEVERITIES}")
    if eh is not None and d.get("severity") is not None and eh != d["severity"]:
        bad(f"exit_hint={eh!r} 与 severity={d['severity']!r} 不同档 —— 机器可读信号被降级")

    # ── ⑬ 资产类别闭合集 + 结构性禁止字段（v10.2）──
    for sym, t in (d.get("tech") or {}).items():
        rr = (t or {}).get("rules") or {}
        if not rr:
            continue
        at = rr.get("asset_type")
        if at is not None and at not in ALLOWED_ASSET_TYPES:
            bad(f"tech.{sym}.rules.asset_type={at!r} 不在 {ALLOWED_ASSET_TYPES}")
        if rr.get("applicable") is False and at not in (None, "special"):
            bad(f"tech.{sym} applicable=False 却 asset_type={at!r}（应为 special）")
        if rr.get("applicable") is True and at == "special":
            bad(f"tech.{sym} applicable=True 却 asset_type=special")

    # 决策类字段必须**递归扫到底**。原实现只扫顶层 + signals.*，于是
    # portfolio_optimization.recommendation（"建议参考 … 方案再平衡"）在守卫眼皮下活了下来 ——
    # 键名在白名单里，但嵌套深度不在。
    def _scan_forbidden(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                p = f"{path}.{k}" if path else k
                if k in STRUCT_FORBIDDEN:
                    bad(f"{p} 出现决策类字段 `{k}` —— 本项目只描述状态，不给操作")
                _scan_forbidden(v, p)
        elif isinstance(o, list):
            for _i, v in enumerate(o):
                _scan_forbidden(v, f"{path}[{_i}]")
    _scan_forbidden(d, "")


def fixtures():
    """合成边界场景。不含网络依赖，纯字典。"""
    base_tech = lambda sym, rules: {sym: {"price": 100.0, "rules": rules}}
    ok_rules = {"applicable": True, "verdicts": [], "buy": 2, "sell": 0, "hold": 1,
                "total": 3, "majority": "买入", "tie": False, "failed": 0,
                "asset_type": "equity"}
    out = {}

    def mk(signals, tech=None, dq=None, positions=None, extra=None):
        d = {"timestamp": "2026-09-12 08:00:00 CST",
             "positions": positions if positions is not None else {"QQQ": {"qty": 1}},
             "tech": tech if tech is not None else base_tech("QQQ", ok_rules),
             "signals": signals, "portfolio_risk": {"total_value": 100.0, "weights": {"QQQ": 1.0}},
             "data_quality": dq or {"持仓": "ok", "行情": "1/1"},
             "alerts": [], "deltas": {"has_prev": True, "state_changes": {}},
             "news": {}, "ml_predictions": {}, "llm_analyst_prompts": {},
             "config_version": CUR_SCHEMA, "severity": "ok", "exit_hint": "ok",
             "failure_codes": [], "schema_version": CUR_SCHEMA,
             "generated_at": "2026-09-12T00:00:00+00:00",
             "provenance": {"schema_version": CUR_SCHEMA, "config_version": CUR_SCHEMA,
                            "code_version": CUR_SCHEMA, "code_fingerprint": "0123456789ab"}}
        if extra:
            d.update(extra)
        return d

    out["全成功"] = mk({"QQQ": {"signal": "买入", "buy": 2, "sell": 0, "hold": 1, "total": 3}})
    out["部分数据失败"] = mk({"QQQ": {"signal": "买入", "buy": 2, "sell": 0, "hold": 1, "total": 3}},
                             dq={"持仓": "ok", "行情": "fail(1/2)", "基本面": "1/2"},
                             positions={"QQQ": {}, "VOO": {}},
                             extra={"missing_assets": ["VOO"], "severity": "degraded",
                                    "exit_hint": "degraded",
                                    "failure_codes": ["MARKET_DATA_PARTIAL",
                                                       "FUNDAMENTALS_DEGRADED"]})
    out["所有数据失败"] = mk({}, tech={}, dq={"持仓": "fail", "行情": "0/0"}, positions={})
    out["SGOV 特殊资产"] = mk(
        {"SGOV": {"signal": "持有", "applicable": False, "reason": "cash_equivalent",
                  "thesis": "货币基金"}},
        tech={"SGOV": {"price": 100.0, "rules": {"applicable": False, "reason": "cash_equivalent",
                                                 "verdicts": [], "total": 0, "majority": None}}})
    out["规则策略异常"] = mk(
        {"BTC-USD": {"signal": "持有", "buy": 0, "sell": 0, "hold": 0, "total": 0,
                     "failed": 3, "majority": None}},
        tech={"BTC-USD": {"price": 100.0, "rules": {"applicable": True, "total": 0,
                                                    "failed": 3, "majority": None}}})
    out["新闻无日期"] = mk(
        {"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}},
        extra={"news": {"per_symbol": {"QQQ": {"items": [{"title": "x", "date_unknown": True}]}}}})
    out["Crypto 多数票状态"] = mk(
        {"BTC-USD": {"signal": "买入", "buy": 2, "sell": 1, "hold": 0, "total": 3,
                     "signal_age_days": 9, "last_signal_date": "2026-09-03",
                     "strategy_ages": {"AdxSmas": {"state": "买入", "signal_age_days": 9},
                                       "BbandRsi": {"state": "卖出", "signal_age_days": 9},
                                       "AwesomeMacd": {"state": "买入", "signal_age_days": 23}}}})
    out["跨年交易日"] = mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}})
    return out


def mutations():
    """故意破坏的样本 —— 契约测试必须能抓到它们，否则就是摆设。"""
    def mk(sig, tech=None, dq=None, positions=None):
        return {"positions": positions if positions is not None else {"QQQ": {}},
                "tech": tech or {"QQQ": {"rules": {"applicable": True, "total": 3}}},
                "signals": sig, "portfolio_risk": {}, "data_quality": dq or {"行情": "1/1"},
                "alerts": [], "deltas": {}, "news": {}, "ml_predictions": {},
                "llm_analyst_prompts": {}, "config_version": CUR_SCHEMA,
                "severity": "ok", "exit_hint": "ok", "failure_codes": [],
                "schema_version": CUR_SCHEMA,
                "generated_at": "2026-09-12T00:00:00+00:00",
                "provenance": {"schema_version": CUR_SCHEMA, "config_version": CUR_SCHEMA,
                               "code_version": CUR_SCHEMA, "code_fingerprint": "0123456789ab"},
                "timestamp": "2026-09-12 08:00:00 CST"}
    return {
        "特殊资产却有投票": mk({"SGOV": {"applicable": False, "total": 3, "majority": "买入"}}),
        "特殊资产状态被配置改空": mk({"SGOV": {"applicable": False, "signal": None}}),
        "票数不自洽": mk({"QQQ": {"signal": "买入", "buy": 2, "sell": 1, "hold": 1, "total": 3}}),
        "signal 出现五档措辞": mk({"QQQ": {"signal": "强烈买入", "buy": 3, "sell": 0, "hold": 0, "total": 3}}),
        "年龄为负": mk({"QQQ": {"signal": "买入", "buy": 2, "sell": 0, "hold": 1, "total": 3,
                            "signal_age_days": -1, "last_signal_date": "2026-09-01"}}),
        "有年龄无日期": mk({"QQQ": {"signal": "买入", "buy": 2, "sell": 0, "hold": 1, "total": 3,
                               "signal_age_days": 3}}),
        "分母自指": mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}},
                    dq={"行情": "2/2"}, positions={"QQQ": {}, "VOO": {}, "SMH": {}}),
        "已删除字段复活": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}}),
                    "attribution": {"technology": 0.4}},
        "tech 与 signals 双轨": mk({"SGOV": {"applicable": False, "thesis": "货币基金"}},
                              tech={"SGOV": {"rules": {"applicable": True, "total": 3,
                                                       "majority": "持有"}}}),
        # v10.1 新增不变量的变异样本 —— 抓到才说明检查真的有效
        "风险贡献合计不为1": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}}),
                       "portfolio_risk": {"risk": {"risk_contribution_90d": {"A": 0.5, "B": 0.2}}}},
        "风险贡献含NaN": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}}),
                      "portfolio_risk": {"risk": {"risk_contribution_90d": {"A": float("nan"), "B": 1.0}}}},
        "权重越界": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}}),
                 "portfolio_risk": {"weights": {"A": 1.6, "B": -0.6}}},
        "信号日期在未来": mk({"QQQ": {"signal": "买入", "buy": 3, "sell": 0, "hold": 0, "total": 3,
                               "last_signal_date": "2099-01-01"}}),
        "severity 非法值": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}}),
                        "severity": "totally-fine"},
        "改档说没改": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}}),
                    "deltas": {"state_changes": {"QQQ": {"previous_state": "买入",
                                                          "current_state": "卖出",
                                                          "changed": False}}}},
        # v10.2 新增不变量的变异样本
        "schema 与配置版本漂移": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0,
                                          "hold": 2, "total": 3}}), "schema_version": "9.9"},
        "ok 却有失败码": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                    "total": 3}}), "failure_codes": ["SOURCE_FAILED"]},
        "degraded 无失败码": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                     "total": 3}}),
                          "severity": "degraded", "exit_hint": "degraded"},
        "exit_hint 静默降档": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                        "total": 3}}),
                           "severity": "critical", "exit_hint": "degraded",
                           "failure_codes": ["PORTFOLIO_INCOMPLETE"]},
        "失败码拼错": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                 "total": 3}}),
                   "severity": "degraded", "exit_hint": "degraded",
                   "failure_codes": ["degrade"]},
        "基本面分母含加密": mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}},
                          dq={"持仓": "ok", "行情": "9/9", "基本面": "6/9"},
                          positions={"QQQ": {}, "VOO": {}, "SCHD": {}, "SMH": {}, "SGOV": {},
                                     "SIVR": {}, "bitcoin": {}, "ethereum": {}, "binancecoin": {}}),
        "决策类字段混入（深层）": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0,
                                      "hold": 2, "total": 3}}),
                            "portfolio_optimization": {"recommendation": "buy"}},
        "资产类别拼错": mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2, "total": 3}},
                     tech={"QQQ": {"rules": {"applicable": True, "total": 3,
                                             "asset_type": "etf"}}}),
        # v10.3 新增不变量的变异样本
        "固定档资产进入状态迁移": {**mk({"SGOV": {"applicable": False, "signal": "持有"}}),
                           "deltas": {"state_changes": {"SGOV": {"previous_state": "持有",
                                                                 "current_state": "买入",
                                                                 "changed": True}}}},
        "风险宇宙假完整": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                    "total": 3}}),
                      "portfolio_risk": {"risk": {
                          "risk_contribution_90d": {"A": 0.5, "B": 0.5},
                          "risk_universe": {"expected_assets": 3, "used_assets": 2,
                                            "missing_assets": ["C"], "complete": True,
                                            "observations": 90}}}},
        "风险宇宙 used 超 expected": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0,
                                               "hold": 2, "total": 3}}),
                                "portfolio_risk": {"risk": {
                                    "risk_contribution_90d": {"A": 1.0},
                                    "risk_universe": {"expected_assets": 1, "used_assets": 2,
                                                      "missing_assets": [], "complete": True,
                                                      "observations": 90}}}},
        "风险贡献无风险宇宙": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0,
                                       "hold": 2, "total": 3}}),
                        "portfolio_risk": {"risk": {
                            "risk_contribution_90d": {"A": 0.5, "B": 0.5}}}},
        "有特征无缺失掩码": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                     "total": 3}}),
                       "ml_features": {"QQQ": {"rsi": 0.0}}},
        "特征向量静默缩水": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                     "total": 3},
                                "VOO": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                        "total": 3}}),
                        "ml_features": {"QQQ": {"rsi": 0.0, "vix": 0.0},
                                        "VOO": {"rsi": 0.0}},
                        "ml_features_missing": {}},
        "指纹不是十六进制": {**mk({"QQQ": {"signal": "持有", "buy": 1, "sell": 0, "hold": 2,
                                     "total": 3}}),
                       "provenance": {"schema_version": CUR_SCHEMA, "config_version": CUR_SCHEMA,
                                      "code_version": CUR_SCHEMA, "code_fingerprint": "NOT-A-HASH"}},
    }


def check_rule_sources(problems):
    """生产规则名单与回测约定必须一致（评审 Perplexity #4）。

    为什么放在契约测试里：`rule_eval` 的 selfcheck 已经断言过一次，但那只覆盖回测；
    契约测试覆盖**产物**。两处都断，才挡得住「回测改了、生产没改」这类静默分叉。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ghostfolio_rules as gr
        if len(gr.CRYPTO_SOURCES) != 3:
            problems.append(f"[规则名单] 加密策略应为 3 条，实际 {len(gr.CRYPTO_SOURCES)}")
        if len(gr.US_SOURCES) != 3:
            problems.append(f"[规则名单] 美股模型应为 3 条，实际 {len(gr.US_SOURCES)}")
        names = {n for n, _, _, _ in gr.CRYPTO_SOURCES}
        expect = {"freqtrade AdxSmas", "freqtrade BbandRsi", "freqtrade AwesomeMacd"}
        if names != expect:
            problems.append(f"[规则名单] 加密策略名不符: {names} != {expect}")
    except Exception as e:
        problems.append(f"[规则名单] 无法执行检查: {type(e).__name__}: {e}")


def check_renderer_accepts_current_schema(problems):
    """渲染器的 schema 闸门必须接受当前 collect 的版本（v10.5 漏的就是这条）。

    这个检查为什么必须存在：v10.5 把 collect 的 SCHEMA_VERSION 连升两级到 10.5，
    而 gen_report 的精确白名单还停在 {"10.3"} —— 于是**每一份正常产出的报告**
    顶部都挂一条「产物版本不匹配」假红条。收尾时人眼只看了报告正文和数据，
    契约测试也全绿，两个都没拦住。

    教训：契约测试再复杂，只要不覆盖**跨脚本的版本握手**，就挡不住
    「同一套仓库的两个文件互相不认」这类失效 —— 它不是数据问题，是接线问题。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ghostfolio_collect as gc
        src = (Path(__file__).resolve().parent / "gen_report.py").read_text(encoding="utf-8")
        if re.search(r'^SUPPORTED_SCHEMA\s*=\s*\{', src, re.M):
            problems.append("[渲染器] gen_report.py 又出现精确白名单 SUPPORTED_SCHEMA —— "
                            "版本号单调递增，精确相等必然周期性假警报")
        m = re.search(r'^_MIN_SCHEMA\s*=\s*\((\d+),\s*(\d+)\)', src, re.M)
        if not m:
            problems.append("[渲染器] gen_report.py 找不到 _MIN_SCHEMA —— 闸门被删或改写？")
            return
        floor = (int(m.group(1)), int(m.group(2)))
        cur = tuple(int(x) for x in str(_gc.SCHEMA_VERSION).split("."))
        if cur < floor:
            problems.append(f"[渲染器] 当前 schema {gc.SCHEMA_VERSION} 低于 gen_report 要求的最低 "
                            f"{floor[0]}.{floor[1]} —— 正常产出会被自己的渲染器判成版本不匹配")
        cfg = json.loads((Path(__file__).resolve().parent / "ghostfolio_config.json").read_text(encoding="utf-8"))
        if str(cfg.get("version")) != str(_gc.SCHEMA_VERSION):
            problems.append(f"[渲染器] config.version={cfg.get('version')} != "
                            f"collect.SCHEMA_VERSION={gc.SCHEMA_VERSION}")
    except Exception as e:
        problems.append(f"[渲染器] 无法执行检查: {type(e).__name__}: {e}")


def check_renderer_schema_bounds(problems):
    """渲染器的 schema 区间：上限必须等于当前 SCHEMA_VERSION，且判的是区间不是单向。

    这条为什么必须存在：v10.6 只判「不低于最低」，于是更新版本的产物（可能已删掉
    本渲染器要读的字段）照样被接受 ——「版本更高」不等于「向后兼容」。
    上限绑到 SCHEMA_VERSION，是因为「忘同步的下场」就是 v10.5 那次假红条事故。
    """
    try:
        src = (Path(__file__).resolve().parent / "gen_report.py").read_text(encoding="utf-8")
    except Exception as e:
        problems.append(f"[schema区间] 读不到 gen_report.py: {e}")
        return
    m_min = re.search(r"^_MIN_SCHEMA = \((\d+), (\d+)\)", src, re.M)
    m_max = re.search(r"^_MAX_SCHEMA = \((\d+), (\d+)\)", src, re.M)
    if not m_min or not m_max:
        problems.append("[schema区间] gen_report.py 缺少 _MIN_SCHEMA 或 _MAX_SCHEMA")
        return
    lo = tuple(int(x) for x in m_min.groups())
    hi = tuple(int(x) for x in m_max.groups())
    cur = tuple(int(x) for x in str(_gc.SCHEMA_VERSION).split("."))
    if hi != cur:
        problems.append(f"[schema区间] gen_report 上限 {hi[0]}.{hi[1]} != 当前 SCHEMA_VERSION "
                        f"{_gc.SCHEMA_VERSION} —— 升版本时必须同步（v10.5 假红条就是忘同步）")
    if lo > hi:
        problems.append(f"[schema区间] 下限 {lo} 大于上限 {hi}")
    if "_MIN_SCHEMA <= v <= _MAX_SCHEMA" not in src:
        problems.append("[schema区间] _schema_ok 必须同时判上下限 —— 只判 >= 会接受不兼容的新版本")


def check_feature_parity(problems):
    """训练侧与生产侧的技术指标必须同值。

    两处各写了一份实现（backfill 训练、collect 生产），漏改一头就会静默漂移 ——
    而特征指纹只覆盖「列名+语义版本」，看不见实现差异（评审 ChatGPT 两条同指）。
    用检查哨代替大重构：同一序列上比末值。
    """
    try:
        import numpy as np, pandas as pd
        import ghostfolio_backfill as bf
        rng = np.random.default_rng(20260913)
        closes = list(100.0 + np.cumsum(rng.normal(0, 1, 600)))
        s = pd.Series(closes)

        def _last(x):
            return float(x[-1]) if isinstance(x, (list, tuple)) else float(x)

        pairs = [("rsi14", _last(_gc.calc_rsi(closes)), float(bf.rsi(s).iloc[-1])),
                 ("ma20", _last(_gc.calc_ma(closes, 20)), float(s.rolling(20).mean().iloc[-1])),
                 ("ma50", _last(_gc.calc_ma(closes, 50)), float(s.rolling(50).mean().iloc[-1])),
                 ("ma200", _last(_gc.calc_ma(closes, 200)), float(s.rolling(200).mean().iloc[-1]))]
        for name, prod, train in pairs:
            if prod is None or train is None:
                problems.append(f"[特征一致] {name} 有一侧返回 None")
            elif abs(prod - train) > 1e-6 * max(1.0, abs(train)):
                problems.append(f"[特征一致] {name}：生产侧 {prod:.8f} != 训练侧 {train:.8f} "
                                "—— 两处实现已分叉，改了这头忘那头会静默漂移")
    except Exception as e:
        problems.append(f"[特征一致] 检查无法执行: {type(e).__name__}: {e}")


def check_config_description(problems):
    """config.description 里不许出现版本号。

    它没有任何校验者，必然漂移（v10.6→v10.7 就漂了一次，两家评审都报了）。
    版本只有一个权威位置：config.version（已断言与 SCHEMA_VERSION 一致）。
    """
    try:
        cfg = json.loads((Path(__file__).resolve().parent
                          / "ghostfolio_config.json").read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"[版本] 读不到 config: {e}")
        return
    m = re.search(r"v?\d+\.\d+", str(cfg.get("description") or ""))
    if m:
        problems.append(f"[版本] config.description 里出现版本号 {m.group()} —— "
                        "description 无人校验必然漂移，版本只写在 config.version")


def check_market_open_year_boundary(problems):
    """跨年交易日（元旦落周六 → 前挪到上一年 12-31）。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from zoneinfo import ZoneInfo
        import ghostfolio_market_open as mo
        for dstr in ("2021-12-31", "2032-12-31"):
            y, m, dd = map(int, dstr.split("-"))
            r = mo.check(datetime(y, m, dd, 10, 0, tzinfo=ZoneInfo("America/New_York")))
            is_open = r[0] if isinstance(r, (tuple, list)) else bool(r)
            if is_open:
                problems.append(f"[跨年交易日] {dstr} 判成开市，应为休市（元旦前挪）")
    except Exception as e:
        problems.append(f"[跨年交易日] 无法执行检查: {type(e).__name__}: {e}")



def check_empty_response_guard(problems):
    """v10.5：HTTP 200 但内容为空，必须被当成失败，不能静默当有效数据。

    假成功是「看起来在跑」的典型形态：上游限流回 200 + `{}`，
    `json.loads("{}")` 不抛异常，下游 `.get("data", [])` 得到空列表，
    表现成「今天没数据」——报告照出，severity 不动，没人知道源挂了。
    """
    try:
        import urllib.request as _u
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ghostfolio_collect as gc

        class _Resp:
            def __init__(self, body):
                self._b = body
            def read(self):
                return self._b

        _orig = _u.urlopen
        cases = [("200 + 空体", b""), ("200 + 空对象", b"{}"),
                 ("200 + 空数组", b"[]"), ("200 + null", b"null"),
                 ("200 + 空白", b"  \n ")]
        for label, body in cases:
            _u.urlopen = lambda *a, **k: _Resp(body)
            try:
                gc.fetch_url("https://example.invalid/x")
            except ValueError:
                pass                                   # 期望：拒绝
            except Exception as e:
                problems.append(f"[空响应] {label} 抛了 {type(e).__name__}，"
                                f"应为 ValueError（调用方靠 except 记源异常）: {e}")
            else:
                problems.append(f"[空响应] {label} 被当成有效响应 —— 这正是要挡的静默失效")

        # 反向：正常响应必须放行，守卫不能误伤
        _u.urlopen = lambda *a, **k: _Resp(b'{"data": []}')
        try:
            gc.fetch_url("https://example.invalid/x")
        except Exception as e:
            problems.append(f"[空响应] 正常响应（data 为空但体合法）被误挡: {e}")
        _u.urlopen = _orig
    except Exception as e:
        problems.append(f"[空响应] 无法执行检查: {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="额外校验一份真实产物")
    ap.add_argument("--report", help="扫描生成的中文报告，禁止裸操作指令词（评审 Grok 建议）")
    a = ap.parse_args()

    problems = []

    print("═══ ① 正常 fixture（应全部通过）═══")
    for name, d in fixtures().items():
        before = len(problems)
        check_contract(d, name, problems)
        n = len(problems) - before
        print(f"  {name:18s} {'✅' if n == 0 else f'❌ {n} 个问题'}")

    print("\n═══ ② 故意破坏的样本（必须全部抓到，否则测试是摆设）═══")
    caught = 0
    muts = mutations()
    for name, d in muts.items():
        probe = []
        check_contract(d, name, probe)
        ok = len(probe) > 0
        caught += ok
        print(f"  {name:18s} {'✅ 已抓到' if ok else '❌ 漏掉了！'}")
    if caught != len(muts):
        problems.append(f"[变异测试] {len(muts) - caught}/{len(muts)} 个破坏样本未被抓到")

    print("\n═══ ③ 跨年交易日 ═══")
    before = len(problems)
    check_renderer_accepts_current_schema(problems)
    check_renderer_schema_bounds(problems)
    check_feature_parity(problems)
    check_config_description(problems)
    check_market_open_year_boundary(problems)
    check_empty_response_guard(problems)
    print(f"  {'✅' if len(problems) == before else '❌'}")

    print("\n═══ ④ 生产规则名单 ═══")
    before = len(problems)
    check_rule_sources(problems)
    print(f"  {'✅' if len(problems) == before else '❌'}")

    if a.json:
        print(f"\n═══ ⑤ 真实产物 {a.json} ═══")
        before = len(problems)
        try:
            check_contract(json.loads(Path(a.json).read_text()), Path(a.json).name, problems)
        except Exception as e:
            problems.append(f"读取失败: {e}")
        print(f"  {'✅' if len(problems) == before else '❌'}")

    if a.report:
        print(f"\n═══ ⑥ 报告措辞黑名单 {a.report} ═══")
        try:
            body = Path(a.report).read_text()
            hits = []
            for ln, line in enumerate(body.splitlines(), 1):
                for w in REPORT_BANNED:
                    if w in line and not any(ctx in line for ctx in REPORT_ALLOW_CTX):
                        hits.append(f"{ln}: {w}  ← {line.strip()[:70]}")
                # 抓「代码泄进报告」—— 本次真踩到：f-string 里写了 {{...}}，
                # 转义成字面量，报告里直接打出 {'买入': '多数偏多'}.get(...)。
                # 这类错误编译通过、跑得通、只有读输出才发现 → 必须自动查。
                # 结构性：票数合计行不得再用交易符号（v10.2）
                # 「判定规则（源码条件原文）」列是**上游项目自己的措辞**（事实层转述），
                # 允许出现买/卖；其余任何位置出现票数式买/卖都算漏改。
                _src_col = ("源码条件原文" in line
                            or bool(re.match(r"\|\s*(Lean|freqtrade)", line)))
                _m = re.search(r"\d+\s*买\s*/\s*\d+\s*卖", line)
                if _m and not _src_col:
                    hits.append(f"{ln}: 票数合计仍是交易符号 [{_m.group()}]"
                                f"  ← {line.strip()[:60]}")
                for pat in ("{'", "}.get(", ":', '", "None,", "__", "f\""):
                    if pat in line:
                        hits.append(f"{ln}: 疑似代码泄进报告 [{pat}]  ← {line.strip()[:60]}")
                # 变档路径的措辞泄漏（v10.3，评审 Grok）—— 今天样本没有变档，
                # 所以这两条只在**真的变档那天**才会咬人，属于最典型的漏测形状。
                _m2 = re.search(r"(买入|卖出|持有)\s*→", line)
                if _m2:
                    hits.append(f"{ln}: 「比昨天」列仍是原生枚举 [{_m2.group()}]"
                                f"  ← {line.strip()[:60]}")
                _m3 = re.search(r"规则状态从「(买入|卖出|持有)", line)
                if _m3:
                    hits.append(f"{ln}: 状态变化播报仍是原生枚举 [{_m3.group()}]"
                                f"  ← {line.strip()[:60]}")
                # allow-list（v10.3，评审 ChatGPT P2）：黑名单只能事后抓已知措辞，
                # 「当前风险收益比不值得参与」不含任何禁用词却仍是操作建议。
                # 所以再压一层：报告里只允许出现 观察/状态变化/风险提示/数据异常 四类语义。
                for _w in _ACTION_WORDS:
                    if _w in line and not any(x in line for x in _ACTION_ALLOW_CTX):
                        hits.append(f"{ln}: 操作语义词 [{_w}]（只允许 观察/状态变化/"
                                    f"风险提示/数据异常）  ← {line.strip()[:60]}")
            for h in hits:
                problems.append(f"[报告黑名单] {h}")
            print(f"  {'✅ 无裸操作指令词' if not hits else f'❌ {len(hits)} 处'}")
        except Exception as e:
            problems.append(f"[报告黑名单] 读取失败: {e}")
            print("  ❌ 读取失败")

    print()
    if problems:
        print(f"❌ 契约测试失败（{len(problems)} 项）：")
        for p in problems:
            print(f"    · {p}")
        return 1
    print("✅ 契约测试全部通过（含变异测试：故意破坏的样本都被抓到了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
