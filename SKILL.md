---
name: ghostfolio-daily-report
description: 每日自动生成 Ghostfolio 持仓诊断报告并推送飞书/微信。触发词：持仓报告、每日诊断、ghostfolio。
version: 10.8
author: Bob Vane
---

# Ghostfolio 自动化持仓分析（公开量化规则判定 + 研究参考 v10.8）

拉取 NAS 上 Ghostfolio 记账本的持仓数据，结合国际权威行情、技术指标、基本面、宏观、链上、ML 模型，**由 Hermes 默认主模型直接处理 LLM 推理**，生成诊断报告推送到**飞书（唯一渠道，不用微信）**。微信(iLink)限流不稳定，Bob 已决定后续少用微信端，本任务不设微信兜底。

**核心原则**：全免费数据源，无付费服务商依赖；LLM 推理由 Hermes Cron 默认主模型直接处理，脚本只输出结构化 Prompt。

## 触发条件

用户说"持仓报告"、"每日诊断"、"ghostfolio 分析"，或 cron 定时任务触发。

## 架构（数据源 → 特征 → **规则判定** → AI 定性 → 新闻 → 输出）

> 判定层只有一个来源：**公开量化项目的规则投票**。自研打分系统（旧的五层/六层评分、
> 权重分配、`alpha_models`、`legacy_signal_mapping`）已在 v9.7 整体废弃。
> ML 只做解释参考，**没有买卖权**。

```
Layer 1: 数据源层（全免费）—— 市场 / 宏观 / 基本面 / 链上 / 持仓，完整清单见下一节

Layer 2: 特征工程 (ghostfolio_features.py)
  ├── 技术面: RSI, MA距离, 动量, 趋势斜率, 波动率, 分位数
  ├── 宏观: VIX, 息差, DXY, Fed利率, 情绪指数
  ├── 链上: BTC占比, 市值变化, 稳定币
  └── 基本面: ROE, FCF yield, P/E, P/B, Margin, D/E
  ⚠️ 只服务 ML 参考层，**不参与判定**。缺失一律登记 ml_features_missing，禁止填 0 充数。

Layer 3: 规则判定 (ghostfolio_rules.py) —— 唯一判定来源，无权重、无自研阈值
  ├── 美股（每只 3 票，各一票，多数胜）
  │   ├── Lean RsiAlphaModel.cs     —— RSI 状态机 30进/35出、70进/65出
  │   ├── Lean EmaCrossAlphaModel.cs —— EMA(12) 与 EMA(26) 位置
  │   └── Lean MacdAlphaModel.cs    —— MACD信号/价格 与 ±1% 比较
  ├── 加密（每只 3 票，各一票，多数胜）
  │   ├── freqtrade AdxSmas     —— ADX(14)>25 且 SMA(3) 上穿 SMA(6)（趋势跟随）
  │   ├── freqtrade BbandRsi    —— RSI<30 且 收盘<布林下轨（布林用典型价）
  │   └── freqtrade AwesomeMacd —— MACD 与 AO 双确认
  ├── 策略抛异常 → 该票 None，**不参与投票**（total 只数有效票），登记 failed
  ├── 三方各一票 → 「持有（无共识）」，不设人为倾向
  └── 特殊资产（SGOV）→ applicable=False，不产生 votes，状态由代码给「持有」

Layer 4: AI 定性层（由 Hermes 默认主模型直接处理）
  ├── ML 推理 (ghostfolio_ml.py): LightGBM 基线，输出 conviction / prediction_strength /
  │   reliability —— **仅展示**，无买卖权。CV 只评稳定性，最终模型全量重训
  ├── LLM 三角色点评 Prompt: Buffett / Munger / Druckenmiller（附录性质）
  └── 仅定性，不覆盖量化判定结论

Layer 5: 新闻/事件层 (ghostfolio_news.py)
  ├── 逐标的: TradingView headlines + Yahoo Finance RSS（兜底覆盖 9/9）
  ├── 宏观: 美联储官方 / CNBC / MarketWatch
  ├── 源质量过滤: 白名单优先 + 黑名单剔除 + 内容农场限量
  └── 落盘 cache/news/YYYY-MM-DD.json，供回溯

Layer 6: 输出层 (gen_report.py)
  ├── 状态词: 展示只许「多数偏多 / 多数偏空 / 持有」—— 内部枚举只给机器
  ├── 组合风控: 市值权重 + 风险贡献 (单标的≤25%, 加密≤30%, 相关性聚类≤50%)
  └── 已删除、不得复活: 归因分析 / 置信区间 / 再平衡建议 / confidence 百分比
```

## 数据源（全部免费、国际权威、结构化、无爬虫）

