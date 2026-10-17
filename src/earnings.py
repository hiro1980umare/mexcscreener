"""次回決算日。2つの情報源(yfinance, Finnhub)を照合する。取れなければ None のまま(推測で補わない)。"""
import time
from datetime import date, datetime, timedelta

import requests


def to_date(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if hasattr(x, "to_pydatetime"):
        return x.to_pydatetime().date()
    try:
        return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def pick_next(dates, today):
    """今日以降で最も早い日付。無ければ None。"""
    ds = sorted(d for d in (to_date(x) for x in (dates or [])) if d is not None and d >= today)
    return ds[0] if ds else None


def fetch_yf(ticker, today):
    """(日付 or None, エラー文 or None)"""
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        eds = cal.get("Earnings Date") if isinstance(cal, dict) else None
        return pick_next(eds, today), (None if eds else "yfinance: 日付なし")
    except Exception as e:  # noqa: BLE001
        return None, f"yfinance: {type(e).__name__}"


def fetch_finnhub(ticker, today, token, session=None, lookahead_days=120):
    if not token:
        return None, "finnhub: トークン未設定"
    s = session or requests
    try:
        r = s.get("https://finnhub.io/api/v1/calendar/earnings",
                  params={"from": today.isoformat(), "to": (today + timedelta(days=lookahead_days)).isoformat(), "symbol": ticker},
                  headers={"X-Finnhub-Token": token}, timeout=20)
        if r.status_code != 200:
            return None, f"finnhub: HTTP {r.status_code}"
        rows = (r.json() or {}).get("earningsCalendar") or []
        return pick_next([x.get("date") for x in rows], today), (None if rows else "finnhub: 日付なし")
    except Exception as e:  # noqa: BLE001
        return None, f"finnhub: {type(e).__name__}"


def reconcile(a, b, conflict_days=3):
    """2つの情報源の日付を照合。不一致のときは、決算前の注意が漏れないよう、早いほうを採用する。"""
    if a is None and b is None:
        return {"date": None, "status": "UNAVAILABLE", "note": "取得できず(判定不能)"}
    if a is None or b is None:
        d = a or b
        return {"date": d, "status": "SINGLE", "note": "1つの情報源のみ(" + ("yfinance" if a else "Finnhub") + ")"}
    if abs((a - b).days) <= conflict_days:
        return {"date": min(a, b), "status": "CONFIRMED", "note": "2情報源で一致"}
    return {"date": min(a, b), "status": "CONFLICT", "note": f"情報源で不一致(yfinance {a} / Finnhub {b})。早いほうを表示"}


def get_earnings(tickers, today, token="", fetch_yf_fn=fetch_yf, fetch_fh_fn=fetch_finnhub, sleep=time.sleep, pause=1.1):
    out = {}
    for t in tickers:
        a, ea = fetch_yf_fn(t, today)
        b, eb = fetch_fh_fn(t, today, token)
        r = reconcile(a, b)
        r["yf"], r["fh"], r["errors"] = a, b, [e for e in (ea, eb) if e]
        out[t] = r
        if token:
            sleep(pause)                      # Finnhubは60回/分
    return out


def summarize_earnings(earn):
    """決算日の取得状況と、取得できなかった理由の集計(診断用。通知文には入れない)。"""
    from collections import Counter
    st = Counter(v["status"] for v in earn.values())
    er = Counter(e for v in earn.values() for e in v.get("errors", []))
    return ("決算日の取得状況: " + " / ".join(f"{k} {n}" for k, n in sorted(st.items())) +
            ("。取得できなかった理由: " + "; ".join(f"{k} x{n}" for k, n in er.most_common()) if er else ""))
