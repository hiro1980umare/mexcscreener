#!/usr/bin/env python3
"""V2の凍結（事前登録）。
  1回目:  python freeze_v2.py
      成功基準(criteria_v2.yaml)のハッシュを記録。以後、基準が1文字でも変わると最終検証は実行できない。
  V2bの登録(V2bを走らせる前に、1回だけ):
      python freeze_v2.py --register-v2b
      rules_v2b.yaml / criteria_v2b.yaml / docs/v2b_preregistration.md のハッシュを記録し、
      以後dev期間で試せるルールを『V2とV2bの2つだけ』に固定する(V2cは作れない)。
  最終候補を決めたとき(dev期間の調整が終わったとき):
      python freeze_v2.py --final-candidate config/rules_v2.yaml
      最終検証で使う『1つのルール』と、コードのハッシュを記録。1回しか実行できない。
実行後は必ず git add / commit する。
"""
import argparse
import csv
import glob
import hashlib
import json
import sys
import time
from pathlib import Path

import yaml

MANIFEST = Path("config/v2_manifest.json")
CRITERIA = Path("config/criteria_v2.yaml")
CODE = ["evaluate_criteria.py", "evaluate_v2b.py", "run_v2.py", "src/quality_v2b.py", "src/sec_pit.py", "src/v2.py", "src/signals.py",
        "src/indicators.py", "src/backtest.py", "src/data_v2.py"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


ap = argparse.ArgumentParser()
ap.add_argument("--final-candidate", default="")
ap.add_argument("--register-v2b", action="store_true")
a = ap.parse_args()

if MANIFEST.exists():
    M = json.loads(MANIFEST.read_text())
    if M["criteria"]["sha256"] != sha(CRITERIA):
        sys.exit("【中止】成功基準(criteria_v2.yaml)が登録時から変更されています。この基準では最終検証できません。")
else:
    R1 = Path("config/rules_v1.yaml")
    M = {"created": time.strftime("%Y-%m-%d %H:%M:%S"),
         "criteria": {"file": str(CRITERIA), "sha256": sha(CRITERIA)},
         "timing_source": {"file": str(R1), "sha256": sha(R1)},
         "code_hashes_at_registration": {f: sha(f) for f in CODE if Path(f).exists()},
         "final_candidate": None}
    print("成功基準を登録しました。")
if a.register_v2b:
    if M.get("v2b"):
        sys.exit("【中止】V2bはすでに登録済みです。V2bは最後の開発版で、登録は1回だけです。")
    files = {"rules": "config/rules_v2b.yaml", "criteria": "config/criteria_v2b.yaml", "preregistration": "docs/v2b_preregistration.md"}
    for f in files.values():
        if not Path(f).exists():
            sys.exit(f"【中止】{f} がありません。")
    Kb = yaml.safe_load(Path(files["criteria"]).read_text(encoding="utf-8"))
    Rb = yaml.safe_load(Path(files["rules"]).read_text(encoding="utf-8"))
    v2_sha = sha("config/rules_v2.yaml")
    if Kb["base_sha256"] != M["criteria"]["sha256"]:
        sys.exit("【中止】criteria_v2b.yaml が参照するV2の基準ハッシュが、登録済みのV2基準と違います。")
    if Rb["timing_source"]["sha256"] != M["timing_source"]["sha256"]:
        sys.exit("【中止】V2bのTiming(v1)のハッシュが登録時と違います。")
    if Rb["derived_from"]["sha256"] != v2_sha:
        sys.exit("【中止】rules_v2b.yaml の出所(rules_v2.yaml)が、現在のrules_v2.yamlと違います。")
    log = Path("results/v2_run_log.csv")
    if log.exists():
        used = {r[2] for r in list(csv.reader(log.open()))[1:] if r[3] == "dev"}
        if used - {v2_sha[:12]}:
            sys.exit("【中止】V2以外のdev実行がすでにあります: " + ", ".join(sorted(used - {v2_sha[:12]})) + "。V2bの登録は、V2b実行の前に行ってください。")
    M["v2b"] = {"registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                **{k: {"file": f, "sha256": sha(f)} for k, f in files.items()},
                "v2_rules": {"file": "config/rules_v2.yaml", "sha256": v2_sha},
                "max_dev_versions": 2, "dev_versions_allowed": [v2_sha, sha(files["rules"])]}
    print("V2bを登録しました。dev期間で試せるルールは、V2とV2bの2つだけです(V2cは作れません)。")
if a.final_candidate:
    if M.get("final_candidate"):
        sys.exit(f"【中止】最終候補はすでに凍結済みです: {M['final_candidate']}")
    R = yaml.safe_load(Path(a.final_candidate).read_text(encoding="utf-8"))
    if R["timing_source"]["sha256"] != M["timing_source"]["sha256"]:
        sys.exit("【中止】Timing(v1)のハッシュが登録時と違います。")
    # ---- 最終検証に進む条件(事前登録)を、機械的に検査する ----
    cand_sha = sha(a.final_candidate)
    if R.get("version") == "v2b" and not M.get("v2b"):
        sys.exit("【中止】V2bが登録されていません。")
    if M.get("v2b") and cand_sha not in M["v2b"]["dev_versions_allowed"]:
        sys.exit("【中止】登録されたルール(V2, V2b)以外は、最終候補にできません。")
    metas = [(json.loads(Path(p).read_text()), p) for p in glob.glob("results/v2_dev*/meta.json")]
    metas = [m for m in metas if m[0]["rules_sha256"] == cand_sha]
    if not metas:
        sys.exit("【中止】このルールのdev実行結果がありません。先にdevで実行してください。")
    import pandas as pd
    from evaluate_v2b import final_eligibility
    mt, mp = sorted(metas, key=lambda x: x[0]["run_at"])[-1]
    ok, _ = final_eligibility(a.final_candidate, pd.read_csv(Path(mp).parent / "signals.csv.gz", parse_dates=["date"]), M)
    if not ok:
        sys.exit("【中止】このルールは、事前登録した『最終検証に進む条件』を満たしていません。最終検証は行いません(封印のまま)。")
    M["final_candidate"] = {"file": a.final_candidate, "sha256": sha(a.final_candidate), "frozen_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    M["code_hashes_final"] = {f: sha(f) for f in CODE if Path(f).exists()}
    print(f"最終候補を凍結しました: {a.final_candidate}")
MANIFEST.write_text(json.dumps(M, ensure_ascii=False, indent=1))
print(json.dumps({k: v for k, v in M.items() if k not in ("code_hashes_at_registration", "code_hashes_final")}, ensure_ascii=False, indent=1))
print("\n次に必ず: git add config/ && git commit -m 'freeze v2' ")
