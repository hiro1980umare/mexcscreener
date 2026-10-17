#!/usr/bin/env python3
"""通知Botの凍結。研究報告書・ルール・基準・Botの仕様のハッシュを記録する。
    python freeze_bot.py            # 初回の登録
    python freeze_bot.py --supersede  # 新しい版の報告書を作った場合のみ。旧manifestは config/bot_manifest_history/ に保存
登録後、run_bot.py は起動時にこれらを検査し、1文字でも変わっていれば実行しない。
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from src.bot_records import sha256_file

FILES = ["docs/research_report.md", "config/rules_v1.yaml", "config/rules_v2.yaml", "config/criteria_v2.yaml",
         "config/rules_v2b.yaml", "config/criteria_v2b.yaml", "docs/v2b_preregistration.md", "config/bot_spec.yaml"]
CHECK_IN_REPORT = FILES[1:7]            # 報告書にハッシュ(先頭8字)が記載されているべきファイル

ap = argparse.ArgumentParser()
ap.add_argument("--supersede", action="store_true")
a = ap.parse_args()
M = Path("config/bot_manifest.json")
for f in FILES:
    if not Path(f).exists():
        sys.exit(f"【中止】{f} がありません")
report = Path("docs/research_report.md").read_text(encoding="utf-8")
for f in CHECK_IN_REPORT:
    if sha256_file(f)[:8] not in report:
        sys.exit(f"【中止】研究報告書に {f} のハッシュ({sha256_file(f)[:8]})が記載されていません。報告書と実ファイルが一致していません。")
if M.exists():
    if not a.supersede:
        sys.exit("【中止】すでに登録済みです。変更する場合は、新しい版の報告書を作り、--supersede で登録し直してください(旧版は履歴に残ります)。")
    h = Path("config/bot_manifest_history")
    h.mkdir(exist_ok=True)
    shutil.copy(M, h / f"bot_manifest_{time.strftime('%Y%m%d_%H%M%S')}.json")
M.write_text(json.dumps({"registered_at": time.strftime("%Y-%m-%d %H:%M:%S"), "report_version": "1.0",
                         "files": {f: sha256_file(f) for f in FILES}}, ensure_ascii=False, indent=1), encoding="utf-8")
print("通知Botの凍結ファイルを登録しました:")
for f in FILES:
    print(f"  {sha256_file(f)[:8]}  {f}")
print("次: git add config/ docs/ && git commit -m 'register bot manifest'")
