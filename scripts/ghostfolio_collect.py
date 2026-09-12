#!/usr/bin/env python3
"""
Ghostfolio 持仓数据采集脚本 v3
- 认证 Ghostfolio API → 拉取持仓
- yfinance → 美股 ETF 现价 + 技术指标（MA20/50/200, RSI）
- yfinance/CoinGecko → 加密市值
- yfinance ^VIX → 恐慌指数
- alternative.me → Fear & Greed 情绪指数
- CoinGecko → 加密市场数据（市值排名、成交量、ATH距离）
- FRED API → 宏观数据（利率、息差、CPI、DXY）
- Glassnode/CoinGecko/DefiLlama → 链上指标（MVRV、NUPL、稳定币、交易所流量）
- Econdb/Fed Treasury → 免费宏观数据（GDP、CPI、失业率、美债）
- CoinCap/CoinPaprika/CryptoCompare → 免费加密行情备源
- yfinance info → 基本面数据（ROE、FCF、P/E、P/B、Margin、D/E）
输出结构化 JSON 到 stdout（供 Hermes cron 的 prompt 读取）

运行环境：/opt/data/ghostfolio-venv/bin/python
"""
import os, sys, json, math, time
import urllib.request
import urllib.parse
import urllib.error
import datetime
import statistics
import pickle
import socket
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# 旁路由 SOCKS5 代理 —— 被墙源直连失败时自动切换。
# 换机器/换网只改环境变量 GF_SOCKS_PROXY=host:port；默认值是本机现网地址，保持零配置可跑。
_socks = os.environ.get("GF_SOCKS_PROXY", "192.168.2.5:7891").split(":")
SOCKS_PROXY = (_socks[0], int(_socks[1]))

# ─── 数据源失效记录 ───
# 为什么需要：脚本里有大量 `except Exception: pass`。对日报来说这很危险 ——
# CoinMetrics 挂了 / CoinGecko 429 / yfinance 返回空，程序照样 exit 0 出报告，
# 读者看到「今天没有风险」，实际只是「数据没拿到」。统一登记进 data_quality。
_SOURCE_FAILURES = []


def record_failure(source: str, err) -> None:
    """登记一次数据源失效（去重保留首次）。任何异常都吞掉 —— 记录本身不能反过来炸主流程。"""
    try:
        msg = f"{type(err).__name__}: {err}"[:160]
        if not any(f["source"] == source for f in _SOURCE_FAILURES):
            _SOURCE_FAILURES.append({"source": source, "error": msg})
    except Exception:
        pass          # ← 故意保留：记录失败不能再触发记录，否则无限递归



# 展示层状态词（v10.3）。判定层内部枚举保持 买入/卖出/持有 不变（机器可读），
# 但**任何写给读者看的文本**都必须在写出前翻译成 偏多/偏空/中性。
# 教训：v10.2 只翻译了报告主表，「状态变化播报」和告警文案仍在拼原生枚举 ——
# 今天样本恰好没有变档，所以测不出来；一旦变档，报告立刻退回交易信号板。
_STATE_WORD = {"买入": "偏多", "卖出": "偏空", "持有": "中性"}


def state_words(s: str) -> str:
    """把含原生枚举的展示串（如 '买入→卖出'）整体换成展示词。"""
    for k, v in _STATE_WORD.items():
        s = (s or "").replace(k, v)
    return s


def fetch_url(url: str, headers: dict = None, timeout: int = 15) -> bytes:
    """统一 HTTP 获取：先直连，失败自动走 SOCKS5 代理重试。

    v10.5 —— **HTTP 200 但内容为空，也算失败**（第四轮评审 ChatGPT P1）。

    上游限流时常见两种「假成功」：200 + 空体、200 + `{}`。`json.loads("{}")` 不抛异常，
    下游 `.get("data", [])` 拿到空列表 —— 于是表现成「今天没数据」而不是
    「这个源此刻不可用」，一路静默传到底，`severity` 也不会动。

    守卫放在这里而不是评审建议的 `fetch_with_retry`：全仓 16 个抓取点走的都是
    `json.loads(fetch_url(...))`，`fetch_with_retry` 只有 1 个调用方。
    放在咽喉处，各调用方原有的 `except` → `record_failure()` 自动生效，16 个点一行不用改。

    ponytail: 只挡「整个响应体为空」这一层。体是合法 JSON、但里面关键字段空
    （`{"data": []}`）不在这里判 —— 那属于各源自己的语义，硬判会误伤。
    """
    headers = headers or {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)

    def _read() -> bytes:
        body = urllib.request.urlopen(req, timeout=timeout).read()
        stripped = body.strip()
        if not stripped or stripped in (b"{}", b"[]", b"null"):
            raise ValueError(f"空响应（HTTP 200 但内容为空）: {url[:100]}")
        return body

    try:
        return _read()
    except ValueError:
        raise               # 空响应不是网络问题，再走代理重试一次也是白发
    except Exception:
        import socks
        old_socket = socket.socket
        socks.set_default_proxy(socks.SOCKS5, *SOCKS_PROXY)
        socket.socket = socks.socksocket
        try:
            return _read()
        finally:
            socket.socket = old_socket
            socks.set_default_proxy()

# ============ ML/Feature Engineering imports ============
try:
    import lightgbm as lgb
    LGBM_AVAILABLE = True
except ImportError:
    LGBM_AVAILABLE = False

try:
    from ghostfolio_features import (
        build_feature_vector, save_features, load_feature_history,
        compute_technical_features, compute_macro_features, compute_onchain_features
    )
    FEATURES_AVAILABLE = True
except ImportError:
    FEATURES_AVAILABLE = False

