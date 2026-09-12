#!/usr/bin/env python3
# Ghostfolio 每日持仓诊断报告生成器
# 消费 collect.py 输出的 JSON（含 llm_analyst_prompts），生成中文 Markdown 报告
# AI 评论家观点从 llm_analyst_prompts 读取（不再硬编码）；若 Hermes 主模型尚未填充回答，显示待填充占位
# 面向没有金融背景的读者：全篇白话，术语一律附白话解释
import json, sys, os

JSON_PATH = sys.argv[1] if len(sys.argv) > 1 else '/tmp/gf_report.json'
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else '/tmp/gf_report.md'


def num(x, fmt='.2f', dash='?', scale=1):
    """空值安全的数字格式化：None/非数字 -> dash，避免整份报告因 TypeError 生成失败。"""
    try:
        if x is None:
            return dash
        return format(float(x) * scale, fmt)
    except (TypeError, ValueError):
        return dash


# 情绪英文 -> 中文
SENT_CN = {
    'extreme fear': '极度恐慌', 'fear': '恐慌', 'neutral': '中性',
    'greed': '贪婪', 'extreme greed': '极度贪婪',
}


def sent_cn(v):
    return SENT_CN.get(str(v).strip().lower(), v if v else '?')


d = json.load(open(JSON_PATH))

# ── schema 版本闸门（v10.2，评审 ChatGPT #1）──
# 渲染器只接受自己认识的产物 schema。缺字段时旧行为是静默渲染出半张表，
# 没人发现。现在版本不匹配直接红条置顶并继续（报告仍要推给 Bob，但要显式说出来）。
# v10.5 修：原先写的是精确白名单 {"10.3"}。collect 升版本、这里忘了跟，就会把
# **同一套仓库的正常产物**判成「版本不匹配」，每份报告顶部挂一条假红条
# （v10.5 实际发生过：collect 10.5 vs 白名单 {"10.3"}，而契约测试当时全绿）。
# 渲染器真正要的语义是「产物不能比我认识的更旧」，不是「必须完全相等」——
# 在单调递增的版本号上用精确相等，必然周期性假警报。
_MIN_SCHEMA = (10, 2)
# 上限必须等于当前 SCHEMA_VERSION —— 契约测试会断言这一点。
# v10.6 只判了「不低于最低」，于是更新版本的产物（可能已删掉本渲染器要读的字段）
# 照样被接受 ——「版本更高」不等于「向后兼容」（评审 Perplexity P0）。
_MAX_SCHEMA = (10, 8)


def _schema_ok(sv) -> bool:
    """产物 schema 是否落在渲染器认识的区间内（下限兼容旧产物，上限挡未知新版本）"""
    try:
        v = tuple(int(x) for x in str(sv).split("."))
    except (TypeError, ValueError):
        return False
    return _MIN_SCHEMA <= v <= _MAX_SCHEMA


SCHEMA_WARN = ""
_sv = d.get("schema_version")
if not _sv:
    SCHEMA_WARN = "产物缺少 schema_version（不是 v10.2 的 collect 产出的？）"
elif not _schema_ok(_sv):
    _pv = tuple(int(x) for x in str(_sv).split(".") if str(x).isdigit()) or (0,)
    if _pv > _MAX_SCHEMA:
        SCHEMA_WARN = (f"产物 schema_version={_sv} 高于本渲染器认识的 "
                       f"{_MAX_SCHEMA[0]}.{_MAX_SCHEMA[1]} —— 它可能已删除本表要读的字段")
    else:
        SCHEMA_WARN = (f"产物 schema_version={_sv} 低于本渲染器要求的最低 "
                       f"{_MIN_SCHEMA[0]}.{_MIN_SCHEMA[1]}")

# ── 一句话结论：汇总表 ──
signals = d.get('signals', {})
tech = d.get('tech', {})
ml = d.get('ml_predictions', {})
pr = d.get('portfolio_risk', {})

risk_flags = pr.get('flags', [])                       # 给人显示的字符串
# 程序判断用结构化数据 —— 不再从中文串里 split()[0] 反解 symbol（那会随文案改动而失效）
flag_symbols = {f.get('symbol') for f in (pr.get('flag_details') or [])

                if f.get('symbol')}
# 兼容：旧快照没有 flag_details 时退回字符串解析
if not flag_symbols and risk_flags:
    flag_symbols = {f.split()[0] for f in risk_flags if f}
# 报告显示名：加密货币用简称（映射由 collect.py 输出，避免两处重复定义）
DISP = (d.get('symbol_display') or {})


def disp(sym, _=None):
    return DISP.get(sym, sym)


# 展示层用的单票状态词。内部 JSON 仍是 买入/卖出/持有（判定层的原生枚举），
# 只在**报告渲染**时翻译 —— 否则同一份报告上半写「多数偏多」、下半写「买入」，
# 两套叙事互相拆台（评审 Grok 指出）。契约测试会扫描生成报告，禁止裸的买入/卖出。
_STATE_WORD = {"买入": "偏多", "卖出": "偏空", "持有": "中性"}


def state_word(v, dash="—"):
    """单票状态词（区别于 rule_state 的多数据措辞）。"""
    return _STATE_WORD.get(v, dash) if v else dash


