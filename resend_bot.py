#!/usr/bin/env python3
"""送信に失敗した通知の、未送信の通だけを再送する(送信済みの通は送らない)。
  python resend_bot.py --dry-run                         # 未送信がある記録の一覧だけ表示
  python resend_bot.py                                   # 未送信がある全ての記録を、古い順に再送
  python resend_bot.py --record bot_records/2026-10-15   # 指定した記録だけ
終了コード: 0=未送信なし/再送完了  5=まだ未送信が残っている
再送は保存済みの文面(message_parts.json)をそのまま送る。判定・銘柄・文面は作り直さない。
"""
import argparse
import os
import sys
from pathlib import Path

from src.bot_send import deliver, unsent_count

ap = argparse.ArgumentParser()
ap.add_argument("--records", default="bot_records")
ap.add_argument("--record", default="", help="対象の記録フォルダ(省略=未送信がある全ての記録)")
ap.add_argument("--dry-run", action="store_true", help="送らずに、未送信の記録を表示する")
a = ap.parse_args()

targets = [Path(a.record)] if a.record else sorted(p for p in Path(a.records).glob("*") if p.is_dir() and not p.name.startswith("dryrun_"))
counts = [(d, unsent_count(d)) for d in targets]
if a.record and counts[0][1] is None:
    print(f"【再送できません】{a.record} には通ごとの送信状況の記録がありません(旧形式、またはdry-run)。何が送信済みか分からないため、自動では再送しません。", file=sys.stderr)
    sys.exit(1)
todo = [(d, n) for d, n in counts if n]
if not todo:
    print("未送信の通知はありません。")
    sys.exit(0)
rc = 0
for d, n in todo:
    if a.dry_run:
        print(f"[dry-run] {d}: 未送信 {n}通")
        continue
    try:
        status, err = deliver(d, os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""))
    except RuntimeError as e:
        print(f"【再送できません】{e}", file=sys.stderr)
        rc = 5
        continue
    print(f"{d}: {status}")
    if err:
        print(f"【送信失敗】{err}\n未送信の通が残っています。しばらくして python resend_bot.py --record {d} を再実行してください。", file=sys.stderr)
        rc = 5
sys.exit(rc)
