#!/usr/bin/env python3
"""
ML 模型模块 - LightGBM 基线模型
预测 5/20 日前向收益，输出 conviction [-1, 1]
"""
import os
import json
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
FEATURE_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "features"

# 配置
TARGET_HORIZONS = [5, 20]  # 预测 5 日和 20 日前向收益
# 已删除 TRAIN_WINDOW / VAL_WINDOW / FEATURE_IMPORTANCE_THRESHOLD —— grep 全仓零消费者，
# 留着只会和「生产 2y / 训练 5y」这两个真实口径混成三个数字（评审 ChatGPT P2-4）。
MIN_TRAIN_SAMPLES = 500

# 特征语义版本：**改公式或口径**时 +1（改名/增删列由列名哈希自动覆盖）。
FEATURE_SCHEMA = 2
# 只在加密标的上有意义的特征 —— 对美股是结构性缺失，不算「数据源故障」。
CRYPTO_ONLY_FEATURES = {"btc_dominance", "market_cap_change_24h", "stablecoin_log",
                        "stablecoin_growth", "mvrv", "nupl", "exchange_flow"}

# 目标特征列表（必须与 ghostfolio_features.py 对齐）
FEATURE_COLS = [
    "rsi14", "rsi14_z", "ma20_dist", "ma50_dist", "ma200_dist",
    "pos_52w", "momentum_5d", "momentum_20d", "ma_bull_score", "range_52w_ratio",
    "vix", "vix_z", "fear_greed", "yield_spread", "yield_spread_inverted",
    "dxy", "dxy_z", "fed_rate", "fed_z",
    "btc_dominance", "market_cap_change_24h", "stablecoin_log",
    "stablecoin_growth", "mvrv", "nupl", "exchange_flow"
]


def _load_feature_history(sym: str, max_records: int = 2000) -> pd.DataFrame:
    """加载特征历史"""
    path = FEATURE_CACHE_DIR / f"{sym}.jsonl"
    if not path.exists():
        return pd.DataFrame()
    from collections import deque
    # deque(maxlen) 逐行读：原实现「先全读进 RAM 再 tail」只限制了结果条数，
    # 没限制 IO/内存（评审 ChatGPT P2-5）。
    records = deque(maxlen=max_records)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(records)


def feature_fingerprint() -> str:
    """特征定义指纹 = 列名顺序 + 特征语义版本。

    模型带上它，加载时不一致就**当作没有模型** —— 否则改了特征定义、旧模型
    照常返回预测，生产特征与训练特征语义不同却无人察觉（评审 ChatGPT P2-6）。
    """
    import hashlib
    raw = "|".join(FEATURE_COLS) + f"|fs{FEATURE_SCHEMA}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def load_model(sym: str, horizon: int = 5) -> Optional[Dict]:
    """加载模型；特征指纹不一致 → 返回 None（宁可退回启发式，也不给漂移的预测）"""
    model_path = MODEL_DIR / f"{sym}_h{horizon}.pkl"
    if not model_path.exists():
        return None
    with open(model_path, "rb") as f:
        md_ = pickle.load(f)
    if md_.get("feature_fingerprint") != feature_fingerprint():
        return None
    return md_


def predict_conviction(sym: str, features: Dict, horizon: int = 5) -> Dict[str, float]:
    """预测 conviction [-1, 1]"""
    model_data = load_model(sym, horizon)
    if not model_data:
        return {"conviction": 0.0, "available": False, "strength_level": None}
    
    model = model_data["model"]
    feat_cols = model_data.get("features", FEATURE_COLS)
    
    # 构建特征向量
    x = np.array([[features.get(c, 0.0) for c in feat_cols]])
    
    # 预测收益率
    pred_return = model.predict(x)[0]
    
    # 收益率 -> conviction 映射
    # 假设 5 日收益 2% 对应 conviction 0.5，线性映射
    conviction = np.tanh(pred_return * 25)  # tanh 映射到 (-1, 1)
    
    # ⚠️ 原来的 confidence = 100*(1-rmse/0.05)*(1+|pred|*10) 是伪精确数字：
    # RMSE 不是概率，这个式子也没有统计含义，却能算出「73.46%」这种看着很确定的数。
    # 改成信号噪比：|预测幅度| / 模型历史误差 —— 小于 1 就说明预测幅度还没模型误差大。
    rmse = model_data.get("val_rmse", 0.02) or 0.02
    strength = abs(pred_return) / rmse if rmse else 0.0
    if strength >= 2:
        reliability = "高"
    elif strength >= 1:
        reliability = "中"
    else:
        reliability = "低"
    return {
        "conviction": float(conviction),
        "prediction_strength": round(float(strength), 3),
        "strength_level": reliability,
        "predicted_return": float(pred_return),
        "available": True
    }


if __name__ == "__main__":
    # 自测：需要先有特征缓存
    print("ML 模块就绪。需先积累特征缓存再训练。")
    print(f"模型目录: {MODEL_DIR}")
    print(f"特征缓存: {FEATURE_CACHE_DIR}")