def state_words(s):
    """把可能含原生枚举的展示串（如 '买入→卖出'）整体换成展示词（v10.3）。"""
    for k, v in _STATE_WORD.items():
        s = (s or "").replace(k, v)
    return s


def rule_state(sym, v=None):
    """把判定层的三值 买入/卖出/持有 翻译成「规则状态」措辞（v10.0）。

    为什么改措辞：报告原先直接写「买入 / 卖出」，读者会记成「今天该买 BTC」，
    而项目自己的回测已证明规则层不能作为长期择时依据。
    改成状态描述后，报告的身份从「交易信号」变成「状态监控」——
    这是 ChatGPT 评审建议的核心产品调整，Grok / Perplexity 方向一致。

    映射：买入→多数偏多 / 卖出→多数偏空 / 持有且无共识→无共识 / 持有→多数中性
         特殊资产（不参与择时）→固定资产状态
    """
    v = v if v is not None else (signals.get(sym) or {})
    if v.get('applicable') is False or not v.get('total'):
        return "固定资产状态"
    if v.get('tie'):
        return "无共识"
    return {"买入": "多数偏多", "卖出": "多数偏空", "持有": "多数中性"}.get(
        v.get('signal'), "—")


no_action = [s for s, v in signals.items()
             if v.get('applicable') is not False and v.get('total')
             and v.get('signal') == '持有' and s not in flag_symbols]

# 「今天只需看一件事」：取优先级最高的预警
alerts = d.get('alerts') or []
lines = ["# Ghostfolio 每日持仓诊断报告", ""]
# 报告元数据（评审 #17：否则无法区分「今天的真实报告」和「旧样例」）
_meta_bits = []
if d.get('timestamp'):
    _meta_bits.append(f"数据采集 {d['timestamp']}")
if (d.get('news') or {}).get('as_of'):
    _meta_bits.append(f"新闻截至 {str(d['news']['as_of'])[:16].replace('T', ' ')}")
if d.get('config_version'):
    _meta_bits.append(f"配置 v{d['config_version']}")
if _meta_bits:
    lines.append("*" + " ｜ ".join(_meta_bits) + "*")
    lines.append("")
# ── 数据质量横幅（v10.0）──
# 执行步骤新增的闸门要求：数据有洞时**照常推送，但报告顶部必须说清楚缺了什么**。
# Bob 选择的处理方式是「收到一份说明自己缺什么的残缺报告」，而不是「什么都收不到」。
# ── 产物版本横幅（v10.2）──
# 渲染器只认自己认识的 schema；版本对不上就显式说出来。旧行为是缺字段时
# 静默渲染出半张表，没人发现（评审 ChatGPT #1）。
if SCHEMA_WARN:
    lines.append("> ## ⚠️ 产物版本不匹配")
    lines.append(f"> {SCHEMA_WARN}")
    lines.append("> 下面这张表可能缺列 —— 先确认 collect 与 gen_report 是同一版本。")
    lines.append("")

# ── critical 横幅（v10.2）──
# severity=critical 表示持仓或行情整体不可用。此时**照推**（Bob 选择收到残缺报告），
# 但必须写明「下面的判断不成立」，不能让它看起来像一份正常的日报
# （评审 Perplexity #4：全失败仍继续生成，与 critical 语义冲突）。
_CRITICAL_TAG = ("> ⚠️ **本轮数据 critical：下面这节的结论不成立**，只为留痕。\n\n"
                 if d.get('severity') == 'critical' else '')
if d.get('severity') == 'critical':
    _codes = "、".join(d.get('failure_codes') or []) or '—'
    lines.append("> ## 🔴 本次数据不可用（severity=critical）")
    lines.append(f"> 失败码：**{_codes}**。持仓或行情整体采集失败。")
    lines.append("> **下面的规则状态、风险贡献与组合结论均不成立**，"
                 "仅保留采集到的元数据供排查。")
    lines.append("")

_dq = d.get('data_quality') or {}
_dq_bad = {k: v for k, v in _dq.items() if isinstance(v, str) and 'fail' in v.lower()}
if _dq_bad:
    lines.append("> ## 🔴 本次数据不完整")
    lines.append("> " + "；".join(f"**{k}** {v}" for k, v in _dq_bad.items()))
    lines.append("> ")
    lines.append("> 缺失项会影响结论的完整性 —— 下面报告里凡涉及缺失项的判断，"
                 "**请勿当作可用结论**。数据源异常明细见文末「数据质量」一节。")
    lines.append("")

# ── 今日组合状态：按风险等级分层（v10.0）──
# 报告身份从 Trading Signal Generator 转为 Portfolio State Monitor：
# 开篇回答「组合有什么值得你注意的变化」，而不是「今天该买什么」。
# 带操作语气的句子只留给风控越线（pri<=2）；规则状态一律归「信息级」。
def _lvl(pri):
    return ("🔴 高优先级" if pri <= 2 else
            "🟡 中优先级" if pri <= 4 else "🟢 信息级")

lines.append("## 今日组合状态")
if alerts:
    _by = {}
    for a in alerts:
        _by.setdefault(_lvl(a.get('pri', 9)), []).append(a)
    for _lv in ("🔴 高优先级", "🟡 中优先级", "🟢 信息级"):
        if _lv in _by:
            lines.append(f"### {_lv}")
            for a in _by[_lv]:
                lines.append(f"- {a['text']}")
else:
    lines.append("> 组合没有需要特别关注的变化。")
