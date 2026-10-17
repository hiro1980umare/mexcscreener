#!/usr/bin/env python3
"""極端なリターンが結果を動かしていないかを見る診断（除外はしない。一覧にするだけ）。
    python top_returns.py --run results/v1_dev_datafix1
"""
import argparse
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--run", default="results/v1_dev_datafix1")
a = ap.parse_args()
sig = pd.read_csv(Path(a.run) / "signals.csv.gz", parse_dates=["date"])
fl = Path("data/audit/anomalies_daily.csv")
flagged = set(pd.read_csv(fl).ticker) if fl.exists() else set()
print(f"シグナル行数: {len(sig)} / 異常値一覧に載っている銘柄数: {len(flagged)}")
for h in ("1m", "6m"):
    c = f"ret_{h}"
    r = sig[c].dropna()
    print(f"\n=== {h}後: 行数{len(r)}  中央値{r.median():.1%}  平均{r.mean():.1%}  +300%超:{(r > 3).sum()}行  +1000%超:{(r > 10).sum()}行  -90%以下:{(r < -0.9).sum()}行 ===")
    for title, d in (("上位15", sig.nlargest(15, c)), ("下位10", sig.nsmallest(10, c))):
        print(f"\n[{title}]")
        d = d[["ticker", "date", c, "grade", "types"]].copy()
        d["date"] = d["date"].dt.date
        d[c] = d[c].map(lambda x: f"{x:+.0%}")
        d["異常値一覧に載る"] = d.ticker.isin(flagged).map({True: "★", False: ""})
        print(d.to_string(index=False))
    for g, m in (("ALL", sig.simple_rsi | ~sig.simple_rsi), ("SIMPLE_RSI", sig.simple_rsi), ("grade_A", sig.grade == "A")):
        x = sig.loc[m, c].dropna().sort_values()
        k = max(int(len(x) * 0.01), 1)
        print(f"  参考({g}): 平均 {x.mean():.2%} → 上位1%({k}行)を除いた平均 {x.iloc[:-k].mean():.2%} （除外ではなく感度の確認）")
