#!/usr/bin/env python3
"""
历史数据回填 + LightGBM 训练
从 yfinance 拉 3 年日线，按日计算特征，以未来 5/20 日收益为标签训练模型。
用法: python3 ghostfolio_backfill.py
"""
import numpy as np
import pandas as pd
import yfinance as yf
import ghostfolio_ml as ml

# 持仓 symbol -> yfinance ticker
SYMBOL_MAP = {
    "QQQ": "QQQ", "VOO": "VOO", "SCHD": "SCHD", "SMH": "SMH",
    "SIVR": "SIVR", "SGOV": "SGOV",
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "binancecoin": "BNB-USD",
}
MACRO_TICKERS = {"vix": "^VIX", "tnx": "^TNX", "irx": "^IRX", "dxy": "DX-Y.NYB"}


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def build_features(close: pd.Series, macro: pd.DataFrame) -> pd.DataFrame:
    """逐日计算与 ml.FEATURE_COLS 对齐的特征"""
    df = pd.DataFrame(index=close.index)
    px = close
    ma20, ma50, ma200 = px.rolling(20).mean(), px.rolling(50).mean(), px.rolling(200).mean()
    hi52, lo52 = px.rolling(252).max(), px.rolling(252).min()
    r = rsi(px)

    df["rsi14"] = r / 100.0
    df["rsi14_z"] = (r - 50) / 25.0
    df["ma20_dist"] = (px / ma20 - 1) * 100
    df["ma50_dist"] = (px / ma50 - 1) * 100
    df["ma200_dist"] = (px / ma200 - 1) * 100
    df["pos_52w"] = (px - lo52) / (hi52 - lo52)
    df["momentum_5d"] = px.pct_change(5) * 100
    df["momentum_20d"] = px.pct_change(20) * 100
    df["ma_bull_score"] = ((px > ma20).astype(int) + (px > ma50).astype(int) + (px > ma200).astype(int)) / 3.0
    df["range_52w_ratio"] = (hi52 - lo52) / px   # 与生产侧同名同式

    # 宏观特征（按日期对齐，缺失前值填充）
    if not macro.empty:
        m = macro.reindex(df.index).ffill()
        if "vix" in m:
            df["vix"] = m["vix"]
            df["vix_z"] = (m["vix"] - 20) / 10
        if "tnx" in m and "irx" in m:
            spread = m["tnx"] - m["irx"]
            df["yield_spread"] = spread
            df["yield_spread_inverted"] = (spread < 0).astype(float)
        if "dxy" in m:
            df["dxy"] = m["dxy"]
            df["dxy_z"] = (m["dxy"] - 100) / 10

    # 无历史来源的特征补 0
    for col in ml.FEATURE_COLS:
        if col not in df.columns:
            df[col] = 0.0
    return df[ml.FEATURE_COLS]


def download(ticker: str) -> pd.Series:
    h = yf.download(ticker, period="5y", interval="1d", progress=False, auto_adjust=True)
    if h.empty:
        return pd.Series(dtype=float)
    s = h["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return s.dropna()


def main():
    # 宏观序列一次性下载
    macro = pd.DataFrame({k: download(t) for k, t in MACRO_TICKERS.items()})

    trained, skipped = [], []
    for sym, ticker in SYMBOL_MAP.items():
        px = download(ticker)
        if len(px) < ml.MIN_TRAIN_SAMPLES + 25:
            print(f"[{sym}] 历史不足: {len(px)}")
            skipped.append(sym)
            continue
        X = build_features(px, macro).dropna()
        for horizon in ml.TARGET_HORIZONS:
            y = px.pct_change(horizon).shift(-horizon).reindex(X.index)
            mask = y.notna()
            Xh, yh = X[mask], y[mask]
            if len(Xh) < ml.MIN_TRAIN_SAMPLES:
                print(f"[{sym}] h{horizon} 样本不足: {len(Xh)}")
                continue
            model = _train(sym, Xh, yh, horizon)
            if model:
                trained.append(f"{sym}_h{horizon}")
    print(f"\n完成: 训练 {len(trained)} 个模型, 跳过 {skipped}")


def _train(sym, X, y, horizon):
    from sklearn.model_selection import TimeSeriesSplit
    import pickle
    # gap=horizon：标签是 y[t] = 收益(t → t+horizon)，训练集末尾 horizon 根的标签
    # 会用到验证期的价格。不留 gap 就是前视泄漏。
    tscv = TimeSeriesSplit(n_splits=5, gap=horizon)
    params = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 31, "learning_rate": 0.05, "feature_fraction": 0.8,
        "bagging_fraction": 0.8, "bagging_freq": 5, "verbose": -1, "seed": 42,
    }
    import lightgbm as lgb
    scores, iters = [], []
    for tr, va in tscv.split(X):
        dtrain = lgb.Dataset(X.iloc[tr], label=y.iloc[tr])
        dval = lgb.Dataset(X.iloc[va], label=y.iloc[va], reference=dtrain)
        m = lgb.train(params, dtrain, num_boost_round=500,
                      valid_sets=[dval], callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])
        scores.append(float(np.sqrt(np.mean((m.predict(X.iloc[va]) - y.iloc[va]) ** 2))))
        iters.append(int(getattr(m, "best_iteration", 0) or 500))
    if not scores:
        return None
    # CV 只用来「评稳定性 + 定轮数」，最终模型用**全部**历史重训。
    # 旧写法挑 RMSE 最低的那个 fold 当模型并把它当模型误差：既是选出来的乐观值，
    # 又只用了某个 fold 的训练集。val_rmse 现在存 CV 均值 = 泛化误差。
    rounds = max(50, int(round(float(np.mean(iters)))))
    final = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=rounds)
    cv_mean, cv_std = float(np.mean(scores)), float(np.std(scores))
    path = ml.MODEL_DIR / f"{sym}_h{horizon}.pkl"
    with open(path, "wb") as f:
        pickle.dump({"model": final, "features": ml.FEATURE_COLS, "horizon": horizon,
                     "feature_fingerprint": ml.feature_fingerprint(),
                     "trained_at": pd.Timestamp.now().isoformat(),
                     "train_samples": len(X), "val_rmse": cv_mean,
                     "cv_rmse_mean": cv_mean, "cv_rmse_std": cv_std,
                     "cv_rmse_each_fold": [round(s, 6) for s in scores],
                     "final_rounds": rounds, "cv_gap": horizon,
                     "train_start": str(X.index.min())[:10],
                     "train_end": str(X.index.max())[:10]}, f)
    print(f"[{sym}] h{horizon}: {len(X)} 样本, CV-RMSE={cv_mean:.4f}±{cv_std:.4f} "
          f"({len(scores)} folds, gap={horizon}), 全量重训 {rounds} 轮 -> {path}")
    return final


if __name__ == "__main__":
    main()