lines.append("")

# ── 状态变化（Change Detection）──
# 只陈述「规则状态从 A 变为 B」这个事实。三方评审一致要求：**不得**接「所以建议买入」。
# 系统负责 Detect，用户负责 Decide。
_dl = d.get('deltas') or {}
_sc = {k: v for k, v in (_dl.get('state_changes') or {}).items() if v.get('changed')}
lines.append("### 状态变化（今日）")
if not _dl.get('has_prev'):
    lines.append("> 没有昨日快照，今天不做状态变化对比。")
elif _sc:
    for _sym, _v in _sc.items():
        lines.append(f"- **{disp(_sym)}**：规则状态从「{state_words(_v['previous_state'])}」"
                     f"变为「{state_words(_v['current_state'])}」（今日发生变化）")
    lines.append("")
    lines.append("> 以上仅为状态播报，**不代表应据此调仓** —— "
                 "规则层已被回测证明不能作为长期择时依据。")
else:
    lines.append("> 今天没有任何标的的规则状态发生变化。")
lines.append("")

# 「今天大环境」：组合级因素，从每只标的的理由里抽出来（去重）
mnotes = []
for _v in signals.values():
    for _n in (_v.get('macro_notes') or []):
        if _n not in mnotes:
            mnotes.append(_n)
if mnotes:
    lines.append("## 今天大环境")
    lines.append(" ｜ ".join(mnotes[:6]))
    lines.append("")

lines.append(_CRITICAL_TAG + "## 一句话结论")
md = "\n".join(lines) + """
| 名称 | 规则状态 | 为什么 | 风险 |
|---|---|---|---|
"""
for sym, s in signals.items():
    one_line = (s.get('reasons') or ['—'])[0] if s.get('reasons') else s.get('thesis', '—')
    flagged = '⚠️' if sym in flag_symbols else '✅'
    md += f"| {disp(sym)} | {rule_state(sym, s)} | {one_line[:60]} | {flagged} |\n"

if no_action:
    md += (f"\n**规则层维持中性**：{', '.join(disp(x) for x in no_action)}"
           f"（状态未变，无需关注）\n")

# ── 大盘温度 ──
sent = d.get('sentiment', {})
pc = sent.get('put_call', {})
macro = d.get('macro', {})
onchain = d.get('onchain', {})
md += f"""
## 大盘温度（看整体市场冷不冷）
| 看什么 | 现在多少 | 怎么理解 |
|---|---|---|
| 市场情绪 | {sent.get('value','?')}（{sent_cn(sent.get('classification'))}） | 0 是极度恐慌，100 是极度贪婪，越高越容易追高 |
| 恐慌指数 VIX | {num(pc.get('vix'), '.2f')} | 越高说明市场越害怕、波动越大 |
| 长短期国债利差 | {num(macro.get('yield_spread_10y2y'))}% | 10年期减2年期国债利率；负数叫倒挂，历史上常是衰退预警 |
| 美国基准利率 | {num(macro.get('fed_funds_rate', macro.get('fed_funds')))}% | 美联储定的利率，越高借钱越贵、股市越受压 |
| 美元强弱 | {num(macro.get('dxy', macro.get('DXYNYB')), '.2f')} | 越高美元越强，通常压大宗商品和新兴市场 |
| 比特币占比 | {num(onchain.get('btc_dominance'), '.2f')}% | 比特币占整个加密市场的比重，越高说明资金越往龙头躲 |
"""

# ── 我的持仓 ──
md += f"""
## 我的持仓
- 现在值多少：${num(pr.get('total_value'), ',.0f')}
- 持有几只：{len(signals)}
- 风险提醒：{', '.join(risk_flags) if risk_flags else '没有超限项'}
"""

