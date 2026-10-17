#!/usr/bin/env python3
"""データ取得（STEP4b: 取得状況を必ず表示・保存する版）。ルール判定は一切しない。

    python fetch_data.py                     # 本番
    python fetch_data.py --max-tickers 300   # お試し(★お試しデータでfinalは実行しない)

保存物:
    data/fetch_report.md      取得状況の要約（銘柄数・成功数・失敗数・成功率）
    data/failed_tickers.csv   失敗/履歴不足の銘柄一覧（段階と理由つき）
    data/price_coverage.csv   全銘柄の取得行数・最初と最後の日付
"""
import argparse
import random
import time

import pandas as pd

from src.data import download_robust, fetch_mcaps_robust, load_meta, load_universe, save_panel
from src.rules import load_rules

ap = argparse.ArgumentParser()
ap.add_argument("--rules", default="config/rules_v1.yaml")
ap.add_argument("--max-tickers", type=int, default=0)
ap.add_argument("--data-dir", default="data")
ap.add_argument("--refresh-mcap", action="store_true", help="時価総額を全て取り直す")
ap.add_argument("--refresh-prices", action="store_true", help="株価を全て取り直す（既定: 同日に取得済みの銘柄は再利用）")
a = ap.parse_args()

R = load_rules(a.rules)
from pathlib import Path  # noqa: E402
DD = Path(a.data_dir)
DD.mkdir(exist_ok=True)
rep = []


def say(*lines):
    for s in lines:
        print(s)
        rep.append(s)


# ---- 1. 銘柄リスト ----
uni = load_universe(R)
say(f"# データ取得レポート ({time.strftime('%Y-%m-%d %H:%M')})", "",
    f"- 銘柄リスト(ETF・ワラント等除外後): {len(uni)}銘柄")

# ---- 2. 時価総額（失敗分だけ最大3回やり直す） ----
mfile = DD / "mcap.csv"
if mfile.exists() and not a.refresh_mcap:
    mcap = pd.read_csv(mfile, index_col=0).iloc[:, 0].reindex(uni)
    print(f"時価総額キャッシュ使用（未取得 {int(mcap.isna().sum())}銘柄は取り直す）")
else:
    mcap = pd.Series(float("nan"), index=uni, name="market_cap")
mcap = fetch_mcaps_robust(mcap, cache_file=mfile)
n_cap_ok, n_cap_ng = int(mcap.notna().sum()), int(mcap.isna().sum())
say(f"- 時価総額: 取得成功 {n_cap_ok} / 取得失敗 {n_cap_ng}  (成功率 {n_cap_ok / len(uni):.1%})")
target = [t for t in uni if mcap[t] >= R["universe"]["current_market_cap_min"]]
say(f"- 現在の時価総額が下限(${R['universe']['current_market_cap_min'] / 1e6:.0f}M)以上 = **株価取得の対象: {len(target)}銘柄**")
if a.max_tickers and len(target) > a.max_tickers:
    target = sorted(random.Random(42).sample(target, a.max_tickers))
    say(f"- ⚠ --max-tickers により {len(target)}銘柄に制限（お試しデータ）")

# ---- 3. 株価（失敗分は小さいチャンクでやり直し。同日に取得済みの銘柄は再利用） ----
reuse, o0, c0, v0, cov0, have = False, None, None, None, None, set()
cfile = DD / "close.pkl"
if cfile.exists() and not a.refresh_prices and (DD / "price_coverage.csv").exists():
    saved = load_meta(DD)["saved_at"][:10]
    if saved == time.strftime("%Y-%m-%d"):
        o0, c0, v0 = (pd.read_pickle(DD / f"{n}.pkl") for n in ("open", "close", "volume"))
        spy0 = pd.read_pickle(DD / "spy.pkl")
        o0["SPY"], c0["SPY"], v0["SPY"] = spy0["open"], spy0["close"], 0.0
        cov0 = pd.read_csv(DD / "price_coverage.csv")
        have = set(cov0.loc[cov0.status == "ok", "ticker"]) & set(c0.columns)
        reuse = True
        say(f"- 同日に取得済みの株価を再利用: {len(have & set(target))}銘柄（残りだけダウンロード）")
    else:
        print(f"保存済みの株価は{saved}のものなので使わず、全て取り直します（日付が混ざるのを防ぐため）")
