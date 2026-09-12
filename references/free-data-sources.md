# Ghostfolio Daily Report — 免费数据源速查表

最后更新：v5.1

---

## Layer 1 数据源清单（全免费、无需 Key、生产可用）

| 类别 | 来源 | 端点/方法 | 关键字段 | 限流/注意 |
|------|------|-----------|----------|-----------|
| **持仓** | Ghostfolio API | `GET /api/v1/positions` | qty, avg_cost, symbol | NAS 本地，需 GHOSTFOLIO_ACCESS_TOKEN |
| **美股行情+技术指标** | yfinance | `Ticker.history(1y)` + 自算 | price, MA20/50/200, RSI14, high_52w, low_52w | 即时但可能 15min 延迟，PEP668 需 venv |
| **加密现价** | yfinance + CoinGecko | `Ticker.history` + `/api/v3/simple/price` | price, change_24h_pct | 双源交叉验证，CoinGecko 429 偶发 |
| **加密市场深度/备源** | CoinGecko | `/api/v3/global`, `/api/v3/coins/markets` | market_cap, volume, btc_dominance | 免费额度充足 |
| | CoinCap | `/v2/assets` | price, changePercent24Hr, volumeUsd24Hr | 无 Key，主流币前 10 |
| | CoinPaprika | `/v1/tickers` | quotes.USD.price, percent_change_24h | 无 Key，ticker 级 |
| | CryptoCompare | `/data/pricemultifull` | RAW.{SYM}.USD.{PRICE,CHANGEPCT24HOUR,VOLUME24HOURTO} | 无 Key，多交易所聚合 |
| **恐慌指数 VIX** | yfinance | `^VIX` | price, change_pct | 实时 |
| **市场情绪 Fear&Greed** | alternative.me | `/api/fng/` | value, classification | 免费 |
| **宏观 FRED** | FRED API | `/series/observations` | FEDFUNDS, DGS10, DGS2, DGS3MO, CPIAUCSL, DTWEXBGS, UNRATE | 可选 Key（额度更高），无 Key 也可用 |
| **宏观免费增强 Econdb** | Econdb | `/api/series/{CODE}?format=json` | US.GDP.A, US.CPI.M, US.UNR.M, US.FEDFUNDS.M, US.G10Y.M, US.G2Y.M | 无 Key，年/月频 |
| **宏观免费增强 Fed Treasury** | FiscalData Treasury | `/v2/accounting/od/avg_interest_rates`（**v2**，v1 无此表；查询串里的空格必须写成 `%20`，否则 urllib 直接拒绝） | avg_tbill_rate | 无 Key |
| **基本面** | yfinance `.info` | `Ticker.info` | ROE, FCF, P/E, P/B, Margin, D/E, 增长率, Beta, 股息率 | 免费，ETF/Stock 通用 |
| **基本面 SEC EDGAR** | SEC API | `/api/xbrl/companyfacts/CIK{...}.json` | 财报 XBRL | 免费，需 CIK 映射（待实现） |
| **链上全球** | CoinGecko | `/api/v3/global` | total_market_cap, btc_dominance, volume_24h, market_cap_change_24h | 免费 |
| **稳定币** | DefiLlama | `/stablecoincharts/all` | peggedAssets[].circulating.peggedUSD | 免费 |
| **链上价格多源** | CoinCap/CoinPaprika/CryptoCompare | 见上 | BTC/ETH/BNB 多源价格 | 免费，去重/交叉验证 |

---

## 被移除的付费/需 Key 源（v5.1 起不再使用）

| 源 | 原用途 | 移除原因 |
|------|--------|----------|
| Glassnode | MVRV, NUPL, 交易所流量 | 需付费 Key，免费 tier 极限 |
| Financial Modeling Prep (FMP) | TTM 比率、标准化基本面 | ratios-ttm 403，付费墙 |
| Financial Datasets | 机构级基本面 | 付费为主 |
| Alpha Vantage | 基本面+技术面 | 免费额度极小 |
| Bitquery / Covalent | 链上深度 | 需 Key |
| Portfolio Optimizer API | 有效前沿、Black-Litterman | 需注册 Key，本地实现已足够 |
| 外部 LLM API (OpenAI/Anthropic) | 评论家团队 | 改用 Hermes 默认主模型 |

---

## 判断标准：新增源是否接入

1. **完全免费**（无 Key 或免费 Key 无调用限制）
2. **结构化 JSON/REST**（无需爬虫、无反爬）
3. **国际权威/主流**（Reuters/CNBC/交易所级）
4. **无 `.cn`/中文自媒体/国内站点**
5. **字段可映射到现有特征向量或信号因子**

不满足任一项 → 不接入，记录在「后续演进」备选。

---

## 链上估值/流量数据（v5.7 新增）

**CoinMetrics 社区版** —— `https://community-api.coinmetrics.io/v4`

| 项 | 实测结果 |
|---|---|
| 需要注册/Key？ | **不需要**，直连即可 |
| 免费可用指标（BTC/ETH） | `CapMVRVCur`(31个含此)、`FlowInExUSD/Out`、`SplyExUSD`、`CapMrktCurUSD`、`PriceUSD`、`AdrActCnt` 等 |
| 历史深度 | BTC 4271 天、ETH 4052 天（2015 起）；足够训练 |
| **BNB** | ❌ 免费层只有 2019-04 前的旧数据（647 行）→ 有过期守卫自动跳过 |
| 交易所流量指标 | ✅ `FlowInExUSD` / `FlowOutExUSD` 可用 |
| NUPL | 无需单独申请：`NUPL = 1 − 1/MVRV` |

**被排除的源**：CoinGlass（无免费 API，$29/月起）、Glassnode（免费层拿不到 API）、
BGeometrics（免费但要注册，且**仅 BTC**、8次/小时）。

**待标定（重要）**：`mvrv_z_score` 用可得历史的标准差近似 Glassnode 全历史算法，
绝对值有偏差；`exchange_net_flow_pct_mcap` 阈值（0.01%）为经验值。
两者都需要用历史分位数做正式标定 —— 见后续「量化推算准确率」讨论。

---

## v5.8 补齐的三类数据（采集范围：**仅持仓标的 + 大盘，不做全市场**）

| 数据 | 来源 | 落点 | 体积 |
|---|---|---|---|
| 成交量（当日/20日均/量比） | yfinance（原本就返回，之前只取了 Close） | `tech.<sym>` | 0 |
| 价格+成交量日线历史（1年） | yfinance `history(period=1y)` | `/opt/data/cache/prices/<sym>.csv` | 约 84KB / 9 只 |
| 稳定币总量 + YoY 增长 | DefiLlama `/stablecoins` + `/stablecoincharts/all` | `onchain.*` | 0 |
| BTC占比 / 总市值 / 24h变化 兜底 | CoinPaprika `/v1/global` | `onchain.*`（CoinGecko 失败时） | 0 |

**踩过的坑**：
1. DefiLlama `/stablecoincharts/all` 返回的是**列表**（历史序列），不是 `{"peggedAssets":...}`。
   旧代码拿它当总量接口，`"peggedAssets" in d` 永远为假 → 两个特征一直空着。
2. 该接口的 `date` 字段是**字符串**，做时间差前必须 `int()`。
3. 加密标的曾走独立分支、只保留 7 个字段，丢掉 52周高低/ma200/5日·20日前收盘/成交量，
   而训练侧是真实值 → 训练/推理错位。现已改为整体继承。

**体积控制**：不做全市场扫描；价格历史只写持仓标的，1 年日线共约 84KB。
