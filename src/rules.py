"""ルールファイルの読み込みと検証。ファイルのSHA256を記録して「どのルールで検証したか」を残す。"""
import hashlib
from pathlib import Path

import yaml


def max_score(R):
    s = R["score"]
    total = sum(s["trend"].values())
    for k in ("depth_bins", "rsi_bins", "volume_bins", "rel_bins"):
        total += max(b["points"] for b in s[k])
    return total


def validate(R):
    assert max_score(R) == 100, f"スコアの満点が100ではありません: {max_score(R)}"
    for g in ("A", "B", "C"):
        assert g in R["grades"], f"grades.{g} がありません"
    assert R["grades"]["A"]["min_score"] > R["grades"]["B"]["min_score"] > R["grades"]["C"]["min_score"]
    for p in R["backtest"]["periods"].values():
        assert p["signal_start"] < p["signal_end"]
    dev, fin = R["backtest"]["periods"]["dev"], R["backtest"]["periods"]["final"]
    overlap = not (dev["signal_end"] < fin["signal_start"] or fin["signal_end"] < dev["signal_start"])
    assert not overlap, "dev期間とfinal期間が重なっています"
    if "quality" in R and R["quality"].get("enabled"):
        Q = R["quality"]
        tot = sum(max(b["points"] for b in Q["bins"][k]) for k in Q["items"])
        assert tot == 100, f"Qualityの満点が100ではありません: {tot}"


def load_rules(path):
    p = Path(path)
    raw = p.read_bytes()
    R = yaml.safe_load(raw)
    R["_sha256"] = hashlib.sha256(raw).hexdigest()
    R["_path"] = str(p)
    validate(R)
    return R
