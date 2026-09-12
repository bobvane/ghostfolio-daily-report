# Layer 6 新闻/事件层 —— 规划书

> 版本：草案 v1 ｜ 日期：2026-09-12 ｜ 状态：**待 Bob 确认后实施**
> 所有数据源结论均来自本机实测（见文末「实测记录」），非推测。

---

## 一、这一层要解决什么问题

现在 Skill 里的"新闻"是**假新闻层**：

- SKILL.md 第 133 行写的是"用 web_search 检索" —— 由 AI 在对话里临时搜
- **不可复现**：同一份数据，今天跑和明天跑结果不一样
- **不落盘**：没有历史留存，无法回溯"当时为什么这么判断"
- **AI 分析师团看不到新闻**：Buffett / Munger / Druckenmiller 三个角色只能看到数字，看不到"为什么"

要变成：**每天采集一次、落盘、可追溯的持仓相关消息面**，作为分析师团的参考资料和报告的一个独立小节。

---

## 二、数据源（全部实测可用）

代理：`192.168.2.5:7890`。**不走代理直连会失败**（Google News / Yahoo / CoinDesk 实测全部不通）。

### 主源

| 源 | 用途 | 覆盖 | 格式 | 实测 |
|---|---|---|---|---|
| **TradingView headlines** | 逐标的 | 8/9（SGOV 无） | JSON，200 条/标的 | ✅ |
| **Yahoo Finance RSS** | 逐标的兜底 | **9/9** | RSS，11~20 条 | ✅ |
| CNBC 财经 / 科技 | 大盘 + 宏观 | 全局 | RSS | ✅ |
| MarketWatch 头条 | 大盘 | 全局 | RSS | ✅ |
| Nasdaq 市场 | 大盘 | 全局 | RSS | ✅ |
| 美联储新闻 | 官方货币政策 | 全局 | RSS | ✅ |
| CoinTelegraph / CoinDesk / The Block / Decrypt | 加密 | 全局 | RSS | ✅ |

### 备源（按需）

Google News RSS（任意查询）、Barron's/WSJ、Seeking Alpha、Investing.com

### 为什么两个平台必须并用

| 持仓 | TradingView | Yahoo RSS | 说明 |
|---|---|---|---|
| QQQ | 200 | 20 | |
| VOO | 200 | 19 | |
| SCHD | 112 | 20 | |
| SMH | 200 | 20 | |
| SIVR | **25** | **11** | 冷门标的，两者都少 |
| **SGOV** | **0** | **17** | **TV 完全没覆盖，必须靠 Yahoo** |
| BTC | 200 | 20 | |
| ETH | 200 | 20 | |
| BNB | 200 | 19 | |

- **TradingView 优势**：结构化 JSON、条目多、带 `relatedSymbols` 可直接做相关性校验
- **TradingView 短板**：SGOV 零覆盖；被低质源垄断（见下）
- **Yahoo 优势**：覆盖 9/9（含 SGOV）；简单可靠
- **Yahoo 短板**：只有 RSS；BTC 条目常跑偏到大盘金融股

---

## 三、关键实现细节（全部是实测踩出来的，必须写进代码）

### 1. TradingView 交易所前缀必须精确

```
NASDAQ:QQQ    ✅ 200 条        AMEX:QQQ     ❌ 0 条
AMEX:VOO      ✅ 200 条        NASDAQ:VOO   ❌ 0 条
AMEX:SCHD     ✅ 112 条        NASDAQ:SCHD  ❌ 0 条
NASDAQ:SMH    ✅ 200 条        AMEX:SMH     ❌ 0 条
AMEX:SIVR     ✅  25 条        NASDAQ:SIVR  ❌ 0 条
```

**前缀错了不报错，静默返回 0 条** —— 必须写死验证过的映射表，不能靠猜。

### 2. 必须用 `relatedSymbols` 做相关性校验

TV 在匹配不到内容时会**静默回退到全局新闻流**：QQQ / VOO / SMH 三个不同标的曾返回**同一条** "Iran War" 头条。

校验方式：返回条目里 `relatedSymbols` 必须含自身。

| 符号 | 条目 | 自身命中 | 通过率 |
|---|---|---|---|
| NASDAQ:QQQ | 200 | 200 | 100% |
| AMEX:VOO | 200 | 200 | 100% |
| AMEX:SCHD | 112 | 112 | 100% |
| NASDAQ:SMH | 200 | 200 | 100% |
| AMEX:SIVR | 25 | 25 | 100% |
| BINANCE:BNBUSDT | 200 | 200 | 100% |
| CRYPTO:BTCUSD | 200 | 197 | 98.5% |
| CRYPTO:ETHUSD | 200 | 188 | 94% |

→ 阈值：**自身命中率 < 90% 视为该符号映射有问题，记入 `degraded` 并改用 Yahoo 兜底**。

### 3. 源质量黑名单（必须过滤，否则报告会变垃圾）

实测各标的的新闻来源构成：

| 标的 | 来源 top | 问题 |
|---|---|---|
| QQQ | **Stocktwits 131** / Benzinga 26 / Zacks 16 / GuruFocus 10 / etf.com 8 | Stocktwits 占 65%，是**社交交易闲聊**，不是新闻 |
| VOO | etf.com 152 / Zacks 31 | 单一来源垄断 |
| SCHD | **Zacks 87** / etf.com 24 | Zacks 占 78%，**自动生成稿** |
| SMH | Benzinga 76 / Zacks 61 / etf.com 47 | |
| SIVR | Zacks 19 / etf.com 3 / Reuters 1 | 条目本就不多，需放宽 |
| BNB | **Binance News 84** / U.Today 26 | 交易所自营，**有立场** |

