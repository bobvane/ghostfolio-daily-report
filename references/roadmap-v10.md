# v10 改进计划（三家评审汇总后定稿）

> 汇总日期：2026-09-12
> 来源：ChatGPT（8.6/10）+ Grok + Perplexity 三方对 v9.9 的评审
> 状态：**待 Bob 拍板**（分歧点见第三节）


> ### ✅ 本文档是**历史计划**，不是现状
> **v10.0 与 v10.1 均已完工，本文档所有 P0/P1/P2 条目都已落地或明确关闭。**
> 想了解**当前实现**，请看 `SKILL.md` 的 v10.0 / v10.1 变更章节与仓库根 `README.md`。
>
> 📌 **为什么加这段**：v10.1 评审中，有评审把本文档「P0-3 待做」的措辞
> 当成了当前代码状态，据此报了两个**早已修好**的问题（SGOV 残留字段、
> 执行步骤死引用）。文档里的「计划」与「现状」必须分开标注。

---

## 一、三家共识（无争议，可直接采纳）

| 项 | 提出方 | 说明 |
|---|---|---|
| **ADX ↔ TA-Lib 一次性离线验证** | **三家全提** | 输出 max_abs_error / mean_abs_error / **threshold_flip_count**（重点看 ADX 23~27 区域），不进生产依赖 |
| **修失败可见性** | Grok + ChatGPT | 已发生三次静默失败 |
| **特殊资产从 collect 层移出 rules** | Grok + ChatGPT | 不要继续在 renderer 层加 guard |
| **产品转向风险度量、弱化买卖语言** | **三家全提** | 项目身份：Trading Signal Generator → Portfolio State Monitor |
| **状态变化 = 事实播报，不是建议** | **三家全提** | 「系统负责 Detect，用户负责 Decide」 |
| **不再测第五种仓位映射** | Perplexity + ChatGPT | ChatGPT 明确警告这是 **negative result hunting**，会降低统计可信度 |
| **成本敏感性实验**（0 / 0.5× / 1× / 2×）| Grok + ChatGPT | 替代实验。若 0 成本仍跑输 → 问题不是成本而是市场暴露；若 0 成本接近基准 → 问题是 turnover 过高 |
| **「三位投资人怎么看」价值最低、建议降级** | ChatGPT | 另两家未提；详见分歧点 A |

---

## 二、我核实后的三处修正

评审意见不能照单全收。以下是**核实后与评审说法不一致**的地方。

### 修正 ①：ChatGPT 的「exit code 分级」方案不适用

**ChatGPT 说**：给 `ghostfolio_collect.py` 定义 `0=成功 / 1=部分失败 / 2=关键失败 / 3=程序异常`，
因为「Hermes Cron 会认为 exit 0 = 成功」。

**核实结果**：`/opt/data/cron/jobs.json` 里 Ghostfolio 任务是 **agent job（prompt 驱动），不是 shell job**。
调度器看的是 **agent 的完成状态**，不是采集脚本的退出码 —— **退出码被 agent 吞掉了**。
定义 0/1/2/3 **解决不了任何问题**。

**真正缺的是**：`SKILL.md` 的「执行步骤」里**完全没有数据质量闸门**。
全文唯一一处「不推送」是开市判定 `CLOSED`。数据缺一半时，agent 照样生成报告并推送。

**修法**：把闸门加在**执行步骤**里（agent 的说明书），而不是脚本退出码：
- `data_quality` 有 fail → 报告顶部显式标红 + 推送时带警告
- 关键数据（持仓/行情）全 fail → 不推送，只报告失败

### 修正 ②：语义漂移比 ChatGPT 说的严重 —— 执行步骤在吃「不存在的字段」

**ChatGPT 说**：SKILL.md 还有「旧架构叙述残留」。

**核实结果**：不只是叙述，是**执行步骤在教 agent 读已经不存在的字段**。逐条实测：

