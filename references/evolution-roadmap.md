# Ghostfolio Daily Report — 演进路线图

最后更新：v5.1

---

## 已完成（v5.1 当前状态）

- [x] 五层架构全链路跑通
- [x] 全免费数据源（14 macro + 9 onchain + 6 fundamentals + 市场/技术/情绪）
- [x] Layer 2 特征工程模块化（`ghostfolio_features.py`）
- [x] Layer 3 标准化 Signal（conviction/confidence/thesis/reasons/risks）
- [x] Layer 4 ML 推理（LightGBM + 启发式回退）
- [x] Layer 4 LLM 评论家团队 Prompt 生成（Buffett/Munger/Druckenmiller）
- [x] Layer 5 因子归因、置信区间、组合优化（4 方案）、风控标记
- [x] Hermes Cron 集成模式（Prompt → 默认主模型 → 填充 comment）
- [x] 配置驱动（`ghostfolio_config.json` 单一事实来源）

---

## 近期（1-2 周，免费源可直接做）

| 任务 | 价值 | 实现路径 |
|------|------|----------|
| LightGBM 训练管道 | ML conviction 从启发式变真模型 | `train_lgbm.py`：积累特征+前向收益样本 → 走 AI-Hedge-Fund `validation/` 路线 → 定期回测部署 |
| yfinance quarterly financials | ROE/FCF 趋势、质量因子更稳 | `.quarterly_financials` 解析 → 滚动同比 → 注入 Layer 2 特征 |
| SEC EDGAR 完整实现 | 公司级财报免费获取 | 构建 CIK 映射表（ticker→CIK，静态文件） → `/api/xbrl/companyfacts/` 拉取 → 标准化字段映射 |

---

## 中期（1-2 月，免费源可做）

| 任务 | 价值 | 实现路径 |
|------|------|----------|
| 期权/波动率面建模 | IV rank、VIX term structure、skew 作为宏观/风控新因子 | CBOE VIX 期限结构（免费）、yfinance 期权链（免费） → 计算 IV rank、VIX 期限斜率、25Δ skew |
| 新闻 NLP 量化 | Reuters/CNBC 标题 → 情绪分类、实体提取、事件驱动标记 | HuggingFace `distilbert-base-uncased-finetuned-sst-2-english` 本地跑（免费） → 实体：公司/ETF/币种/宏观事件 |
| 回测框架 | Point-in-time 诚实性、避免前视偏差 | 复刻 AI-Hedge-Fund `backtest.py`：滚动窗口、事后可得数据隔离、交易成本建模 |

---

## 长期（架构演进）

| 任务 | 价值 | 备注 |
|------|------|------|
| 多资产相关性动态聚类 | 替代静态 sector 映射，风控更精准 | 免费：yfinance 日频收益率滚动相关性谱聚类 |
| 另类数据免费源挖掘 | Google Trends、Reddit 情绪、GitHub 开发活跃度 | 免费 API/爬虫轻量级 |
| 组合优化约束增强 | 税务损失收割、流动性分层、ESG 过滤 | 本地实现，无外部依赖 |

---

## 永不做（付费/需 Key/违背原则）

- Glassnode、FMP、Financial Datasets、Alpha Vantage、Bitquery、Covalent、Portfolio Optimizer API
- 外部 LLM API（OpenAI/Anthropic/Google 等）
- 任何 `.cn` 域名、中文财经媒体、国内自媒体、非结构化源

---

## 判断新任务是否接入的检查清单

1. 完全免费（无 Key 或免费 Key 无调用限制）？
2. 结构化 JSON/REST（无需爬虫、无反爬）？
3. 国际权威/主流（Reuters/CNBC/交易所级）？
4. 字段可映射到现有特征向量或信号因子？
5. 不破坏现有五层契约（config-driven，Layer 接口不变）？

全是 → 接入；任一否 → 记录在此表「备选/拒绝」区。