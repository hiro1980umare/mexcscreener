"""Telegram送信。トークンは画面・ログ・エラー文に出さない。長文は4096字の制限に合わせて分割する。"""
import time

import requests


class TelegramError(RuntimeError):
    pass


def redact(text, token):
    return str(text).replace(token, "***") if token else str(text)


def split_message(text, limit=3800):
    """行の区切りで分割(1行がlimitを超える場合のみ強制分割)。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur.rstrip("\n")); cur = ""
            chunks.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur.rstrip("\n")); cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


def _check_config(token, chat_id):
    if not token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が未設定です")


def post_one(token, chat_id, text, s, retries=2, sleep=time.sleep, label=""):
    """1通を送る。成功したらmessage_id、失敗したら TelegramError(トークンは伏せる)。"""
    where = f", {label}" if label else ""
    for attempt in range(retries + 1):
        try:
            r = s.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=20)
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise TelegramError(redact(f"送信に失敗(通信エラー {type(e).__name__}): {e}", token)) from None
            sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return (r.json().get("result") or {}).get("message_id")
        wait = 2 * (attempt + 1)
        if r.status_code == 429:
            try:
                wait = int((r.json().get("parameters") or {}).get("retry_after", wait))
            except Exception:  # noqa: BLE001
                pass
        if attempt == retries:
            raise TelegramError(redact(f"送信に失敗(HTTP {r.status_code}{where}): {r.text[:200]}", token))
        sleep(wait)


def send_message(token, chat_id, text, session=None, limit=3800, retries=2, sleep=time.sleep):
    """成功したメッセージIDのリストを返す。失敗したら TelegramError(トークンは伏せる)。(接続確認用。Botの本番送信は send_parts)"""
    _check_config(token, chat_id)
    s = session or requests
    chunks = split_message(text, limit)
    return [post_one(token, chat_id, c, s, retries, sleep, f"{k}/{len(chunks)}通目") for k, c in enumerate(chunks, 1)]


# ---------------------------------------------------------------- 銘柄の途中で切らない分割と、通ごとの送信状況
NUMBER_RESERVE = 10          # 先頭に付ける「[k/n]」の分(例: "[12/12]\n" = 8字)を、上限から確保する


def pack_units(units, limit=3800):
    """分けてはいけない塊(銘柄ブロックなど)を、順番を保ったまま上限内に詰める。塊の途中では切らない。
    1つの塊が上限を超えるときだけ、最後の手段として行の区切りで分ける。"""
    chunks, cur = [], ""
    for u in units:
        for piece in ([u] if len(u) <= limit else split_message(u, limit)):
            cand = f"{cur}\n{piece}" if cur else piece
            if cur and len(cand) > limit:
                chunks.append(cur.strip("\n"))
                cur = piece
            else:
                cur = cand
    if cur.strip():
        chunks.append(cur.strip("\n"))
    return chunks


def make_parts(units, limit=3800):
    """送信する通のリスト。2通以上のときだけ、各通の先頭に「[1/3]」を付ける。"""
    chunks = pack_units(units, limit - NUMBER_RESERVE)
    n = len(chunks)
    return chunks if n <= 1 else [f"[{k}/{n}]\n{c}" for k, c in enumerate(chunks, 1)]


def init_state(n):
    return [{"n": k, "of": n, "status": "pending", "message_id": None, "error": None, "attempts": 0} for k in range(1, n + 1)]


def summarize_state(state):
    """(send_status, 成功したmessage_idのリスト)。全て成功='sent' / 1通も成功していない='failed: ...' / 一部='partial: k/n通送信済み; ...'"""
    ids = [x["message_id"] for x in state if x["status"] == "sent"]
    if len(ids) == len(state):
        return "sent", ids
    err = next((x["error"] for x in state if x["status"] == "failed" and x["error"]), "未送信")
    return (f"failed: {err}" if not ids else f"partial: {len(ids)}/{len(state)}通送信済み; {err}"), ids


def send_parts(token, chat_id, parts, state, session=None, retries=2, sleep=time.sleep, on_update=None):
    """state(通ごとの状況)のうち、まだ送っていない通だけを順番に送る。失敗したらそこで止める(順番を崩さない)。
    1通ごとに on_update(state) を呼ぶので、途中で止まっても送信状況が残る。戻り値: (state, 最初のエラー or None)"""
    s = session or requests
    try:
        _check_config(token, chat_id)
    except TelegramError as e:
        for x in state:
            if x["status"] != "sent":
                x["status"], x["error"] = "failed", str(e)
        if on_update:
            on_update(state)
        return state, str(e)
    for x, text in zip(state, parts):
        if x["status"] == "sent":
            continue
        x["attempts"] += 1
        try:
            x["message_id"] = post_one(token, chat_id, text, s, retries, sleep, f"{x['n']}/{x['of']}通目")
            x["status"], x["error"] = "sent", None
        except TelegramError as e:
            x["status"], x["error"] = "failed", str(e)
            if on_update:
                on_update(state)
            return state, str(e)
        if on_update:
            on_update(state)
    return state, None