| SKILL.md 执行步骤写的 | 实际输出 JSON | 判定 |
|---|---|---|
| 第1步列出 `attribution`（因子归因） | **没有这个字段** | ❌ 死引用 |
| 第1步列出 `confidence_intervals`（置信区间）| **没有这个字段** | ❌ 死引用 |
| 第1步说 `signals` = 「五档 + `score` 0-100 + `conviction` + `confidence`」| `score` 已删；`signal` 只有**三值**（买入/卖出/持有）| ❌ 死引用 |
| 第4步要求输出「信号五档 🟢强烈买入/🟢买入/🟡持有/🟠减仓/🔴卖出」+ 展示 `conviction` | 现行判定只产出三值，无五档 | ❌ 与代码矛盾 |
| 第4步「LLM 视角：Buffett/Munger/Druckenmiller」| 仍在跑（cron prompt 里有三个角色）| ⚠️ 见分歧点 A |
| 第1步说「Layer 1-5 完整」| 五层架构已废弃 | ❌ 死叙述 |

**这是本项目当前最紧的一处不一致**：每天真正驱动流水线的是这份说明书，
而说明书里有 5 处以上指向已删除的东西。agent 照着读要么读到空、要么自己编。

**ChatGPT 的「single source of truth」建议成立**，且优先级应高于它给的排序。

### 修正 ③：config 里还活着三个废弃评分字段

第一轮评审说「自研评分配置已清掉」——**大部分对，但不完全**。

```
scoring / alpha_models / thresholds  → ✅ 确实已删除（只剩 _cleanup_note 里的说明文字）
special_assets.SGOV.score            → ❌ 还活着（= 60）
special_assets.SGOV.conviction       → ❌ 还活着（= 0.2）
special_assets.SGOV.confidence       → ❌ 还活着（= 70）
```

后三个正是已废弃的自研评分体系字段，躺在一个「不参与择时」的资产上。

---

## 三、分歧点（需要 Bob 拍板）

### A. 「三位投资人怎么看」怎么处理？

- **ChatGPT**：删弱，降级为可选模块、默认不进日报主体。理由：不提高可靠性/风控，易产生幻觉，
  会让严肃的 Portfolio Monitor 看起来像 AI 投资顾问。
- **Grok / Perplexity**：未提。
- **实际影响范围**（我核实的）：这一块**不只在报告里** —— `/opt/data/cron/jobs.json` 的
  prompt 里有一步专门要求主模型填充 `buffett` / `munger` / `druckenmiller` 三个角色。
  删掉要**同时改**：cron prompt + SKILL.md 第2/4步 + 报告模板 + config。
- **我的建议**：**保留但降级** —— 从日报主体移到末尾附录，且明确标注「定性闲聊，不改判定」。
  理由：它是你当初主动加的（属于你的表达），且成本已经付了；但它不该占据主体位置。

### B. 失败可见性做成什么形态？

- **ChatGPT**：退出码分级 0/1/2/3
- **Grok**：非 0 退出 **或** 飞书标红
- **我的判断**：退出码方案已证不适用（修正 ①）。建议做成**报告内失败等级 + 推送闸门**：
  - `data_quality` 全 ok → 正常推送
  - 部分 fail → 推送，但报告顶部 🔴 标出缺了什么
  - 关键数据（持仓/行情）fail → **不推送**，改为推送一条失败告警

  需要你定：**关键数据缺失时，你要「收到一份残缺报告」还是「收到一条失败告警」？**

### C. `scaled` 口径的缺陷要不要修？

- **Grok 指出**：`pos = 支持买入票数 / 总票数` —— 所以「1买2卖」仓位仍是 1/3，
  与「多数偏空」直觉不符。更合理是 `(buy - sell) / total` 再 clip 到 [0,1]。
- **ChatGPT**：别再测新映射了。
- **我的建议**：**要么修口径、要么删掉这个模式**。留着一个口径可疑的实验，
  反而让「12 组结论」的可信度打折。二选一，我倾向前者（改 1 行，结论不会翻）。

### D. 78KB 的 `ghostfolio_collect.py` 要不要拆？

