"""記録と、凍結ファイルの検査。通知を受け取るだけでなく、後から実際の成績を検証できるように、判定日時点の情報を保存する。"""
import hashlib
import json
import time
from pathlib import Path

import pandas as pd


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def verify_manifest(path="config/bot_manifest.json"):
    """凍結したファイルが1文字でも変わっていれば、問題のリストを返す。空なら一致。"""
    p = Path(path)
    if not p.exists():
        return ["config/bot_manifest.json がありません。先に python freeze_bot.py で登録してください。"]
    M = json.loads(p.read_text(encoding="utf-8"))
    bad = []
    for f, h in M["files"].items():
        if not Path(f).exists():
            bad.append(f"{f} がありません")
        elif sha256_file(f) != h:
            bad.append(f"{f} が登録時から変更されています")
    return bad


def already_done(records_dir, asof):
    return (Path(records_dir) / str(asof)).exists()


def save_day(records_dir, name, all_df, shown_df, message, meta, parts=None):
    """records_dir/name/ に signals.csv.gz(Gate通過の全銘柄) / notified.csv(表示した銘柄) / message.txt / meta.json を保存。"""
    d = Path(records_dir) / name
    if d.exists():
        raise FileExistsError(f"{d} はすでにあります(上書きしません)")
    d.mkdir(parents=True)
    all_df.to_csv(d / "signals.csv.gz", index_label="ticker")
    shown_df.to_csv(d / "notified.csv", index_label="ticker")
    (d / "message.txt").write_text(message, encoding="utf-8")
    names = ["signals.csv.gz", "notified.csv", "message.txt"]
    if parts is not None:                                   # 実際に送る通(番号つき)。再送のとき、同じ文面を送るために保存する
        (d / "message_parts.json").write_text(json.dumps(parts, ensure_ascii=False, indent=1), encoding="utf-8")
        names.append("message_parts.json")
    meta = dict(meta, files={f: sha256_file(d / f) for f in names}, saved_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return d


def update_meta(d, **kw):
    p = Path(d) / "meta.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m.update(kw)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
