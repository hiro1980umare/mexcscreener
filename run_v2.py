#!/usr/bin/env python3
"""V2の実行。dev期間(2017年以降)と、封印された最終検証期間(2011-2016)を厳密に分ける。
    python run_v2.py --rules config/rules_v2.yaml --period dev [--label xxx]
    python run_v2.py --rules <凍結した最終候補> --period final        # 1回だけ。結果は実行後に初めて表示される
"""
import argparse
import csv
import glob
import hashlib
import json
import pickle
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

import evaluate_criteria as EC
from src.backtest import summarize, to_markdown
from src.data_v2 import load_v2
from src.indicators import build
from src.rules import load_rules
from src.v2 import collect_signals_v2, coverage_table, make_groups_v2

MANIFEST, LOCK, RUNLOG = Path("config/v2_manifest.json"), Path("results/v2_final_lock.json"), Path("results/v2_run_log.csv")
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731


def git_clean(*paths):
    r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, None
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for p in paths:
        if subprocess.run(["git", "ls-files", "--error-unmatch", p], capture_output=True).returncode != 0:
            return False, commit
        if subprocess.run(["git", "status", "--porcelain", "--", p], capture_output=True, text=True).stdout.strip():
            return False, commit
    return True, commit


def load_sec(d, tickers):
    sec = {}
    for t in tickers:
        f = Path(d) / f"{t}.pkl"
        if f.exists():
            with f.open("rb") as fh:
                sec[t] = pickle.load(fh)
    return sec


ap = argparse.ArgumentParser()
ap.add_argument("--rules", required=True)
ap.add_argument("--period", choices=["dev", "final"], required=True)
ap.add_argument("--label", default="")
ap.add_argument("--data", default="", help="省略するとrules_v2.yamlの data.<period>.dir を使う")
ap.add_argument("--sec", default="", help="省略するとrules_v2.yamlの data.sec_dir を使う")
ap.add_argument("--allow-dirty", action="store_true", help="devのみ")
a = ap.parse_args()

R2 = yaml.safe_load(Path(a.rules).read_text(encoding="utf-8"))
R2_sha = sha(a.rules)
R1 = load_rules(R2["timing_source"]["file"])
if R1["_sha256"] != R2["timing_source"]["sha256"]:
    sys.exit("【中止】Timing(v1)のルールが、V2が参照している版と違います。")
K = yaml.safe_load(Path("config/criteria_v2.yaml").read_text(encoding="utf-8"))
if not MANIFEST.exists():
    sys.exit("【中止】先に python freeze_v2.py を実行して、成功基準を登録してください。")
M = json.loads(MANIFEST.read_text())
if M["criteria"]["sha256"] != sha("config/criteria_v2.yaml"):
    sys.exit("【中止】成功基準が登録時から変更されています。")
V2B = M.get("v2b")
is_v2b = bool(V2B) and R2_sha == V2B["rules"]["sha256"]
if R2.get("version") == "v2b" and not is_v2b:
    sys.exit("【中止】rules_v2b.yaml が登録されていない、または登録後に変更されています。"
             "登録: python freeze_v2.py --register-v2b。登録後の変更は禁止です(V2bは最後の開発版)。")
if V2B:
    for k in ("criteria", "preregistration"):
        if sha(V2B[k]["file"]) != V2B[k]["sha256"]:
            sys.exit(f"【中止】V2bの事前登録({V2B[k]['file']})が登録時から変更されています。")
    if a.period == "dev" and R2_sha not in V2B["dev_versions_allowed"]:
        sys.exit("【中止】dev期間で試せるルールは、登録済みの2つ(V2, V2b)だけです。V2cは作れません。")
if is_v2b:
    from evaluate_v2b import load_K_v2b, report_v2b
    K, REPORT = load_K_v2b(), report_v2b
else:
    REPORT = EC.report

P = R2["periods"][a.period]
data_dir = a.data or R2["data"][a.period]["dir"]
sec_dir = a.sec or R2["data"]["sec_dir"]
clean, commit = git_clean(a.rules, "config/criteria_v2.yaml", str(MANIFEST),
                          *([V2B["criteria"]["file"], V2B["preregistration"]["file"]] if is_v2b else []))