| 数据类型 | 来源 | 说明 |
|---|---|---|
| 持仓 | Ghostfolio API | NAS 本地记账本 |
| 美股 ETF 行情+技术指标 | yfinance 库 | MA20/50/200 + RSI14 自算 |
| 加密货币现价 | yfinance + CoinGecko API | 双源交叉验证 |
| 加密市场深度/备源 | CoinGecko, CoinCap, CoinPaprika, CryptoCompare | 免费多源聚合 |
| 恐慌指数 | yfinance ^VIX | 市场波动与恐慌水平 |
| 市场情绪 | alternative.me Fear & Greed | 结构化 JSON |
| 宏观 | FRED API + yfinance | 联邦基金利率、10Y/2Y/3M、息差、DXY |
| **宏观免费增强** | Econdb, Fed Treasury | GDP、CPI、失业率、美债收益率、联邦债务 |
| 基本面 | yfinance .info | ROE、FCF、P/E、P/B、Margin、D/E、Beta、股息 |
| 链上（市场） | CoinGecko Global + DefiLlama + CoinCap/CoinPaprika/CryptoCompare | 总市值、BTC占比、稳定币、主要币种多源价格 |
| 链上兜底 | **CoinPaprika /v1/global** | CoinGecko 常 429，用它补 BTC占比 / 总市值 / 24h变化（来源记在 `onchain.btc_dominance_source`） |
| **链上（估值/流量）** | **CoinMetrics 社区版**（免 key、免注册） | MVRV、MVRV Z-Score（自算）、NUPL（由 MVRV 推）、交易所净流量。**仅 BTC/ETH 有数据**；BNB 免费数据停在 2019-04，已加过期守卫自动跳过 |
| 组合优化 | 本地实现 | 有效前沿近似、风险平价、最大夏普 |

**禁止**：所有 `.cn` 域名、中文财经媒体、国内自媒体。

## 环境

| 项 | 值 |
|---|---|
| 采集脚本 | `/opt/data/scripts/ghostfolio_collect.py` |
| **回测脚本** | `/opt/data/scripts/ghostfolio_rule_eval.py`（无前视回放 + 4 种仓位口径 + 成本敏感性） |
| 配置（阈值/打分局/权重，热改无需动代码） | `/opt/data/scripts/ghostfolio_config.json` |
| 特征工程模块 | `/opt/data/scripts/ghostfolio_features.py` |
| 价格/成交量历史缓存 | `/opt/data/cache/prices/<sym>.csv`（**只存持仓标的**，现配 2 年日线、落盘截到 600 行，约 300KB/15 个文件） |
| Python venv | `/opt/data/ghostfolio-venv/bin/python` |
| Token 存于 | `/opt/data/.env` 的 `GHOSTFOLIO_ACCESS_TOKEN` |
| Ghostfolio 地址 | `http://192.168.2.2:3333` |
| 时区 | Asia/Hong_Kong |
| 推送 | **唯一渠道：飞书**（微信限流不稳定，不设兜底；失败则记录错误） |
| 可选 Key | 仅 `FRED_API_KEY`（FRED 免费额度更高），其余全免费无 Key |
| 可选环境变量 | `SEC_EDGAR_UA`（SEC 要求 UA 带联系方式，默认用项目 URL；填自己的邮箱更合规） |

## 执行步骤

### 1. 采集数据

```bash
cd /opt/data
set -a; source .env 2>/dev/null; set +a
/opt/data/ghostfolio-venv/bin/python /opt/data/scripts/ghostfolio_collect.py
```

输出 JSON 关键字段（**v10.0 实测核对过，只列真实存在的**）：

- `positions`：持仓（数量、加权成本）
- `tech`：技术指标（price、change_pct、ma20/50/200、rsi14、52周高低、`rules` 判定结果、`returns_90d`/`vol_annual`/`max_dd_1y` 风险输入）
- `vix` / `sentiment` / `crypto_market`：恐慌指数 / Fear & Greed / 加密市场深度
- `macro`：宏观（FEDFUNDS、DGS10/DGS2、yield_spread_10y2y、DXY、Econdb GDP/CPI/失业率、Fed Treasury 美债）
- `onchain`：链上（total_market_cap_usd、btc_dominance、稳定币流通量、多源加密价格）
- `fundamentals`：基本面（ROE、P/E、P/B、Margin、D/E、增长率、Beta、股息）
- `ml_features` / `ml_predictions`：标准化特征 / ML 推理（LightGBM 或启发式回退；`conviction` 语义已改为**信号噪比**，不是伪精确百分比）
- `llm_analyst_prompts`：三角色 Prompt（由 Hermes 默认主模型填充）
- `signals`：**判定结果（最重要）**。字段：`signal`（**三值**：买入/卖出/持有）、
  `buy`/`sell`/`hold`/`total`/`tie`/`failed`、`strategy_ages`（三策略各自状态与年龄）、
  `signal_age_days`/`last_signal_date`/`age_basis`、`thesis`/`reasons`/`risks`。
  特殊资产额外带 `applicable: false` + `reason`。
- `portfolio_risk`：组合风控（total_value、weights、flags/flag_details、**`risk` 风险层**：
  资产类别集中度、相关性、风险贡献、组合波动率与回撤）
- `deltas`：与昨日对比。`state_changes` 是**结构化状态变化**
  （`previous_state`/`current_state`/`changed`/`state_age_days`），供报告做状态播报。
- `alerts` / `data_quality`：预警（带 `pri` 优先级）/ 数据质量

