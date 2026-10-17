"""時価総額を取得できなかった銘柄の原因を診断する（データは変更しない）。
    python diagnose_mcap_failures.py          # 150銘柄をサンプルして約5分
"""
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

sys.path.insert(0, ".")


def names():
    base, d = "https://www.nasdaqtrader.com/dynamic/SymDir/", {}
    for fn in ("nasdaqlisted.txt", "otherlisted.txt"):
        lines = requests.get(base + fn, timeout=30).text.strip().splitlines()
        h = lines[0].split("|")
        for ln in lines[1:]:
            f = dict(zip(h, ln.split("|")))
            s = f.get("Symbol") or f.get("ACT Symbol")
            if s:
                d[s] = f.get("Security Name", "")
    return d


def probe(t):
    import yfinance as yf
    time.sleep(0.3)
    r, tk = {"ticker": t}, yf.Ticker(t)
    try:
        fi = tk.fast_info
        r["fast_mcap"], r["last_price"], r["shares"] = (getattr(fi, k, None) for k in ("market_cap", "last_price", "shares"))
    except Exception as e:  # noqa: BLE001
        r["err"] = f"{type(e).__name__}: {str(e)[:50]}"
    try:
        r["info_mcap"] = (tk.info or {}).get("marketCap")
    except Exception as e:  # noqa: BLE001
        r["err"] = r.get("err", "") + f" info:{type(e).__name__}"
    try:
        r["has_price"] = len(tk.history(period="5d")) > 0
    except Exception:  # noqa: BLE001
        r["has_price"] = False
    return r


failed = pd.read_csv("data/failed_tickers.csv")
failed = failed[failed.stage == "mcap"].ticker.tolist()
sample = random.Random(1).sample(failed, min(150, len(failed)))
nm = names()
print(f"時価総額の取得失敗: {len(failed)}銘柄 → {len(sample)}銘柄を診断します")
with ThreadPoolExecutor(3) as ex:
    rows = list(ex.map(probe, sample))
df = pd.DataFrame(rows)
for c in ("fast_mcap", "info_mcap", "last_price", "shares"):
    if c not in df:
        df[c] = None


def cat(r):
    if r.fast_mcap and r.fast_mcap > 0:
        return "A 今回は取得できた(一時的な失敗だった)"
    if r.info_mcap and r.info_mcap > 0:
        return "B info経由なら取得できた"
    if r.has_price and r.last_price and r.shares:
        return "C 価格と株式数はある(時価総額は計算可能)"
    if r.has_price:
        return "D 価格はあるが時価総額も株式数も無い"
    return "E 価格も取れない(上場廃止・コード不一致など)"


df["category"] = df.apply(cat, axis=1)
df["mcap_best"] = [r.fast_mcap or r.info_mcap or ((r.last_price or 0) * (r.shares or 0)) or None for r in df.itertuples()]
df["name"] = df.ticker.map(nm).str[:42]
n = len(df)
print("\n## 原因の内訳（サンプル）")
for k, v in sorted(Counter(df.category).items()):
    print(f"- {k}: {v}件 ({v / n:.0%}) → 全{len(failed)}件に当てはめると約{round(v / n * len(failed))}件")
big = df[df.mcap_best.fillna(0) >= 1e9]
print(f"\n- 取れた時価総額が$1B以上: {len(big)}件 ({len(big) / n:.0%}) → 全体で約{round(len(big) / n * len(failed))}件の取りこぼし")
errs = Counter(str(e)[:40] for e in df.get("err", pd.Series(dtype=str)).dropna())
print(f"- エラーメッセージ上位: {errs.most_common(4)}")
print("\n## 例（各カテゴリから最大6件）")
pd.set_option("display.width", 200, "display.max_colwidth", 44)
for k, g in df.groupby("category"):
    print(f"\n[{k}]")
    print(g.head(6)[["ticker", "name", "mcap_best"]].to_string(index=False))
df.to_csv("data/mcap_failure_diagnosis.csv", index=False)
print("\n保存: data/mcap_failure_diagnosis.csv")