**黑名单（直接剔除）**：
`Stocktwits`（社交闲聊）、`Zacks`（自动生成）、`GuruFocus`（自动生成）、`U.Today`、`Coinpedia`、`CryptoProwl`、`Binance News`（交易所自营）

**白名单（优先保留，有则置顶）**：
`Reuters`、`Dow Jones Newswires`、`MarketWatch`、`CNBC`、`Barron's`、`The Block`、`CoinDesk`、`Cointelegraph`、`Crypto Briefing`

**中间档（保留）**：`etf.com`、`Benzinga`、`Seeking Alpha`、`Barchart`

> ⚠️ 若某标的过滤后条目为 0（可能发生在 SIVR / SGOV 这类冷门标的），**降级保留中间档**，不要因为它冷门就变成空白。

### 4. 复用现成的 `feed.py`，零新依赖

`skills/research/rss-feeds/scripts/feed.py` —— 纯 Python 标准库，已在本机，RSS/Atom/JSON 全能读，已实测可解析 Yahoo 和 Google News。

TradingView 是 JSON 接口，用 `urllib` 直接取即可，也无需新依赖。

---

## 四、输出结构（落盘，供追溯）

```
/data/cache/news/2026-09-12.json
```

```json
{
  "as_of": "2026-09-12T08:00:00+08:00",
  "per_symbol": {
    "QQQ": {
      "items": [
        {"title": "...", "source": "Reuters", "published": "2026-09-11T17:13:29Z",
         "link": "https://...", "tier": "high", "related": true}
      ],
      "sources_used": ["tradingview", "yahoo"],
      "raw_count": 220, "kept": 3
    }
  },
  "macro_events": [
    {"title": "...", "source": "美联储新闻", "published": "...", "link": "..."}
  ],
  "degraded": ["SGOV: TradingView 无覆盖，仅 Yahoo", "SIVR: TV 自身命中 25/25 但总量少"]
}
```

**去重规则**：标题归一化（小写、去标点）后取前 60 字符做指纹，跨源去重 —— 同一件事 Reuters 和 Yahoo 都报，只留白名单优先的那个。

**时间窗**：默认最近 72 小时（覆盖周末和节假日），最多保留每标的 3 条。

---

## 五、消费方式

1. **报告新增「持仓消息面」小节** —— 每只标的最多 3 条，标注来源与时间，全中文表述
2. **喂给 AI 分析师团** —— 这是明确要求：三位投资人角色（Buffett / Munger / Druckenmiller）的 prompt 里注入对应持仓的新闻，让他们**基于真实消息**而不是凭空点评
3. **落盘留档** —— 支持"回溯当时为什么这么判断"

---

## 六、成本与失败控制

| 项 | 做法 | 理由 |
|---|---|---|
| 采集范围 | **只采持仓 9 只**，不做全市场扫描 | Bob 明确要求"保证这个 Skill 不会特别巨大" |
| 条目上限 | 每标的去重后 3 条 | 防臃肿 |
| LLM 摘要 | **不做** | 省 token；摘要交给现有分析师团环节，不重复调模型 |
| 单源失败 | 记入 `degraded`，**不阻塞报告** | 新闻层是增强，不是前提 |
| 超时 | 单源 15 秒，总预算 90 秒 | 失败快速跳过 |
| 语言 | 标题保留英文原文 + 报告里用中文转述 | 见下方待确认项 |

---

## 七、分阶段实施

| 阶段 | 内容 | 可否立即验证 |
|---|---|---|
| **P1** | 采集器 `ghostfolio_news.py` + 落盘 + 报告小节 | ✅ 可，纯代码 |
| **P2** | 注入 AI 分析师团 prompt | ✅ 可 |
| **P3** | 历史回溯、去重优化 | 视 P1 效果 |

---

## 八、待 Bob 确认

1. **源黑名单同意吗？** 剔除 Stocktwits / Zacks / GuruFocus / U.Today / Binance News 等（理由见 §3.3）
2. **新闻标题要不要翻译成中文？** 报告要求全中文，但翻译需调模型 → 增加 token。三个选项：
   - (a) 保留英文标题 + 中文一句话转述（推荐，成本低）
   - (b) 全翻译成中文（成本高）
   - (c) 原样英文（不符合"全中文"要求）
3. **每标的保留几条？** 建议 3 条
4. **宏观事件要不要单列一节？**（美联储 / CPI / 利率决议）

---

## 附：实测记录

**直连（失败）**
```
❌ Yahoo Finance 个股RSS      HTTP 403
❌ Google News RSS            timeout
❌ CoinDesk RSS               Network unreachable
❌ Reuters 商业               Network unreachable
```

**直连（可用）**
```
✅ CoinTelegraph / CNBC 财经 / CNBC 科技 / Nasdaq 市场 / 美联储新闻 / MarketWatch
```

**走代理 192.168.2.5:7890（全部可用）**
```
✅ Google News RSS  200  126KB
✅ Yahoo 个股RSS     200   14KB
✅ CoinDesk / The Block / Decrypt / Barron's / Investing.com
✅ TradingView headlines API  200 条结构化 JSON（带 relatedSymbols）
```

**TradingView 接口**
```
https://news-headlines.tradingview.com/v2/headlines?client=web&lang=en&symbol=<EXCHANGE:SYMBOL>
返回字段: id, title, provider, source, published(unix), urgency, link, relatedSymbols
注意: 需 Origin/Referer 头；v3 版本 404，只有 v2 可用
```

**Yahoo 接口**
```
https://finance.yahoo.com/rss/headline?s=<SYMBOL>
需浏览器 UA + 代理；加密用 BTC-USD / ETH-USD / BNB-USD
```
