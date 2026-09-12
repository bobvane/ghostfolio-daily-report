#!/usr/bin/env python3
"""
特征工程模块 - 将原始行情数据转换为标准化特征
支持：z-score 标准化、分位数、趋势斜率、动量、波动率
"""
import os
import json
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "features"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 通用配置
LOOKBACK_WINDOWS = [5, 10, 20, 60, 120]
ZSCORE_WINDOWS = [60, 120, 252]
PERCENTILE_WINDOWS = [252]


def _safe_get(tech: Dict, key: str, default=None):
    """安全获取技术指标"""
    if not tech or "error" in tech:
        return default
    return tech.get(key, default)


def _get_price_series(tech: Dict, macro: Dict, onchain: Dict) -> Optional[pd.Series]:
    """尝试从各数据源重构价格序列（近似）"""
    # 当前只用 tech 里的单点价格，实际应存历史缓存
    # 这里返回 None，后续可接入历史缓存文件
    return None


def compute_technical_features(tech: Dict, macro: Dict, onchain: Dict) -> Dict[str, float]:
    """计算技术面特征（单标的）"""
    feats = {}
    
    # 基础指标
    rsi = _safe_get(tech, "rsi14", 50)
    price = _safe_get(tech, "price", 0)
    change = _safe_get(tech, "change_pct", 0)
    ma20 = _safe_get(tech, "ma20", 0)
    ma50 = _safe_get(tech, "ma50", 0)
    ma200 = _safe_get(tech, "ma200", 0)
    high_52w = _safe_get(tech, "high_52w", 0)
    low_52w = _safe_get(tech, "low_52w", 0)
    
    if price <= 0:
        return {}
    
    # RSI 归一化 (0-100 -> -1 to 1)
    feats["rsi14"] = rsi / 100.0
    feats["rsi14_z"] = (rsi - 50) / 25.0  # 近似 z-score
    
    # 均线距离 (百分比)
    if ma20 > 0:
        feats["ma20_dist"] = (price / ma20 - 1) * 100
    if ma50 > 0:
        feats["ma50_dist"] = (price / ma50 - 1) * 100
    if ma200 > 0:
        feats["ma200_dist"] = (price / ma200 - 1) * 100
    
    # 52周位置
    if high_52w > 0 and low_52w > 0 and high_52w != low_52w:
        feats["pos_52w"] = (price - low_52w) / (high_52w - low_52w)
    
    # 真实 5/20 日动量（用 tech 里的 5/20 日前收盘价，与训练数据对齐）
    close_5d_ago = _safe_get(tech, "close_5d_ago", 0)
    close_20d_ago = _safe_get(tech, "close_20d_ago", 0)
    if close_5d_ago > 0:
        feats["momentum_5d"] = (price / close_5d_ago - 1) * 100
    else:
        feats["momentum_5d"] = change  # 兜底
    if close_20d_ago > 0:
        feats["momentum_20d"] = (price / close_20d_ago - 1) * 100
    else:
        feats["momentum_20d"] = change * 4  # 兜底
    
    # 趋势斜率（MA 排列）
    bull_count = sum([price > ma20 if ma20 else 0, price > ma50 if ma50 else 0, price > ma200 if ma200 else 0])
    feats["ma_bull_score"] = bull_count / 3.0
    
    # 短期波动（用 high/low 近似）
    if high_52w > 0 and low_52w > 0:
        feats["range_52w_ratio"] = (high_52w - low_52w) / price   # 52周价格区间/现价，不是波动率
    
    return feats