- **ChatGPT**：God Script，是「分母自指 / 状态语义混乱 / 特殊资产双轨 / exit code」这些问题的根源之一。
- **我的判断**：**先立数据契约，不急着拆文件。**
  拆文件本身不产生正确性收益，纯风险；而契约立起来后，非法组合根本进不了 JSON，
  此时拆不拆只是风格问题。所以：**契约优先（P0），拆分降级为可选（P3）。**

---

## 四、v10 计划

### P0 — 可靠性（必须先做）

| # | 项 | 依据 |
|---|---|---|
| 1 | **执行步骤加数据质量闸门**（替代 exit code 方案）| 修正 ① |
| 2 | **清理执行步骤里 5 处死引用**（attribution / confidence_intervals / 五档 / score / Layer 1-5）| 修正 ② |
| 3 | **特殊资产 `applicable: false` 契约** —— collect 层就不写 rules | Grok + ChatGPT |
| 4 | **AssetSnapshot 数据契约** —— 消灭 `signals` / `tech.rules` 双轨 | ChatGPT #10 |
| 5 | **config 清掉 SGOV 的 score / conviction / confidence** | 修正 ③ |
| 6 | **end-to-end contract test** —— fixture 覆盖：全成功 / 部分失败 / 全失败 / SGOV / 策略异常 / 无日期新闻 / 跨年 / 多数票 / 配置缺失；invariants：`final_state ∈ allowed`、`total == buy+sell+hold`、`age ≥ 0`、`last_signal_date ≤ generated_at`、`applicable=False → votes is None` | ChatGPT #11 |
| 7 | **ADX ↔ TA-Lib 离线验证** `scripts/validate_adx_against_talib.py` | **三家全提** |

### P1 — 产品方向

| # | 项 |
|---|---|
| 8 | 报告首页改「**风险事件等级**」：🔴高优先级 / 🟡中优先级 / 🟢信息级 |
| 9 | `买入/卖出/持有` → **规则状态**：多数偏多 / 多数偏空 / 无共识 / 固定资产状态 |
| 10 | 状态变化 = **Change Detection**：`previous_state` / `current_state` / `changed` / `state_age_days`；只报「状态从 A 变为 B」，**不接「所以建议买入」** |
| 11 | 报告优先级重排：组合风险 > 风险变化 > 集中度 > 波动率 > 相关性 > 规则状态变化 > ML/宏观/新闻解释 |
| 12 | **成本敏感性实验** 0 / 0.5× / 1× / 2× |

### P2 — 风险层（新增计算）

单资产集中度 · 资产类别集中度 · Crypto exposure · **相关性集中度** · rolling volatility · 最大回撤 · **风险贡献（Risk Contribution）**

> ChatGPT 特别强调 Risk Contribution：你有 QQQ / VOO / SMH 三只，
> 但「持仓数量 3」≠「3 个独立风险来源」。

### 明确不做

- ❌ **第五、第六种仓位映射**（Perplexity + ChatGPT 一致反对，属 negative result hunting）
- ❌ **加新数据源 / 新指标 / 新策略**（ChatGPT 明确反对：规则层已被自己证明没有收益优势）
- ❌ **拆分 `ghostfolio_collect.py`**（降级 P3，见分歧点 D）

---

## 五、我建议先做的 3 件

如果只选三件：

1. **执行步骤清理 + 数据质量闸门**（P0-1、P0-2）
   —— 每日流水线真正跑的就是这份说明书，而它现在有 5 处指向已删除的字段。
   **这是唯一一处「今天就在出错」的问题**，其余都是「将来会出错」。

2. **AssetSnapshot 契约 + 特殊资产 `applicable`**（P0-3、P0-4、P0-5）
   —— 一次修掉双轨、假票数、config 残留三类问题，且是「从源头不产生非法状态」而非层层加守卫。

3. **报告首页风险化 + 状态变化语义**（P1-8、P1-9、P1-10）
   —— 完成项目身份转换。三家一致，且这是你自己回测结论的自然推论。

**如果只有一件**：选 1。说明书和代码不符，是唯一「已经在产生错误输出」的问题。