> ⛔ **以下字段曾经存在于旧版，现已删除，不要在报告里引用**：
> `attribution`（因子归因）、`confidence_intervals`（置信区间）、
> `signals.score`（0-100 自研分）、信号「五档」评级。
> 自研评分体系在 v9.7 整体废弃，这些是它的残留。
> 核实方法：`python3 -c "import json;print(list(json.load(open('/tmp/gf_report.json')).keys()))"`

### 1.5 ⚠️ 数据质量闸门（v10.0 新增 · 必做，不可跳过）

**为什么必须做**：这个项目已经发生过**三次静默失败**（配置路径找不到返回 `{}`、
数据质量分母自指、`config_version` 写错位置导致加密标的全丢），
每次的表象都是「报告看起来正常」。而本 skill 的执行步骤原先**没有任何数据质量闸门** ——
唯一的中止条件是开市判定 `CLOSED`。

上一步的产出 `data_quality` 是判断依据。**按下面三档处理，不要跳过也不要自己加码**：

**判据用 `severity` / `failure_codes`，不要用人眼读中文类别名**（v10.2）：

| `severity` | 含义 | 处理 |
|---|---|---|
| `ok` | 全部就绪 | 正常继续，报告不加横幅 |
| `degraded` | 有洞但报告仍可读 | **继续生成并推送**；报告顶部标 🔴 并列出缺了什么（`gen_report.py` 自动） |
| `critical` | 持仓或行情整体不可用 | **仍推送**（Bob 选择「收到残缺报告」而非「什么都没有」），但报告顶部必须写明**本次数据不可用、不要据此判断** |

`failure_codes` 说明**坏在哪**，可用于分流：`PORTFOLIO_INCOMPLETE` / `MARKET_DATA_MISSING`
→ 立即告警；`SOURCE_FAILED` / `FUNDAMENTALS_DEGRADED` / `ML_UNAVAILABLE`
→ 记一笔、报告照推。**推送前先读这两个字段**，不要只看「报告文件有没有生成」：

```
severity == "ok"        → 正常推送
severity == "degraded"  → 推送 + 标题带「数据不完整」
severity == "critical"  → 推送 + 标题带「数据不可用」+ 显式告警一行
```

> ⚠️ 这条是 v10.0 遗留的**集成债**（评审 Grok 指出）：字段加了、闸门也写了，
> 但如果执行步骤只看「有没有 report.md」，`severity` 等于没接线。

**闸门位置**：本闸门是**第 3 步（生成报告）与第 4 步（推送）之间的硬性前置**。
顺序是：采集 →（本闸门读 `severity`/`failure_codes`）→ 生成报告 → 推送。
不允许「先生成再补看」—— 三档处理的差异体现在**报告标题**上，标题在生成时就定了。

> 📄 **文档唯一来源**：每日流程只以本 SKILL 为准。
> `references/hermes-cron-integration.md` 只讲 Cron 接线与闸门消费契约，
> **不是第二份执行步骤**（它曾在 v5.1 停了两百多个版本没跟上，是漂移源）。

**禁止**：在数据缺失时把 `data_quality` 的 fail 说成「正常」；也禁止因为 fail 就静默跳过推送 ——
Bob 要的是「看到一份说明了自己缺什么的报告」，不是「什么都收不到」。

### 2. Hermes Cron 处理 LLM Prompt

Hermes 定时任务读取 `llm_analyst_prompts.prompts`，对每个角色依次调用**默认主模型**：

```
system_prompt + user_prompt → model → comment
```

将返回的 comment 填回对应角色，形成最终的 `llm_analysts` 结构供报告使用。

### 3. 翻译消息面标题（Layer 6）

采集脚本**已经自动抓好了新闻**（第 1 步里完成，落盘 `cache/news/YYYY-MM-DD.json`），**不要再用 web_search 去搜新闻**——那是旧的假新闻层，已废弃。

读 `llm_analyst_prompts.news_translation_task`，把 `per_symbol` 与 `macro` 里的英文标题逐条译成**一句中文**（≤40 字，直说要点、不加评论、不用术语腔），写回 output JSON：

```json
"news_translations": {
  "per_symbol": {"QQQ": ["译1", "译2"], "bitcoin": ["译1"]},
  "macro": ["译1", "译2"]
}
```

**顺序必须与给定标题一一对应**，数组长度一致。没有新闻的标的不写键。

### 4. 生成诊断

**核心：直接采用采集脚本 `signals` 字段给出的判定**。
该判定 = **公开量化项目的规则投票**（美股 Lean 三模型 / 加密 freqtrade 三策略），**不是自研打分** —— 自研评分系统已在 v9.7 整体废弃（`scoring`/`alpha_models`/`legacy_signal_mapping` 均已删除）。
ML / 基本面 / 宏观 / 链上**没有买卖权**，只作为解释与风险参考。

**报告措辞用「规则状态」，不要用「买入/卖出」（v10.0 硬要求）：**

| 判定值 | 报告里写 |
|---|---|
| 买入 | **多数偏多** |
| 卖出 | **多数偏空** |
| 持有（有共识） | **多数中性** |
| 持有（三方各一票） | **无共识** |
| 特殊资产 | **固定资产状态** |