def compute_macro_features(macro: Dict, vix: Dict = None, sentiment: Dict = None) -> Dict[str, float]:
    """计算宏观特征。

    ponytail: collect 输出里 vix / sentiment 是顶层字段，不在 macro 内，必须由调用方显式传入
    （旧的 macro["vix"] 写法永远取不到）。仍兼容"把 vix 塞进 macro"的旧调用。
    dxy 用 classic DX-Y.NYB：训练侧 ghostfolio_backfill.MACRO_TICKERS 用的就是它；
    FRED 广义美元指数(DTWEXBGS，约118) 与训练分布和阈值 105/100 完全对不上量纲。
    """
    feats = {}

    if not isinstance(vix, dict):
        vix = macro.get("vix") if isinstance(macro.get("vix"), dict) else None
    if not isinstance(sentiment, dict):
        sentiment = macro.get("sentiment") if isinstance(macro.get("sentiment"), dict) else None

    # v10.3：**不可得也写键**（值 None），由 build_feature_vector 统一填 0 并记进缺失掩码。
    # 旧写法 `if 有值: feats[k] = ...` 在数据源整个挂掉时会把键**直接省掉** ——
    # 特征向量从 23 个静默缩到 10 个，而掩码只看 None/NaN，于是完全记不到
    # （评审 ChatGPT P1）。现在键集恒定、缺失可见，模型输入仍是 0.0（行为不变）。
    # VIX
    _v = vix.get("price") if vix else None
    feats["vix"] = float(_v) if _v else None
    feats["vix_z"] = (float(_v) - 20) / 10 if _v else None  # 近似标准化

    # Fear & Greed
    _fg = sentiment.get("value") if sentiment else None
    feats["fear_greed"] = float(_fg) / 100.0 if _fg else None

    # 息差（inverted 在 spread>=0 时**真实**为 0.0，与「拿不到」的 None 区分开）
    spread = macro.get("yield_spread_10y2y")
    feats["yield_spread"] = spread
    feats["yield_spread_inverted"] = ((1.0 if spread < 0 else 0.0)
                                      if spread is not None else None)

    # DXY（与训练同源：DX-Y.NYB）
    dxy = macro.get("DXYNYB") or macro.get("dxy") or macro.get("DXY") or macro.get("DTWEXBGS")
    feats["dxy"] = float(dxy) if dxy else None
    feats["dxy_z"] = (float(dxy) - 100) / 10 if dxy else None

    # Fed 利率（FRED 落库 key 是 fed_funds_rate，不是 FEDFUNDS）
    fed = macro.get("fed_funds_rate") or macro.get("fed_funds") or macro.get("FEDFUNDS")
    feats["fed_rate"] = float(fed) if fed else None
    feats["fed_z"] = (float(fed) - 3) / 2 if fed else None

    return feats


def compute_onchain_features(onchain: Dict, sym: str = None) -> Dict[str, float]:
    """计算链上特征（sym 用于取该标的专属的估值指标）"""
    feats = {}

    # 按标的的链上估值（CoinMetrics 免费源；仅 BTC/ETH 有）
    # 注：这两个特征在当前模型里 gain=0（训练时为 0，模型未使用），
    # 填入真值不改变预测结果，但让特征向量完整、为将来重训备好数据。
    # v10.3：同 compute_macro_features —— 不可得的特征也写键（None），交给掩码记录。
    ps = ((onchain.get("per_symbol") or {}).get(sym or "") or {}) if sym else {}
    _av = bool(ps.get("available"))
    _mvrv_ps = float(ps["mvrv"]) if (_av and ps.get("mvrv") is not None) else None
    _nupl_ps = float(ps["nupl"]) if (_av and ps.get("nupl") is not None) else None
    # 沿用既有 exchange_flow 定义（原 BTC 枚数 / 10000 归一化）
    _nf = ps.get("exchange_net_flow_native") if _av else None
    feats["mvrv"] = _mvrv_ps
    feats["nupl"] = _nupl_ps
    feats["exchange_flow"] = float(_nf) / 10000.0 if _nf is not None else None

    # BTC dominance
    btc_dom = onchain.get("btc_dominance")
    feats["btc_dominance"] = btc_dom / 100.0 if btc_dom else None

    # 市值变化
    mcap_chg = onchain.get("market_cap_change_24h_pct")
    feats["market_cap_change_24h"] = mcap_chg / 100.0 if mcap_chg else None

    # 稳定币流通量（对数）
    stable = onchain.get("stablecoin_total_circulating_usd")
    feats["stablecoin_log"] = np.log10(stable) if stable else None
    # YoY 增长：由 collect 从 DefiLlama /stablecoincharts/all 算好（v5.8 起真实值）。
    # 拿不到就 None（会被掩码记下）—— 旧写法写 0.0，让「增长率真的是 0」和「源挂了」
    # 在产物里长得一模一样，正是评审 ChatGPT P1 指出的语义污染。
    _g = onchain.get("stablecoin_yoy_growth")
    feats["stablecoin_growth"] = float(_g) if _g is not None else None

    # 顶层兜底（原始优先级：顶层值非 None 时覆盖 per_symbol 的取值）
    mvrv = onchain.get("mvrv_z_score")
    if mvrv is not None:
        feats["mvrv"] = mvrv

    nupl = onchain.get("nupl")
    if nupl is not None:
        feats["nupl"] = nupl

    ex_flow = onchain.get("exchange_net_flow_btc")
    if ex_flow is not None:
        feats["exchange_flow"] = ex_flow / 10000.0  # 归一化

    return feats


