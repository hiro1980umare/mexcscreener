#!/usr/bin/env python3
"""今日(データ最終日)のスクリーニング。Telegram通知はSTEP6。決算前チェックはA/B/C候補にだけ実施。"""
import argparse
from datetime import date, datetime, timedelta

import pandas as pd

from src.data import load_meta, load_panel
from src.indicators import build
from src.rules import load_rules
from src.signals import downgrade, evaluate_at

ap = argparse.ArgumentParser()
ap.add_argument("--rules", default="config/rules_v1.yaml")
ap.add_argument("--top", type=int, default=10)
a = ap.parse_args()
R = load_rules(a.rules)
panel, meta = load_panel(), load_meta()
ind = build(panel, R)
i = len(ind.dates) - 1
df = evaluate_at(ind, i, R)
print(f"データ最終日: {ind.dates[i].date()}  Gate通過: {len(df)}銘柄  (rules {R['version']})")
if (date.today() - ind.dates[i].date()).days > 4:
    print("⚠ データが古い可能性があります。fetch_data.py を再実行してください。")


def next_earnings(t):
    try:
        import yfinance as yf
        eds = yf.Ticker(t).calendar.get("Earnings Date") or []
        eds = [d for d in eds if d >= date.today()]
        return min(eds) if eds else None
    except Exception:  # noqa: BLE001
        return None


cand = df[df.grade.isin(["A", "B", "C"])].sort_values(["grade", "score"], ascending=[True, False]).head(a.top * 3)
rows = []
blk = R["earnings"]["block_days_before"]
for t, r in cand.iterrows():
    ne = next_earnings(t)
    soon = ne is not None and (ne - date.today()).days <= blk
    g = downgrade(r.grade) if (soon and R["earnings"]["applied_in_live"]) else r.grade
    rows.append(dict(ticker=t, grade=g, raw_grade=r.grade, score=r.score, types=r.types or "-", rsi=round(r.rsi, 1),
                     dd_high=f"{r.dd_high:.1%}", vs50=f"{r.vs_sma50:+.1%}", vs200=f"{r.vs_sma200:+.1%}",
                     vol_ratio=round(r.vol_ratio, 2), red_flags=int(r.rf_count),
                     next_earnings=str(ne) if ne else "不明", earnings_soon=soon, close=round(r.close, 2),
                     sma20=round(r.close / (1 + r.vs_sma20), 2), sma50=round(r.close / (1 + r.vs_sma50), 2)))
res = pd.DataFrame(rows)
if not res.empty:
    res = res.sort_values(["grade", "score"], ascending=[True, False]).head(a.top)
    pd.set_option("display.width", 200)
    print(res.to_string(index=False))
    res.to_csv(f"results/screen_{ind.dates[i].date()}.csv", index=False)
    print("\n※ v1はテクニカルのみ。業績・財務の良し悪しは評価していません。投資判断はご自身で。")
else:
    print("A/B/C候補なし")