**为什么改措辞**：报告原先直接写「买入/卖出」，读者会记成「今天该买 BTC」——
而项目自己的回测已证明规则层**不能作为长期择时依据**（4 种仓位口径 × 3 个时段共 12 组全部跑输买入持有）。
状态描述是事实，买入建议是承诺，两者不能混。

**AI 在报告里可以补充的**（都不改判定）：
- **触发原因**：引用 `signals.<sym>.reasons`（技术面理由）
- **风险因素**：引用 `signals.<sym>.risks` + 新闻里的宏观风险 + 组合风险层（集中度/相关性/风险贡献）
- **状态年龄**：引用 `signal_age_days` —— 「多数偏多，已持续 9 个交易日」，
  避免读者误以为是今天新出现的信号

> ⛔ **不要写**：❌「建议买入/卖出」 ❌「可以调仓」 ❌「跌破 MA50 止损」这类操作指令
> ❌ 引用已删除的 `attribution` / `confidence_intervals` / `signals.score`
> ❌ 信号「五档」评级（🟢强烈买入/🟠减仓…）—— 判定层只产出三值
>
> ✅ 唯一允许带操作语气的地方是**风控越线**（`portfolio_risk.flags`，如「SMH 权重 45% 超过上限 25%」）——
> 那是客观事实且可执行，与预测力无关。

### 5. 推送

```bash
/opt/hermes/.venv/bin/hermes send --to feishu --subject "标题" --file /path/report.md
```
**只推飞书，失败也不回退微信**（微信限流不稳定，Bob 已决定少用微信端）。失败就如实报告错误。

## 报告模板（白话版）

```markdown
# Ghostfolio 每日持仓诊断报告
## 今天只需看一件事    一行，取优先级最高的预警；无预警写「今天没有需要特别关注的事」
## 今天大环境          组合级因素（VIX/情绪/美元/利率/息差/自满），从个股理由里抽出后去重
## 一句话结论          表格：名称/规则状态/为什么/风险 + 「今天不用动」清单
## 大盘温度（看整体市场冷不冷）  表格：看什么/现在多少/怎么理解（每项一句白话解释）
## 我的持仓            现值 / 持有几只 / 风险提醒
## 每只怎么看          表格：名称/现价/今日涨跌/RSI/**判定**/比昨天/模型看5天/模型看20天
                      「规则状态」列 = 量化项目规则投票结果，如「**多数偏多** 2偏多/0偏空/3家」
## 规则判定明细        表格：来源/判定规则/逐标的投票（🟢偏多 🔴偏空 ⚪中性）+ 合计 + 多数判定
## 想调仓的话，几种摆法对比   表格：摆法/性价比/白话解释 + 性价比白话说明
## 三位投资人怎么看     巴菲特/芒格/德鲁肯米勒，各一段白话点评
## 持仓消息面（过去 72 小时真正相关的消息）  逐标的列中文译述 + 来源与日期；无新闻的写「近期没有相关消息」；末尾可跟「大环境发生了什么」列宏观事件

## 数据质量            各数据源 ok/失败；有失败则提示"结论可能不完整"
## 术语速查（白话）     RSI、判定、模型预测、性价比、大环境、比昨天 各一句白话
---
数据源 / 生成方式 / 免责声明
```

**写作硬要求**：全篇白话，面向没有金融背景的读者。禁止出现 RSI 之外的裸术语；必须用时立刻附半句白话解释。「不要」写"综上所述""基于以上分析"这类套话。

## 开市判定（v9.3 · 触发前的静默开关）

**每天 21:00（北京时间）触发前先跑**：
```bash
/opt/data/ghostfolio-venv/bin/python /opt/data/scripts/ghostfolio_market_open.py
```
- `CLOSED` → 今晚美股休市 → **不采集、不生成、不推送，只回 `[SILENT]` 结束**
- `OPEN` → 继续正常流程

**原理**：21:00 北京 = 09:00 ET（夏令时）/ 08:00 ET（冬令时），都在美股 09:30 开盘之前，
所以「今晚开不开市」等价于「今天（美东日期）是不是纽交所交易日」。

**实现**：`ghostfolio_market_open.py` —— 纯标准库（`zoneinfo`），假日用**规则计算**
（含复活节算法、假日遇周六前挪周五 / 遇周日后挪周一），**不装 pandas_market_calendars / holidays**，
改年份自动生效、无需年度维护假日表。半日市（13:00 ET 收盘）算开市。

**已验证**：与官方 NYSE 2026 日历逐条吻合（元旦 1/1、MLK 1/19、华盛顿 2/16、耶稣受难日 4/3、
阵亡将士 5/25、六月节 6/19、独立日 7/4 周六→**前挪 7/3**、劳动节 9/7、感恩节 11/26、圣诞 12/25）。
自带 `--selfcheck`（假日/复活节算法/周末/假日前后交易日）。

**为什么不用 cron 的 `monitor_script`**：那是**变更检测**机制（输出没变就抑制），
对「每天该不该跑」会漏发 —— 连续开市日输出都是 OPEN，第二天就被误抑制。