# ── 组合风险（v10.0 新增）──
# ChatGPT 评审的核心建议：报告重心从「规则买卖」转向风险度量。
# 最有价值的是 risk_contribution —— QQQ / VOO / SMH 看着是三只，
# 但「持仓数量 3」≠「3 个独立风险来源」。
_rk = pr.get('risk') or {}
if _rk and _rk.get('asset_class'):
    _ac = _rk['asset_class']
    md += "\n## 组合风险\n"
    md += "| 看什么 | 现在多少 | 怎么理解 |\n|---|---|---|\n"
    md += (f"| 股票 ETF 占比 | **{_ac.get('etf', 0):.0%}** | 你大部分钱在股票上，"
           f"股市整体下跌时会同步影响 |\n")
    md += (f"| 加密货币占比 | **{_ac.get('crypto', 0):.0%}** | 波动远大于股票，"
           f"同样的钱在加密上承担的风险要大得多 |\n")
    md += (f"| 现金等价物占比 | {_ac.get('cash_equivalent', 0):.0%} | "
           f"货币基金，几乎不波动，起缓冲垫作用 |\n")
    _co = _rk.get('correlation')
    if _co:
        md += (f"| 相关性最高的一对 | {disp(_co['max_pair'][0])} 与 "
               f"{disp(_co['max_pair'][1])}：**{_co['max_value']}** | "
               f"越接近 1 越像「同一只」—— 涨跌同步，分散不了风险 |\n")
        md += (f"| 平均相关性 | {_co['mean_pairwise']} | "
               f"越低说明持仓之间越互补，越接近 0 越分散 |\n")
    if _rk.get('portfolio_vol_annual') is not None:
        md += (f"| 组合年化波动率 | **{_rk['portfolio_vol_annual']:.1%}** | "
               f"整体一年上下起伏的大致幅度，越大越颠簸 |\n")
    if _rk.get('portfolio_max_dd_90d') is not None:
        md += (f"| 组合近 90 日最大回撤 | **{_rk['portfolio_max_dd_90d']:.1%}** | "
               f"这段时间从最高点跌下来的最大幅度 |\n")
    _rc = _rk.get('risk_contribution_90d') or _rk.get('risk_contribution') or {}
    if _rc:
        # 正贡献与负贡献**分开列**（v10.2，评审 ChatGPT）：把 -0.6% 混进
        # 「风险最大 → 风险最小」的一条排序里，读者会把它读成「这只最安全」。
        # 它的真实含义只是「在当前窗口的协方差结构下对组合波动有轻微抵消」。
        _pos = sorted(((k, v) for k, v in _rc.items() if v > 0), key=lambda x: -x[1])
        _neg = sorted(((k, v) for k, v in _rc.items() if v <= 0), key=lambda x: x[1])
        _top = _pos[:3]
        _rk_txt = _rk.get('risk_model') or 'historical_covariance'
        _rw = _rk.get('risk_window_days') or 90
        md += (f"\n**主要风险来源**（近 {_rw} 个交易日**波动**风险贡献 · "
               f"{_rk_txt}；谁在真正贡献波动，不是谁占比大）："
               + "　".join(f"{disp(k)} **{v:.0%}**" for k, v in _top) + "\n")
        if _neg:
            md += ("**分散化贡献**（同一窗口下对组合整体波动有轻微抵消）："
                   + "　".join(f"{disp(k)} {v:.1%}" for k, v in _neg) + "\n")
            md += ("> 负值只表示该资产与组合的边际协方差为负 —— **在近 "
                   f"{_rw} 个交易日这个窗口里**起了轻微抵消作用，"
                   "**不等于「没有风险」**，也**不代表压力情景下仍能对冲**。"
                   "窗口很短，对权重与相关性估计敏感。\n")
        _w = pr.get('weights') or {}
        _mismatch = [(k, v) for k, v in _top
                     if _w.get(k) is not None and v - _w[k] > 0.08]
        if _mismatch:
            md += ("> ⚠️ " + "、".join(
                f"{disp(k)} 占比 {_w.get(k, 0):.0%} 却贡献了 {v:.0%} 的风险"
                for k, v in _mismatch)
                + " —— **持仓数量不等于独立风险来源**。\n")
    if _rk.get('note'):
        md += f"> 说明：{_rk['note']}\n"

# ── 每只怎么看 ──
deltas = (d.get('deltas') or {})
# 内部枚举 → 展示词（v10.3）。signal_changed 的值是 "买入→卖出" 这类内部拼串，
# 直接渲染会让「比昨天」列偶尔冒出交易语言 —— 变档那天才出现，平时测不到（评审 Grok）。
tier_changed = {k: state_words(v) for k, v in (deltas.get('signal_changed') or {}).items()}

md += """
## 每只怎么看
| 名称 | 现价 | 今日涨跌 | RSI | 规则状态 | 比昨天 | 模型看5天 | 模型看20天 |
|---|---|---|---|---|---|---|---|
"""
for sym, s in signals.items():
    t = tech.get(sym, {})
    m = ml.get(sym, {})
    price = num(t.get('price'), '.2f')
    chg = num(t.get('change_pct'), '.2f')
    rsi = num(t.get('rsi14'), '.1f')
    yday = tier_changed.get(sym, '—')
    _r = (t.get('rules') or {})
    # 固定档资产（SGOV 货币基金等）：规则层没有有效投票，绝不能用通用规则算出的
    # 「1买1卖/3家（无共识）」冒充判定 —— 它的判定是配置里固定的「持有」。
    if not (signals.get(sym) or {}).get('total'):
        rule_txt = "**固定资产状态**<br><sub>货币基金，不走技术规则</sub>"
    elif _r.get('majority'):
        rule_txt = (f"**{rule_state(sym, signals.get(sym) or {})}**"
                    f"<br><sub>{_r['buy']}偏多 / {_r['sell']}偏空 / 共 {_r['total']} 条</sub>")
        if _r.get('failed'):
            rule_txt += f"<br><sub>⚠️ {_r['failed']} 个策略数据异常，未参与投票</sub>"
        if _r.get('tie'):
            rule_txt += "（无共识）"
        # 加密侧是状态机 → 标出信号年龄，避免把旧状态误读成今天新信号
        _age = _r.get('signal_age_days')
        if _age is not None:
            # age=0 写「0 个交易日」读起来像笔误（评审 Grok 两次提到）→ 改「今日触发」
            _age_txt = "今日触发" if _age == 0 else f"已持续 {_age} 天"
            rule_txt += f"<br><sub>信号{_age_txt}</sub>"
    else:
        rule_txt = s.get('signal', '—')
    md += (f"| {disp(sym)} | {price} | {chg}% | {rsi} | {rule_txt} "
           f"| {yday} "
           f"| {num(m.get('predicted_return_5d'), '+.2%')} | {num(m.get('predicted_return_20d'), '+.2%')} |\n")

