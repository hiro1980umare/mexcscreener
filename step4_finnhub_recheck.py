#!/usr/bin/env python3
"""STEP4冒頭: Finnhub財務指標の『正確なキー名』ごとの取得率を確認する(約2分)。
前回は正規表現でまとめたため、ROEが5年平均(roe5Y)で数えられるなど水増しがあった。今回はキー単位で数える。
    python step4_finnhub_recheck.py --n 60      # FINNHUB_API_KEY が必要
"""
import argparse
import os
import random
import re
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CAND = {
    "ROE": ["roeTTM", "roeRfy", "roe5Y"],
    "営業利益率": ["operatingMarginTTM", "operatingMarginAnnual", "operatingMargin5Y"],
    "純利益率": ["netProfitMarginTTM", "netProfitMarginAnnual", "netProfitMargin5Y"],
    "売上成長": ["revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy", "revenueGrowth3Y", "revenueGrowth5Y"],
    "EPS成長": ["epsGrowthTTMYoy", "epsGrowthQuarterlyYoy", "epsGrowth3Y", "epsGrowth5Y"],
    "FCF": ["freeCashFlowTTM", "freeCashFlowAnnual", "currentEv/freeCashFlowTTM", "currentEv/freeCashFlowAnnual",
            "pfcfShareTTM", "pfcfShareAnnual"],
    "負債": ["totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual", "longTermDebt/equityQuarterly",
             "longTermDebt/equityAnnual", "currentRatioQuarterly"],
    "PER": ["peTTM", "peAnnual", "peBasicExclExtraTTM", "peNormalizedAnnual"],
    "PEG": ["pegTTM", "forwardPEG"],
    "PSR": ["psTTM", "psAnnual"],
}
ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=60)
a = ap.parse_args()
KEY = os.getenv("FINNHUB_API_KEY", "")
if not KEY:
    raise SystemExit("FINNHUB_API_KEY が未設定です")

must = ["CROX", "DECK", "AAPL", "NVDA", "MSFT", "ENPH", "ETSY"]
mfile = Path("data/mcap.csv")
if mfile.exists():
    m = pd.read_csv(mfile, index_col=0).iloc[:, 0]
    pool = sorted(m[m >= 1e9].index)
else:  # fetch_data.py 実行前でも動くようにする
    from src.data import fetch_mcaps, load_universe
    from src.rules import load_rules
    uni = random.Random(1).sample(load_universe(load_rules("config/rules_v1.yaml")), 250)
    m = fetch_mcaps(uni)
    pool = sorted(m[m >= 1e9].index)
syms = list(dict.fromkeys(must + random.Random(7).sample(pool, min(a.n - len(must), len(pool)))))

present, vals, allkeys, ok = Counter(), {}, Counter(), 0
for i, s in enumerate(syms, 1):
    r = requests.get("https://finnhub.io/api/v1/stock/metric", params={"symbol": s, "metric": "all"},
                     headers={"X-Finnhub-Token": KEY}, timeout=30)
    if r.status_code == 429:
        time.sleep(5)
        continue
    met = (r.json() or {}).get("metric") or {} if r.status_code == 200 else {}
    if met:
        ok += 1
        for k, v in met.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                allkeys[k] += 1
        for g, ks in CAND.items():
            for k in ks:
                v = met.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    present[k] += 1
                    vals.setdefault(k, []).append(v)
    if i % 20 == 0:
        print(f"  {i}/{len(syms)}")
    time.sleep(1.05)

lines = [f"# Finnhub 財務指標 キー別取得率 (対象 {ok}/{len(syms)}銘柄, 時価総額$1B以上)", ""]
for g, ks in CAND.items():
    lines += [f"## {g}", "", "| キー | 取得率 | 中央値 |", "|---|---|---|"]
    for k in ks:
        v = sorted(vals.get(k, []))
        med = f"{v[len(v) // 2]:.3g}" if v else "-"
        lines.append(f"| {k} | {100 * present[k] / max(ok, 1):.0f}% ({present[k]}/{ok}) | {med} |")
    pat = re.compile({"ROE": "^roe", "営業利益率": "^operatingMargin", "純利益率": "^netProfitMargin", "売上成長": "^revenueGrowth",
                      "EPS成長": "^epsGrowth", "FCF": "freeCash|pfcf", "負債": "debt|currentRatio", "PER": "^pe[A-Z]|peg",
                      "PEG": "peg", "PSR": "^ps[A-Z]"}[g], re.I)
    extra = [f"{k}({c})" for k, c in allkeys.most_common() if pat.search(k) and k not in ks]
    if extra:
        lines.append(f"\n他に見つかったキー: {', '.join(extra[:12])}")
    lines.append("")
Path("api_check_output_b").mkdir(exist_ok=True)
Path("api_check_output_b/finnhub_recheck.md").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