## 判定层（v9.2 · 唯一判定来源）

**报告里只有一个判定，全部来自公开量化项目的规则。** 自研「自身历史分」那套已退出生产
（自己都验证不过，留着只会稀释结论）；「综合分/建议/未验证」列全部删除。

### 按资产类别采用不同项目

| 资产类别 | 采用项目 | 判定条件（源码原文） |
|---|---|---|
| **美股 / ETF** | **QuantConnect/Lean**（21,588⭐） | ① RsiAlphaModel：RSI 状态机（>70空 / <30多 / 35、65 缓冲）<br>② EmaCrossAlphaModel：EMA(12) vs EMA(26)<br>③ MacdAlphaModel：MACD.Signal/Price 与 ±0.01 比较 |
| **加密货币** | **freqtrade**（54,274⭐）· **三个策略多数表决** | **AdxSmas**（趋势）买入：ADX(14)>25 且 SMA(3) 上穿 SMA(6)；卖出：ADX(14)<25 且 SMA(6) 上穿 SMA(3)<br>**BbandRsi**（均值回归）买入：RSI<30 且 收盘价<布林下轨；卖出：RSI>70——⚠️ 布林带用**典型价** (H+L+C)/3，官方是 `qtpylib.typical_price()`，不是收盘价<br>**AwesomeMacd**（动量）买入：MACD>0 且 AO>0 且 AO 上穿 0；卖出：MACD<0 且 AO<0 且 AO 下穿 0 |

**为什么分开**（已核实官方文档）：freqtrade README 自述是 crypto trading bot，**不含美股**；
Lean 官方文档确认支持 Equities/Crypto 等九类资产。

**合成**：各来源一票，多数胜；三方各一票时记「持有（无共识）」——不设人为倾向。
**TradingView 的技术评级已完全移除**（退出判定 + 从报告删掉「第三方」列 + 不再发那 8 个网络请求）。
⚠️ **移除的是「技术评级」，不是「新闻源」** —— TradingView 的 headlines API **仍然在用**（持仓新闻双源之一，见上文），别把新闻接口一起删了。

**报告表格列**：名称 / 现价 / 今日涨跌 / RSI / **判定** / 比昨天 / 模型看5天 / 模型看20天

## 信号年龄（v9.5 · 两侧语义不同，必须标注）

| 侧 | 判定机制 | 报告怎么标 |
|---|---|---|
| **美股** | Lean 三模型**当天重算的当前状态** | 无年龄（每天都是「现在」）|
| **加密** | 三策略各自都是**持仓状态延续** —— 触发后保持到反向信号 | **「信号已持续 N 个交易日」**，且年龄**只取与最终多数同向的票里最近那次触发**；无多数共识时不显示年龄 |
| **美股** | EMA 交叉 / MACD 是**当天重算**；但 **Lean 的 RSI 模型也是粘性状态机**（跌穿 30 → 要回到 35 以上才转中性） | 目前美股不标年龄，报告里已写明这层区别 |

**为什么必须标**：不标的话，读者会把 BTC 的「买入」误读成「今天刚出的买入信号」，
但它可能只是 9 天前（甚至更久）那一次穿越留下的状态。
字段：`signals[sym].signal_age_days` / `last_signal_date` / `signal_semantics`。

## 模型可靠度（v9.5 · 取代伪精确 confidence）

**可靠度 = |预测幅度| ÷ 模型历史误差（信号噪比）**：≥2 高 / ≥1 中 / <1 低。

**为什么换**：原来的 `confidence = 100*(1-rmse/0.05)*(1+|pred|*10)` 是伪精确 ——
RMSE 不是概率，那个式子也没有统计含义，却能算出「73.46%」这种看着很确定的数字。

**⚠️ 实测结果**：9 只里 **8 只可靠度都是「低」**（强度 0.03~0.76），
即预测幅度还没模型自己的历史误差大。固定档资产（SGOV）已从该统计中排除
（货币基金价格几乎不动 → RMSE 极小 → 强度虚高成假象）。

**组合优化降级**：若某标的没有训练好的模型、走了规则式兜底，
`optimization_quality.heuristic_fallback_count > 0`，报告会警示
「今天的优化方案可信度打了折扣，只作结构参考」。

## ⚠️ 规则策略的回测结论（v9.6 实测，必须知道）

**完整交易回放（`ghostfolio_rule_eval.py`，无前视偏差，含手续费+滑点）：**

| | 规则年化 | 买入持有 | 超额 |
|---|---|---|---|
| **BNB** | +15.5% | +12.0% | **+3.5%** ✅ |
| QQQ | +6.9% | +14.4% | −7.6% |
| SCHD | +1.8% | +9.9% | −8.1% |
| ETH | −14.0% | −5.8% | −8.3% |
| VOO | +4.7% | +13.0% | −8.3% |
| SIVR | +12.3% | +21.8% | −9.5% |
| BTC | −1.4% | +10.9% | −12.3% |
| SMH | +10.4% | +33.9% | −23.4% |