# ── 模型预测的可靠度（避免把 ML 数字当准数）──
_rel = [(k, (ml.get(k) or {}).get("strength_level"), (ml.get(k) or {}).get("prediction_strength"))
        for k in signals
        # 排除固定档资产（如 SGOV 货币基金）：价格几乎不动 → RMSE 极小 → 强度虚高成假象
        if signals.get(k, {}).get('total')]
_rel = [(disp(k), r, st) for k, r, st in _rel if r]
if _rel:
    _order = {"高": 0, "中": 1, "低": 2}
    _rel.sort(key=lambda x: _order.get(x[1], 3))
    md += ("\n> **模型预测幅度档位**（= |预测幅度| ÷ 模型历史误差；≥2 高 / ≥1 中 / <1 低）："
           + "　".join(f"{k} {r}" + (f"（{st}）" if st is not None else "") for k, r, st in _rel)
           + "\n> 可靠度低表示「预测幅度还没模型自己的历史误差大」，**别当准数看**。"
           + ("　（另有 " + "、".join(disp(k) for k, m2 in ml.items() if m2.get("is_heuristic"))
              + " 无训练模型，用的是规则式兜底）" if any(m2.get("is_heuristic") for m2 in ml.values()) else "")
           + "\n")

# ── 模型训练信息（provenance，评审 ChatGPT P2-4）──
_prov = {(str(v.get("trained_at"))[:10], v.get("train_start"), v.get("train_end"),
          v.get("cv_rmse_mean"), v.get("cv_rmse_std"))
         for v in ml.values() if (v or {}).get("trained_at")}
if _prov:
    # 9 个模型各有自己的数据窗口，取并集 + CV 取均值，不要只报某一个标的的（会误导）
    _tas = sorted({p[0] for p in _prov})
    _ts = min(p[1] for p in _prov if p[1])
    _te = max(p[2] for p in _prov if p[2])
    _cms = [p[3] for p in _prov if p[3] is not None]
    _css = [p[4] for p in _prov if p[4] is not None]
    _cv = (f"，CV-RMSE 平均 {sum(_cms) / len(_cms):.2%} ± {sum(_css) / len(_css):.2%}（5 折，gap=horizon）"
           if _cms and _css else "")
    md += (f"\n> **模型训练信息**：{_tas[0]} 训练（{len(_prov)} 个模型），数据范围 {_ts} ~ {_te}{_cv}。"
           "\n> 训练时间越久、数据窗口越旧，预测越该打折看 —— 只如实标注，不设阈值。\n")

# ── 加密三策略各自的状态与年龄 ──
# 三家的共识是「不要让一个策略的年龄代表整个多数票」。这里把每票摊开：
# 每票自己最近一次变状态距今多久，以及最终多数是怎么来的。
_ca = [(k, (signals.get(k) or {}).get('strategy_ages')) for k in signals]
_ca = [(k, v) for k, v in _ca if v]
if _ca:
    md += "\n### 加密三策略明细（每票各自的状态，不与多数票混算）\n\n"
    md += "| 币种 | 规则状态 | AdxSmas（趋势） | BbandRsi（均值回归） | AwesomeMacd（动量） |\n"
    md += "|---|---|---|---|---|\n"
    for _k, _sa in _ca:
        _cells = []
        for _nm in ("freqtrade AdxSmas", "freqtrade BbandRsi", "freqtrade AwesomeMacd"):
            _a = _sa.get(_nm) or {}
            _st = state_word(_a.get('state'))
            _ag = _a.get('signal_age_days')
            _cells.append(f"{_st}" + (f"<br><sub>{_ag} 天前</sub>" if _ag is not None else ""))
        md += (f"| {disp(_k)} | **{rule_state(_k)}** | "
               + " | ".join(_cells) + " |\n")
    md += ("\n> 「N 天前」= 该票自己最近一次改变状态距今的**日线根数**；"
           "顶部的「信号已持续 N 天」只统计**与最终多数同向**的票里最近的那次。"
           "无多数共识时不显示年龄。"
           "（单位是日线根数，不是股市日历的交易日 —— 加密 7×24 连续交易，一根=一天）\n")

# ── 规则判定明细（每个公开量化模型分别给出什么信号）──
_rows = [(sym, t.get('rules') or {}) for sym, t in tech.items()
         if (t.get('rules') or {}).get('majority')]