# ============ 配置（从 .env 或环境变量读取） ============
BASE_URL = os.environ.get("GHOSTFOLIO_BASE_URL", "http://192.168.2.2:3333")
ACCESS_TOKEN = os.environ.get("GHOSTFOLIO_ACCESS_TOKEN", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")  # 可选，无 key 也能用但有频率限制
# SEC EDGAR 要求 UA 带联系方式；默认中性，真实邮箱放 .env（避免随代码外发泄露）
# 实测：SEC 拒绝纯 "Mozilla/5.0" 和"无联系方式的描述"（403），接受带 URL 的标识；
# 用项目 URL 作默认，避免把个人邮箱写进代码随包外发。可用 SEC_EDGAR_UA 覆盖。
SEC_EDGAR_UA = os.environ.get("SEC_EDGAR_UA", "GhostfolioReport/1.0 (+https://bobvane.top)")

# 路径基准：优先环境变量 GF_HOME；否则取本文件所在目录的上一级（即项目根，默认 /opt/data）。
# 这样把项目挪到别的机器/目录也能跑，不再硬编码 /opt/data。
_HERE = Path(__file__).resolve().parent
GF_HOME = Path(os.environ.get("GF_HOME", _HERE.parent))

# 配置查找：先看脚本同级（开发/生产布局），再看项目根（评审包布局 scripts/ + config 同级于根）。
# 环境变量 GF_CONFIG 可显式指定。找不到就**大声报错**，不再静默返回 {} —— 静默空配置会导致
# special_assets（SGOV 固定持有）、risk_budget、新闻/保留策略全部悄无声息失效。
def _find_config():
    cands = [os.environ.get("GF_CONFIG"),
             str(_HERE / "ghostfolio_config.json"),
             str(GF_HOME / "ghostfolio_config.json")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None

CONFIG_PATH = _find_config()

def load_config():
    if not CONFIG_PATH:
        raise FileNotFoundError(
            "找不到 ghostfolio_config.json —— 已查找: "
            f"{_HERE}/ , {GF_HOME}/ 。请设置 GF_CONFIG 或 GF_HOME 环境变量。")
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

CONFIG = load_config()
# 特殊资产（不参与择时）—— 判定层据此从源头不产生 rules，见 get_tech_indicators
_SPECIAL_ASSETS = set((CONFIG.get("special_assets") or {}).keys())

def fetch(url, headers=None, timeout=15):
    """通用 HTTP GET —— 统一走 fetch_url，直连失败自动切 SOCKS 代理"""
    return json.loads(fetch_url(url, headers=headers or {"User-Agent": "Mozilla/5.0"}, timeout=timeout))

def fetch_with_retry(url, headers=None, timeout=15, max_retries=2):
    """带重试的抓取。

    ⚠️ 本函数**只做重试**，不登记任何失败 —— 原先的 docstring 声称「失败登记到
    _SOURCE_FAILURES（区分超时/HTTP码/解析错）」，但函数体里没有任何登记，是假的。
    真正的登记在各调用方通过 `record_failure(source, err)` 完成。
    """
    for i in range(max_retries + 1):
        try:
            return fetch(url, headers, timeout)
        except Exception as e:
            if i == max_retries:
                raise
            time.sleep(1 * (i + 1))
    return None

def get_auth_token():
    """Ghostfolio 匿名认证，获取临时 JWT"""
    data = json.dumps({"accessToken": ACCESS_TOKEN}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/anonymous",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode())
    return resp["authToken"]

def get_positions():
    """拉取全部持仓数据，汇总每个标的的数量与加权成本"""
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"}
    data = fetch(f"{BASE_URL}/api/v1/export", headers=headers)

    positions = {}
    _unsupported = {}      # 非 BUY/SELL 的 activity 类型计数
    for a in data.get("activities", []):
        sym = a["symbol"]
        qty = a["quantity"]
        price = a["unitPrice"]
        fee = a.get("fee", 0) or 0
        t = a["type"]

        if sym not in positions:
            positions[sym] = {"qty": 0.0, "cost": 0.0, "buys": 0, "sells": 0,
                              "currency": a["currency"], "first_date": a["date"],
                              "last_date": a["date"]}
        p = positions[sym]
        if t == "BUY":
            p["qty"] += qty
            p["cost"] += qty * price + fee
            p["buys"] += 1
        elif t == "SELL":
            # ponytail: 按加权平均成本同比例冲减，不能用卖出价（会把剩余持仓的成本基础带偏）
            _avg = (p["cost"] / p["qty"]) if p["qty"] > 0 else 0
            p["cost"] = max(p["cost"] - qty * _avg, 0.0)
            p["qty"] -= qty
            p["sells"] += 1
        else:
            # SPLIT/TRANSFER/DIVIDEND/FEE 等当前不参与归集。不猜 Ghostfolio 的
            # 字段名硬写 split 数学（猜错更危险），但**绝不能静默** —— 计数上报，
            # 出现时报告里直接可见。当前真实账户 23 条 activity 全是 BUY。
            _unsupported[t] = _unsupported.get(t, 0) + 1
        p["last_date"] = a["date"]

    # 计算加权成本，只保留有持仓的
    result = {}
    for sym, p in positions.items():
        if p["qty"] > 0:
            p["avg_cost"] = p["cost"] / p["qty"] if p["qty"] > 0 else 0
            result[sym] = p
    _meta = dict(data.get("meta", {}))
    if _unsupported:
        _meta["unsupported_activities"] = _unsupported
    return result, _meta

# ============ 技术指标计算 ============
import numpy as np

def calc_rsi(closes, period=14):
    """计算 RSI（Wilder 平滑）"""
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ma(closes, window):
    """简单移动平均"""
    if len(closes) < window:
        return None
    return float(np.mean(closes[-window:]))

PRICE_CACHE_DIR = GF_HOME / "cache" / "prices"
# 价格历史长度：由 config 控制。1y 只够算指标；做因子评价(IC/分层收益)需要 5y
# 才有统计意义（9 只 × 1250 天 ≈ 1.1 万观测）。仍只采持仓 + 大盘，不做全市场。
HISTORY_PERIOD = (CONFIG.get("feature_engineering", {}) or {}).get("price_history_period", "2y")


# 保留策略：判定所需最大窗口 = 自身历史分位 252 根。
# 实测 250 根与 5 年结果 8/8 完全一致 → 留 600 行（约 2.4 倍余量）。
RETENTION_ROWS = int((CONFIG.get("retention", {}) or {}).get("price_rows", 600))
RETENTION_NEWS_DAYS = int((CONFIG.get("retention", {}) or {}).get("news_days", 30))


def cleanup_cache() -> dict:
    """跑完清理：价格 CSV 截到保留行数，新闻 JSON 删过期。

    判据来自实测 —— 判定所需窗口之外的数据对本 Skill 无价值，留着只会臃肿。
    任何异常都吞掉，清理失败不该影响当日报告。
    """
    freed, notes = 0, []
    try:
        for f in PRICE_CACHE_DIR.glob("*.csv"):
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
                if len(lines) - 1 > RETENTION_ROWS:
                    f.write_text("\n".join([lines[0]] + lines[-(RETENTION_ROWS):]) + "\n",
                                 encoding="utf-8")
                    notes.append(f"{f.stem} 截到 {RETENTION_ROWS} 行")
            except Exception:
                continue
    except Exception as _e:
        record_failure('cleanup_cache', _e)
    try:
        # 特征 JSONL 是一天一行追加，价格 CSV 有截断而它没有 —— 同一套保留策略补上。
        for f in (GF_HOME / 'cache' / 'features').glob('*.jsonl'):
            try:
                lines = f.read_text(encoding='utf-8', errors='ignore').splitlines()
                if len(lines) > RETENTION_ROWS:
                    f.write_text('\n'.join(lines[-RETENTION_ROWS:]) + '\n', encoding='utf-8')
                    notes.append(f'{f.stem}.jsonl 截到 {RETENTION_ROWS} 行')
            except Exception:
                continue
    except Exception as _e:
        record_failure('cleanup_cache', _e)
    try:
        import datetime as _dt
        cutoff = _dt.date.today() - _dt.timedelta(days=RETENTION_NEWS_DAYS)
        for f in (GF_HOME / "cache" / "news").glob("*.json"):
            try:
                if _dt.date.fromisoformat(f.stem) < cutoff:
                    freed += f.stat().st_size
                    f.unlink()
                    notes.append(f"删除过期新闻 {f.stem}")
            except Exception:
                continue
    except Exception as _e:
        record_failure('cleanup_cache', _e)
    return {"freed_bytes": freed, "notes": notes}


def _persist_price_history(sym: str, h) -> None:
    """把日线收盘/成交量落盘，供后续算真实波动率、相关性、回撤、回测用。

    ponytail: 只写持仓标的（9 个），1 年日线约 250 行/标的，总计约 100KB —— 不做全市场采集。
    历史长度由 config.feature_engineering.price_history_period 控制（现配 2y；实测 250 根即够）。
    """
    try:
        PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cols = [c for c in ("Close", "Volume") if c in h.columns]
        df = h[cols].copy().tail(RETENTION_ROWS)      # 判定窗口外的直接不写盘
        df.index = [d.strftime("%Y-%m-%d") for d in df.index]
        df.to_csv(PRICE_CACHE_DIR / f"{sym}.csv", float_format="%.6g")
    except Exception as _e:
        record_failure('_persist_price_history', _e)


# 产物 schema 版本（v10.2，评审 ChatGPT）。
# 与 config["version"] 分开：配置改的是参数，schema 改的是**字段结构**，
# 两者可以不同步。契约测试会断言 schema_version == config_version == SCHEMA_VERSION
# —— 让漂移被测出来，而不是靠人记得同步。
SCHEMA_VERSION = "10.8"
# 参与产出的脚本清单：指纹用于回答「这份报告是同一套代码产出的吗」。
# （本目录不是 git 仓库，取不到 commit；指纹比 commit 更适合本地流水线，
#   而且能抓到 ChatGPT 提的那个场景：collect 升级了、gen_report 没升级。）
PIPELINE_SCRIPTS = ("ghostfolio_collect.py", "ghostfolio_rules.py", "gen_report.py",
                    "ghostfolio_news.py", "ghostfolio_market_open.py")


def _code_fingerprint() -> str:
    """参与产出的脚本内容指纹（前 12 位）。任何一处改动都会变。"""
    import hashlib
    h = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in PIPELINE_SCRIPTS:
        try:
            with open(os.path.join(here, name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:12]


def get_tech_indicators(ticker, sym: str = None):
    """用 yfinance 拉取历史数据，计算技术指标（sym 传入时顺手落盘价格历史）"""
    import yfinance as yf
    try:
        h = yf.Ticker(ticker).history(period=HISTORY_PERIOD)
        if h.empty:
            return None
        closes = h["Close"].tolist()
        current = float(closes[-1])
        prev = float(closes[-2])
        ma20 = calc_ma(closes, 20)
        ma50 = calc_ma(closes, 50)
        ma200 = calc_ma(closes, 200)
        rsi14 = calc_rsi(closes, 14)
        # 52周高低
        hi52 = float(max(h["High"][-252:]))
        lo52 = float(min(h["Low"][-252:]))
        # 真实 5/20 日前收盘价（供特征工程计算真实动量，与训练数据对齐）
        close_5d_ago = float(closes[-6]) if len(closes) >= 6 else current
        close_20d_ago = float(closes[-21]) if len(closes) >= 21 else current
        # 成交量（yfinance 本来就返回，之前只取了 Close）
        vols = h["Volume"].tolist() if "Volume" in h.columns else []
        vol_today = float(vols[-1]) if vols else None
        vol_avg20 = float(np.mean(vols[-20:])) if len(vols) >= 20 else None
        # 数据新鲜度：记录最后一根 K 线的日期。
        # 光看「price 有没有值」不够 —— API 失败回退缓存时价仍存在，但可能是几天前的。
        try:
            _lb = h.index[-1]
            last_bar = str(_lb.date()) if hasattr(_lb, "date") else str(_lb)[:10]
            _aged = (datetime.date.today() - datetime.date.fromisoformat(last_bar)).days
        except Exception:
            last_bar, _aged = None, None
        if sym:
            _persist_price_history(sym, h)
        # 判定层：公开量化项目的「买/卖/持」判定（美股 Lean / 加密 freqtrade），非预测模型
        #
        # 【数据契约】特殊资产（货币基金等）不参与择时 → 根本不该有 rules。
        # 此前是「照样跑完整规则、显示层各处加 guard」—— 漏一处就出假票数
        # （v9.9 修复的第 2 条 bug 就是漏了合计行那一处）。
        # 现在改为源头不产生：applicable=False 时 votes/majority 一律为空，
        # 由 ghostfolio_contract_test.py 断言「applicable=False → 无 votes」。
        if sym and sym in _SPECIAL_ASSETS:
            rules_res = {"applicable": False, "reason": "cash_equivalent",
                         "asset_type": "special",
                         "verdicts": [], "total": 0, "failed": 0,
                         "buy": 0, "sell": 0, "hold": 0,
                         "majority": None, "tie": False}
        else:
            try:
                import ghostfolio_rules as gfr
                # 美股走 QuantConnect/Lean 三模型，加密走 freqtrade 三策略 —— 两套完全分开
                # 资产类别显式化（v10.2，评审 ChatGPT #4）：此前 `is_crypto=(sym in
                # CRYPTO_MAP)` 让「不在加密表里」隐式等于「按美股处理」。现在类别是
                # 具名字段并随产物输出，契约测试可断言 crypto↔加密策略、
                # special↔applicable=False，不再靠读代码猜。
                # （没有 price 的标的在函数开头 `if h.empty: return None` 就被挡掉了，
                #   所以这里只可能是 special/crypto/equity 三类 —— 不设 unknown 死分支。）
                # 边界：新增的自研评分体系此前会在类别外再判断一次，v9.7 已删。
                _atype = ("crypto" if sym in CRYPTO_MAP
                          else "special" if sym in _SPECIAL_ASSETS
                          else "equity")
                rules_res = gfr.evaluate(h, is_crypto=(_atype == "crypto"))
                rules_res["applicable"] = True
                rules_res["asset_type"] = _atype
            except Exception as _e:
                rules_res = {"applicable": True, "error": f"{type(_e).__name__}: {_e}",
                             "asset_type": ("crypto" if sym in CRYPTO_MAP else "equity"),
                             "majority": None, "verdicts": [], "buy": 0, "sell": 0,
                             "hold": 0, "tie": False, "total": 0, "failed": 0}
        # ── 风险层输入（v10.0）──
        # 组合级的相关性 / 风险贡献 / 组合回撤需要「对齐后的收益矩阵」，
        # 而这些数据在算指标时手上就有 —— 存最近 90 个交易日的日收益即可，
        # 不必再去读一遍价格历史。年化波动与近一年最大回撤是单标的标量，顺手一起算。
        _cl = h["Close"].to_numpy(dtype=float)
        _r = _cl[1:] / _cl[:-1] - 1.0
        _risk = {
            "returns_90d": [round(float(x), 6) for x in _r[-90:]],
            "vol_annual": (round(float(_r[-252:].std() * (252 ** 0.5)), 4)
                           if len(_r) >= 20 else None),
        }
        if len(_cl) >= 2:
            _eq = _cl[-252:] / _cl[-252]
            _risk["max_dd_1y"] = round(float((_eq / np.maximum.accumulate(_eq) - 1).min()), 4)
        else:
            _risk["max_dd_1y"] = None

        return {
            "rules": rules_res,
            **_risk,
            "price": current,
            "change_pct": (current / prev - 1) * 100,
            "ma20": ma20, "ma50": ma50, "ma200": ma200,
            "rsi14": rsi14,
            "high_52w": hi52, "low_52w": lo52,
            "close_5d_ago": close_5d_ago,
            "close_20d_ago": close_20d_ago,
            "above_ma20": current > ma20 if ma20 else None,
            "above_ma50": current > ma50 if ma50 else None,
            "above_ma200": current > ma200 if ma200 else None,
            "volume": vol_today,
            "volume_20d_avg": vol_avg20,
            "volume_ratio": round(vol_today / vol_avg20, 3) if (vol_today and vol_avg20) else None,
            # 新鲜度：last_bar 是数据本身的日期，bar_age_days 是它距今的**自然日**差
            "last_bar": last_bar,
            "bar_age_days": _aged,
        }
    except Exception as e:
        return {"error": str(e)}

# ============ 类目映射 ============
ETF_TICKERS = {"QQQ": "QQQ", "VOO": "VOO", "SCHD": "SCHD", "SMH": "SMH", "SGOV": "SGOV", "SIVR": "SIVR"}
CRYPTO_MAP = {"bitcoin": ("BTC-USD", "bitcoin"), "ethereum": ("ETH-USD", "ethereum"),
              "binancecoin": ("BNB-USD", "binancecoin")}

# 报告里显示的简称（内部键不动 —— 它们还连着 Ghostfolio / 链上数据 / 新闻源映射）
SYMBOL_DISPLAY = {"bitcoin": "BTC", "ethereum": "ETH", "binancecoin": "BNB"}


def disp(sym: str) -> str:
    return SYMBOL_DISPLAY.get(sym, sym)

def get_crypto_price(coin_id):
    """CoinGecko 拉加密现价（跨源校验用）"""
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        d = json.loads(fetch_url(url, timeout=10))
        return {"price": d[coin_id]["usd"], "change_24h_pct": d[coin_id].get("usd_24h_change")}
    except Exception as e:
        return {"error": str(e)}

def get_fear_greed():
    """市场情绪指数"""
    try:
        d = json.loads(fetch_url("https://api.alternative.me/fng/", timeout=10))
        v = d["data"][0]
        return {"value": int(v["value"]), "classification": v["value_classification"]}
    except Exception as e:
        return {"error": str(e)}

def get_put_call_ratio():
    """VIX/VIX3M 比率（波动率期限结构，yfinance 免费）作为情绪因子。
    >1.0 倒挂=恐慌，<0.9 正常溢价=平静。CBOE Put/Call 免费源已失效，用此替代。"""
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(period="5d")["Close"]
        vix3m = yf.Ticker("^VIX3M").history(period="5d")["Close"]
        if vix.empty or vix3m.empty:
            return {"error": "no data"}
        ratio = float(vix.iloc[-1]) / float(vix3m.iloc[-1])
        return {"ratio": round(ratio, 3), "vix": float(vix.iloc[-1]), "vix3m": float(vix3m.iloc[-1])}
    except Exception as e:
        return {"error": str(e)}

def get_vix():
    """恐慌指数 VIX（yfinance ^VIX）"""
    import yfinance as yf
    try:
        vix = yf.Ticker("^VIX").history(period="5d")
        if vix.empty:
            return None
        current = float(vix["Close"].iloc[-1])
        prev = float(vix["Close"].iloc[-2])
        hi5 = float(vix["High"].max())
        lo5 = float(vix["Low"].min())
        return {"price": current, "change_pct": (current / prev - 1) * 100,
                "high_5d": hi5, "low_5d": lo5}
    except Exception as e:
        return {"error": str(e)}

def get_crypto_market_data(coin_id):
    """CoinGecko 市场深度数据：市值排名、24h成交量、ATH距离（资金活跃度参考）"""
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false"
        d = json.loads(fetch_url(url, timeout=15))
        md = d.get("market_data", {})
        return {
            "market_cap_rank": d.get("market_cap_rank"),
            "total_volume_24h": md.get("total_volume", {}).get("usd"),
            "ath": md.get("ath", {}).get("usd"),
            "ath_date": md.get("ath_date", {}).get("usd", "")[:10],
            "atl": md.get("atl", {}).get("usd"),
            "price_change_24h_pct": md.get("price_change_percentage_24h"),
            "price_change_7d_pct": md.get("price_change_percentage_7d_in_currency", {}).get("usd"),
        }
    except Exception as e:
        return {"error": str(e)}

# ============ 新增：宏观数据 ============
def get_macro_data():
    """获取宏观数据：FRED + yfinance 兜底"""
    macro = {}
    
    # 1. FRED API（利率、息差、CPI、DXY）
    if FRED_API_KEY:
        fred_series = CONFIG.get("data_sources", {}).get("macro", {}).get("fred_series", {})
        for name, series_id in fred_series.items():
            try:
                url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=12"
                d = fetch_with_retry(url)
                if d and "observations" in d:
                    latest = d["observations"][0]
                    macro[name] = float(latest["value"]) if latest["value"] != "." else None
            except Exception:
                macro[name] = None
    
    # 2. yfinance 兜底/补充：10Y/3M/实时 DXY
    import yfinance as yf
    try:
        tickers = CONFIG.get("data_sources", {}).get("macro", {}).get("yfinance_tickers", ["^TNX", "^IRX", "DX-Y.NYB"])
        for t in tickers:
            hist = yf.Ticker(t).history(period=HISTORY_PERIOD)
            if not hist.empty:
                key = t.replace("^", "").replace("-", "").replace(".", "")
                macro[key] = float(hist["Close"].iloc[-1])
                # 落盘宏观历史：做因子评价时要按日期对齐，不能只有当天值
                _persist_price_history(f"macro_{key}", hist)
    except Exception as _e:
        record_failure('get_macro_data', _e)
    
    # 计算 10Y-2Y 息差
    if "DGS10" in macro and "DGS2" in macro and macro["DGS10"] and macro["DGS2"]:
        macro["yield_spread_10y2y"] = macro["DGS10"] - macro["DGS2"]
    elif "TNX" in macro and "IRX" in macro:
        macro["yield_spread_10y2y"] = macro["TNX"] - macro["IRX"]
    
    return macro

# ============ 新增：链上指标 ============
def get_coinmetrics_onchain() -> Dict:
    """CoinMetrics 社区版链上指标（**免 key、免注册**，实测可用）。

    只覆盖 BTC / ETH：免费层给 MVRV + 交易所流入/流出/余额；BNB 无 MVRV 也无流量类（返回空）。

    - MVRV：市值 / 已实现市值（原始值，非 Z-Score）
    - MVRV Z-Score：自算 (市值 − 已实现市值) / 市值标准差
      ponytail: 用可得历史的标准差近似 Glassnode 的全历史算法，绝对值会有偏差；
      阈值标定（Z>7 过热 / Z<1 过冷）留待量化讨论时校准，先按同量纲接入。
    - NUPL = 1 − 1/MVRV（与 Glassnode 定义等价：(市值−已实现市值)/市值）
    - 交易所净流量：美元净额 ÷ 当日价 → 折算成该币枚数，沿用现有 ±1000 枚阈值
    """
    out = {}
    base = (CONFIG.get("data_sources", {}).get("onchain", {})
            .get("coinmetrics_community", "https://community-api.coinmetrics.io/v4"))
    for sym, asset in (("bitcoin", "btc"), ("ethereum", "eth"), ("binancecoin", "bnb")):
        try:
            url = (f"{base}/timeseries/asset-metrics?assets={asset}"
                   "&metrics=CapMVRVCur,CapMrktCurUSD,PriceUSD,FlowInExUSD,FlowOutExUSD"
                   "&frequency=1d&page_size=10000&start_time=2015-01-01")
            rows = json.loads(fetch_url(url, timeout=60)).get("data", [])
            mcaps, last, last_time = [], None, None
            for r in rows:
                try:
                    mcap = float(r["CapMrktCurUSD"]); mvrv = float(r["CapMVRVCur"])
                except (TypeError, ValueError, KeyError):
                    continue          # 缺值的行直接跳过
                mcaps.append(mcap)
                last = (mcap, mvrv, r.get("PriceUSD"), r.get("FlowInExUSD"), r.get("FlowOutExUSD"))
                last_time = r.get("time", "")
            if not last or len(mcaps) < 200:
                out[sym] = {"available": False, "reason": "免费层不覆盖该资产的 MVRV"}
                continue
            # ⚠️ 过期守卫：BNB 的免费数据停在 2019-04（实测），
            # 不加这道拦截会把 7 年前的 MVRV 当今天的最新值喂进打分，静默污染信号。
            try:
                _d = datetime.datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                stale_days = (datetime.datetime.now(datetime.timezone.utc) - _d).days
            except Exception:
                stale_days = 999
            if stale_days > 7:
                out[sym] = {"available": False, "as_of": (last_time or "")[:10],
                            "reason": f"数据停更 {stale_days} 天（最后一笔 {(last_time or '')[:10]}）"}
                continue
            mcap, mvrv, price, fin, fout = last
            realized = mcap / mvrv
            sd = statistics.pstdev(mcaps) or 1.0
            rec = {
                "available": True,
                "mvrv": round(mvrv, 4),
                "mvrv_z_score": round((mcap - realized) / sd, 3),
                "nupl": round(1 - 1 / mvrv, 4) if mvrv else None,
                "source": "coinmetrics-community",
                "history_days": len(mcaps),
                "as_of": (last_time or "")[:10],
            }
            if price and fin is not None and fout is not None:
                try:
                    net_usd = float(fin) - float(fout)
                    rec["exchange_net_flow_native"] = round(net_usd / float(price), 1)
                    # 占市值比例：唯一能跨币种用同一阈值的量纲
                    # （±1000 枚这个阈值是按 BTC 定的；1000 ETH≈$2.6M，1000 BTC≈$78M，套到 ETH 会放大约 30 倍）
                    rec["exchange_net_flow_pct_mcap"] = round(net_usd / mcap, 8)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            out[sym] = rec
        except Exception as e:
            out[sym] = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    return out


def get_onchain_data():
    """获取链上指标：Glassnode免费端点 + CoinGecko全球 + DefiLlama稳定币 + CoinCap/CoinPaprika/CryptoCompare"""
    onchain = {}
    
    # 1. CoinGecko 全球数据（总市值、BTC占比、24h成交量）
    try:
        url = "https://api.coingecko.com/api/v3/global"
        d = json.loads(fetch_url(url, timeout=10))
        gd = d.get("data", {})
        onchain["total_market_cap_usd"] = gd.get("total_market_cap", {}).get("usd")
        onchain["btc_dominance"] = gd.get("market_cap_percentage", {}).get("btc")
        onchain["total_volume_24h_usd"] = gd.get("total_volume", {}).get("usd")
        onchain["market_cap_change_24h_pct"] = gd.get("market_cap_change_percentage_24h_usd")
    except Exception as _e:
        record_failure('get_onchain_data', _e)
    
    # 1b. 兜底：CoinGecko 免费额度常 429，用 CoinPaprika /v1/global 补 BTC占比/总市值/24h变化
    if not onchain.get("btc_dominance"):
        try:
            g = json.loads(fetch_url("https://api.coinpaprika.com/v1/global", timeout=20))
            if g.get("bitcoin_dominance_percentage") is not None:
                onchain["btc_dominance"] = float(g["bitcoin_dominance_percentage"])
                onchain["btc_dominance_source"] = "coinpaprika"
            if g.get("market_cap_usd"):
                onchain.setdefault("total_market_cap_usd", float(g["market_cap_usd"]))
            if g.get("market_cap_change_24h") is not None:
                onchain.setdefault("market_cap_change_24h_pct", float(g["market_cap_change_24h"]))
        except Exception as _e:
            record_failure('get_onchain_data', _e)

    # 2. 稳定币（DefiLlama）—— 两个端点各司其职
    #    /stablecoins            → 当前总量（返回 {"peggedAssets":[...]}）
    #    /stablecoincharts/all   → 历史序列（返回列表），用来算 YoY 增长
    #    旧代码把 charts 当总量接口用，"peggedAssets" in d 永远为假 → 这两个特征一直空着
    try:
        d = json.loads(fetch_url("https://stablecoins.llama.fi/stablecoins?includePrices=true", timeout=25))
        total = sum(((a.get("circulating") or {}).get("peggedUSD") or 0)
                    for a in d.get("peggedAssets", []))
        if total > 0:
            onchain["stablecoin_total_circulating_usd"] = total
    except Exception as _e:
        record_failure('get_onchain_data', _e)
    try:
        hist = json.loads(fetch_url("https://stablecoins.llama.fi/stablecoincharts/all", timeout=25))

        def _sc_usd(r):
            return (((r.get("totalCirculating") or {}).get("peggedUSD"))
                    or ((r.get("totalCirculatingUSD") or {}).get("peggedUSD")))

        if isinstance(hist, list) and hist:
            # ponytail: date 字段是字符串（实测 "1789084800"），必须转 int，否则 str-int 抛错被吞
            def _ts(r):
                try:
                    return int(r.get("date") or 0)
                except (TypeError, ValueError):
                    return 0

            latest = _sc_usd(hist[-1])
            target = _ts(hist[-1]) - 365 * 86400
            past = min(hist, key=lambda r: abs(_ts(r) - target))
            prev = _sc_usd(past)
            if latest and prev:
                onchain["stablecoin_yoy_growth"] = round(latest / prev - 1, 4)
                onchain["stablecoin_yoy_ref"] = "1y"
    except Exception as _e:
        record_failure('get_onchain_data', _e)
    
    # 3. CoinCap - 免费加密资产数据
    try:
        url = CONFIG.get("data_sources", {}).get("onchain", {}).get("coincap_assets", "https://api.coincap.io/v2/assets")
        d = json.loads(fetch_url(url, timeout=10))
        # 提取 BTC/ETH 主要指标
        for asset in d.get("data", [])[:10]:
            sym = asset.get("symbol", "").upper()
            if sym in ["BTC", "ETH", "BNB"]:
                onchain[f"coincap_{sym.lower()}_price"] = float(asset.get("priceUsd", 0))
                onchain[f"coincap_{sym.lower()}_change24h"] = float(asset.get("changePercent24Hr", 0))
                onchain[f"coincap_{sym.lower()}_volume24h"] = float(asset.get("volumeUsd24Hr", 0))
    except Exception as _e:
        record_failure('get_onchain_data', _e)
    
    # 4. CoinPaprika - 免费ticker数据
    try:
        url = CONFIG.get("data_sources", {}).get("onchain", {}).get("coinpaprika_tickers", "https://api.coinpaprika.com/v1/tickers")
        d = json.loads(fetch_url(url, timeout=10))
        for ticker in d[:10]:
            sym = ticker.get("symbol", "").upper()
            if sym in ["BTC", "ETH", "BNB"]:
                quotes = ticker.get("quotes", {}).get("USD", {})
                onchain[f"paprika_{sym.lower()}_price"] = float(quotes.get("price", 0))
                onchain[f"paprika_{sym.lower()}_change24h"] = float(quotes.get("percent_change_24h", 0))
                onchain[f"paprika_{sym.lower()}_volume24h"] = float(quotes.get("volume_24h", 0))
    except Exception as _e:
        record_failure('get_onchain_data', _e)
    
    # 5. CryptoCompare 多交易所聚合价格
    try:
        url = CONFIG.get("data_sources", {}).get("onchain", {}).get("cryptocompare_price", "https://min-api.cryptocompare.com/data/pricemultifull")
        params = "?fsyms=BTC,ETH,BNB&tsyms=USD"
        d = json.loads(fetch_url(url + params, timeout=10))
        for sym in ["BTC", "ETH", "BNB"]:
            if sym in d.get("RAW", {}):
                raw = d["RAW"][sym]["USD"]
                onchain[f"cc_{sym.lower()}_price"] = float(raw.get("PRICE", 0))
                onchain[f"cc_{sym.lower()}_change24h"] = float(raw.get("CHANGEPCT24HOUR", 0))
                onchain[f"cc_{sym.lower()}_volume24h"] = float(raw.get("VOLUME24HOURTO", 0))
    except Exception as _e:
        record_failure('get_onchain_data', _e)
    
    return onchain


# ============ 新增：免费宏观数据源 ============
def get_econdb_macro():
    """Econdb 免费宏观数据（无需 Key）"""
    macro = {}
    base = CONFIG.get("data_sources", {}).get("macro", {}).get("econdb_base", "https://www.econdb.com/api")
    try:
        indicators = {
            "gdp_us": "series/US.GDP.A",
            "cpi_us": "series/US.CPI.M",
            "unemployment_us": "series/US.UNR.M",
            "fed_funds": "series/US.FEDFUNDS.M",
            "10y_yield": "series/US.G10Y.M",
            "2y_yield": "series/US.G2Y.M"
        }
        for name, series in indicators.items():
            try:
                url = f"{base}/{series}?format=json"
                d = json.loads(fetch_url(url, timeout=10))
                if d and "data" in d and d["data"]:
                    macro[name] = float(d["data"][-1][1]) if d["data"][-1][1] is not None else None
            except Exception:
                macro[name] = None
    except Exception as _e:
        record_failure('get_econdb_macro', _e)
    return macro


def get_fed_treasury_data():
    """Fed Treasury 免费美债/财政数据（无需 Key）"""
    macro = {}
    base = CONFIG.get("data_sources", {}).get("macro", {}).get("fed_treasury_base", "https://api.fiscaldata.treasury.gov/services/api/fiscal_service")
    try:
        # v10.4 修复：这两处让整个数据源**从未成功过**（每次必抛 InvalidURL 被记成源异常）：
        #   · avg_interest_rates 在 Fiscal Data 上是 **v2**，v1 返回 404
        #   · 查询串里 "Treasury Bills" 的空格必须编码，urllib 直接拒绝含空格的 URL
        url = f"{base}/v2/accounting/od/avg_interest_rates?filter=security_desc:eq:Treasury%20Bills&sort=-record_date&page[size]=12"
        d = json.loads(fetch_url(url, timeout=15))
        if d and "data" in d and d["data"]:
            macro["avg_tbill_rate"] = float(str(d["data"][0].get("avg_interest_rate_amt", 0)).replace(",", ""))
    except Exception as _e:
        record_failure('get_fed_treasury_data', _e)
    # v10.4 删除：原本还有一个 mspd_table_1 取 federal_debt 的分支，两个问题叠在一起 ——
    #   ① 该表根本没有 tot_pub_debt_out_amt 字段（实际叫 total_mil_amt / debt_held_public_mil_amt），
    #      .get(..., 0) 于是**静默返回 0.0** —— 「美国国债 = 0」这种假值正是本项目一直在抓的一类；
    #   ② 这个值全流水线**没有任何消费方**（判定层、特征层、报告层都不读）。
    # 没用且写错 → 删掉，而不是修好一个没人要的字段。
    return macro


# ============ 新增：基本面数据 ============
def get_fundamental_data(sym: str):
    """获取基本面数据：yfinance info + Financial Modeling Prep (需 Key)"""
    fund = {}
    try:
        import yfinance as yf
        ticker = yf.Ticker(sym)
        info = ticker.info
        if info:
            # 关键基本面指标
            fund["roe"] = info.get("returnOnEquity")  # ROE
            # ⚠️ freeCashflow 是「金额（美元）」，不是收益率 —— 曾被误命名为 fcf_yield，
            # 会让「fcf_yield > 3」这类百分数阈值比较完全失真。现拆成两个字段。
            fund["free_cash_flow"] = info.get("freeCashflow")
            fund["pe_ratio"] = info.get("trailingPE")  # P/E
            fund["pb_ratio"] = info.get("priceToBook")  # P/B
            fund["profit_margin"] = info.get("profitMargins")  # 净利率
            fund["debt_equity"] = info.get("debtToEquity")  # D/E
            fund["revenue_growth"] = info.get("revenueGrowth")  # 营收增速
            fund["earnings_growth"] = info.get("earningsGrowth")  # 盈利增速
            fund["market_cap"] = info.get("marketCap")  # 市值
            # FCF 收益率 = 自由现金流 / 市值 × 100（算不出来就留 None，不硬凑）
            _fcf, _mc = fund.get("free_cash_flow"), fund.get("market_cap")
            if _fcf is not None and _mc:
                fund["fcf_yield_pct"] = round(_fcf / _mc * 100, 2)
            else:
                fund["fcf_yield_pct"] = None
            fund["dividend_yield"] = info.get("dividendYield")  # 股息率
            fund["beta"] = info.get("beta")  # Beta
            # 规范化
            if fund["roe"] is not None:
                fund["roe"] = fund["roe"] * 100  # 转百分比
            if fund["profit_margin"] is not None:
                fund["profit_margin"] = fund["profit_margin"] * 100
            if fund["revenue_growth"] is not None:
                fund["revenue_growth"] = fund["revenue_growth"] * 100
            if fund["earnings_growth"] is not None:
                fund["earnings_growth"] = fund["earnings_growth"] * 100
            if fund["dividend_yield"] is not None:
                fund["dividend_yield"] = fund["dividend_yield"] * 100
    except Exception as _e:
        record_failure('get_fundamental_data', _e)
    
    return fund

_SEC_TICKER_MAP = None  # 缓存 ticker -> CIK

def get_sec_edgar_fundamentals(sym: str):
    """SEC EDGAR 官方财报数据（免费，无需 Key）。仅对个股有效，ETF 自动跳过。"""
    global _SEC_TICKER_MAP
    try:
        if _SEC_TICKER_MAP is None:
            data = json.loads(fetch_url("https://www.sec.gov/files/company_tickers.json",
                                        headers={"User-Agent": SEC_EDGAR_UA}, timeout=20))
            _SEC_TICKER_MAP = {v["ticker"].upper(): v["cik_str"] for v in data.values()}
        cik = _SEC_TICKER_MAP.get(sym.upper())
        if not cik:
            return {}
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/Revenues.json"
        d = json.loads(fetch_url(url, headers={"User-Agent": SEC_EDGAR_UA}, timeout=20))
        # 年度营收（10-K, fy 标记）
        annual = [u for u in d["units"]["USD"] if u.get("fp") == "FY" and u.get("form") == "10-K"]
        annual.sort(key=lambda x: x["end"])
        if len(annual) >= 2:
            prev, cur = annual[-2]["val"], annual[-1]["val"]
            if prev:
                return {"sec_revenue_latest": cur, "sec_revenue_growth_pct": (cur / prev - 1) * 100,
                        "sec_fy_end": annual[-1]["end"]}
        if annual:
            return {"sec_revenue_latest": annual[-1]["val"], "sec_fy_end": annual[-1]["end"]}
    except Exception as _e:
        # v10.4：404 = 该标的本就没有这个 us-gaap 概念（ETF/信托没有 Revenues），
        # 属「查得到、但没这个字段」，不是源故障。记成源异常会白拉低 severity。
        if "404" not in str(_e):
            record_failure('get_sec_edgar_fundamentals', _e)
    return {}

# ============ 主流程 ============
def main():
    # 配置在 main 顶部取一次：既供输出元数据用，也避免放进 try 里被吞
    # （曾经把 config_version 写在持仓 try 块内，cfg 未定义 → NameError 被 except 吞掉
    #  → 整个持仓块中断 → 加密标的全部消失，而 collect 仍然 exit 0）
    cfg = load_config()
    output = {"symbol_display": SYMBOL_DISPLAY,
              "config_version": str(cfg.get("version") or ""),
              "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()),
              "positions": {}, "tech": {}, "sentiment": {}, "vix": {}, 
              "crypto_market": {}, "macro": {}, "onchain": {}, "errors": []}
    
    # 1. Ghostfolio 持仓
    try:
        positions, meta = get_positions()
        output["positions"] = positions
        output["base_currency"] = meta.get("baseCurrency", "USD")
        if meta.get("unsupported_activities"):
            output["errors"].append(
                "未处理的 activity 类型 "
                + ", ".join(f"{k}×{v}" for k, v in meta["unsupported_activities"].items())
                + " —— 持仓数量/成本可能失真，需人工核对")
    except Exception as e:
        output["errors"].append(f"Ghostfolio: {e}")
        positions = {}
    
    # 2. 美股 ETF 技术指标
    for sym in ETF_TICKERS:
        try:
            output["tech"][sym] = get_tech_indicators(sym, sym)
        except Exception as e:
            output["tech"][sym] = {"error": str(e)}
    
    # 3. 加密货币价格（yfinance + CoinGecko 双源）
    for sym in positions:
        if sym in CRYPTO_MAP:
            ysym, coin_id = CRYPTO_MAP[sym]
            try:
                # 必须传 sym：否则价格历史不落盘
                mkt = get_tech_indicators(ysym, sym) or {}
                cg = get_crypto_price(coin_id)
                # ⚠️ 旧写法只挑 7 个字段塞进 combined，把 52周高低 / ma200 /
                # 5日·20日前收盘 / 成交量全丢了 —— 而训练侧这些是真实值，
                # 导致加密的推理特征与训练严重错位（动量走"当日涨跌"兜底、
                # pos_52w/vol_proxy/ma200_dist 直接缺失）。这里改为整体继承再覆盖。
                combined = dict(mkt)
                combined.update({
                    "price": mkt.get("price") or cg.get("price"),
                    "coingecko": cg.get("price"),
                    "change_24h_pct": cg.get("change_24h_pct"),
                })
                output["tech"][sym] = combined
            except Exception as e:
                output["tech"][sym] = {"error": str(e)}
    
    # 4. 市场情绪
    output["sentiment"] = get_fear_greed()
    output["sentiment"]["put_call"] = get_put_call_ratio()
    
    # 5. VIX 恐慌指数
    output["vix"] = get_vix()
    
    # 6. 加密市场深度数据（资金活跃度参考）—— 免费 API 可能限流，失败不阻塞
    for sym in positions:
        if sym in CRYPTO_MAP:
            _, coin_id = CRYPTO_MAP[sym]
            try:
                output["crypto_market"][sym] = get_crypto_market_data(coin_id)
            except Exception as e:
                output["crypto_market"][sym] = {"error": str(e)}
    
    # 7. 新增：宏观数据
    try:
        output["macro"] = get_macro_data()
        # 额外免费宏观源
        econdb = get_econdb_macro()
        fed_treasury = get_fed_treasury_data()
        output["macro"].update(econdb)
        output["macro"].update(fed_treasury)
    except Exception as e:
        output["errors"].append(f"Macro: {e}")
        output["macro"] = {}
    
    # 8. 新增：链上指标
    try:
        output["onchain"] = get_onchain_data()
        try:
            output["onchain"]["per_symbol"] = get_coinmetrics_onchain()
        except Exception as e:
            output["errors"].append(f"CoinMetrics: {e}")
    except Exception as e:
        output["errors"].append(f"Onchain: {e}")
        output["onchain"] = {}

    # 9. 新增：基本面数据（ETF 用 yfinance info）
    output["fundamentals"] = {}
    for sym in positions:
        try:
            # ETF 用 yfinance，加密跳过（或后续接入链上基本面）
            if sym in ETF_TICKERS:
                fund = get_fundamental_data(sym)
                fund.update(get_sec_edgar_fundamentals(sym))  # 个股才有效，ETF 自动空
                output["fundamentals"][sym] = fund
        except Exception as e:
            output["fundamentals"][sym] = {"error": str(e)}
    
    # 9. 判定（买入/卖出/持有）—— **完全来自公开量化项目的规则**，见 ghostfolio_rules.py
    #    美股走 QuantConnect/Lean 三个 Alpha 模型；加密走 freqtrade 三个策略
    #    （AdxSmas + BbandRsi + AwesomeMacd）多数表决。
    #    不用自研打分：那套「自身历史分」已废弃（自己都验证不过）。
    output["signals"] = {}
    _special = CONFIG.get("special_assets", {}) or {}
    for sym, tech in output["tech"].items():
        if "error" in tech or tech is None:
            continue
        if sym in _special:                       # 货币基金等非技术资产走固定档
            # 只保留语义字段。score/conviction/confidence 是已废弃的自研评分体系残留
            # （scoring/alpha_models/thresholds 在 v9.7 已删，这三个漏在了特殊资产上）。
            _sp = dict(_special[sym])
            # 配置只提供**解释性**字段（thesis/reasons/risks）。状态本身是事实层，
            # 必须由代码给出 —— v10.2 踩过：原先 `signal: "持有"` 写在配置里，
            # 把它删掉（以为与 applicable 语义重复）状态就变成 None，还触发了一次
            # 假的「状态变化」告警（持有→None）。配置能改空的东西，就不是事实。
            for _stale in ("score", "conviction", "confidence", "signal"):
                _sp.pop(_stale, None)
            _sp["applicable"] = False
            _sp["reason"] = "cash_equivalent"
            _sp["signal"] = "持有"          # 现金等价物的判定层原生值：不触发买卖
            _sp["signal_source"] = "code:cash_equivalent"
            output["signals"][sym] = _sp
            continue
        rr = (tech.get("rules") or {})
        maj = rr.get("majority")
        if not maj:
            continue
        def _short(src):
            """来源名压缩成报告里能读的短名"""
            for k, v in (("RSI", "RSI"), ("EMA", "EMA"), ("MACD", "MACD"),
                         ("AdxSmas", "AdxSmas"), ("BbandRsi", "BbandRsi"),
                         ("AwesomeMacd", "AwesomeMacd")):
                if k in src:
                    return v
            return src

        _buys = [_short(v["source"]) for v in rr["verdicts"] if v["vote"] == "买入"]
        _sells = [_short(v["source"]) for v in rr["verdicts"] if v["vote"] == "卖出"]
        _holds = [_short(v["source"]) for v in rr["verdicts"] if v["vote"] == "持有"]
        if maj == "买入":
            _why = "看多 " + "、".join(_buys)
            if _holds:
                _why += "（" + "、".join(_holds) + " 中性）"
        elif maj == "卖出":
            _why = "看空 " + "、".join(_sells)
            if _holds:
                _why += "（" + "、".join(_holds) + " 中性）"
        else:
            _bits = []
            if _buys:
                _bits.append("看多 " + "、".join(_buys))
            if _sells:
                _bits.append("看空 " + "、".join(_sells))
            if _holds:
                _bits.append("中性 " + "、".join(_holds))
            _why = "；".join(_bits)
        output["signals"][sym] = {
            "signal": maj,
            # conviction 由票数导出（供组合优化加权用），不再是自研打分的产物
            "conviction": round((rr["buy"] - rr["sell"]) / max(1, rr["total"]), 2),
            "thesis": _why or "无",
            "reasons": [_why] if _why else [],
            # 反向意见只列**成功投票且与多数不同向**的 —— 失败票是数据异常，不是分歧
            "risks": [_short(v["source"]) for v in rr["verdicts"]
                      if v.get("vote") is not None and v["vote"] != maj and v["vote"] != "持有"],
            "buy": rr["buy"], "sell": rr["sell"], "hold": rr["hold"],
            "total": rr["total"], "tie": rr.get("tie", False),
            "failed": rr.get("failed", 0),
            "strategy_errors": [f"{_short(v['source'])}: {v['error']}"
                                for v in rr["verdicts"] if v.get("error")],
            "source_project": rr.get("source_project"),
            # 加密侧是穿越型状态机 → 必须给出信号年龄，否则「买入」会被误读成今天刚出
            "signal_semantics": rr.get("signal_semantics"),
            "last_signal_date": rr.get("last_signal_date"),
            "signal_age_days": rr.get("signal_age_days"),
            # 三个策略各自的年龄/状态（年龄只取与多数同向者，这里保留明细供报告展示）
            "strategy_ages": rr.get("strategy_ages"),
            "age_basis": rr.get("age_basis"),
        }

    # 10. 组合层风控视图（AI-Hedge-Fund 的 fund-level risk 思路：只看权重与集中度）
    try:
        output["portfolio_risk"] = build_portfolio_view(positions, output["tech"])
    except Exception as e:
        output["errors"].append(f"PortfolioView: {e}")

    # 11. 特征工程 + ML 推理（Layer 2/3/4 集成）
    try:
        output["ml_features"] = {}
        output["ml_features_missing"] = {}
        output["ml_predictions"] = {}
        if FEATURES_AVAILABLE:
            for sym, tech in output["tech"].items():
                if "error" in tech or tech is None:
                    continue
                # v10.3：missing_out 此前是**死参数** —— build_feature_vector 支持它、
                # 却没有任何调用方传。于是「缺值填 0」和「真实值就是 0」在产物里
                # 完全无法区分，回看历史预测时没法判断当时是数据源挂了还是模型真的看到 0
                # （评审 ChatGPT P1，本项目第 6 次「同一处的另一半没接上」）。
                _miss = []
                feats = build_feature_vector(sym, tech, output["macro"], output["onchain"],
                                             output.get("vix"), output.get("sentiment"),
                                             missing_out=_miss)
                output["ml_features"][sym] = feats
                if _miss:
                    output["ml_features_missing"][sym] = sorted(_miss)
                # 保存特征到缓存（用于后续训练）
                save_features(sym, feats, datetime.datetime.now().isoformat())
        
        # ML 推理（LightGBM 基线）
        if LGBM_AVAILABLE:
            output["ml_predictions"] = run_ml_inference(
                output["ml_features"], positions, output.get("ml_features_missing") or {})


    except Exception as e:
        output["errors"].append(f"ML: {e}")

    # 8.5 数据/模型质量标记 —— 组合优化若掺了启发式，必须在报告里降级说明
    try:
        _preds = output.get("ml_predictions") or {}
        _heu = [k for k, v in _preds.items() if v.get("is_heuristic")]
        _ml = [k for k, v in _preds.items() if v.get("model") == "lightgbm"]
        output["optimization_quality"] = {
            "ml_model_coverage": len(_ml),
            "heuristic_fallback_count": len(_heu),
            "heuristic_symbols": _heu,
            "trustworthy": len(_heu) == 0,
        }
    except Exception as e:
        output["errors"].append(f"OptimizationQuality: {e}")

    # 11.5 新闻层（Layer 6）—— 只采持仓相关消息；失败只记录，绝不阻塞主报告
    try:
        import ghostfolio_news as gfnews
        output["news"] = gfnews.collect(list(positions.keys()), gfnews.load_config())
        output["news"]["cache_path"] = gfnews.save(output["news"])
        _nn = sum(v["kept"] for v in output["news"]["per_symbol"].values())
        print(f"[news] 采集完成: {_nn} 条 / 宏观 {len(output['news']['macro_events'])} 条 "
              f"/ 耗时 {output['news']['elapsed_s']}s", file=sys.stderr, flush=True)
    except Exception as e:
        output["news"] = {"per_symbol": {}, "macro_events": [],
                          "degraded": [f"新闻层整体失败: {type(e).__name__}: {e}"]}
        output["errors"].append(f"News: {e}")

    # 12. LLM 评论家团队 Prompt（Layer 4，供 Hermes 主模型直接处理）
    try:
        output["llm_analyst_prompts"] = build_llm_analyst_prompts(output["signals"], output["tech"], output["macro"], 
                                                                 output["onchain"], output["fundamentals"],
                                                                 output["ml_predictions"], positions,
                                                                 output.get("vix"), output.get("sentiment"),
                                                                 output.get("news"))
    except Exception as e:
        output["errors"].append(f"LLM Prompts: {e}")

    # 14. 组合优化建议（Portfolio Optimizer - Layer 5 输出增强）
    try:
        output["portfolio_optimization"] = run_portfolio_optimizer(output["signals"], output["ml_predictions"], 
                                                                   output["tech"], positions)
    except Exception as e:
        output["errors"].append(f"PortfolioOptimizer: {e}")

    # 15. 数据质量 / 昨日对比 / 今日预警
    try:
        prev = _load_prev_snapshot()
        # 分母必须是「**应该**有多少持仓」，不能用 len(tech) —— 那是自指的：
        # 标的整个采失败时 tech 变短，分母跟着变小，9 个持仓缺 3 个仍报「6/6 ok」。
        # （曾实际发生：config_version 写错位置 → NameError 被吞 → 加密三标的全丢，
        #  而 data_quality 显示行情 6/6 正常。）
        # 缺了哪些标 —— 必须显式写出来（评审 ChatGPT #6）：
        # 过去三次静默失效的共同形态是「产出看起来正常」，而分母自指那次的根因就是
        # 「丢了 3 个持仓」这件事没有任何字段记录。契约测试会断言：
        # 持仓数 > 有行情的标的数 时，missing_assets 必须存在且非空。
        _miss = sorted(set(output.get("positions") or {}) - set(output.get("tech") or {}))
        output["missing_assets"] = _miss
        output["data_quality"] = build_data_quality(output, len(output.get("positions") or {}))
        # 顶层镜像一份（评审 Grok：上层调度要能一眼读到，不必知道它装在 data_quality 里）。
        # 只在这里镜像一次 —— 值仍由 build_data_quality 单点计算，不会漂移。
        output["severity"] = output["data_quality"].get("severity")
        output["exit_hint"] = output["data_quality"].get("exit_hint")
        output["failure_codes"] = list(output["data_quality"].get("failure_codes") or [])
        # 产源快照（v10.2，评审 ChatGPT #11）：这份 JSON 是哪个 schema / 配置 /
        # 代码产出的，必须能自答。项目已发生过「配置版本、脚本版本、字段版本」不同步，
        # 而旧报告一旦离开现场就无法追溯。
        output["schema_version"] = SCHEMA_VERSION
        output["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds")
        output["provenance"] = {
            "schema_version": SCHEMA_VERSION,
            "config_version": str(cfg.get("version") or ""),
            "code_version": SCHEMA_VERSION,
            "code_fingerprint": _code_fingerprint(),
            "generated_at": output["generated_at"],
        }
        output["deltas"] = build_deltas(output, prev)
        output["alerts"] = build_alerts(output, prev)
        # 触发式 LLM：只在"确有事"时才需要上游主模型写点评，平时省额度
        # pri<=4 是真实信号事件；pri=5 只是数据源异常，不值得花 token
        output["llm_needed"] = any(a.get("pri", 9) <= 4 for a in output["alerts"])
        output["llm_skip_reason"] = ("" if output["llm_needed"]
                                     else "无信号事件（无越线/无变档/无极端/无冲突）")
        save_snapshot(output, prev)
    except Exception as e:
        output["errors"].append(f"Alerts: {e}")

    # 清理：只保留判定所需窗口，防 Skill 臃肿（失败不影响报告）
    try:
        _cl = cleanup_cache()
        output["cache_cleanup"] = _cl
        if _cl.get("notes"):
            print(f"[cleanup] {'; '.join(_cl['notes'][:5])}", file=sys.stderr, flush=True)
    except Exception as e:
        output["cache_cleanup"] = {"error": str(e)}

    # 输出 JSON（供 cron 读取）
    print(json.dumps(output, ensure_ascii=False))

# ============ 数据质量 / 昨日对比 / 今日预警（Layer 5 增强） ============
STATE_DIR = GF_HOME / "state"
SNAPSHOT_PATH = STATE_DIR / "gf_snapshot.json"


def build_data_quality(output: Dict, expected: int) -> Dict:
    """从产出反推各数据源状态（不改各源的 try/except，避免漏记）"""
    macro = output.get("macro") or {}
    onchain = output.get("onchain") or {}
    tech = output.get("tech") or {}
    fund = output.get("fundamentals") or {}
    mlp = output.get("ml_predictions") or {}
    priced = len([v for v in tech.values() if (v or {}).get("price")])
    # 分母不能用持仓总数：采集侧只对 ETF_TICKERS 取基本面（加密结构上就没有），
    # 用 9 做分母会让「6 只 ETF 全成功」也判成 FUNDAMENTALS_DEGRADED ——
    # 正常日报每天天然 degraded，这个信号等于作废（两家评审同时指出）。
    fund_expected = len([s for s in (output.get("positions") or {}) if s in ETF_TICKERS])
    fund_ok = len([v for v in fund.values() if v and not v.get("error")])
    ml_ok = len([v for v in mlp.values() if (v or {}).get("model") == "lightgbm"])
    # ── 数据新鲜度 ──
    # ChatGPT/Perplexity 都指出：只看「有没有值」会把「API 挂了、回退到 3 天前的缓存」
    # 判成 ok。这里改成看**数据本身的最后一根 K 线日期**，分 fresh/stale 两级。
    # 阈值：加密日日有成交 → >2 自然日即 stale；美股遇周末+假日会自然停 3~4 天，
    # 故放宽到 >5 自然日才判 stale（避免每周一误报）。
    fresh_stale, fresh_unknown = [], []
    for sym, v in tech.items():
        age = (v or {}).get("bar_age_days")
        if age is None:
            fresh_unknown.append(sym); continue
        _lim = 2 if sym in CRYPTO_MAP else 5
        if age > _lim:
            fresh_stale.append(f"{disp(sym)} 数据已 {age} 天未更新"
                               f"（最后一根 {(v or {}).get('last_bar')}）")
    if fresh_stale:
        _fresh = "stale：" + "；".join(fresh_stale[:3])
    elif fresh_unknown and len(fresh_unknown) == len(tech):
        _fresh = "unknown（取不到K线日期）"
    elif fresh_unknown:
        _fresh = f"fresh（{len(tech) - len(fresh_unknown)}/{len(tech)} 已核对，"
        _fresh += f"其余 {len(fresh_unknown)} 个无日期）"
    else:
        _fresh = f"fresh（{len(tech)}/{len(tech)} 最后一根 K 线均在阈值内）"

    # ── 失败等级（v10.1）──
    # ok      : 全部就绪，报告可信
    # degraded: 有洞但报告仍可读 —— 仍应推送（Bob 选择「收到残缺报告」）
    # critical: 持仓或行情整体不可用 —— 报告不可信，需显式告警
    _pos_fail = not output.get("positions")
    _price_fail = priced == 0

    # ── 结构化失败码（v10.2，评审 ChatGPT）──
    # severity 只说「多严重」，failure_codes 说「哪里出问题」。调度侧才能区分
    # 「SOURCE_FAILED 可以继续」和「PORTFOLIO_INCOMPLETE 必须告警」，
    # 而不是所有 degraded 一视同仁。
    # 只登记**已进入 severity 判定**的那几类。VIX/情绪/链上深度不单独升级为
    # degraded —— 它们本来就通过 _SOURCE_FAILURES 进来，再单列会让周一的
    # 一次 429 变成日常噪声（告警疲劳 = 新的静默失效）。
    _codes = []
    if _pos_fail:
        _codes.append("PORTFOLIO_INCOMPLETE")
    if _price_fail:
        _codes.append("MARKET_DATA_MISSING")
    elif expected and priced < expected:
        _codes.append("MARKET_DATA_PARTIAL")
    if fund_expected and fund_ok < fund_expected:
        _codes.append("FUNDAMENTALS_DEGRADED")
    if expected and ml_ok < expected:
        _codes.append("ML_UNAVAILABLE")
    if _SOURCE_FAILURES:
        _codes.append("SOURCE_FAILED")

    # exit_hint 必须与 severity 同档（v10.1 里 critical 配的是 degraded ——
    # 机器可读信号被静默降级了一档，正是本项目一直在抓的那类问题）。
    # 「critical 也要推送」是**执行步骤**的决定，不是把 exit_hint 改低。
    if _pos_fail or _price_fail:
        _severity, _exit_hint = "critical", "critical"
    elif _codes:
        _severity, _exit_hint = "degraded", "degraded"
    else:
        _severity, _exit_hint = "ok", "ok"

    return {
        "severity": _severity,
        "exit_hint": _exit_hint,
        "持仓": "fail" if not output.get("positions") else "ok",
        # 持仓报告里少一只 = 有洞，不是部分成功 —— 必须显式 fail，否则读者以为数据是全的
        "行情": (f"{priced}/{expected}" if priced == expected
                 else f"fail({priced}/{expected})"),
        "新鲜度": _fresh,
        "VIX": "ok" if (output.get("vix") or {}).get("price") else "fail",
        "情绪": "ok" if (output.get("sentiment") or {}).get("value") else "fail",
        "宏观息差": "ok" if macro.get("yield_spread_10y2y") is not None else "fail",
        "美元": "ok" if (macro.get("DXYNYB") or macro.get("dxy")) else "fail",
        "加密深度": "ok" if onchain.get("btc_dominance") else "fail",
        "基本面": f"{fund_ok}/{fund_expected}",
        "ML": "fail" if ml_ok == 0 else f"{ml_ok}/{expected}",
        "链上指标": _onchain_quality(onchain),
        # 登记到的数据源失效（比「从产出反推」更准 —— 能拿到具体错误）
        "源异常": ("；".join(f"{f['source']}({f['error'][:80]})" for f in _SOURCE_FAILURES[:4])
                   if _SOURCE_FAILURES else "ok"),
        # 失败码：上层按码分流，不必解析中文类别名。
        "failure_codes": _codes,
    }


def _onchain_quality(onchain: Dict) -> str:
    """链上指标质量：BTC/ETH 有数据即算正常；BNB 停更单独标注"""
    ps = (onchain or {}).get("per_symbol") or {}
    if not ps:
        return "fail"
    ok = [s for s, v in ps.items() if (v or {}).get("available")]
    bad = [s for s, v in ps.items() if not (v or {}).get("available")]
    if not ok:
        return "fail"
    return f"{len(ok)}/{len(ps)}" + (f"（缺 {','.join(disp(b) for b in bad)}）" if bad else "")


def _load_prev_snapshot() -> Dict:
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return {}


def save_snapshot(output: Dict, prev: Dict) -> None:
    """保存今日快照（供明天做对比）；把今天的 alerts 也留下，方便判断"首次" """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps({
            "date": (output.get("timestamp") or "")[:10],
            # 只留 signal。score / conviction 是已废弃的自研评分子段，
            # 继续写进快照会让人以为它们还在被消费（评审 Grok）。
            "signals": {s: {"signal": v.get("signal")}
                        for s, v in (output.get("signals") or {}).items()},
            "weights": (output.get("portfolio_risk") or {}).get("weights", {}),
            "flags": (output.get("portfolio_risk") or {}).get("flags", []),
            "vix": (output.get("vix") or {}).get("price"),
            "spread": (output.get("macro") or {}).get("yield_spread_10y2y"),
        }, ensure_ascii=False, indent=2))
    except Exception as e:
        output.setdefault("errors", []).append(f"Snapshot: {e}")


def build_deltas(output: Dict, prev: Dict) -> Dict:
    """与昨日对比：状态变化、权重变化。

    【v10.0】状态变化按 Change Detection 语义输出 —— 只陈述「从 A 变成 B」这个事实，
    绝不附带「所以该买入/卖出」。评审三方一致强调：系统负责 Detect，用户负责 Decide。
    报告层只允许原样播报这些字段，不得据此生成操作建议。
    """
    cur_sig = {s: (v or {}).get("signal") for s, v in (output.get("signals") or {}).items()}
    prev_sig = {s: (v or {}).get("signal") for s, v in (prev.get("signals") or {}).items()}
    cur_w = (output.get("portfolio_risk") or {}).get("weights", {}) or {}
    prev_w = prev.get("weights") or {}
    tiers, wdelta, changes = {}, {}, {}

    def _age(sym):
        """当前状态已持续几个交易日（来自判定层的 signal_age_days）"""
        return ((output.get("signals") or {}).get(sym) or {}).get("signal_age_days")

    for s, sig in cur_sig.items():
        # 固定档资产（applicable=False，如 SGOV 货币基金）**不参与状态迁移**（v10.3，
        # 评审 ChatGPT P0）。它的状态是事实层常量、不是规则投票结果；让它进来只可能
        # 产生假告警 —— v10.2 已经真发生过一次（配置键被删 → 状态 None → 假的「持有→None」）。
        # 只断「有 signal」不够，还必须断「不产生状态变化」，否则以后改状态映射还会复发。
        if ((output.get("signals") or {}).get(s) or {}).get("applicable") is False:
            continue
        p = prev_sig.get(s)
        changed = bool(p) and p != sig
        if changed:
            tiers[s] = f"{p}→{sig}"
        # 结构化字段（供报告做「状态变化」播报；无昨日数据时 changed=False）
        if p:
            changes[s] = {"previous_state": p, "current_state": sig,
                          "changed": changed,
                          "state_age_days": (0 if changed else _age(s))}
    for s, w in cur_w.items():
        if s in prev_w:
            wdelta[s] = round((w - prev_w[s]) * 100, 2)
    return {"has_prev": bool(prev_sig), "signal_changed": tiers,
            "state_changes": changes, "weight_delta_pct": wdelta}


def build_alerts(output: Dict, prev: Dict) -> list:
    """「今天只需看一件事」候选，按重要性排序（pri 越小越重要）"""
    alerts = []
    flags = (output.get("portfolio_risk") or {}).get("flags", []) or []
    prev_flags = prev.get("flags") or []
    d = build_deltas(output, prev)

    for f in flags:
        sym = f.split()[0] if f else ""
        first = not any(sym and sym in x for x in prev_flags)
        alerts.append({"pri": 1, "kind": "weight_breach",
                       "text": f"{f}" + ("（首次越线）" if first else "")})

    for s, chg in d["signal_changed"].items():
        # pri=5（信息级），不是 2（高优先级）。规则状态变档是**播报**，不是风控越线：
        # 此前 pri=2 让它和「集中度超限」同档进 🔴，既稀释了真正的风控信号，也和
        # gen_report 里「带操作语气的句子只留给风控越线（pri<=2）」的注释自相矛盾（评审 Grok）。
        # 文案同步去交易语：chg 是 "买入→卖出" 这类内部枚举，展示前必须翻译。
        alerts.append({"pri": 5, "kind": "tier_change",
                       "text": f"{disp(s)} 规则状态 {state_words(chg)}"})

    vix = output.get("vix") or {}
    vc = vix.get("change_pct")
    if vc is not None and abs(vc) > 15:
        alerts.append({"pri": 3, "kind": "vix_move",
                       "text": f"恐慌指数 VIX 单日 {vc:+.0f}%（现 {vix.get('price'):.1f}）"})

    spread = (output.get("macro") or {}).get("yield_spread_10y2y")
    p_spread = prev.get("spread")
    if spread is not None and p_spread is not None and (spread < 0) != (p_spread < 0):
        alerts.append({"pri": 3, "kind": "curve_flip",
                       "text": f"长短期利率{'转为倒挂' if spread < 0 else '倒挂解除'}（{spread:.2f}%）"})

    for s, sig in (output.get("signals") or {}).items():
        rsi = ((output.get("tech") or {}).get(s) or {}).get("rsi14")
        if rsi is not None and (rsi > 80 or rsi < 20):
            alerts.append({"pri": 4, "kind": "rsi_extreme", "text": f"{disp(s)} RSI {rsi:.0f}（极端）"})
        m = (output.get("ml_predictions") or {}).get(s) or {}
        mc, sc = m.get("conviction"), (sig or {}).get("conviction")
        if mc is not None and sc is not None and mc * sc < 0 and abs(mc) > 0.3:
            # 措辞：不写「与信号相反」—— 那暗示存在一个「正确方向」。
            # ML 无决策权（三方评审一致：系统 Detect，用户 Decide）。
            # 注意：映射必须在 f-string **外面**算好 —— 写进 {{...}} 会被转义成字面量。
            _maj_txt = {"买入": "多数偏多", "卖出": "多数偏空"}.get(sig["signal"], "多数中性")
            alerts.append({"pri": 4, "kind": "ml_conflict",
                           "text": f"{disp(s)} 规则层{_maj_txt}；"
                                   f"模型 5 日方向偏{'空' if mc < 0 else '多'}（{mc:+.2f}）"
                                   f"（可靠度{m.get('reliability') or '低'}）"
                                   f"—— 仅供对照，不参与状态判定"})

    dq = output.get("data_quality") or {}
    bad = [k for k, v in dq.items() if v == "fail"]
    if bad:
        alerts.append({"pri": 5, "kind": "data_quality", "text": f"数据源异常：{', '.join(bad)}"})

    alerts.sort(key=lambda a: a["pri"])
    return alerts


def _risk_universe(weights: Dict, syms, observations: int) -> Dict:
    """记录「算风险贡献时到底用了几个标的」——expected 必须来自权重表（独立期望值），
    不能来自算出来的 syms，否则又是分母自指。"""
    return {
        "expected_assets": len(weights),
        "used_assets": len(syms),
        "missing_assets": sorted(set(weights) - set(syms)),
        "complete": len(syms) == len(weights),
        "observations": int(observations),
    }


def _portfolio_risk_layer(weights: Dict, tech: Dict) -> Dict:
    """组合级风险度量（v10.0）。

    评审（ChatGPT）指出：**持仓数量 ≠ 独立风险源数量** ——
    QQQ / VOO / SMH 看着是三只，风险暴露可能高度同涨同跌。
    这里用真实收益矩阵把它量化出来，而不是靠持仓个数给人错觉。

    数据来源：各标的 tech 里的 returns_90d（算指标时顺手存的，无额外 IO）。
    """
    # ① 资产类别集中度（不依赖收益序列，永远能算）
    cls = {"etf": 0.0, "crypto": 0.0, "cash_equivalent": 0.0}
    for sym, w in weights.items():
        if sym in CRYPTO_MAP:
            cls["crypto"] += w
        elif sym in _SPECIAL_ASSETS:
            cls["cash_equivalent"] += w
        else:
            cls["etf"] += w
    out = {"asset_class": {k: round(v, 4) for k, v in cls.items()},
           "crypto_exposure": round(cls["crypto"], 4)}

    # ② 相关性 / 风险贡献 / 组合波动与回撤（需要对齐后的收益矩阵）
    syms = [s for s in weights
            if len(((tech.get(s) or {}).get("returns_90d") or [])) >= 30]
    if len(syms) < 2:
        out["note"] = f"可算收益序列的标的不足 2 个（{len(syms)}），跳过相关性/风险贡献"
        out["risk_universe"] = _risk_universe(weights, syms, 0)
        return out

    n = min(len(tech[s]["returns_90d"]) for s in syms)
    M = np.array([tech[s]["returns_90d"][-n:] for s in syms], dtype=float)  # syms × n
    w = np.array([weights[s] for s in syms], dtype=float)
    w = w / w.sum()                       # 在有收益序列的标的间归一（口径写进 note）
    cov = np.cov(M)
    var = float(w @ cov @ w)
    if var <= 0:
        out["note"] = "组合方差非正，跳过"
        return out

    # 风险贡献：RC_i = w_i (Σw)_i / (w'Σw)，合计为 1。
    # 这是「谁在贡献风险」，不是「谁占比大」—— 两者常常差很远。
    mrc = cov @ w
    # 字段名把窗口编进去（评审 ChatGPT 建议）：未来若加 60d/252d 不必迁移 schema。
    # 口径务必说清 —— 这是协方差口径的**波动**风险贡献，不是尾部/压力风险贡献。
    out["risk_contribution_90d"] = {syms[i]: round(float(w[i] * mrc[i] / var), 4)
                                    for i in range(len(syms))}
    # 口径参数显式入字段（评审 ChatGPT）：将来换 EWMA / shrinkage / 252d 时
    # 不会让同一个风险贡献字段的含义被悄悄改写。
    out["risk_model"] = "historical_covariance"
    out["risk_window_days"] = int(n)
    out["risk_universe"] = _risk_universe(weights, syms, n)
    out["risk_contribution_basis"] = (f"近 {n} 个交易日协方差口径的波动风险贡献，"
                                      f"共 {len(syms)} 个有收益序列的标的；"
                                      f"不含尾部/压力风险")
    # 风险宇宙完整性（v10.3，评审 ChatGPT P0）。只算「有收益序列的标的」时，
    # 报告若仍叫「组合风险贡献」，读者会默认是 9/9 全组合 —— 实际可能是 8/9。
    # 结构化记下来，渲染层据此显式说明，不靠顶部 data_quality 兜底
    # （顶部说的是一次性汇总，覆盖不到单个字段的语义）。

    # 相关性：最大两两相关 + 平均两两相关（对角元排除）
    sd = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov / np.outer(sd, sd)
    off = corr[~np.eye(len(syms), dtype=bool)]
    off = off[np.isfinite(off)]
    if off.size:
        iu = np.triu_indices(len(syms), k=1)
        vals = corr[iu]
        k = int(np.nanargmax(vals))
        out["correlation"] = {
            "max_pair": [syms[iu[0][k]], syms[iu[1][k]]],
            "max_value": round(float(vals[k]), 3),
            "mean_pairwise": round(float(np.nanmean(vals)), 3),
        }

    out["portfolio_vol_annual"] = round(float(np.sqrt(var) * np.sqrt(252)), 4)
    # 组合回撤：按当前权重把日收益加总成组合净值曲线再取回撤
    eq = np.cumprod(1.0 + M.T @ w)
    out["portfolio_max_dd_90d"] = round(float((eq / np.maximum.accumulate(eq) - 1).min()), 4)
    return out


def build_portfolio_view(positions, tech):
    """市值权重 + 风险预算校验（max_single_position / max_crypto_allocation 等，见 config）"""
    rb = CONFIG.get("portfolio", {}).get("risk_budget", {})
    weights, total = {}, 0.0
    for sym, pos in positions.items():
        p = (tech.get(sym) or {}).get("price") or 0
        if p > 0:
            w = pos["qty"] * p
            weights[sym] = w
            total += w
    if total <= 0:
        return {"error": "no priced positions"}
    # flags = 给人看的字符串；flag_details = 给程序判断用的结构化数据（避免反向解析中文）
    view = {"total_value": round(total, 2), "weights": {}, "flags": [], "flag_details": []}
    crypto_w = 0.0
    for sym, w in sorted(weights.items(), key=lambda x: -x[1]):
        pct = w / total
        view["weights"][sym] = round(pct, 4)
        if sym in CRYPTO_MAP:
            crypto_w += pct
        if pct > rb.get("max_single_position", 0.25):
            view["flags"].append(f"{sym} 权重 {pct:.0%} 超过单标的上限 {rb.get('max_single_position'):.0%}")
            view["flag_details"].append({
                "symbol": sym, "type": "single_position_limit", "severity": "high",
                "actual": round(pct, 4), "limit": rb.get("max_single_position"),
                "message": f"{sym} 权重 {pct:.0%} 超过单标的上限 {rb.get('max_single_position'):.0%}"})
    if crypto_w > rb.get("max_crypto_allocation", 0.30):
        view["flags"].append(f"加密总权重 {crypto_w:.0%} 超过上限 {rb.get('max_crypto_allocation'):.0%}")
        view["flag_details"].append({
            "symbol": None, "type": "crypto_allocation", "severity": "high",
            "actual": round(crypto_w, 4), "limit": rb.get("max_crypto_allocation"),
            "message": f"加密总权重 {crypto_w:.0%} 超过上限 {rb.get('max_crypto_allocation'):.0%}"})
    # ── 风险层（v10.0）── 类别集中度 / 相关性 / 风险贡献 / 组合波动与回撤
    view["risk"] = _portfolio_risk_layer(view["weights"], tech)
    return view


# ============ ML 推理（LightGBM 基线） ============
def run_ml_inference(ml_features: Dict, positions: Dict, missing: Dict = None) -> Dict:
    """LightGBM 基线模型推理 - 预测 5/20 日前向收益，输出 conviction [-1, 1]"""
    predictions = {}
    
    for sym in ml_features:
        if sym not in positions:
            continue

        try:
            sys.path.insert(0, str(_HERE))
            import ghostfolio_ml as ml_mod
            feats = ml_features[sym]

            # 「数据源挂了被填 0」≠「真实值就是 0」。缺**关键**特征就不给预测
            # （评审 ChatGPT P1-1）；加密专属特征对美股结构性缺失，不计入。
            _blocking = sorted(set((missing or {}).get(sym) or [])
                               - ml_mod.CRYPTO_ONLY_FEATURES)
            if _blocking:
                predictions[sym] = {
                    "model": "unavailable", "is_heuristic": False, "available": False,
                    "reason": "FEATURE_MISSING", "missing_features": _blocking,
                    "predicted_return_5d": None, "predicted_return_20d": None,
                    "conviction": None, "prediction_strength": None,
                    "strength_level": None}
                continue
            m5 = ml_mod.load_model(sym, 5)
            m20 = ml_mod.load_model(sym, 20)

            if not m5 and not m20:
                # 无模型：启发式兜底（**不是模型预测** —— 如实标注，不冒充）
                momentum = feats.get("momentum_5d", 0) + feats.get("momentum_20d", 0)
                trend = feats.get("ma_bull_score", 0.5) * 2 - 1
                macro_adj = -feats.get("vix_z", 0) * 0.1 + feats.get("yield_spread", 0) * 0.1
                pred = max(-1, min(1, (momentum + trend + macro_adj) / 3))
                predictions[sym] = {
                    "model": "heuristic_fallback",
                    "is_heuristic": True,            # ← 供下游判断，组合优化要据此降级
                    "predicted_return_5d": pred * 0.02,
                    "predicted_return_20d": pred * 0.05,
                    "conviction": round(pred, 3),
                    "prediction_strength": None,
                    "strength_level": "低",          # 规则式算出来的，没有模型误差可参照
                }
                continue

            pred5 = ml_mod.predict_conviction(sym, feats, 5) if m5 else None
            pred20 = ml_mod.predict_conviction(sym, feats, 20) if m20 else None
            r5 = pred5["predicted_return"] if pred5 else 0.0
            r20 = pred20["predicted_return"] if pred20 else r5 * 2.5
            convs = [p["conviction"] for p in (pred5, pred20) if p]
            strs_ = [p.get("prediction_strength") for p in (pred5, pred20)
                     if p.get("prediction_strength") is not None]
            rels = [p.get("strength_level") for p in (pred5, pred20) if p.get("strength_level")]
            _ord = {"高": 0, "中": 1, "低": 2}
            predictions[sym] = {
                "model": "lightgbm",
                "is_heuristic": False,
                "predicted_return_5d": round(r5, 4),
                "predicted_return_20d": round(r20, 4),
                "conviction": round(sum(convs) / len(convs), 3),
                # 信号噪比 = |预测| / 模型历史误差；不再输出伪精确的「置信度百分比」
                "prediction_strength": round(sum(strs_) / len(strs_), 3) if strs_ else None,
                "strength_level": (sorted(rels, key=lambda r: _ord.get(r, 3))[0] if rels else None),
                "val_rmse_5d": m5.get("val_rmse") if m5 else None,
                "train_samples": m5.get("train_samples") if m5 else None,
                # 模型 provenance（评审 ChatGPT P2-4）：训练时间 / 数据范围 / CV 稳定性
                "trained_at": m5.get("trained_at") if m5 else None,
                "train_start": m5.get("train_start") if m5 else None,
                "train_end": m5.get("train_end") if m5 else None,
                "cv_rmse_mean": m5.get("cv_rmse_mean") if m5 else None,
                "cv_rmse_std": m5.get("cv_rmse_std") if m5 else None,
            }
        except Exception as e:
            predictions[sym] = {"error": str(e), "model": "failed"}

    return predictions


# ============ LLM 评论家团队 Prompt（Layer 4，供 Hermes 主模型直接处理） ============
def build_llm_analyst_prompts(signals: Dict, tech: Dict, macro: Dict, onchain: Dict,
                               fundamentals: Dict, ml_predictions: Dict, positions: Dict,
                               vix: Dict = None, sentiment: Dict = None,
                               news: Dict = None) -> Dict:
    """
    构造 LLM 评论家团队的 Prompt，由 Hermes Cron 的默认主模型直接处理
    不在脚本里调用任何外部 API，只输出结构化 prompt 供上游使用
    """
    # 角色定义（AI-Hedge-Fund 风格）
    personas = {
        "buffett": {
            "name": "Warren Buffett",
            "style": "价值投资、护城河、长期持有、安全边际、圈内能力",
            "focus": ["基本面质量", "护城河", "管理层诚信", "长期复利", "价格<价值"]
        },
        "munger": {
            "name": "Charlie Munger",
            "style": "多元思维模型、逆向思考、避免愚蠢、集中投资、耐心等待",
            "focus": ["反常识思考", "错过不等于损失", "高质量集中", "耐心", "简单胜过复杂"]
        },
        "druckenmiller": {
            "name": "Stanley Druckenmiller",
            "style": "宏观顺势、反身性、风险控制、不对称押注、承认错误",
            "focus": ["宏观趋势", "流动性周期", "风险回报比", "止损纪律", "灵活变通"]
        }
    }
    
    # 构造上下文摘要
    def build_context(sym: str) -> str:
        sig = signals.get(sym, {})
        t = tech.get(sym, {})
        f = fundamentals.get(sym, {})
        ml = ml_predictions.get(sym, {})
        pos = positions.get(sym, {})
        
        lines = [f"=== {sym} ==="]
        lines.append(f"Signal: {sig.get('signal', 'N/A')} (conviction: {sig.get('conviction', 'N/A')})")
        lines.append(f"Thesis: {sig.get('thesis', 'N/A')}")
        lines.append(f"Price: {t.get('price', 'N/A')}, RSI: {t.get('rsi14', 'N/A')}, MA20/50/200: {t.get('ma20','N/A')}/{t.get('ma50','N/A')}/{t.get('ma200','N/A')}")
        if f:
            lines.append(f"Fundamentals: ROE={f.get('roe','N/A')}, P/E={f.get('pe_ratio','N/A')}, Margin={f.get('profit_margin','N/A')}, D/E={f.get('debt_equity','N/A')}")
        if ml:
            lines.append(f"ML: {ml.get('model','N/A')}, 5d_ret={ml.get('predicted_return_5d','N/A')}, conviction={ml.get('conviction','N/A')}")
        if pos:
            _px, _cost = t.get('price'), pos.get('avg_cost')
            pnl = ((_px / _cost) - 1) * 100 if (_px and _cost) else 0
            lines.append(f"Position: qty={pos.get('qty','N/A')}, cost={pos.get('avg_cost','N/A')}, PnL={pnl:.1f}%")
        # Layer 6：注入该标的的真实新闻（英文原始标题），供角色基于事实而非空想点评
        _nl = ((news or {}).get("per_symbol") or {}).get(sym, {}).get("items") or []
        if _nl:
            lines.append("Related news (real headlines, English):")
            for _n in _nl[:3]:
                lines.append(f"  - [{_n.get('source','?')}] {_n.get('title','')}")
        else:
            lines.append("Related news: none in the last 72h")
        return "\n".join(lines)
    
    context = "\n\n".join([build_context(sym) for sym in positions.keys()])
    
    # 宏观摘要：逐项容错，避免单个字段缺失把整段摘要变成 "N/A"
    def _f(x, fmt=None, suffix=""):
        if x is None:
            return "N/A"
        return (format(x, fmt) if fmt else str(x)) + suffix

    _vix_px = vix.get("price") if isinstance(vix, dict) else None
    _fng = sentiment.get("value") if isinstance(sentiment, dict) else None
    _stable = onchain.get("stablecoin_total_circulating_usd")
    macro_summary = ", ".join([
        f"VIX: {_f(_vix_px, '.1f')}",
        f"Fear&Greed: {_f(_fng)}",
        f"10Y-2Y Spread: {_f(macro.get('yield_spread_10y2y'), '.2f', '%')}",
        f"DXY: {_f(macro.get('dxy') or macro.get('DXY'), '.1f')}",
        f"Fed Funds: {_f(macro.get('fed_funds_rate') or macro.get('FEDFUNDS'), '.2f', '%')}",
        f"BTC Dom: {_f(onchain.get('btc_dominance'), '.1f', '%')}",
        f"Stablecoin: ${_stable:,.0f}" if _stable else "Stablecoin: N/A",
    ])
    
    # 组合视角：仓位比率与相对强弱 —— Bob 明确要求「仓位比率在三位投资人这里分析」，
    # 不进入任何持仓的买入/卖出档位（档位只看该标的自身历史）。
    # positions 里只有 qty（无市值字段）→ 市值 = qty × 现价，与 portfolio_risk.weights 口径一致
    _mv = {}
    for sy, pp in positions.items():
        _px = (tech.get(sy) or {}).get("price")
        if pp.get("qty") and _px:
            _mv[sy] = abs(pp["qty"] * _px)
    _total = sum(_mv.values()) or 0
    _cs = {sy: (sv.get("score_cross_sectional") or 50) for sy, sv in signals.items()}
    _rank = {sy: i + 1 for i, (sy, _) in
             enumerate(sorted(_cs.items(), key=lambda x: -x[1]))}
    portfolio_view = "\n".join(
        f"  {disp(sy)}: 占仓位 {(_mv.get(sy, 0) / _total * 100 if _total else 0):.1f}%, "
        f"组合内相对强弱排名 {_rank.get(sy, '?')}/{len(_cs)}"
        + (f", 相对成本 {((pp.get('avg_cost') and ((tech.get(sy) or {}).get('price', 0) / pp['avg_cost'] - 1) * 100) or 0):+.1f}%"
           if pp.get("avg_cost") else "")
        for sy, pp in positions.items()) or "  （无持仓数据）"

    # 为每个角色生成 prompt
    prompts = {}
    for key, persona in personas.items():
        prompt = f"""你是 {persona['name']}（{persona['style']}）。
关注点：{", ".join(persona['focus'])}。

当前持仓与信号：
{context}

宏观环境：
{macro_summary}

组合视角（**仓位比率与集中度就看这里** —— 每只标的的买卖档位是按它自身历史单独算的，
与仓位无关，所以「该不该减仓这只」这类配置问题由你来判断）：
{portfolio_view}

请用 3-4 句**大白话**中文给出定性评论（读者没有金融背景）：
1. 这套持仓配置（**仓位比例是否合理、有没有过度集中**）你觉得怎么样
2. 最担心的是什么
3. 最看好哪个方向
4. 一句最实在的建议

说话方式要求（重要）：
- 说人话，能不用术语就不用；必须用（如"估值""波动"）时，后面紧跟半句大白话解释
- 可以用生活化的比方，比如"像把鸡蛋分几个篮子放"
- 每句不超过 30 字，直说结论，别绕
- 不写"综上所述""基于以上分析"这类套话
- 只给定性观点，不给评分、不覆盖已有的量化信号
- **加密货币一律用简称：BTC、ETH、BNB**（不要写 bitcoin / ethereum / binancecoin）"""
        
        prompts[key] = {
            "name": persona["name"],
            "style": persona["style"],
            "system_prompt": f"You are {persona['name']}, a legendary investor. 回答一律用简体中文、大白话，面向没有金融背景的普通读者：少用术语，必须用时立刻用半句白话解释；可用生活化的比方；每句不超过30字。",
            "user_prompt": prompt
        }
    
    # 翻译任务：报告要求全中文，标题是英文 —— 由主模型在同一步译好，脚本只负责渲染
    _tr = {}
    for _sym, _v in ((news or {}).get("per_symbol") or {}).items():
        if _v.get("items"):
            _tr[_sym] = [i.get("title", "") for i in _v["items"]]
    _mev = [m.get("title", "") for m in ((news or {}).get("macro_events") or [])]

    return {
        "status": "prompts_ready",
        "instruction": "由 Hermes 默认主模型逐个调用 system_prompt + user_prompt，填充 comment 字段",
        "prompts": prompts,
        "news_translation_task": {
            "instruction": ("把下面每条英文新闻标题译成一句中文（不超过 40 字，直说要点、不加评论、不用术语腔），"
                            "写入 output JSON 的 news_translations 字段，"
                            "结构为 per_symbol 与 macro 两个键，每个键下是译文数组，"
                            "顺序必须与给定标题一一对应"),
            "per_symbol": _tr,
            "macro": _mev,
        } if (_tr or _mev) else None,
        "disclaimer": "以下只是口头看法，不改上面的量化结论，也不构成投资建议"
    }


# ============ 归因分析（Layer 5） ============
# ============ 置信区间（Layer 5） ============
# ============ 组合优化（Layer 5 - Portfolio Optimizer） ============
def _cap_weights(raw: Dict[str, float], min_w: float = 0.01, max_w: float = 0.30,
                 iters: int = 30) -> Dict[str, float]:
    """归一化 + 上下限投影，保证 sum≈1 且 min_w<=wi<=max_w。

    ponytail: 旧写法是"归一化→clip→再归一化"，第二次归一化会把权重重新推过 max_w。
    这里改成迭代投影：clamp 后把差额按"还没触顶/触底"的标的分配，直至收敛。
    若上下限本身不可行（如 n*max_w < 1），则返回 clamp 结果并放弃 sum=1。
    """
    keys = list(raw)
    if not keys:
        return {}
    tot = sum(v for v in raw.values() if v and v > 0)
    if tot <= 0:
        return {k: round(1.0 / len(keys), 6) for k in keys}
    x = {k: max(v, 0.0) / tot for k, v in raw.items()}
    for _ in range(iters):
        x = {k: min(max(v, min_w), max_w) for k, v in x.items()}
        diff = 1.0 - sum(x.values())
        if abs(diff) < 1e-9:
            break
        # 差额要分给"还有空间"的标的：差得多→给没触顶的；超了→从没触底的扣
        # （旧写法把已在 min_w 的排除在外，导致差额无处可去、sum 停在中间值）
        if diff > 0:
            elig = [k for k in keys if max_w - x[k] > 1e-12]
            room = {k: max_w - x[k] for k in elig}
        else:
            elig = [k for k in keys if x[k] - min_w > 1e-12]
            room = {k: x[k] - min_w for k in elig}
        tr = sum(room.values())
        if not elig or tr <= 1e-12:
            break
        for k in elig:
            x[k] += (1 if diff > 0 else -1) * abs(diff) * room[k] / tr
    return {k: round(v, 6) for k, v in x.items()}


def run_portfolio_optimizer(signals: Dict, ml_predictions: Dict, tech: Dict, positions: Dict) -> Dict:
    """Portfolio Optimizer 集成：有效前沿、风险平价、Black-Litterman
    使用 portfoliooptimizer.io 免费 API 或本地简化实现"""
    
    optimizer_config = CONFIG.get("portfolio_optimizer", {})
    if not optimizer_config.get("enabled", True):
        return {"status": "disabled", "reason": "Portfolio optimizer disabled in config"}
    
    # 构造预期收益向量（用 ML conviction 或 signal conviction）
    expected_returns = {}
    for sym in positions:
        ml = ml_predictions.get(sym, {})
        sig = signals.get(sym, {})
        if ml.get("predicted_return_5d") is not None:
            expected_returns[sym] = min(max(ml["predicted_return_5d"] * 52, -0.5), 0.8)  # 年化复利近似，clip 防极端
        else:
            expected_returns[sym] = sig.get("conviction", 0) * 0.15  # 年化近似
    
    # 当前权重
    current_weights = {}
    total_val = 0
    for sym, pos in positions.items():
        p = (tech.get(sym) or {}).get("price", 0)
        if p > 0:
            w = pos["qty"] * p
            current_weights[sym] = w
            total_val += w
    
    if total_val <= 0:
        return {"status": "error", "reason": "No priced positions"}
    
    current_weights = {k: v/total_val for k, v in current_weights.items()}
    
    # 简化的本地优化（风险平价 / 等权 / 信号加权）
    # 实际可接入 portfoliooptimizer.io API
    
    results = {}
    
    # 1. 等权重
    n = len(current_weights)
    equal_weight = {k: 1.0/n for k in current_weights}
    results["equal_weight"] = equal_weight
    
    # 2. 信号加权（正 conviction 加权，负设为最小权重）
    min_w = optimizer_config.get("constraints", {}).get("min_weight", 0.01)
    max_w = optimizer_config.get("constraints", {}).get("max_weight", 0.3)
    conv_weights = {}
    for sym, w in current_weights.items():
        conv = expected_returns.get(sym, 0)
        if conv > 0:
            conv_weights[sym] = conv
        else:
            conv_weights[sym] = min_w
    conv_weights = _cap_weights(conv_weights, min_w, max_w)
    results["signal_weighted"] = conv_weights
    
    # 3. 风险平价（简化：用 1/vol 代理）
    # 这里用一个启发式：波动率越低权重越高
    vol_weights = {}
    for sym in current_weights:
        t = tech.get(sym, {})
        # 用高低价近似波动
        high = t.get("high_52w", 1)
        low = t.get("low_52w", 1)
        price = t.get("price", 1)
        vol_proxy = (high - low) / price if price > 0 else 0.2
        vol_weights[sym] = 1.0 / (vol_proxy + 0.01)
    vol_weights = _cap_weights(vol_weights, min_w, max_w)
    results["risk_parity"] = vol_weights
    
    # 4. Max Sharpe（简化：用 conviction/vol 近似 Sharpe）
    notes = {}
    sharpe_weights = {}
    for sym in current_weights:
        conv = max(expected_returns.get(sym, 0), 0)  # ponytail: 负收益标的不应进入 max_sharpe 多头权重
        t = tech.get(sym, {})
        high = t.get("high_52w", 1)
        low = t.get("low_52w", 1)
        price = t.get("price", 1)
        vol_proxy = (high - low) / price if price > 0 else 0.2
        sharpe_weights[sym] = conv / (vol_proxy + 0.01)
    if sum(sharpe_weights.values()) > 0:
        results["max_sharpe"] = _cap_weights(sharpe_weights, min_w, max_w)
    else:
        # 全部标的预期收益 ≤0 → max_sharpe 无正权重解。
        # ⚠️ 不能把「全 0 权重」当成「建议全部清仓」发给读者 —— 那是没算出解，不是建议。
        results["max_sharpe"] = None
        notes["max_sharpe"] = "全部标的预期收益 ≤0，无正权重解，不给方案"
    
    # 计算各方案的预期收益和风险
    for method, weights in results.items():
        if not weights:
            results[method] = {"weights": None, "sharpe_estimate": None,
                              "note": notes.get(method, "无解")}
            continue
        exp_ret = sum(weights.get(s, 0) * expected_returns.get(s, 0) for s in weights)
        # 简化风险：加权波动
        exp_vol = sum(weights.get(s, 0) * ((tech.get(s, {}).get("high_52w", 1) - tech.get(s, {}).get("low_52w", 1)) / tech.get(s, {}).get("price", 1) if tech.get(s, {}).get("price", 1) > 0 else 0.2) for s in weights)
        results[method] = {
            "weights": weights,
            "expected_return_annual": round(exp_ret, 4),
            "expected_vol_annual": round(exp_vol, 4),
            "sharpe_estimate": round(exp_ret / exp_vol if exp_vol > 0 else 0, 2)
        }
    
    # 与当前持仓对比
    current_exp_ret = sum(current_weights.get(s, 0) * expected_returns.get(s, 0) for s in current_weights)
    current_vol = sum(current_weights.get(s, 0) * ((tech.get(s, {}).get("high_52w", 1) - tech.get(s, {}).get("low_52w", 1)) / tech.get(s, {}).get("price", 1) if tech.get(s, {}).get("price", 1) > 0 else 0.2) for s in current_weights)
    
    return {
        "status": "completed",
        "current_portfolio": {
            "weights": current_weights,
            "expected_return_annual": round(current_exp_ret, 4),
            "expected_vol_annual": round(current_vol, 4),
            "sharpe_estimate": round(current_exp_ret / current_vol if current_vol > 0 else 0, 2)
        },
        "optimized_portfolios": results,
        # 这里曾有一条 "建议参考 risk_parity 或 max_sharpe 方案再平衡"。删掉：
        # 契约测试把 recommendation 列为产物里根本不该存在的决策类字段（零消费者，
        # 报告也不渲染），而它藏在嵌套层里躲过了守卫 —— 守卫已改递归。
        "disclaimer": "优化结果基于简化模型，实际交易请结合流动性、税务、交易成本综合判断"
    }


if __name__ == "__main__":
    main()