**美股/ETF 平均超额 ≈ −10%；加密 ≈ −6%。9 只里只有 BNB 跑赢买入持有。**

**样本外切分（训练 2021-2024 / 验证 2025 / 留出 2026+）——三段全负：**

| | 训练期 | 验证期 | 留出期 |
|---|---|---|---|
| 美股/ETF | −5.7% | −26.9% | **−14.5%** |
| 加密 | −8.1% | +9.9%\* | −3.7% |

\*加密 2025 的 +9.9% **几乎全由 BNB 一只贡献**（+52.6%），其余两只为 −10.0% 和 −12.8%。

→ **该结论不是全样本过拟合，样本外同样成立。**

**仓位口径敏感性（v9.8 增前三种，v9.9 增 `scaled`；回应评审质疑）：**

| 口径 | 训练期 | 验证期 | 留出期 |
|---|---|---|---|
| `long_flat`（原：卖出/持有都清仓）| −5.7% | −26.9% | −14.5% |
| `long_short`（卖出→**做空**，还原 Lean 语义）| **−11.5%** | **−30.9%** | **−26.5%** |
| `hold_keep`（持有→**保持昨日仓位**）| **−2.4%** | **−10.3%** | **−20.1%** |
| `scaled`（仓位=支持票数/总票数）| −5.7% | −25.5% | −15.1% |

**做空不是解药（每期都更差）；HOLD 保持仓位能在训练/验证期收窄缺口，但留出期反而最差；
按票数缩放仓位对 ETF 几乎无差别**（加密训练期有改善，但留出期仍 −3.6%）。
**四种口径 × 三个时段共 12 组，结论全部成立** —— 不是映射假象。

复现：`python3 ghostfolio_rule_eval.py --split`（默认）/ `--short` / `--keep` / `--scaled`

**原因**：长期持仓里大段时间在现金上（仓位占用仅 12%~54%），
**错过上涨的代价大于躲过回撤的收益**。且 ETH 最大回撤仍 −73.9%、BTC −60.4% ——
这些技术状态机对「长期持仓」既没提高收益、也没控住回撤。

**定位**：判定规则仍**如实反映各项目规则的当前判定状态**（这是事实），
但**不能当作能跑赢持有的策略**。报告里**不得暗示「按此操作能赚钱」**。

> ⚠️ 措辞注意：本项目做的是「**时间段留出检验**」（回放连续跑、超额按段切片），
> **不等于**严格的独立样本外 / walk-forward。写文档时别把这两者混为一谈。

完整记录见 `references/rule-backtest-2026-09.md`。

## 固定流程：版本更新 + 三方评审 + GitHub 同步（2026-09-12 确立）

### 一、版本更新的唯一门禁：两家评审全部到齐

**Bob 的要求：后续每个版本的更新，都等 ChatGPT、Perplexity 两家的审核意见全部收到后，
汇总一起完成。** 不允许「收到一家就先改」。
（v10.6 起 **Grok 不再参与评审** —— Bob 2026-09-13 明确「后续只给 ChatGPT 和 Perplexity 两家的评审」。）

流程：

```
改代码 / 自检通过
      ↓
打包评审包（temp/ghostfolio-daily-report-vX.Y/ + tar.gz）
      ↓
Bob 分发给 ChatGPT + Perplexity
      ↓
【停】等两家意见全部到齐，缺一不可
      ↓
逐条对照源码与真实运行输出核实（区分真问题 / 误报）
      ↓
一次性改完 → 升版本号 → 自检 → 同步 GitHub
```

**为什么必须等齐**：漏掉一家的意见就改，会导致下一轮评审报出「你上次那批只改了一半」，
或者不同评审对同一处给出矛盾建议时没有同时看到。等齐再改，一次把矛盾摊开讨论。

**核实纪律**（Bob 的硬要求，不可省）：

- 逐条**对着源码和真实运行输出**核实，不许照单全收；误报当场推翻并写明理由
- 评审可能读到**过期的目录/文件**（发生过两次：一次读了已删的 v9.7，一次以为 `ghostfolio_backfill.py` 不存在）
  —— 核实前先确认它看的是不是当前版本
- 核实结论要能当场跑出来（`--selfcheck` / 实际回放），不能只靠读代码下结论

### 二、同步到 GitHub（固定动作）

仓库：**https://github.com/bobvane/ghostfolio-daily-report** （公开，供免费评审读取）

**不要手动拼命令，直接跑脚本**：

```bash
cd /opt/data/skills/finance/ghostfolio-daily-report/scripts
python3 sync_to_github.py --dry-run   --sample /tmp/gf_report.md   # 先看清单
python3 sync_to_github.py             --sample /tmp/gf_report.md   # 检查通过才推送
```

脚本会做三件人工容易漏、**一旦漏掉就在公开仓库永久留痕**的事：

| 处理 | 不做的后果 |
|---|---|
| 样例报告的金额脱敏 | 你的真实资产总额进公开仓库 |
| commit 用 `bobvane@users.noreply.github.com` | git 全局身份用的个人邮箱会永久写进公开历史 |
| 排除 `references/archive/` | 废弃代码被评审当成现存问题（已发生过一次「读了旧目录」的误报） |