if _rows:
    _order, _meta = [], {}
    for _s_, _rr in _rows:
        for _v in (_rr.get('verdicts') or []):
            if _v['source'] not in _meta:
                _order.append(_v['source'])
                _meta[_v['source']] = _v
    md += ("\n" + _CRITICAL_TAG + "## 规则判定明细（每个公开量化模型分别给出什么信号）\n"
           "> **美股/ETF → QuantConnect/Lean 的官方 Alpha 模型**"
           "（Lean 官方文档确认支持 Equities）；\n"
           "> **加密货币 → freqtrade 的三个社区策略**（AdxSmas 趋势跟随 / BbandRsi 均值回归 / "
           "AwesomeMacd 动量双确认；freqtrade 官方 README 自述是 crypto trading bot，不含美股）。\n"
           "> 三条策略**源码里都没有自定义参数**，且逻辑不同源 —— 这样「多数票」才有意义。\n>\n"
           "> **判定条件和阈值全部来自这两个项目的源码** —— 没有一处是我们自己拍的。\n>\n"
           "> **这是「判定规则」，不是「预测模型」**：它不承诺能预测涨跌，"
           "只说明按这些项目公认的规则，当前该判什么。\n\n"
           "| 来源 | 判定规则（源码条件原文） | " + " | ".join(disp(a, b) for a, b in _rows) + " |\n"
           "|---|---|" + "---|" * len(_rows) + "\n")
    for _src in _order:
        _cells = []
        for _s2, _rr in _rows:
            # 固定档资产（如 SGOV 货币基金）技术指标退化，明细里不该出「假票」
            if not signals.get(_s2, {}).get('total'):
                _cells.append("—")
                continue
            _m = {v['source']: v['vote'] for v in (_rr.get('verdicts') or [])}
            _cells.append({"买入": "🟢偏多", "卖出": "🔴偏空",
                           "持有": "⚪中性"}.get(_m.get(_src), "—"))
        md += f"| {_src} | {_meta[_src].get('detail','')} | " + " | ".join(_cells) + " |\n"
    # 固定档资产（SGOV 货币基金）不参与投票，合计行也必须显示 —，
    # 否则会出现「判定（多数）写固定持有、合计却写 1买/1卖」的自相矛盾（评审指出）
    _tot_cells = []
    for _s4, _rr in _rows:
        if not signals.get(_s4, {}).get('total'):
            _tot_cells.append("—")
        else:
            # 票数也走同一套语义（v10.2）：正文已经全用偏多/偏空/中性，
            # 合计行再写「1买/0卖」就是最后残留的一处交易符号（评审 Perplexity #2）。
            _tot_cells.append(f"**{_rr.get('buy')}偏多/{_rr.get('sell')}偏空**")
    md += "\n| **合计** | | " + " | ".join(_tot_cells) + " |\n"
    # 内联自检：固定档资产在合计行只能显示 —，不能出现票数
    for (_s5, _), _cell in zip(_rows, _tot_cells):
        # 按**结构**断言，而不是只查「买」一个字：这类「只匹配特定文案」的检查
        # 在本项目已静默失效过五次（见 SKILL「静默失效」节）。
        assert "买" not in _cell and "卖" not in _cell, \
            f"{_s5} 的合计单元格出现操作词：{_cell}"
        if not signals.get(_s5, {}).get('total'):
            assert "偏多" not in _cell and "偏空" not in _cell, \
                f"{_s5} 是固定档资产（0 有效票），合计行不该出现票数"
    # 「判定（多数）」也要用状态措辞 —— 否则明细表标题写「规则状态」、
    # 这一行却写「买入/卖出」，同一张表里两套叙事（评审 Grok 指出）。
    _MAJ_WORD = {"买入": "多数偏多", "卖出": "多数偏空", "持有": "多数中性"}
    md += ("| **判定（多数）** | | " + " | ".join(
        ("**固定持有**" if not signals.get(_s3, {}).get('total')
         else (f"**{_MAJ_WORD[_rr['majority']]}**" if _rr.get('majority') in _MAJ_WORD
               else "**—（无有效票）**")
         + ("⚠️" if _rr.get('tie') else ""))
        for _s3, _rr in _rows) + " |\n")
    md += ("\n来源：QuantConnect/Lean（21,588⭐，美股）· freqtrade（54,274⭐，加密货币）。"
           "各来源一票，多数胜；三方各一票时记「多数中性（无共识）」。\n"
           "\n> 📌 **「判定规则」列里的「买/卖」**是 QuantConnect / freqtrade **源码自己的措辞**\n"
           "> （如 `RsiAlphaModel.cs` 的进出场条件、`BbandRsi.py` 的 RSI<30 买入），属**事实层转述**，\n"
           "> 不是本报告的操作指令。本报告自己的结论一律用「偏多 / 偏空 / 中性」表述。\n"
           ">\n"
           "> 📌 **关于票型**：Lean 的 EMA 交叉模型只输出方向、**没有中性档**（在均线上方即看多、下方即看空），"
           "所以它每天都会投出一票；RSI 状态机与 MACD 则经常落在中性。"
           "这意味着「2 偏多 1 偏空 / 3 家」里的票型分布天然不对称，不等于三份独立证据。\n"
           ">\n"
           "> ⚠️ **两侧的「偏多」语义不同**：加密是 freqtrade 三策略的**持仓状态延续** —— "
           "一旦触发就保持到反向信号为止，所以标了「信号已持续 N 天」（单位是"
           "**日线根数**，不是自然日也不是交易日），不代表今天刚出信号。\n"
           ">\n"
           "> 美股侧三个模型里，**EMA 交叉与 MACD 是当天重算的当前状态**；"
           "但 **Lean 的 RSI 模型同样是粘性状态机** —— RSI 跌穿 30 触发偏多后，"
           "要等它回到 35 以上才转中性，中间在 30~35 来回震荡时「偏多」会一直挂着。"
           "所以美股的「偏多」也不必然等于今天刚触发（目前未对美股标年龄）。\n")

