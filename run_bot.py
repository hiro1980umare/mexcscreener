#!/usr/bin/env python3
"""通知Bot(候補リスト・監視ツール)。優位性は確認されていないため、売買の推奨はしない。

  python run_bot.py --source dir --data-dir data_v2 --dry-run        # 保存済みデータで試す(送信しない)
  python run_bot.py                                                  # 本番: 株価を取得 -> 判定 -> 記録 -> Telegram送信

終了コード: 0=成功/対象日は処理済み  2=凍結ファイルの不一致  3=株価が古い  4=禁止表現を検出  5=送信失敗または未送信が残っている(記録は保存済み。python resend_bot.py で未送信分だけ再送)
実行順: 凍結検査 -> データ -> 判定 -> 文面作成と禁止表現の検査 -> **記録の保存** -> 送信(記録は送信より前に残す)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.bot_data import get_panel_live, load_sec_bundle, load_sec_dir, load_universe_file, sec_updated
from src.bot_format import build_units, check_language, earnings_view, grade_after_earnings, quality_rank, sort_candidates, top_percent
from src.bot_records import already_done, sha256_file, sha256_text, save_day, verify_manifest
from src.bot_send import deliver, unsent_count
from src.data_v2 import load_v2
from src.earnings import get_earnings, reconcile, summarize_earnings, to_date
from src.indicators import build
from src.rules import load_rules
from src.tgram import init_state, make_parts
from src.v2 import check_gate_consistency, evaluate_day_v2

def die(code, msg):
    """メッセージをstderrに出して、仕様どおりの終了コードで止まる。"""
    print(msg, file=sys.stderr)
    sys.exit(code)


ap = argparse.ArgumentParser()
ap.add_argument("--spec", default="config/bot_spec.yaml")
ap.add_argument("--manifest", default="config/bot_manifest.json")
ap.add_argument("--source", choices=["live", "dir"], default="live")
ap.add_argument("--data-dir", default="data_v2")
ap.add_argument("--sec-dir", default="")
ap.add_argument("--sec-bundle", default="", help="bundle_sec.py で作った、SEC財務をまとめたファイル(読み込みが速い)")
ap.add_argument("--asof", default="", help="判定日(省略=データの最終日)")
ap.add_argument("--today", default="", help="テスト用: 今日の日付を固定")
ap.add_argument("--dry-run", action="store_true", help="送信しない(画面に表示)")
ap.add_argument("--rehearsal", action="store_true", help="送信テスト: 実際に送信するが、記録は rehearsal_<日付> に別保存し、通知文の先頭にリハーサルと明記する")
ap.add_argument("--write-records", action="store_true", help="dry-runでも記録を保存する(dryrun_ という名前で)")
ap.add_argument("--no-earnings", action="store_true", help="決算日を取得しない(全て『取得できず』と表示)")
ap.add_argument("--earnings-file", default="", help="手動指定の決算日 {ticker: 'YYYY-MM-DD'} のJSON(任意)")
ap.add_argument("--max-universe", type=int, default=0)
a = ap.parse_args()
t0 = time.time()
today = datetime.strptime(a.today, "%Y-%m-%d").date() if a.today else date.today()


def log(msg):
    """進捗(標準エラー出力。通知文の標準出力を汚さない)。実データでは読み込みに数分かかることがある。"""
    print(f"[{time.time() - t0:6.1f}秒] {msg}", file=sys.stderr, flush=True)


log("開始(凍結ファイルを検査します)")

# ---- 1. 凍結ファイルの検査(研究報告書・ルール・基準・仕様) ----
bad = verify_manifest(a.manifest)
if bad:
    die(2, "【中止】凍結したファイルが一致しません(勝手な変更の防止):\n  - " + "\n  - ".join(bad))
spec = yaml.safe_load(Path(a.spec).read_text(encoding="utf-8"))
R1 = load_rules(spec["rules"]["timing_v1"])
R2 = yaml.safe_load(Path(spec["rules"]["v2b"]).read_text(encoding="utf-8"))
assert R2["version"] == "v2b" and R2["quality"]["mode"] == "rank_v2b"
check_gate_consistency(R1, R2)
meta_hash = {"rules_v1_sha8": sha256_file(spec["rules"]["timing_v1"])[:8], "rules_v2b_sha8": sha256_file(spec["rules"]["v2b"])[:8],
             "report_sha8": sha256_file(spec["research_report"])[:8]}

# ---- 2. データ ----
log("凍結ファイルは一致しました。株価を読み込みます(実データは数百MB。ドライブ上だと1〜3分かかることがあります)")
if not Path(spec["universe"]["file"]).exists():
    die(1, f"【中止】銘柄リスト({spec['universe']['file']})がありません。python export_universe.py で作成し、コミットしてください。")
tickers = load_universe_file(spec["universe"]["file"], a.max_universe)
workdir = tempfile.mkdtemp()
if a.source == "live":
    panel, _ = get_panel_live(tickers, today, spec["prices"]["lookback_calendar_days"], workdir)
else:
    panel = load_v2(a.data_dir)
    keep = [t for t in tickers if t in panel.close.columns] if tickers else list(panel.close.columns)
    panel.open, panel.close, panel.close_raw, panel.volume = (x[keep] for x in (panel.open, panel.close, panel.close_raw, panel.volume))
sec_dir = a.sec_dir or spec["sec"]["dir"]
log(f"株価を読み込みました({panel.close.shape[1]}銘柄 x {panel.close.shape[0]}日)。SEC財務を読み込みます(約{panel.close.shape[1]}ファイル。ドライブ上だと数分かかることがあります)")
if a.sec_bundle:
    sec = load_sec_bundle(a.sec_bundle, panel.close.columns)
    sec_upd = date.fromtimestamp(Path(a.sec_bundle).stat().st_mtime)
else:
    sec = load_sec_dir(sec_dir, panel.close.columns, progress=log)
if not a.sec_bundle:
    sec_upd = sec_updated(sec_dir)
if not sec:
    sys.exit(f"【中止】SEC財務キャッシュ({sec_dir})がありません。先に週次のSEC更新(fetch_sec.py)を実行してください。")
MIN_SEC_COVERAGE = 0.8      # 対象銘柄のうちSEC財務がある割合の下限(実データは約99%)
if len(sec) / max(panel.close.shape[1], 1) < MIN_SEC_COVERAGE:
    die(1, f"【中止】SEC財務が対象銘柄の{len(sec) / max(panel.close.shape[1], 1):.0%}({len(sec)}/{panel.close.shape[1]}銘柄)しかありません。"
           f"コピーや取得が途中で止まった可能性があります。{sec_dir} のファイル数を確認してください(通常は約2,900)。")
log(f"SEC財務を読み込みました({len(sec)}銘柄)。指標を計算します")
ind = build(panel, R1)
log("指標を計算しました。判定します")
idx = np.flatnonzero(ind.dates <= pd.Timestamp(a.asof)) if a.asof else np.arange(len(ind.dates))
if len(idx) == 0:
    sys.exit("【中止】指定した判定日より前のデータがありません")
i = int(idx[-1])
asof = ind.dates[i].date()
if not a.asof and (today - asof).days > spec["data_freshness"]["max_age_calendar_days"]:
    die(3, f"【中止】株価の最終日が古すぎます({asof}、今日 {today})。データ取得に失敗した可能性があります(通知しません)。")
if a.rehearsal and a.dry_run:
    die(1, "【中止】--rehearsal と --dry-run は同時に指定できません(リハーサルは実際に送信します)。")
rec_name = f"rehearsal_{asof}" if a.rehearsal else (str(asof) if not a.dry_run else f"dryrun_{asof}")
if a.rehearsal and already_done(spec["records_dir"], rec_name):
    die(1, f"【中止】リハーサルの記録({rec_name})はすでにあります。2回目の送信を避けるため、止めます。")
if not a.dry_run and not a.rehearsal and already_done(spec["records_dir"], asof):
    left = unsent_count(Path(spec["records_dir"]) / str(asof))
    if left:
        die(5, f"{asof} は処理済みですが、未送信の通が{left}通あります(再計算・二重通知はしません)。python resend_bot.py --record {spec['records_dir']}/{asof} で未送信分だけ再送できます。")
    print(f"{asof} は処理済みです(二重通知を避けるためスキップ)。")
    sys.exit(0)

# ---- 3. 判定(バックテストと同じ関数) ----
df = evaluate_day_v2(panel, ind, R1, R2, sec, i)
if df.empty:
    sys.exit("【中止】Gateを通過した銘柄が0件です。データを確認してください。")
df["t_grade"] = df["grade"]
df["close_raw"] = panel.close_raw.iloc[i].reindex(df.index)
df["q_top_pct"] = top_percent(df["q_score"])
df["q_rank"] = quality_rank(df["q_score"])
A = sort_candidates(df[df.v2_grade == "A"])                                         # 区分に入る銘柄の条件は従来のまま。並びと同点数だけ
U = sort_candidates(df[(df.q_status == "UNDET") & df.grade.isin(["A", "B"])])
df.loc[A.index, "timing_tie_n"] = A["timing_tie_n"]
df.loc[U.index, "timing_tie_n"] = U["timing_tie_n"]
shownA, shownU = A.head(spec["display"]["max_items_quality_high"]), U.head(spec["display"]["max_items_quality_undetermined"])
shown_tickers = list(shownA.index) + list(shownU.index)

log(f"判定しました(Gate通過{len(df)}銘柄)。決算日と通知文を作ります")
# ---- 4. 決算日(2情報源を照合。取れなければ判定不能) ----
if a.no_earnings:
    earn = {t: {"date": None, "status": "UNAVAILABLE", "note": "取得できず(判定不能)", "errors": ["未取得(--no-earnings)"]} for t in shown_tickers}
elif a.earnings_file:
    man = json.loads(Path(a.earnings_file).read_text())
    earn = {t: dict(reconcile(to_date(man.get(t)), None), errors=[], note="手動指定") if man.get(t) else
            {"date": None, "status": "UNAVAILABLE", "note": "取得できず(判定不能)", "errors": []} for t in shown_tickers}
    for t in earn:
        if earn[t]["date"]:
            earn[t]["status"] = "MANUAL"
else:
    fh_key = os.getenv("FINNHUB_API_KEY", "")
    log(f"決算日を取得します(Finnhubキー: {'あり(長さ' + str(len(fh_key)) + ')' if fh_key else 'なし'})")
    earn = get_earnings(shown_tickers, today, fh_key)
    log(summarize_earnings(earn))
n_near = n_no = 0
for t in shown_tickers:
    _, days, near = earnings_view(earn.get(t), today, R1)
    n_near += bool(near)
    n_no += earn.get(t, {}).get("date") is None
    e = earn.get(t, {})
    df.loc[t, "earnings_date"] = str(e["date"]) if e.get("date") else ""
    df.loc[t, "earnings_status"] = e.get("status", "")
    df.loc[t, "earnings_errors"] = "; ".join(e.get("errors", []))
    df.loc[t, "days_to_earnings"] = days if days is not None else np.nan
    df.loc[t, "v2_grade_after_earnings"] = grade_after_earnings(df.loc[t, "v2_grade"], near)
df["section"] = np.where(df.index.isin(shownA.index), "A_QualityHigh_TimingHigh", np.where(df.index.isin(shownU.index), "U_QualityUndetermined_TimingHigh", ""))
df["notified"] = df.index.isin(shown_tickers)

# ---- 5. 文面と禁止表現の検査 ----
n_q = int((df.q_status != "UNDET").sum())
stats = {"n_gate": len(df), "n_q": n_q, "q_rate": n_q / len(df), "n_timing": int(df.grade.isin(["A", "B"]).sum()), "n_near": n_near, "n_no_earn": n_no}
price_last = str(asof)
units = build_units({"rehearsal": a.rehearsal, "asof": asof, "today": today, "R1": R1, "spec": spec, "stats": stats, "earn": earn, "meta": meta_hash,
                     "price_last": price_last, "sec_updated": str(sec_upd) if sec_upd else "不明", "sec_old": bool(sec_upd and (today - sec_upd).days > spec["sec"]["warn_age_days"]),
                     "sections": [("Quality高 × Timing高(V2判定A)", A, shownA), ("Timing高だが Quality判定不能", U, shownU)]})
msg = "\n".join(units)
parts = make_parts(units)                                                           # 銘柄の途中で切らない。2通以上なら「[1/3]」を付ける
hits = check_language(msg, spec["language"]["forbidden"])
if hits:
    die(4, f"【中止】通知文に禁止表現が含まれています: {hits}(優位性が確認されていないため送信しません)")

# ---- 6. 記録(送信より前に保存) ----
df["asof"] = str(asof)
df["rules_version"], df["rules_v2b_sha8"], df["rules_v1_sha8"], df["report_sha8"] = "v2b", meta_hash["rules_v2b_sha8"], meta_hash["rules_v1_sha8"], meta_hash["report_sha8"]
try:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
except Exception:  # noqa: BLE001
    commit = None
meta = {"asof": str(asof), "run_date": str(today), "mode": "dry-run" if a.dry_run else ("rehearsal" if a.rehearsal else "live"), "source": a.source, "price_last_date": price_last,
        "n_universe": len(tickers), "n_prices": int(panel.close.shape[1]), "n_sec": len(sec), "sec_dir": sec_dir, "sec_updated": str(sec_upd),
        "n_gate": stats["n_gate"], "n_quality_determinable": n_q, "n_timing_high": stats["n_timing"], "n_notified": len(shown_tickers),
        "rules": meta_hash, "spec_version": spec["version"], "manifest_sha8": sha256_file(a.manifest)[:8], "git_commit": commit,
        "message_sha256": sha256_text(msg), "language_check": "ok", "send_status": "not_sent" if a.dry_run else "pending", "n_parts": len(parts),
        "send_parts": None if a.dry_run else init_state(len(parts)),
        "earnings_unavailable": n_no, "elapsed_sec": round(time.time() - t0, 1)}
rec_dir = None
if a.dry_run and a.write_records and (Path(spec["records_dir"]) / rec_name).exists():
    print(f"[dry-run] 記録 {spec['records_dir']}/{rec_name} はすでにあるため、上書きせず保存をスキップします。")
elif (not a.dry_run) or a.write_records:
    rec_dir = save_day(spec["records_dir"], rec_name, df.drop(columns=[c for c in df.columns if c.startswith("_")]), df[df.notified], msg, meta, parts=parts)

log("記録を保存しました。出力します")
# ---- 7. 出力・送信 ----
if a.dry_run:
    print(msg)
    print(f"\n[dry-run] 送信していません。Gate通過{stats['n_gate']} / 表示{len(shown_tickers)} / 送信予定{len(parts)}通(各{'・'.join(str(len(x)) for x in parts)}字) / 所要{meta['elapsed_sec']}秒" + (f" / 記録: {rec_dir}" if rec_dir else ""))
    sys.exit(0)
status, err = deliver(rec_dir, os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""))   # 通ごとに送信状況を記録
if err:
    print(f"【送信失敗】{err}\n記録は保存済み({status})。未送信分だけ再送するには: python resend_bot.py --record {rec_dir}", file=sys.stderr)
    sys.exit(5)
print(f"送信しました({len(parts)}通)。記録: {rec_dir}")