推送前脚本会跑五项检查，**任一项不过就不推**（退出码 1）：
硬编码凭据 / 个人邮箱 / 未隐去的金额 / 误收 archive / README 与配置版本不一致。

> 这五项检查本身在 2026-09-12 用**投毒副本**验证过确实会报错（不是假检查）。
> 其中金额检查曾写错一次：原先只匹配「现在值多少：$数字」这个固定句式 ——
> 而那个句式在暂存前就已替换掉，拿它当检查项等于永远不可能失败。
> 现改为扫描暂存文件里的**任何金额形态**。

### 三、README 需要人工维护的部分

`templates/repo-README.md` 就是仓库的 README。以下内容**必须随版本手工更新**：

- 「五、可靠性修复史」—— 新增版本的修复条目
- 「六、已知限制 / 待修」—— 已核实为真但未动手的项
- 「七、请重点评审的问题」—— 换成本轮真正想问的

脚本会检查 README 是否提到当前配置版本号，不一致就拒绝推送（防「版本不一致」类误报）。

### 四、评审包的历史遗留问题

旧的 `temp/ghostfolio-daily-report-vX.Y/` 目录**用完即删**。
留着会造成「评审读到旧目录」——已经因此产生过一次完整的误报（Perplexity 读了 v9.7 报出早已修好的问题）。

## 显示名（加密货币用简称）

| 内部键（不可改）| 报告显示 |
|---|---|
| `bitcoin` | **BTC** |
| `ethereum` | **ETH** |
| `binancecoin` | **BNB** |

**内部键不要改** —— 它们连着 Ghostfolio 持仓、CoinMetrics 链上数据、TradingView/Yahoo 新闻源映射。
映射定义在 `ghostfolio_collect.py` 的 `SYMBOL_DISPLAY`，随 output JSON 的 `symbol_display` 输出，
`gen_report.py` 读它并用 `disp()` 显示（**单一数据源，不要在 gen_report 里重复定义**）。
分析师的 prompt 里也要求用简称，否则他们的点评会带出全名。

## 要点

- **信号优先级**：以 `signals` 字段为准，AI 只解释不改档；分析师团观点同理——只呈现不覆盖。若 AI 认为某标的技术面或新闻有明显变化需调整，在报告里说明理由后可覆盖
- **conviction/confidence 展示**：明细表 conviction 保留两位小数（-1~+1），不展示 confidence 数值（避免读者误当胜率）
- **SGOV 是货币基金**：判定层直接标 `applicable=false` → 报告显示「**固定资产状态**」，不产生 votes。
  （v10.2 删掉了配置里遗留的 `signal: "持有"` —— 那是自研五档信号的残留，与 `applicable` 语义重复，
  读者会以为这里真有一个「持有」判定。）
- **SMH 集中度**：portfolio_risk.flags 会自动标出超 25% 单标的上限的集中度，报告务必在「组合风控」节复述
- **组合优化落地**：`portfolio_optimization.optimized_portfolios` 里的 risk_parity 和 max_sharpe 方案，给出具体调仓幅度（当前权重 → 目标权重）
- **LLM 处理（触发式，省额度）**：看 `llm_needed` 字段——为 `true` 才叫主模型写三段点评；为 `false` 时报告直接写「今天无重要变化，跳过 AI 点评」。触发条件（`alerts` 里 pri≤4）：权重越线 / 信号变档 / VIX 单日波动 >15% / 利率倒挂翻转 / 模型与信号相反 / RSI 极端。`llm_skip_reason` 说明为何跳过。
- **ML 为什么不参与打分（v5.6 拍板）**：实测各标的预测误差（RMSE）为 SGOV 0.01% / 宽基 1.3~1.7% / SMH·SIVR 3.6~3.8% / **加密 4.3~9%**。且实测补入免费链上特征（MVRV/NUPL/交易所流量）后误差**无改善**（BTC −0.2%~−0.8%，ETH 20日 **+1.6% 反而变差**）——链上是慢变量，与 5/20 日短周期不匹配。结论：4~9% 误差的模型不该有信号投票权，故 ML 仅作展示与组合优化参考，**没有任何决策权**。ML 只做展示与组合优化参考，判定层的票只来自公开量化规则源码。
- **全篇白话（硬要求）**：报告面向**没有金融背景**的读者（Bob 不是金融专业）。正文、表格列名、触发原因、风险描述一律说人话；除 `RSI` 外不出现裸术语，必须用时立刻附半句白话解释（如"性价比（夏普比率）：每承担 1 分价格晃动换回多少收益"）；文末附「术语速查」。禁止"综上所述""基于以上分析"这类套话。分析师团点评同样受限，勿改写成术语腔。
- 报告给结论先行，**规则状态**打头（不是五档信号 —— 判定层只产出三值），风险提示优先于个股细节

## 排障