need = [t for t in target if t not in have]
if need or not reuse:
    o1, c1, v1, cov1 = download_robust(need + ["SPY"] if not reuse else need, R["universe"]["price_history_start"])
else:
    o1 = c1 = v1 = pd.DataFrame()
    cov1 = pd.DataFrame(columns=["ticker", "n_rows", "first_date", "last_date", "status", "reason"])
if reuse:
    keep0 = [t for t in c0.columns if t in have or t == "SPY"]
    o, c, v = (pd.concat([x[keep0], y], axis=1).sort_index() for x, y in ((o0, o1), (c0, c1), (v0, v1)))
    cov = pd.concat([cov0[cov0.ticker.isin(have | {"SPY"})], cov1], ignore_index=True)
    # 再利用した株価と新規取得分で最終日が違う場合は、短い方にそろえる(銘柄ごとに最終日が違うデータを作らない)
    if len(c1.columns) and c0.index.max() != c1.index.max():
        end_common = min(c0.index.max(), c1.index.max())
        say(f"- ⚠ 再利用分の最終日({c0.index.max().date()})と新規取得分({c1.index.max().date()})が違うため、{end_common.date()}にそろえました")
        o, c, v = (x.loc[:end_common] for x in (o, c, v))
else:
    o, c, v, cov = o1, c1, v1, cov1
cov.to_csv(DD / "price_coverage.csv", index=False)
stk = cov[(cov.ticker != "SPY") & cov.ticker.isin(target)]
n_ok = int((stk.status == "ok").sum())
n_short = int((stk.status == "short_history").sum())
n_fail = int((stk.status == "failed").sum())
n_tgt = len(stk)
say(f"- 株価取得: 対象 {n_tgt} / **成功(252日以上) {n_ok}** / 履歴不足(252日未満・指標が作れない) {n_short} / **失敗 {n_fail}**",
    f"- **成功率: {n_ok / max(n_tgt, 1):.1%}**（履歴不足を除いた取得成功率: {(n_ok + n_short) / max(n_tgt, 1):.1%}）")
spy_ok = bool((cov[cov.ticker == "SPY"].status == "ok").any())
say(f"- SPY: {'成功' if spy_ok else '★失敗'}")

end = pd.Timestamp(c.index.max())
ok_cov = stk[stk.status == "ok"].copy()
ok_cov["last_date"] = pd.to_datetime(ok_cov["last_date"])
stale = ok_cov[ok_cov.last_date < end - pd.Timedelta(days=10)]
say(f"- 最終日がデータ末({end.date()})より10日以上古い銘柄: {len(stale)}  → {list(stale.ticker[:15])}")

fails = [dict(stage="mcap", ticker=t, reason="時価総額を取得できない") for t in mcap[mcap.isna()].index]
fails += [dict(stage="price", ticker=r.ticker, reason=f"{r.status}: {r.reason}") for r in stk[stk.status != "ok"].itertuples()]
pd.DataFrame(fails, columns=["stage", "ticker", "reason"]).to_csv(DD / "failed_tickers.csv", index=False)
say(f"- 失敗/履歴不足の一覧: data/failed_tickers.csv ({len(fails)}件)")
if n_fail:
    top = stk[stk.status == "failed"].reason.value_counts().head(5)
    say("- 失敗理由の内訳(上位): " + "; ".join(f"{k}: {n}件" for k, n in top.items()))

if not spy_ok:
    raise SystemExit("SPYが取得できていないため保存しません。時間をおいて再実行してください。")
save_panel(o, c, v, mcap.dropna(), DD)
say(f"- 保存: {c.shape[1] - 1}銘柄, {c.index.min().date()} ~ {c.index.max().date()}")
(DD / "fetch_report.md").write_text("\n".join(rep), encoding="utf-8")
