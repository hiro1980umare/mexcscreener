"""Bot用のデータ読み込み。"""
import pickle
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .data_v2 import assemble, download_v2, load_v2, save_v2


def load_universe_file(path, limit=0):
    df = pd.read_csv(path)
    t = [str(x).strip() for x in df["ticker"].dropna() if str(x).strip() and str(x).strip() != "SPY"]
    t = sorted(set(t))
    return t[:limit] if limit else t


def load_sec_dir(d, tickers, progress=None, every=500):
    sec, tickers = {}, list(tickers)
    for k, t in enumerate(tickers, 1):
        f = Path(d) / f"{t}.pkl"
        if f.exists():
            with f.open("rb") as fh:
                sec[t] = pickle.load(fh)
        if progress and k % every == 0:
            progress(f"SEC財務を読み込み中 {k}/{len(tickers)}")
    return sec


def save_sec_bundle(sec, path):
    """全銘柄のSEC財務を1つのファイルにまとめる(ドライブ上の小さなファイルを数千個読むより、はるかに速い)。"""
    tmp = Path(str(path) + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(sec, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load_sec_bundle(path, tickers):
    with Path(path).open("rb") as fh:
        allsec = pickle.load(fh)
    return {t: allsec[t] for t in tickers if t in allsec}


def sec_updated(d):
    """SEC財務キャッシュの更新日(最も新しいファイル)。無ければ None。"""
    fs = list(Path(d).glob("*.pkl"))
    return date.fromtimestamp(max(f.stat().st_mtime for f in fs)) if fs else None


def get_panel_since(tickers, start_iso, work_dir, log=print):
    """start_iso 以降の株価を取得して PanelV2 にする。"""
    cache = Path(work_dir) / "chunks"
    download_v2(tickers + ["SPY"], start_iso, cache, log=log)
    mats, cov = assemble(cache, min_rows=1)
    save_v2(mats, cov, Path(work_dir) / "panel")
    return load_v2(Path(work_dir) / "panel"), cov


def get_panel_live(tickers, today, lookback_days, work_dir, log=print):
    """直近 lookback_days の株価(指標に必要な約252営業日分)。履歴不足の銘柄は assemble が除外する。"""
    start = (today - timedelta(days=lookback_days)).isoformat()
    cache = Path(work_dir) / "chunks"
    download_v2(tickers + ["SPY"], start, cache, log=log)
    mats, cov = assemble(cache)
    save_v2(mats, cov, Path(work_dir) / "panel")
    return load_v2(Path(work_dir) / "panel"), cov