# ── 想调仓的话，几种摆法对比（仅供参考） ──
po = d.get('portfolio_optimization', {})
opt = po.get('optimized_portfolios', {})
md += f"""
## 几种持仓构成的对比（不是调仓建议）
> ⚠️ 这**不是调仓建议**，只是把几种常见构成方式放在一起看各自的"性价比"。
> 组合里有货币基金和加密货币，这个数字本身波动很大，**优先看上面的风险提醒**，别只盯数字。

| 摆法 | 性价比 | 白话解释 |
|---|---|---|
| 你现在这样 | {num(po.get('current_portfolio',{}).get('sharpe_estimate'), '.2f')} | 当前持仓不动 |
| 每样一样多 | {num((opt.get('equal_weight') or {}).get('sharpe_estimate'), '.2f')} | 9 只各占约 1/9，最省心 |
| 平均扛风险 | {num((opt.get('risk_parity') or {}).get('sharpe_estimate'), '.2f')} | 让每只对组合的"晃动"贡献一样大 |
| 只看性价比 | {num((opt.get('max_sharpe') or {}).get('sharpe_estimate'), '.2f')} | {(opt.get('max_sharpe') or {}).get('note') or '数学上最划算，但会重仓波动大的币'} |
| 按看好程度加权 | {num((opt.get('signal_weighted') or {}).get('sharpe_estimate'), '.2f')} | 越看好权重越高 |

**性价比（夏普比率）**：每承担 1 分价格晃动，能换回多少收益。数字越高越划算，但高低受组合构成影响，别跨组合硬比。
"""

# 组合优化的可信度降级说明（若 max_sharpe 掺了启发式预测，或本来就没解）
_oq = d.get('optimization_quality') or {}
if _oq and not _oq.get('trustworthy', True):
    md += (f"\n> ⚠️ **今天的优化方案可信度打了折扣**："
           f"{_oq.get('heuristic_fallback_count', 0)} 只标的**没有训练好的模型**，用的是规则式兜底估算"
           f"（{'、'.join(_oq.get('heuristic_symbols') or [])}）。"
           f"所以「只看性价比」那行不是真正的最大夏普组合，**只作结构参考，别当调仓依据**。\n")

# ── 三位投资人怎么看（从 llm_analyst_prompts 消费） ──
lp = d.get('llm_analyst_prompts', {})
prompts = lp.get('prompts', {})
analyst_names = {
    'buffett': '巴菲特（专看公司好不好、买得贵不贵）',
    'munger': '芒格（专挑毛病、避开坑）',
    'druckenmiller': '德鲁肯米勒（专看大势和风险）',
}

# 【v10.0 降级】三位投资人从报告主体移到文末附录。
# 评审（ChatGPT）指出它价值最低：不提高数据可靠性、不提高风控、易产生幻觉，
# 且会让严肃的组合监控看起来像 AI 投资顾问。Bob 决定「保留但降级」。
analyst_md = ""
if not d.get('llm_needed', True):
    analyst_md += "\n## 附录：三位投资人怎么看（定性闲聊，不改任何判定）\n"
    analyst_md += "> 今天没有触发条件，为省额度不叫 AI 点评。\n\n"
    prompts = {}
else:
    analyst_md += "\n## 附录：三位投资人怎么看（定性闲聊，不改任何判定）\n"
analyst_md += f"> {lp.get('disclaimer', '以下只是口头看法，不改变上面的任何状态描述。')}\n\n"

if prompts:
    for key, label in analyst_names.items():
        p = prompts.get(key)
        if not p:
            continue
        # 已有回答（Hermes 主模型填充后写入 llm_analyst_responses）
        resp = d.get('llm_analyst_responses', {}).get(key, '')
        analyst_md += f"### {label}\n"
        if resp:
            analyst_md += f"{resp}\n\n"
        elif d.get('llm_needed') is False:
            analyst_md += (f"*今天无重要变化，跳过 AI 点评（省额度）："
                           f"{d.get('llm_skip_reason','')}*\n\n")
        else:
            analyst_md += "*（今天这份还没生成，暂缺）*\n\n"
else:
    analyst_md += "（今天这份没生成）\n"

# ── 综合建议和模型预测打架的地方 ──
divergences = []
for sym, s in signals.items():
    m = ml.get(sym, {})
    ml_conv = m.get('conviction')
    sig_conv = s.get('conviction')
    if ml_conv is None or sig_conv is None:
        continue
    # 模型偏空但建议持有/加仓，或模型偏多但建议减仓
    # 判定层只产出三值；此前这里还留着「强烈买入」「减仓」两个已废弃的五档值（死引用）
    if (ml_conv < -0.3 and s.get('signal') in ('持有', '买入')) or \
       (ml_conv > 0.3 and s.get('signal') == '卖出'):
        # 措辞去「对错暗示」：ML 无决策权，不该读成「模型不同意规则、所以有一个是对的」。
        # 三方评审一致：系统负责 Detect（陈述两侧各是什么），用户负责 Decide。
        _rel = (m.get('strength_level') or '—')
        divergences.append(
            f"- {disp(sym)}：规则层**{rule_state(sym, s)}**；模型 5 日方向 "
            f"**{'偏多' if ml_conv > 0 else '偏空'}**（可靠度{_rel}）"
            f" —— 仅供对照，**不参与状态判定**")

if divergences:
    md += "### 规则状态与模型方向的对照\n" + "\n".join(divergences) + "\n"
else:
    md += "\n*规则状态与模型方向没有明显分歧。*\n"

