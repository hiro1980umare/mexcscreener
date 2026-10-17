#!/usr/bin/env python3
"""バックテスト実行。ルール凍結のガード付き。
    python run_backtest.py --rules config/rules_v1.yaml --period dev     # 動作確認用(何度でも可・記録される)
    python run_backtest.py --rules config/rules_v1.yaml --period final   # 最終検証(1回きり・実行後ロック)
"""
import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

from src.backtest import collect_signals, make_groups, summarize, to_markdown
from src.data import apply_approved_fixes, load_meta, load_panel
from src.indicators import build
from src.rules import load_rules

LOCK = Path("results/holdout_lock.json")
RUNLOG = Path("results/run_log.csv")


def git(*args):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip()
    except Exception:  # noqa: BLE001
        return 1, ""


def git_state(path):
    """(commit, clean?, in_git?)  ルールファイルがコミット済みで未変更ならclean。"""
    rc, _ = git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return None, False, False
    _, commit = git("rev-parse", "HEAD")
    rc_tracked, _ = git("ls-files", "--error-unmatch", path)
    _, status = git("status", "--porcelain", "--", path)
    return commit, (rc_tracked == 0 and status == ""), True


ap = argparse.ArgumentParser()
ap.add_argument("--rules", required=True)
ap.add_argument("--period", choices=["dev", "final"], required=True)
ap.add_argument("--allow-dirty", action="store_true", help="devのみ。ルールが未コミットでも実行(記録に残る)")
ap.add_argument("--label", default="", help="再実行の識別名（例: datafix1）。結果は results/v1_dev_<label>/ に別保存")
ap.add_argument("--reason", default="", help="再実行の理由（例: データ修正によるv1再実行）。--label指定時は必須")
a = ap.parse_args()
if a.label and not a.reason:
    sys.exit("【中止】--label を付けるときは --reason（再実行の理由）も必須です。")
if a.label and a.period == "final":
    sys.exit("【中止】最終検証期間は再実行できません（--label付きのfinalは禁止）。")

R = load_rules(a.rules)
frozen = Path(a.rules).with_suffix(".sha256")
if frozen.exists():
    want = frozen.read_text().split()[0]
    if want != R["_sha256"]:
        sys.exit(f"【中止】ルールが凍結時から変更されています。\n  凍結: {want}\n  現在: {R['_sha256']}\n"
                 "ルールを変えたい場合は rules_v2.yaml を新規作成してください。")
commit, clean, in_git = git_state(a.rules)
if not clean:
    if a.period == "final":
        sys.exit("【中止】final期間は、ルールファイルをGitにコミットし、変更が無い状態でのみ実行できます。")
    if not a.allow_dirty:
        sys.exit("【中止】ルールファイルが未コミット(またはGit管理外)です。先にコミットしてください。"
                 "(動作確認だけなら --allow-dirty。ただし記録に dirty と残ります)")
if a.period == "final" and LOCK.exists():
    sys.exit(f"【中止】最終検証期間は使用済みです: {LOCK.read_text()}\n"
             "ルールを変更した場合、この期間はもう未使用ではありません。新しい検証期間(将来データ)で確認してください。")

panel, meta_data = load_panel(), load_meta()
FIXES = Path("data/audit/approved_fixes.csv")
panel, applied_fixes = apply_approved_fixes(panel, FIXES)
if applied_fixes:
    print(f"★ 承認済みデータ修正を {len(applied_fixes)}件 適用しました（{FIXES}）")
ind = build(panel, R)
P = R["backtest"]["periods"][a.period]
sig, spy_df = collect_signals(panel, ind, R, P["signal_start"], P["signal_end"])
if sig.empty:
    sys.exit("シグナルが1件もありません。データ(fetch_data.py)を確認してください。")
summary = summarize(make_groups(sig), spy_df)

meta = {
    "rules_version": R["version"], "period": a.period, "rules_sha256": R["_sha256"],
    "git_commit": commit, "git_clean": clean, "signal_range": f"{P['signal_start']} ~ {P['signal_end']}",
    "data_end": meta_data["data_end"], "n_tickers_in_data": meta_data["n_tickers"],
    "n_signal_dates": int(spy_df.shape[0]), "n_gate_rows": int(len(sig)),
    "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "label": a.label, "rerun_reason": a.reason,
    "rules_matches_frozen_sha": frozen.exists(),
    "approved_fixes_applied": applied_fixes,
    "n_tickers_used": int(panel.close.shape[1]),
    "notes": [
        "時価総額$1B判定は『現在の株式数 x 当時の株価』の推定。現在の上場銘柄だけが対象のため生存者バイアスがあり、絶対成績は実際より良く出る。",
        "そのため『ALL_GATE(全銘柄ベースライン)』との差を主に見る。AがSPYに勝ってもALL_GATEに勝たなければシグナルの価値は示されていない。",
        "決算前除外はv1バックテスト未適用(過去の決算日が無い)。Quality(業績・財務)はv1では評価していない。",
        "3か月・6か月の成績は月次シグナルのリターン期間が重なる。nは多く見えても独立標本数は『日数』列に近い。標準誤差は日付単位で計算。",
        "費用は片道0.10%を往復で控除。個別株の流動性が低い場面のスリッページは過小評価の可能性。",
    ],
}
out = Path("results") / (f"{R['version']}_{a.period}" + (f"_{a.label}" if a.label else ""))
out.mkdir(parents=True, exist_ok=True)
sig.to_csv(out / "signals.csv.gz", index=False)
summary.to_csv(out / "summary.csv", index=False)
(out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
md = to_markdown(summary, meta)
if a.reason:
    md = f"> **{a.reason}**（ルール・閾値・スコアはv1から変更なし / sha256一致: {meta['rules_matches_frozen_sha']}）\n\n" + md
(out / "report.md").write_text(md, encoding="utf-8")

HEADER = ["run_at", "rules_version", "sha256_12", "git_commit", "git_clean", "period", "n_rows", "label", "reason"]
if RUNLOG.exists():
    rows_old = list(csv.reader(RUNLOG.open()))
    if rows_old and "label" not in rows_old[0]:
        with RUNLOG.open("w", newline="") as f:
            w0 = csv.writer(f)
            w0.writerow(HEADER)
            for r0 in rows_old[1:]:
                w0.writerow(r0 + [""] * (len(HEADER) - len(r0)))
new = not RUNLOG.exists()
with RUNLOG.open("a", newline="") as f:
    w = csv.writer(f)
    if new:
        w.writerow(["run_at", "rules_version", "sha256_12", "git_commit", "git_clean", "period", "n_rows", "label", "reason"])
    w.writerow([meta["run_at"], R["version"], R["_sha256"][:12], (commit or "")[:10], clean, a.period, len(sig), a.label, a.reason])
if a.period == "final":
    LOCK.write_text(json.dumps({"rules_version": R["version"], "sha256": R["_sha256"], "commit": commit,
                                "used_at": meta["run_at"]}, ensure_ascii=False))
    print("★ 最終検証期間をロックしました(results/holdout_lock.json)。")
print(md)
print(f"\n保存先: {out}/ (report.md, summary.csv, signals.csv.gz, meta.json)")