if a.period == "final":
    fc = M.get("final_candidate")
    if not fc or fc["sha256"] != R2_sha or fc["file"] != a.rules:
        sys.exit("【中止】このルールは『最終候補』として凍結されていません。python freeze_v2.py --final-candidate <ルール> を実行してコミットしてください。")
    now = {f: sha(f) for f in M["code_hashes_final"] if Path(f).exists()}
    if now != M["code_hashes_final"]:
        sys.exit("【中止】最終候補の凍結後にコードが変更されています: " + ", ".join(f for f in now if now[f] != M["code_hashes_final"][f]))
    if not clean:
        sys.exit("【中止】最終検証は、ルール・成功基準・manifestがコミット済みで変更が無い状態でのみ実行できます。")
    if LOCK.exists():
        sys.exit(f"【中止】最終検証期間は使用済み(または実行中に中断)です: {LOCK.read_text()}")
    devs = [json.loads(Path(p).read_text()) for p in glob.glob("results/v2_dev*/meta.json")]
    devs = [d for d in devs if d["rules_sha256"] == R2_sha]
    if not devs:
        sys.exit("【中止】この最終候補と同じルールのdev実行結果がありません。先に --period dev で実行してください。")
    from evaluate_v2b import final_eligibility
    _dm = sorted(devs, key=lambda d: d["run_at"])[-1]
    _dd = [p for p in glob.glob("results/v2_dev*/meta.json") if json.loads(Path(p).read_text())["run_at"] == _dm["run_at"]][0]
    _ok, _ = final_eligibility(a.rules, pd.read_csv(Path(_dd).parent / "signals.csv.gz", parse_dates=["date"]), M)
    if not _ok:
        sys.exit("【中止】このルールは、事前登録した『最終検証に進む条件』を満たしていません。最終検証は実行できません"
                 "（★この中止では最終検証の権利は消費されません）。")
    if not (Path(data_dir) / "close.pkl").exists() or not Path(sec_dir).exists():
        sys.exit(f"【中止】最終検証用の株価({data_dir})またはSEC財務({sec_dir})がありません。"
                 "最終候補の凍結・コミット後に python fetch_data_v2.py --period final を実行してください。"
                 "（★この中止では最終検証の権利は消費されません）")
    LOCK.parent.mkdir(exist_ok=True)
    LOCK.write_text(json.dumps({"rules": a.rules, "sha256": R2_sha, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "note": "最終検証を開始。中断した場合も使用済みとして扱う(削除する場合は理由をrun_logに記録すること)"},
                               ensure_ascii=False))
else:
    assert P["signal_end"] < R2["periods"]["final"]["signal_start"] or P["signal_start"] > R2["periods"]["final"]["signal_end"], \
        "dev期間が最終検証期間と重なっています"
    if not clean and not a.allow_dirty:
        sys.exit("【中止】ルール/成功基準/manifestが未コミットです。コミットしてから実行してください(--allow-dirtyで回避可・記録に残る)。")

if not (Path(data_dir) / "close.pkl").exists():
    sys.exit(f"【中止】{a.period}用の株価データがありません({data_dir})。python fetch_data_v2.py を実行してください。")
panel = load_v2(data_dir)
sealed_end = pd.Timestamp(R2["periods"]["final"]["outcome_end"])
if a.period == "dev" and panel.close.index.min() <= sealed_end:
    sys.exit(f"【中止】dev用データに、封印された最終検証期間の株価が含まれています"
             f"（データ開始 {panel.close.index.min().date()} <= {sealed_end.date()}）。dev環境には{sealed_end.date()}より後のデータだけを置いてください。")
sec = load_sec(sec_dir, panel.close.columns)
print(f"価格 {panel.close.shape[1]}銘柄 / SEC財務 {len(sec)}銘柄 / 期間 {P['signal_start']} ~ {P['signal_end']} ({a.period})")
ind = build(panel, R1)
sig, spy_df = collect_signals_v2(panel, ind, R1, R2, sec, P["signal_start"], P["signal_end"])
if sig.empty:
    sys.exit("シグナルが1件もありません。データを確認してください。")