# ── 持仓消息面（Layer 6）──
nw = d.get('news') or {}
nps = nw.get('per_symbol') or {}
tr_all = d.get('news_translations') or {}
tr = tr_all.get('per_symbol') or {}
tr_macro = tr_all.get('macro') or []

_tr_missing = False
if nps:
    md += "\n## 持仓消息面（过去 72 小时真正相关的消息）\n"
    if not tr:
        md += "\n> ⚠️ 今天的标题未翻译，本节只列来源与时间，不给结论。\n"
    for sym, v in nps.items():
        items = v.get('items') or []
        if not items:
            md += f"\n**{disp(sym)}**：近期没有相关消息。\n"
            continue
        md += f"\n**{disp(sym)}**\n"
        zh = tr.get(sym) or []
        for i, it in enumerate(items):
            # 翻译缺失时不静默回退英文（会违反「全篇中文」硬要求）。
            # 原先写的是 f"（标题待翻译）{英文原标题}" —— 那等于换了句话说英文，照样违反要求。
            if i < len(zh) and zh[i]:
                cn = zh[i]
            else:
                cn = "（标题未翻译，本条不纳入结论）"
                _tr_missing = True
            src = it.get('source', '')
            # 无日期条目（RSS 常缺日期）不能伪装成近期新闻 —— 明写「日期未知」（评审 #9）
            when = "日期未知" if it.get('date_unknown') else (it.get('published_iso') or '')[:10]
            tail = "，".join(x for x in (src, when) if x)
            md += f"- {cn}　*（{tail}）*\n"

mev = nw.get('macro_events') or []
if mev:
    md += "\n### 大环境发生了什么\n"
    for i, it in enumerate(mev):
        # 与持仓新闻用同一套规则；原先这条分支缺失时直接吐英文且**不设告警**，属分支遗漏
        if i < len(tr_macro) and tr_macro[i]:
            cn = tr_macro[i]
        else:
            cn = "（标题未翻译，本条不纳入结论）"
            _tr_missing = True
        src = it.get('source', '')
        when = "日期未知" if it.get('date_unknown') else (it.get('published_iso') or '')[:10]
        tail = "，".join(x for x in (src, when) if x)
        md += f"- {cn}　*（{tail}）*\n"

if _tr_missing:
    md += ("\n> ⚠️ **部分新闻标题未翻译**（模型未按要求回写），已在原文前标注「标题未翻译，本条不纳入结论」。"
           "本次消息面**不纳入结论**。\n")

if nw.get('degraded'):
    md += "\n> 消息面采集提示：" + "；".join(nw['degraded']) + "\n"

# ── 数据质量 ──
dq = d.get('data_quality') or {}
if dq:
    md += "\n## 数据质量（今天的数据全不全）\n| 数据源 | 状态 |\n|---|---|\n"
    _stale = False
    for k, v in dq.items():
        if v == "fail":
            mark = "❌ 失败"
        elif v == "ok":
            mark = "✅ 正常"
        elif isinstance(v, str) and v.startswith("stale"):
            mark = f"⚠️ {v.replace('stale：', '数据陈旧：')}"
            _stale = True
        elif isinstance(v, str) and v.startswith("unknown"):
            mark = "⚠️ 无法核对新鲜度"
        elif isinstance(v, str) and v.startswith("fresh"):
            mark = f"✅ {v}"
        elif isinstance(v, str) and "/" in v and len(set(v.split("/"))) == 1:
            mark = f"✅ {v}"          # 9/9 全到齐也算正常
        else:
            mark = f"⚠️ {v}"          # 6/9 这类缺口的才提醒
        md += f"| {k} | {mark} |\n"
    if any(v == "fail" for v in dq.values()):
        md += "\n> ⚠️ 有数据源失败，今天的结论可能不完整，别据此做大动作。\n"
    if _stale:
        md += ("\n> ⚠️ **有行情数据陈旧** —— 接口可能失败后回退到了旧缓存。"
               "陈旧数据上的判定不代表当前市场，别据此操作。\n")

# ── 术语速查 + 脚注 ──
md += """
### 术语速查（白话）
- **RSI**：近 14 天的涨跌强弱。>70 偏热（可能回调），<30 偏冷（可能反弹）
- **判定**：把公开量化项目的规则投票结果汇总 —— 美股看 QuantConnect/Lean 三个模型，
  加密看 freqtrade 三个策略（AdxSmas 趋势 / BbandRsi 均值回归 / AwesomeMacd 动量）多数表决。
  **不是自研打分，也没有预测能力**：回测证明它跑不赢长期持有（详见脚注）
- **模型看 5/20 天**：机器学习模型预测的 5 天 / 20 天涨跌幅，只是参考
- **性价比（夏普比率）**：每承担 1 分价格晃动换回多少收益，越高越划算
- **大环境**：VIX、市场情绪、美元、利率这些影响全局的因素，对每只标的作用是一样的
- **比昨天**：信号档位跟昨天比有没有变（没变显示「—」）

---
数据源：yfinance、CoinGecko、FRED、CoinPaprika、DefiLlama、alternative.me、SEC EDGAR —— 全免费
生成方式：脚本算量化指标，Hermes 主模型负责白话点评，不调用任何外部 AI 服务
*以上不构成投资建议*
"""

md += analyst_md          # 附录放最后（v10.0 降级：不再占据报告主体）

print(md)
with open(OUT_PATH, 'w') as f:
    f.write(md)
