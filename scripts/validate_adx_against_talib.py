#!/usr/bin/env python3
"""ADX 数值级验证（一次性离线脚本，**不进每日流水线、不进生产依赖**）。

背景：判定层 `ghostfolio_rules.adx()` 用 `ewm(alpha=1/n)` 做平滑，
README 已知限制第 3 条写着「未与独立实现做数值级比对，阈值 25 附近理论上可能翻转判定」。
本脚本把这条限制变成**有数据支撑的结论**。

三方评审（ChatGPT / Grok / Perplexity）都建议做这件事，且都强调：
重点不是 RMSE，而是 **ADX=25 阈值附近会不会翻转决定**。

参照实现：`ta` 包（独立第三方，按 Wilder 原始定义实现）。
注意：TA-Lib 需要 C 库、本机装不上，所以用 `ta` 而非字面的 TA-Lib —— 两者同为
Wilder 定义，但不宣称「已与 TA-Lib 比对」。

用法：
    python3 validate_adx_against_talib.py            # 用回测价格缓存
    python3 validate_adx_against_talib.py --limit 2000
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghostfolio_rules as gr  # noqa: E402

CACHE = Path("/opt/data/cache/eval_prices")
THRESHOLD = 25.0
NEAR = 2.0          # 阈值 ±2 视为「危险区」（评审建议只看 23~27）


def _norm(df):
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None).normalize()
    return df.sort_index()


def load(sym):
    """优先回测缓存，缺了直接拉。"""
    for pat in (f"{sym}_5y.csv", f"{sym}_3y.csv"):
        f = CACHE / pat
        if f.exists():
            df = pd.read_csv(f, index_col=0)
            if {"High", "Low", "Close"} <= set(df.columns):
                return _norm(df)
    import yfinance as yf
    h = yf.Ticker(sym).history(period="5y", auto_adjust=True)
    return _norm(h) if not h.empty else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只用最近 N 根（0=全部）")
    ap.add_argument("--json", help="结果写入该 JSON 文件")
    a = ap.parse_args()

    try:
        from ta.trend import ADXIndicator
    except ImportError:
        print("❌ 需要 `ta` 包：uv pip install --python <venv>/bin/python ta")
        return 1

    syms = ["QQQ", "VOO", "SCHD", "SMH", "SIVR", "SGOV", "BTC-USD", "ETH-USD", "BNB-USD"]
    rows, all_flips, all_bars, all_near, all_near_flips = [], 0, 0, 0, 0

    for sym in syms:
        h = load(sym)
        if h is None or len(h) < 100:
            rows.append({"symbol": sym, "error": "取不到足够历史"})
            continue
        if a.limit:
            h = h.tail(a.limit)

        mine = gr.adx(h["High"], h["Low"], h["Close"], 14)
        ref = ADXIndicator(h["High"], h["Low"], h["Close"], window=14,
                           fillna=False).adx()

        m = pd.Series(mine, index=h.index).dropna()
        r = pd.Series(ref, index=h.index).dropna()
        idx = m.index.intersection(r.index)
        m, r = m.loc[idx], r.loc[idx]
        # ewm 需要预热才收敛；前 100 根只做参考，不计入结论（在报告里说明）
        m, r = m.iloc[100:], r.iloc[100:]
        if len(m) < 50:
            rows.append({"symbol": sym, "error": "可比区间太短"})
            continue

        d = (m - r).abs()
        above_m, above_r = m > THRESHOLD, r > THRESHOLD
        flips = int((above_m != above_r).sum())
        near = ((m - THRESHOLD).abs() <= NEAR) | ((r - THRESHOLD).abs() <= NEAR)
        near_flips = int(((above_m != above_r) & near).sum())
        rows.append({
            "symbol": sym, "bars": int(len(m)),
            "max_abs_error": round(float(d.max()), 4),
            "mean_abs_error": round(float(d.mean()), 4),
            "p99_abs_error": round(float(d.quantile(0.99)), 4),
            "threshold_flip_count": flips,
            "threshold_flip_ratio": round(flips / len(m), 5),
            "bars_near_threshold": int(near.sum()),
            "near_threshold_flips": near_flips,
            "corr": round(float(m.corr(r)), 5),
        })
        all_flips += flips
        all_bars += len(m)
        all_near += int(near.sum())
        all_near_flips += near_flips

    hdr = (f"{'标的':9s} {'K线':>6s} {'最大误差':>9s} {'平均误差':>9s} {'p99':>8s} "
           f"{'阈值翻转':>8s} {'翻转率':>9s} {'阈值附近':>8s}")
    print(f"ADX 数值级比对（我的实现 vs `ta` 包的 Wilder 实现，阈值 {THRESHOLD:.0f}）")
    print("预热 100 根不计入；价格缓存 5 年日线\n")
    print(hdr)
    print("-" * len(hdr))
    for x in rows:
        if "error" in x:
            print(f"{x['symbol']:9s}  {x['error']}")
            continue
        print(f"{x['symbol']:9s} {x['bars']:>6d} {x['max_abs_error']:>9.4f} "
              f"{x['mean_abs_error']:>9.4f} {x['p99_abs_error']:>8.4f} "
              f"{x['threshold_flip_count']:>8d} {x['threshold_flip_ratio']:>8.3%} "
              f"{x['bars_near_threshold']:>8d}")

    if all_bars:
        print(f"\n合计 {all_bars:,} 根 K 线")
        print(f"  阈值翻转 {all_flips} 次（{all_flips/all_bars:.3%}）")
        print(f"  其中落在阈值 ±{NEAR:.0f} 危险区的翻转 {all_near_flips} 次"
              f"（该区域共 {all_near:,} 根）")

    # ── 结论判定 ──
    print("\n" + "═" * 60)
    if all_flips == 0:
        verdict = ("✅ 阈值翻转 0 次 —— README 第 3 条限制可以收窄为"
                   "「已与独立 Wilder 实现比对，阈值判定无差异」")
    elif all_flips / max(1, all_bars) < 0.005:
        verdict = (f"⚠️ 翻转率 {all_flips/all_bars:.3%}（<0.5%）—— 差异存在但极小，"
                   f"属可接受；README 里应写明确切翻转率，不再写「理论上可能翻转」")
    else:
        verdict = (f"❌ 翻转率 {all_flips/all_bars:.3%} 偏高 —— 需要提升"
                   f"「ADX 近似」这条限制的优先级，考虑改用精确 Wilder 平滑")
    print(verdict)

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"threshold": THRESHOLD, "near_band": NEAR, "rows": rows,
             "total_bars": all_bars, "total_flips": all_flips,
             "near_threshold_flips": all_near_flips, "verdict": verdict},
            ensure_ascii=False, indent=2))
        print(f"\n结果已写入 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