out = Path("results") / f"v2_{a.period}" if a.period == "final" else Path("results") / ("v2_dev" + (f"_{a.label}" if a.label else ""))
out.mkdir(parents=True, exist_ok=True)
sig.to_csv(out / "signals.csv.gz", index=False)
meta = {"rules_version": R2["version"], "rules_file": a.rules, "rules_sha256": R2_sha, "period": a.period, "git_commit": commit,
        "git_clean": clean, "signal_range": f"{P['signal_start']} ~ {P['signal_end']}", "n_signal_dates": int(len(spy_df)),
        "n_gate_rows": int(len(sig)), "n_tickers_in_data": int(panel.close.shape[1]), "n_sec": len(sec), "data_dir": data_dir,
        "data_end": json.loads((Path(data_dir) / "meta.json").read_text())["data_end"], "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "notes": ["Gate: 当時の株価(分割調整前) x 提出済みの最新の発行済株式数。株式数が取れない銘柄はGateを通さない(推定しない)。",
                  ("Quality(V2b): 営業利益/総資産・アクルーアル・希薄化の同日順位平均(上位1/3=HIGH)。1つでも取れなければ判定不能(最高でもC)。" if is_v2b else "Quality: 必要な5項目が1つでも取れなければ判定不能(最高でもC)。"), "現在の上場銘柄のみが対象のため生存者バイアスが残る(特に古い期間)。",
                  "決算前除外はバックテスト未適用。費用は片道0.10%を往復で控除。"]}
(out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
new = not RUNLOG.exists()
with RUNLOG.open("a", newline="") as f:
    w = csv.writer(f)
    if new:
        w.writerow(["run_at", "rules_file", "sha256_12", "period", "label", "git_clean", "n_rows"])
    w.writerow([meta["run_at"], a.rules, R2_sha[:12], a.period, a.label, clean, len(sig)])

if a.period == "dev":
    summary = summarize(make_groups_v2(sig), spy_df)
    summary.to_csv(out / "summary.csv", index=False)
    t, reasons = coverage_table(sig)
    md = [to_markdown(summary, {**meta, "rules_version": R2["version"]}), "", "## Qualityの判定可能率（年別）", "",
          "| 年 | Gate通過行 | 判定可能 | HIGH | MID | LOW |", "|---|---|---|---|---|---|"]
    md += [f"| {y} | {int(r.rows)} | {r.determinable:.0%} | {r.high:.0%} | {r.mid:.0%} | {r.low:.0%} |" for y, r in t.iterrows()]
    md += ["", "判定不能の理由(上位):"] + [f"- {k}: {v}行" for k, v in reasons.head(8).items()]
    n_ver = len({r[2] for r in csv.reader(RUNLOG.open()) if r[3] == "dev" and r[2] != "sha256_12"})
    md += ["", f"## 成功基準（事前登録・dev期間）  ※ dev期間で試したルール版: {n_ver}個", "", REPORT(K, sig)]
    (out / "report.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
else:
    dev_meta = sorted([d for d in devs], key=lambda d: d["run_at"])[-1]
    dev_dir = [p for p in glob.glob("results/v2_dev*/meta.json") if json.loads(Path(p).read_text())["run_at"] == dev_meta["run_at"]][0]
    dev_sig = pd.read_csv(Path(dev_dir).parent / "signals.csv.gz", parse_dates=["date"])
    n_ver = len({r[2] for r in csv.reader(RUNLOG.open()) if r[3] == "dev" and r[2] != "sha256_12"})
    rep = REPORT(K, dev_sig, sig)
    txt = f"# {R2['version'].upper()} 最終検証(シグナル2011-01〜2015-11)の結果  ※封印解除\n\n- 最終候補: {a.rules} (sha {R2_sha[:12]})\n- dev期間で試したルール版の数: {n_ver}\n\n{rep}"
    (out / "verdict.md").write_text(txt, encoding="utf-8")
    LOCK.write_text(json.dumps({"rules": a.rules, "sha256": R2_sha, "completed_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
    print(txt)
