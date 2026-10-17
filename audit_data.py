#!/usr/bin/env python3
"""株価データ品質の監査。★検出と分類だけで、データは一切変更しない。
    python audit_data.py            # data/ のデータを監査
保存物(data/audit/): anomalies_daily.csv, basic_checks.csv, audit_report.md
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from src.audit import basic_checks, classify, find_anomalies
from src.data import load_meta, load_panel

ap = argparse.ArgumentParser()
ap.add_argument("--high", type=float, default=0.8, help="HIGH閾値(日次±80%)")
ap.add_argument("--watch", type=float, default=0.5, help="WATCH閾値(日次±50%)")
ap.add_argument("--max-split-lookup", type=int, default=700, help="分割記録を調べる銘柄数の上限")
a = ap.parse_args()

P, meta = load_panel(), load_meta()
out = Path("data/audit")
out.mkdir(parents=True, exist_ok=True)
lines = [f"# データ品質監査 (データ期間 {meta['data_start']} ~ {meta['data_end']}, {meta['n_tickers']}銘柄)", ""]

an = find_anomalies(P.close, P.open, P.volume, a.high, a.watch)
hi, wa = (an.level == "HIGH").sum(), (an.level == "WATCH").sum()
lines += [f"- 日次リターン ±{a.high:.0%}以上(HIGH): **{hi}件** / ±{a.watch:.0%}〜{a.high:.0%}(WATCH): {wa}件",
          f"- 異常日のある銘柄数: {an.ticker.nunique()}", ""]

# 分割記録の確認（HIGH優先、WATCHも含めて上限まで）
tick = list(dict.fromkeys(list(an[an.level == "HIGH"].ticker) + list(an[an.level == "WATCH"].ticker)))[:a.max_split_lookup]


def get_splits(t):
    try:
        import yfinance as yf
        s = yf.Ticker(t).splits
        return t, (s if s is not None else pd.Series(dtype=float))
    except Exception:  # noqa: BLE001
        return t, None


with ThreadPoolExecutor(6) as ex:
    spl = dict(ex.map(get_splits, tick))
rows = [classify(r, spl.get(r["ticker"])) for r in an.to_dict("records")]
res = pd.DataFrame(rows)
if not res.empty:
    res = res.sort_values(["level", "ticker", "date"], ascending=[True, True, True])
res.to_csv(out / "anomalies_daily.csv", index=False)

def short(c):
    return c.split(" ")[0]


if res.empty:
    lines.append("異常値は見つかりませんでした。")
else:
    res["cls"] = res.classification.map(short)
    H = res[res.level == "HIGH"]
    lines += ["## 分類別の件数", "", "| 分類 | HIGH(±80%以上) | WATCH(±50〜80%) |", "|---|---|---|"]
    for k in ["ADJUSTMENT_SUSPECT", "SPLIT_NEAR_RATIO_DIFFERS", "GLITCH_SUSPECT", "UNEXPLAINED", "REAL_MOVE_LIKELY"]:
        lines.append(f"| {k} | {(H.cls == k).sum()} | {((res.level == 'WATCH') & (res.cls == k)).sum()} |")
    lines += ["", "分類の意味: ADJUSTMENT/GLITCH/SPLIT_NEAR/UNEXPLAINED = **確認が必要**、REAL_MOVE_LIKELY = 出来高急増を伴う実際の値動きの可能性が高い。"
              "分類は確認の優先順位のためで、**除外の根拠ではありません**。", ""]
    todo = H[H.cls != "REAL_MOVE_LIKELY"]
    lines += [f"## 確認が必要なHIGH（全{len(todo)}件）", "",
              "| 銘柄 | 日付 | 前日終値 | 終値 | 変化 | 翌日変化 | 出来高/平常 | 分割記録 | 分類 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in todo.itertuples():
        lines.append(f"| {r.ticker} | {r.date} | {r.prev_close} | {r.close} | {r.ret:+.0%} | "
                     f"{'-' if pd.isna(r.next_ret) else format(r.next_ret, '+.0%')} | "
                     f"{'-' if pd.isna(r.vol_spike) else format(r.vol_spike, '.1f') + '倍'} | "
                     f"{'あり ' + format(r.split_ratio_recorded, '.3g') if r.split_recorded_near else 'なし'} | {r.cls} |")
    top = H[H.cls == "REAL_MOVE_LIKELY"].reindex(H[H.cls == "REAL_MOVE_LIKELY"].ret.abs().sort_values(ascending=False).index).head(15)
    lines += ["", "## REAL_MOVE_LIKELY の上位15件（全件は anomalies_daily.csv）", "",
              "| 銘柄 | 日付 | 変化 | 出来高/平常 |", "|---|---|---|---|"]
    for r in top.itertuples():
        lines.append(f"| {r.ticker} | {r.date} | {r.ret:+.0%} | {r.vol_spike:.1f}倍 |")

bc = basic_checks(P.close, P.volume, P.open)
bc.to_csv(out / "basic_checks.csv")
lines += ["", "## 基本チェック（銘柄数）",
          f"- 価格が0以下の日がある: {(bc.nonpositive_price_days > 0).sum()}銘柄",
          f"- 始値が0以下の日がある: {(bc.nonpositive_open_days > 0).sum()}銘柄（0除算の警告の原因）",
          f"- 同じ終値が10日以上続く: {(bc.max_flat_run_days >= 10).sum()}銘柄",
          f"- 出来高0の日が10日以上: {(bc.zero_volume_days >= 10).sum()}銘柄",
          f"- 途中の欠損日が5日以上: {(bc.inner_missing_days >= 5).sum()}銘柄", "",
          "## 次の手順",
          "- ★このスクリプトはデータを変更しません。異常日ごとに原因を確認してください（分割・買収・倒産・データ誤り）。",
          "- 原因が確認できたものだけ、`data/audit/approved_fixes.csv` に理由つきで書くと、バックテストがそれを適用します。",
          "  列: ticker, action(drop_ticker | nan_range), date_from, date_to, reason"]
(out / "audit_report.md").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
