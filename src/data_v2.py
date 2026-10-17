"""V2用のデータ。2009年から取得し、分割調整前の株価の復元に必要な『分割情報』も保存する。
チャンクごとに保存するので、途中で止まっても続きから再開できる。"""
import json
import pickle
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FIELDS = ["Open", "Close", "Adj Close", "Volume", "Stock Splits"]


@dataclass
class PanelV2:
    open: pd.DataFrame          # 調整済み始値 = Open x (Adj Close / Close)   ※v1と同じ基準(配当・分割調整済み)
    close: pd.DataFrame         # 調整済み終値 = Adj Close                   ※v1と同じ基準
    close_raw: pd.DataFrame     # Close(分割調整のみ・配当調整なし)。Gateの株価/時価総額の復元に使う
    volume: pd.DataFrame
    spy_open: pd.Series
    spy_close: pd.Series
    splits: dict                # ticker -> (日付配列 datetime64[D], 分割比配列)
    shares: pd.Series           # v1との互換用(使わない)


def _extract(df, part):
    """yf.downloadの戻り値 -> {ticker: DataFrame(FIELDS)}。列の並びが2通りあるので両方に対応。"""
    out = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = set(df.columns.get_level_values(0))
        if "Close" in lv0:
            tick = df["Close"].columns
            for t in tick:
                out[t] = pd.DataFrame({f: df[f][t] for f in FIELDS if f in lv0})
        else:
            for t in lv0:
                out[t] = df[t][[f for f in FIELDS if f in df[t].columns]]
    else:
        out[part[0]] = df[[f for f in FIELDS if f in df.columns]]
    return out


def download_v2(tickers, start, cache_dir, chunk=100, small=10, rounds=2, sleeper=time.sleep, log=print):
    """チャンク単位でダウンロードしてcache_dirに保存。保存済みのチャンクは飛ばす(再開可能)。
    戻り値: 取得状況のDataFrame(ticker, status, n_rows, reason)"""
    import yfinance as yf
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    status = []
    for k in range(0, len(tickers), chunk):
        part = tickers[k:k + chunk]
        f = cache / f"chunk_{k // chunk:04d}.pkl"
        if f.exists():
            continue
        got, reasons, pending = {}, {}, list(part)
        for rnd in range(rounds + 1):
            if not pending:
                break
            size = len(pending) if rnd == 0 else small
            if rnd:
                sleeper(5 * rnd)
            still = []
            for j in range(0, len(pending), size):
                sub = pending[j:j + size]
                try:
                    df = yf.download(sub, start=start, interval="1d", auto_adjust=False, actions=True,
                                     threads=(rnd == 0), progress=False)
                except Exception as e:  # noqa: BLE001
                    df = None
                    for t in sub:
                        reasons[t] = f"download_error: {str(e)[:60]}"
                ex = _extract(df, sub)
                errs = getattr(getattr(yf, "shared", None), "_ERRORS", {}) or {}
                for t in sub:
                    if t in ex and ex[t]["Close"].notna().sum() > 0:
                        got[t] = ex[t]
                    else:
                        still.append(t)
                        reasons.setdefault(t, str(errs.get(t, "no_data"))[:60])
            pending = still
        with f.open("wb") as fh:
            pickle.dump({"data": got, "failed": {t: reasons.get(t, "no_data") for t in pending}}, fh)
        log(f"  株価取得 {min(k + chunk, len(tickers))}/{len(tickers)} (このチャンクの失敗 {len(pending)})")
    return cache


def assemble(cache_dir, min_rows=252):
    """チャンクを読み込んでPanelV2用の素材を作る。戻り値: (panel素材dict, 状況DataFrame)"""
    data, failed = {}, {}
    for f in sorted(Path(cache_dir).glob("chunk_*.pkl")):
        with f.open("rb") as fh:
            d = pickle.load(fh)
        data.update(d["data"])
        failed.update(d["failed"])
    rows = []
    adj_o, adj_c, raw_c, vol, splits = {}, {}, {}, {}, {}
    for t, df in data.items():
        c = df["Close"].replace(0, np.nan)
        n = int(df["Adj Close"].notna().sum())
        rows.append(dict(ticker=t, n_rows=n, status="ok" if n >= min_rows else "short_history",
                         reason="" if n >= min_rows else f"{n}日分のみ",
                         first_date=str(df["Adj Close"].first_valid_index().date()),
                         last_date=str(df["Adj Close"].last_valid_index().date())))
        if n < min_rows:
            continue
        factor = df["Adj Close"] / c
        adj_o[t] = df["Open"] * factor
        adj_c[t] = df["Adj Close"]
        raw_c[t] = df["Close"]
        vol[t] = df["Volume"]
        sp = df["Stock Splits"].dropna() if "Stock Splits" in df else pd.Series(dtype=float)
        sp = sp[sp > 0]
        splits[t] = (sp.index.values.astype("datetime64[D]"), sp.values.astype(float))
    for t, r in failed.items():
        rows.append(dict(ticker=t, n_rows=0, status="failed", reason=r, first_date="", last_date=""))
    cat = lambda d: pd.DataFrame(d).sort_index()  # noqa: E731
    mats = dict(open=cat(adj_o), close=cat(adj_c), close_raw=cat(raw_c), volume=cat(vol), splits=splits)
    return mats, pd.DataFrame(rows)


def save_v2(mats, cov, d):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    for k in ("open", "close", "close_raw", "volume"):
        mats[k].to_pickle(d / f"{k}.pkl")
    with (d / "splits.pkl").open("wb") as fh:
        pickle.dump(mats["splits"], fh)
    cov.to_csv(d / "coverage.csv", index=False)
    (d / "meta.json").write_text(json.dumps({
        "data_start": str(mats["close"].index.min().date()), "data_end": str(mats["close"].index.max().date()),
        "n_tickers": int(mats["close"].shape[1]), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1))


def load_v2(d):
    d = Path(d)
    o, c, r, v = (pd.read_pickle(d / f"{k}.pkl") for k in ("open", "close", "close_raw", "volume"))
    with (d / "splits.pkl").open("rb") as fh:
        splits = pickle.load(fh)
    spy_cols = [x for x in ("SPY",) if x in c.columns]
    assert spy_cols, "SPYがありません"
    spy_o, spy_c = o["SPY"], c["SPY"]
    stocks = [t for t in c.columns if t != "SPY"]
    return PanelV2(o[stocks], c[stocks], r[stocks], v[stocks], spy_o, spy_c, splits,
                   pd.Series(np.nan, index=stocks))
