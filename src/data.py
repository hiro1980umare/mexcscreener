"""銘柄リスト・時価総額・株価の取得とキャッシュ。"""
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA_DIR = Path("data")


@dataclass
class Panel:
    open: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    spy_open: pd.Series
    spy_close: pd.Series
    shares: pd.Series  # 推定発行済株式数 = 現在の時価総額 / 最新の株価（固定値。過去の増減は無視）

    def since(self, date):
        """指定日以降だけを返す（devでは2016-06以降のみ。2011-2016年の値動きは読み込まない＝封印）。"""
        d = pd.Timestamp(date)
        m = self.close.index >= d
        return Panel(self.open[m], self.close[m], self.volume[m],
                     self.spy_open[self.spy_open.index >= d], self.spy_close[self.spy_close.index >= d], self.shares)

    def upto(self, n):
        """先頭n行だけ（テスト用）。sharesは変えない。"""
        s = slice(0, n)
        return Panel(self.open.iloc[s], self.close.iloc[s], self.volume.iloc[s],
                     self.spy_open.iloc[s], self.spy_close.iloc[s], self.shares)


# ---------------- 銘柄リスト ----------------
def load_universe(R):
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
    bad = re.compile(R["universe"]["exclude_name_regex"], re.I)
    syms = []
    for fn, kind in (("nasdaqlisted.txt", "NASDAQ"), ("otherlisted.txt", "OTHER")):
        lines = requests.get(base + fn, timeout=30).text.strip().splitlines()
        header = lines[0].split("|")
        for ln in lines[1:]:
            if ln.startswith("File Creation"):
                continue
            f = dict(zip(header, ln.split("|")))
            sym = f.get("Symbol") or f.get("ACT Symbol", "")
            if kind == "OTHER" and f.get("Exchange") != "N":  # NYSEのみ
                continue
            if f.get("ETF") != "N" or f.get("Test Issue") != "N":
                continue
            if not re.fullmatch(r"[A-Z]{1,5}", sym) or bad.search(f.get("Security Name", "")):
                continue
            syms.append(sym)
    return sorted(set(syms))


# ---------------- 時価総額 ----------------
MCAP_ERRORS = Counter()   # 失敗の理由を数える（アクセス制限かどうかを後から判断するため）


def _mcap(t, pause=0.0):
    if pause:
        time.sleep(pause)
    try:
        import yfinance as yf
        v = getattr(yf.Ticker(t).fast_info, "market_cap", None)
        if not v:
            MCAP_ERRORS["値なし(None)"] += 1
        return t, float(v) if v else np.nan
    except Exception as e:  # noqa: BLE001
        MCAP_ERRORS[f"{type(e).__name__}: {str(e)[:50]}"] += 1
        return t, np.nan


def fetch_mcaps(tickers, workers=8, pause=0.0):
    from functools import partial
    with ThreadPoolExecutor(workers) as ex:
        return pd.Series(dict(ex.map(partial(_mcap, pause=pause), tickers)), name="market_cap")


def fetch_mcaps_robust(mcap, cache_file=None, max_rounds=6, workers=2, pause=0.6, batch=100, cooldown=120,
                       fetcher=None, sleeper=time.sleep, log=print):
    """時価総額が空の銘柄だけを、少しずつ・ゆっくり取り直す。
    診断では失敗の約97%が『一時的』だった。連続アクセスでYahooの制限にかかると回収できなくなるので、
    100銘柄ごとに結果を見て、回収が半分未満なら制限中とみなして待つ。
    2回連続で0件なら終了。各バッチの後にキャッシュへ保存する。取れない銘柄は推定で埋めない。"""
    fetcher = fetcher or fetch_mcaps
    stuck = 0
    for rnd in range(max_rounds):
        todo = list(mcap[mcap.isna()].index)
        if not todo:
            break
        log(f"時価総額 取得 {rnd + 1}回目: {len(todo)}銘柄 (同時{workers}本, 各{pause}秒待ち, {batch}銘柄ずつ)")
        rec_total = 0
        for k in range(0, len(todo), batch):
            part = todo[k:k + batch]
            MCAP_ERRORS.clear()
            mcap.update(fetcher(part, workers=workers, pause=pause))
            rec = len(part) - int(mcap[part].isna().sum())
            rec_total += rec
            if cache_file is not None:
                mcap.dropna().to_csv(cache_file)
            if rec < 0.5 * len(part):
                log(f"  バッチ{k // batch + 1}: {rec}/{len(part)}件 → アクセス制限の可能性。{cooldown}秒待ちます "
                    f"(失敗理由: {MCAP_ERRORS.most_common(2)})")
                sleeper(cooldown)
        left = int(mcap.isna().sum())
        log(f"  この回の回収 {rec_total}件 / 残り {left}件")
        stuck = stuck + 1 if rec_total == 0 else 0
        if stuck >= 2:
            log("  2回連続で回収0件 → 恒久的に取得できない銘柄とみなして終了")
            break
    return mcap


