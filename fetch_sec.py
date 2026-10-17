#!/usr/bin/env python3
"""SECの財務(companyfacts)を取得し、V2で使う項目だけを小さく保存する。途中から再開可能。
    python fetch_sec.py            # 3,000銘柄で約30〜60分
必要: 環境変数 SEC_USER_AGENT（例: "Taro Yamada taro@example.com"）
"""
import argparse
import os
import pickle
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import requests

from src.sec_pit import compact_facts

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data_v2")
ap.add_argument("--max-tickers", type=int, default=0)
ap.add_argument("--tickers-file", default="", help="銘柄リスト(ticker列のCSV)。省略すると <data>/coverage.csv のstatus=ok")
ap.add_argument("--sec-dir", default="sec", help="保存先フォルダ名(data_v2/<名前>)。V2bは sec_v2b")
a = ap.parse_args()
UA = os.getenv("SEC_USER_AGENT", "")
if "@" not in UA:
    raise SystemExit("SEC_USER_AGENT が未設定です（例: 'Taro Yamada taro@example.com'）")
out = Path(a.data) / a.sec_dir
out.mkdir(parents=True, exist_ok=True)
if a.tickers_file:
    tick = sorted(set(pd.read_csv(a.tickers_file)["ticker"].dropna().astype(str)) - {"SPY"})
else:
    cov = pd.read_csv(Path(a.data) / "coverage.csv")
    tick = sorted(cov[(cov.status == "ok") & (cov.ticker != "SPY")].ticker)
if a.max_tickers:
    tick = tick[:a.max_tickers]
s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
last = [0.0]


def get(url):
    for attempt in range(3):
        w = 0.12 - (time.time() - last[0])
        if w > 0:
            time.sleep(w)
        last[0] = time.time()
        try:
            r = s.get(url, timeout=60)
        except Exception:  # noqa: BLE001
            time.sleep(5)
            continue
        if r.status_code in (403, 429):
            time.sleep(10 * (attempt + 1))
            continue
        return r
    return None


r = get("https://www.sec.gov/files/company_tickers.json")
if r is None or r.status_code != 200:
    raise SystemExit("ticker→CIK表を取得できません（User-Agentを確認）")
cik = {v["ticker"]: v["cik_str"] for v in r.json().values()}
st, rows, t0 = Counter(), [], time.time()
for i, t in enumerate(tick, 1):
    f = out / f"{t}.pkl"
    if f.exists():
        st["cached"] += 1
        continue
    if t not in cik:
        st["no_cik"] += 1
        rows.append((t, "no_cik"))
        continue
    r = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik[t]:010d}.json")
    if r is None or r.status_code != 200:
        code = getattr(r, "status_code", "error")
        st[f"http_{code}"] += 1
        rows.append((t, f"http_{code}"))
        continue
    with f.open("wb") as fh:
        pickle.dump(compact_facts(r.json()), fh)
    st["ok"] += 1
    if i % 100 == 0:
        print(f"  {i}/{len(tick)}  {dict(st)}  経過{(time.time() - t0) / 60:.0f}分")
failed_name = "sec_failed.csv" if a.sec_dir == "sec" else f"{a.sec_dir}_failed.csv"
pd.DataFrame(rows, columns=["ticker", "status"]).to_csv(Path(a.data) / failed_name, index=False)
n = len(tick)
have = sum((out / f"{t}.pkl").exists() for t in tick)
print(f"\n# SEC取得レポート\n- 対象 {n} / 財務を保存できた {have} ({have / max(n, 1):.1%}) / 内訳 {dict(st)}\n- 保存先: {out} / 失敗一覧: {a.data}/{failed_name}")