- **链上指标缺失/异常**：CoinMetrics 社区版对 BNB 只有 2019 年前的旧数据 —— 代码已加 7 天过期守卫自动跳过并记入 `data_quality`。BTC/ETH 正常。判据：`onchain.per_symbol.<sym>.available`
- **特征覆盖率不够**：跑完看 `ml_features.<sym>` 的键数（满 26）。BTC/ETH 应为 26/26；ETF/BNB 为 23/26（mvrv/nupl/exchange_flow 无免费源，属正常）。若低于此数，先查 `data_quality` 里哪一行是 ❌
- **`stablecoin_yoy_growth` 为 None**：DefiLlama `/stablecoincharts/all` 的 `date` 是**字符串**，必须 `int()` 转换后再算时间差；直接做减法会抛错并被 except 吞掉
- **数据拉不到**：先 `curl http://192.168.2.2:3333/api/v1/auth/anonymous` 测连通，Check token 是否在 .env
- **yfinance 报错**：确认用 venv python（系统 PEP668 限制装不了）
- **行情差异**：yfinance 即时但可能有 15 分钟延迟，CoinGecko 做加密交叉验证
- **CoinGecko 限流(429)**：免费 API 加密市场深度数据可能偶尔失败，不影响主信号（price/RSI 来自 yfinance），BNB 若缺 crypto_market 属正常
- **ML 模型缺失**：首次运行无模型文件，自动使用启发式回退（heuristic_fallback），输出 conviction 但 confidence=40；积累足够特征历史后可训练 LightGBM
- **LLM Prompt 生成失败**：返回 prompts_ready: false，不阻塞主流程；上游 Hermes 模型处理时若失败，报告里对应角色留空并标注
- **LLM Prompt 宏观摘要显示 N/A**：`build_llm_analyst_prompts` 需接收 `vix`/`sentiment` 参数（顶层独立字段，不在 `macro` 里）；单字段缺失只该显示该项 N/A，不该整段变 N/A。
- **报告生成报 TypeError**：`gen_report.py` 的 `num()` 兜底后仍报错，说明该字段没走 `num()`；JSON 里 yfinance/CoinGecko 失败会留 `null`。
- **网络**：NAS 走旁路由透明代理，一般无需额外代理配置

---

## Layer 6 新闻层（v7.2 新增）

`ghostfolio_news.py` —— 只采持仓相关消息，全免费，纯标准库零新依赖。规划书见 `references/news-layer-plan.md`。

**必须记住的 4 个坑**（都是实测踩出来的）：

| 坑 | 现象 | 对策（已写进代码） |
|---|---|---|
| TradingView 交易所前缀 | `AMEX:QQQ` 静默返回 **0 条**（`NASDAQ:QQQ` 才对） | 符号表写死，已实测验证 |
| TradingView 回退全局流 | 匹配不到内容时返回同一条 "Iran War" | 用 `relatedSymbols` 校验，命中率 <90% 判为映射错误 |
| 低质源垄断 | QQQ 65% 是 Stocktwits 闲聊、SCHD 78% 是 Zacks 自动稿 | 黑名单 + 内容农场降档限量 |
| 直连全挂 | Google News / Yahoo / CoinDesk 直连全部不通 | 强制走代理 192.168.2.5:7890 |

**两个平台必须并用**：TradingView 结构化好但 **SGOV 零覆盖**；Yahoo 覆盖 9/9 但只有 RSS。

**源分层**：白名单（Reuters/CNBC/CoinDesk/The Block 等）> 中档（etf.com/Benzinga）> 其他 > 低档内容农场（24/7 Wall St 等，每标的限量 1 条）。

**无新闻是有效结论**：SIVR 这类冷门标的确实没有新闻流（最新真新闻在 2 个月前），报告写「近期没有相关消息」，**不要硬塞旧闻**。

## 第三方对照（TradingView）—— **已废弃，见存档**

TradingView 的技术评级**已完全退出本 Skill**：既退出判定，也从报告里删掉「第三方」列与分歧说明，
collect.py 也不再发那 8 个网络请求。历史追查过程（含为什么判定它不准确）存档在
`references/archive/tradingview-deprecated.md`，仅备查，**不要据此恢复任何报告列**。
`scripts/ghostfolio_ta_benchmark.py` 保留为按需离线工具，日常不跑。

## 参考文档（references/）

- `references/free-data-sources.md` — 免费数据源完整清单、字段映射、限流说明、被移除的付费源
- `references/five-layer-architecture.md` — 五层架构实现规范、函数签名、配置契约、部署检查清单
- `references/hermes-cron-integration.md` — Hermes Cron 调用 LLM Prompt 的标准模式、临时工作流、未来内部 API 升级路径
- `references/evolution-roadmap.md` — 近期/中期/长期演进路线、永不做清单、新任务接入检查清单
- `references/changelog-v10.md` —— v9.8–v10.6 完整变更与评审复盘（历史资料，跑日报不需要读）

## 变更记录

v9.8–v10.6 的完整变更与历次评审复盘已移出本文件 → **`references/changelog-v10.md`**
（移出 763 行）。原因：本文件每次加载都进上下文，历史复盘会挤掉真正的执行说明。
当前版本（v10.6）改了什么、哪些是误报，见该文件开头。