# ---------------- 株価 ----------------
def download_prices(tickers, start, chunk=200, retries=2):
    import yfinance as yf
    O, C, V = [], [], []
    for k in range(0, len(tickers), chunk):
        part = tickers[k:k + chunk]
        df = None
        for _ in range(retries + 1):
            try:
                df = yf.download(part, start=start, interval="1d", auto_adjust=True,
                                 threads=True, progress=False)
                break
            except Exception:  # noqa: BLE001
                time.sleep(5)
        if df is None or df.empty:
            continue
        for lst, field in ((O, "Open"), (C, "Close"), (V, "Volume")):
            x = df[field]
            if isinstance(x, pd.Series):
                x = x.to_frame(part[0])
            lst.append(x)
        print(f"  株価取得 {min(k + chunk, len(tickers))}/{len(tickers)}")
    cat = lambda L: pd.concat(L, axis=1).sort_index()  # noqa: E731
    o, c, v = cat(O), cat(C), cat(V)
    keep = c.columns[c.notna().sum() >= 252]
    return o[keep], c[keep], v[keep]


# ---------------- 保存・読込 ----------------
def save_panel(o, c, v, mcap, d=DATA_DIR):
    d.mkdir(exist_ok=True)
    spy_o, spy_c = o["SPY"], c["SPY"]
    stocks = [t for t in c.columns if t != "SPY"]
    o[stocks].to_pickle(d / "open.pkl")
    c[stocks].to_pickle(d / "close.pkl")
    v[stocks].to_pickle(d / "volume.pkl")
    pd.DataFrame({"open": spy_o, "close": spy_c}).to_pickle(d / "spy.pkl")
    mcap.to_csv(d / "mcap.csv")
    (d / "meta.json").write_text(json.dumps({
        "data_end": str(c.index.max().date()), "data_start": str(c.index.min().date()),
        "n_tickers": len(stocks), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1))


def load_panel(d=DATA_DIR):
    o, c, v = (pd.read_pickle(d / f"{n}.pkl") for n in ("open", "close", "volume"))
    spy = pd.read_pickle(d / "spy.pkl")
    mcap = pd.read_csv(d / "mcap.csv", index_col=0).iloc[:, 0]
    last = c.ffill().iloc[-1]
    shares = (mcap.reindex(c.columns) / last).replace([np.inf, -np.inf], np.nan)
    return Panel(o, c, v, spy["open"], spy["close"], shares)


def load_meta(d=DATA_DIR):
    return json.loads((d / "meta.json").read_text())


# =====================================================================
# データ修正用（STEP4b）: 取得失敗を黙って落とさず、理由と共に記録する
# =====================================================================
def _extract(df, part):
    """yf.downloadの戻り値 -> {ticker: (open, close, volume)}。列の並びが2通りあるので両方に対応。"""
    out = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = set(df.columns.get_level_values(0))
        if "Close" in lv0:  # (field, ticker)
            O, C, V = df["Open"], df["Close"], df["Volume"]
            for t in C.columns:
                out[t] = (O[t], C[t], V[t])
        else:               # (ticker, field)
            for t in lv0:
                out[t] = (df[t]["Open"], df[t]["Close"], df[t]["Volume"])
    else:
        out[part[0]] = (df["Open"], df["Close"], df["Volume"])
    return out


def download_robust(tickers, start, chunk=100, rounds=3, min_rows=252, sleeper=time.sleep, log=print):
    """失敗した銘柄だけを、チャンクを小さくしながら最大rounds回やり直す。
    戻り値: (open, close, volume, coverage)  coverageは全銘柄の取得状況(成功/履歴不足/失敗と理由)。
    ★ 取得できなかった銘柄は推定で埋めず、coverageに理由つきで残す。"""
    import yfinance as yf
    got, reasons = {}, {}
    pending = list(tickers)
    for rnd in range(rounds + 1):
        if not pending:
            break
        size = chunk if rnd == 0 else max(10, chunk // (2 ** rnd))
        if rnd > 0:
            log(f"  再取得 {rnd}回目: 残り{len(pending)}銘柄 (チャンク{size})")
            sleeper(5 * rnd)
        still = []
        for k in range(0, len(pending), size):
            part = pending[k:k + size]
            try:
                df = yf.download(part, start=start, interval="1d", auto_adjust=True,
                                 threads=(rnd == 0), progress=False)
            except Exception as e:  # noqa: BLE001
                df = None
                for t in part:
                    reasons[t] = f"download_error: {str(e)[:80]}"
            ex = _extract(df, part)
            errs = getattr(getattr(yf, "shared", None), "_ERRORS", {}) or {}
            for t in part:
                if t in ex and ex[t][1].notna().sum() > 0:
                    got[t] = ex[t]
                else:
                    still.append(t)
                    reasons.setdefault(t, str(errs.get(t, "no_data"))[:80])
            if rnd == 0:
                log(f"  株価取得 {min(k + size, len(pending))}/{len(pending)}")
        pending = still
    rows = []
    for t in tickers:
        if t in got:
            c = got[t][1].dropna()
            n = len(c)
            rows.append(dict(ticker=t, n_rows=n, first_date=str(c.index.min().date()), last_date=str(c.index.max().date()),
                             status="ok" if n >= min_rows else "short_history", reason="" if n >= min_rows else f"{n}日分のみ"))
        else:
            rows.append(dict(ticker=t, n_rows=0, first_date="", last_date="", status="failed", reason=reasons.get(t, "no_data")))
    cov = pd.DataFrame(rows)
    ok = list(cov.loc[cov.status == "ok", "ticker"])
    cat = lambda i: pd.concat([got[t][i].rename(t) for t in ok], axis=1).sort_index()  # noqa: E731
    return cat(0), cat(1), cat(2), cov


def apply_approved_fixes(panel, path):
    """人が原因を確認して承認した修正だけを適用する。ファイルが無ければ何もしない。
    列: ticker, action(drop_ticker | nan_range), date_from, date_to, reason(必須)
    ★ 自動では一切作られない。audit_data.py の一覧を見て、人が書く。"""
    path = Path(path)
    if not path.exists():
        return panel, []
    fx = pd.read_csv(path)
    applied = []
    o, c, v, sh = panel.open.copy(), panel.close.copy(), panel.volume.copy(), panel.shares.copy()
    for _, r in fx.iterrows():
        assert isinstance(r.get("reason"), str) and r["reason"].strip(), f"理由の無い修正は適用できません: {r.to_dict()}"
        t = r["ticker"]
        if t not in c.columns:
            continue
        if r["action"] == "drop_ticker":
            o, c, v = o.drop(columns=t), c.drop(columns=t), v.drop(columns=t)
            sh = sh.drop(index=t, errors="ignore")
        elif r["action"] == "nan_range":
            sl = (c.index >= pd.Timestamp(r["date_from"])) & (c.index <= pd.Timestamp(r["date_to"]))
            o.loc[sl, t], c.loc[sl, t], v.loc[sl, t] = np.nan, np.nan, np.nan
        else:
            raise ValueError(f"未知のaction: {r['action']}")
        applied.append(r.to_dict())
    return Panel(o, c, v, panel.spy_open, panel.spy_close, sh), applied
