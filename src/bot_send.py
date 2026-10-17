"""記録フォルダを単位にした送信と再送。通ごとの送信状況を meta.json に残し、未送信の通だけを送る。"""
import json
import time
from pathlib import Path

from .bot_records import sha256_file, update_meta
from .tgram import init_state, send_parts, summarize_state


def _meta(d):
    return json.loads((Path(d) / "meta.json").read_text(encoding="utf-8"))


def unsent_count(d):
    """未送信(送信済みでない)の通の数。送信状況の記録がない記録(旧形式・dry-run)は None。"""
    try:
        m = _meta(d)
    except (OSError, ValueError):
        return None
    st = m.get("send_parts")
    if not st or m.get("mode") == "dry-run":
        return None
    return sum(x["status"] != "sent" for x in st)


def deliver(d, token, chat_id, session=None, sleep=time.sleep):
    """記録フォルダ d の、まだ送っていない通を順に送る。戻り値: (send_status, 最初のエラー or None)。
    保存済みの通(message_parts.json)が記録時のハッシュと違えば、送らずに止める(記録の改ざん・破損の検知)。"""
    d = Path(d)
    m = _meta(d)
    pf = d / "message_parts.json"
    want = (m.get("files") or {}).get("message_parts.json")
    if not pf.exists() or not want:
        raise RuntimeError(f"{d} には送信用の通(message_parts.json)の記録がありません(旧形式)。再送の対象外です。")
    if sha256_file(pf) != want:
        raise RuntimeError(f"{pf} が記録時から変更されています。再送を中止します。")
    parts = json.loads(pf.read_text(encoding="utf-8"))
    state = m.get("send_parts")
    if not state or len(state) != len(parts):
        state = init_state(len(parts))

    def save(st):
        status, ids = summarize_state(st)
        update_meta(d, send_parts=st, send_status=status, telegram_message_ids=ids, send_updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    state, err = send_parts(token, chat_id, parts, state, session=session, sleep=sleep, on_update=save)
    save(state)
    return summarize_state(state)[0], err