def compute_fundamental_features(sym: str, tech: Dict) -> Dict[str, float]:
    """基本面特征（yfinance fundamentals 或 Financial Datasets API）"""
    # 暂时返回空，后续接入 yfinance .info 或 Financial Datasets API
    # 需要的字段：ROE, FCF yield, P/E, Profit Margin, Debt/Equity
    return {}


def build_feature_vector(sym: str, tech: Dict, macro: Dict, onchain: Dict,
                         vix: Dict = None, sentiment: Dict = None,
                         missing_out: list = None) -> Dict[str, float]:
    """组装完整特征向量（vix/sentiment 为顶层字段，需显式传入）。

    missing_out: 传入 list 时，被填 0 的特征名会追加进去（缺失掩码）。
    """
    feats = {}
    feats.update(compute_technical_features(tech, macro, onchain))
    feats.update(compute_macro_features(macro, vix, sentiment))
    feats.update(compute_onchain_features(onchain, sym))
    feats.update(compute_fundamental_features(sym, tech))
    
    # 填充缺失值 —— 0.0 是 **imputation value，不是观测值**（v10.2，评审 ChatGPT #10）。
    # 「特征不可得」和「特征等于 0」是两个不同状态：数据源挂了被填 0，模型会当成
    # 「增长率真的是 0」。所以同时返回缺失清单，调用方可判断本次特征可用性。
    # ponytail: 不改返回值形状（避免动 ML 训练链路），用可选出参回传掩码。
    for k, v in feats.items():
        if v is None or (isinstance(v, float) and np.isnan(v)):
            feats[k] = 0.0
            if missing_out is not None:
                missing_out.append(k)

    return feats


def save_features(sym: str, feats: Dict, timestamp: str):
    """保存特征到缓存（用于后续 ML 训练）"""
    path = CACHE_DIR / f"{sym}.jsonl"
    record = {"timestamp": timestamp, "features": feats}
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_feature_history(sym: str, max_records: int = 1000) -> pd.DataFrame:
    """加载特征历史用于训练"""
    path = CACHE_DIR / f"{sym}.jsonl"
    if not path.exists():
        return pd.DataFrame()
    records = []
    with open(path) as f:
        for line in f:
            records.append(json.loads(line))
    df = pd.DataFrame(records)
    if len(df) > max_records:
        df = df.tail(max_records)
    return df


if __name__ == "__main__":
    # 自测
    test_tech = {
        "price": 100, "change_pct": 1.5, "rsi14": 65,
        "ma20": 99, "ma50": 97, "ma200": 95,
        "high_52w": 110, "low_52w": 80
    }
    test_macro = {
        "vix": {"price": 18}, "sentiment": {"value": 65},
        "yield_spread_10y2y": 1.2, "DXY": 102, "FEDFUNDS": 4.5
    }
    test_onchain = {
        "btc_dominance": 55, "market_cap_change_24h_pct": 2.1,
        "stablecoin_total_circulating_usd": 150_000_000_000,
        "mvrv_z_score": 2.5, "nupl": 0.45, "exchange_net_flow_btc": -5000
    }
    feats = build_feature_vector("TEST", test_tech, test_macro, test_onchain)
    print(json.dumps(feats, indent=2, ensure_ascii=False))