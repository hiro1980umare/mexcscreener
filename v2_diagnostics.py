#!/usr/bin/env python3
"""V2 dev結果の診断（記述のみ。データ・ルール・基準は一切変更しない）。
    python v2_diagnostics.py [--run results/v2_dev_v2a]
見たいこと: (1)Qualityが判定不能になった原因 (2)主要検証の差が特定の日に偏っていないか (3)年別の差
"""
import argparse

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--run", default="results/v2_dev_v2a")
ap.add_argument("--horizon", default="3m")
ap.add_argument("--min-n", type=int, default=10)
a = ap.parse_args()
sig = pd.read_csv(f"{a.run}/signals.csv.gz", parse_dates=["date"])
col = f"ret_{a.horizon}"
print(f"# V2 dev 診断 ({a.run}) / Gate通過 {len(sig)}行 / {sig.date.nunique()}日\n")

# (1) 判定不能の原因（複数の項目が同時に欠けることがある）
und = sig[sig.q_status == "UNDET"]
print(f"## (1) Quality判定不能 {len(und)}行 ({len(und) / len(sig):.0%})  ※1行で複数項目が欠けることがある")
miss = und[und.q_reason.fillna("").str.startswith("欠損")]
items = miss.q_reason.str.replace("欠損: ", "", regex=False).str.split("/").explode()
print(f"- 『必要項目の欠損』{len(miss)}行のうち、各項目が欠けていた割合:")
for k, v in items.value_counts().items():
    print(f"    {k}: {v / len(miss):.0%}")
other = und[~und.q_reason.fillna("").str.startswith("欠損")].q_reason.value_counts()
print("- それ以外の理由: " + ", ".join(f"{k} {v}行" for k, v in other.items()))

# (2) 主要検証(V2_A vs ALL_GATE)の日ごとの差
g, b = sig[sig.v2_grade == "A"], sig
cnt = g.groupby("date")[col].count()
keep = cnt[cnt >= a.min_n].index
d = (g[g.date.isin(keep)].groupby("date")[col].mean() - b[b.date.isin(keep)].groupby("date")[col].mean()).dropna().sort_index()
n_by = cnt.reindex(d.index)
print(f"\n## (2) 日ごとの差（V2_A - ALL_GATE, {a.horizon}後, V2_Aが{a.min_n}件以上ある{len(d)}日）")
print(f"- 平均 {d.mean():+.2%} / 中央値 {d.median():+.2%} / プラスの日 {(d > 0).mean():.0%} / 標準偏差 {d.std():.2%}")
pooled = g[col].mean() - b[col].mean()
print(f"- 参考: 全行をまとめた単純平均の差(件数で加重) {pooled:+.2%}  ← 日ごとの平均差との違いは『シグナルが多い日』の影響")
top = n_by.sort_values(ascending=False).head(10)
print(f"- V2_Aのシグナルが多い上位10日に、全シグナルの {top.sum() / cnt.sum():.0%} が集中（全{len(cnt)}日中）")
print("- シグナルが多い上位10日の差: " + ", ".join(f"{i.date()}({n_by[i]:.0f}件){d[i]:+.1%}" for i in top.index if i in d.index))
for k in (3, 5):
    print(f"- 参考(除外ではなく感度): 差の上位{k}日を除くと平均 {d.sort_values().iloc[:-k].mean():+.2%} / 下位{k}日を除くと {d.sort_values().iloc[k:].mean():+.2%}")

# (3) 年別
print("\n## (3) 年別の差（平均差 / 日数）")
y = d.groupby(d.index.year).agg(["mean", "count"])
for yr, r in y.iterrows():
    print(f"- {yr}: {r['mean']:+.2%} / {int(r['count'])}日" + ("  ←基準の対象外(6日未満)" if r["count"] < 6 else ""))

# (4) QualityとTimingの寄与(記述のみ)
print(f"\n## (4) {a.horizon}後の平均（件数加重 / 日ごと平均の差をALL_GATE比で）")
base = sig.groupby("date")[col].mean()
rows = {"ALL_GATE": sig, "Timing高のみ": sig[sig.grade.isin(["A", "B"])], "Quality HIGHのみ": sig[sig.q_status == "HIGH"],
        "Q HIGH x T高(V2_A)": g, "Q MID x T高": sig[(sig.q_status == "MID") & sig.grade.isin(["A", "B"])],
        "Q LOW x T高": sig[(sig.q_status == "LOW") & sig.grade.isin(["A", "B"])], "Q判定不能 x T高": sig[(sig.q_status == "UNDET") & sig.grade.isin(["A", "B"])]}
for k, v in rows.items():
    m = v.groupby("date")[col].agg(["mean", "count"])
    m = m[m["count"] >= a.min_n] if k != "ALL_GATE" else m
    dd = (m["mean"] - base.reindex(m.index)).mean()
    print(f"- {k}: n={len(v)} 平均 {v[col].mean():+.1%} / 日ごと差 {dd:+.2%} (日数{len(m)})")
