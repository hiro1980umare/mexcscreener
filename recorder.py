import os
import csv
import json
import time
import requests
from datetime import datetime, timezone, timedelta

BASE = "https://api.mexc.com"
JST = timezone(timedelta(hours=9))
OPEN_FILE = "paper_open.json"
SIGNALS = "signals.csv"
BARS = "market_bars.csv"
STATE = "recorder_state.json"

TRACK_SECONDS = 7 * 86400 + 3600
NEW_WINDOW = 1800
RETRY_LIMIT = 3600
TIME_BUDGET = 240
LOOKBACK = 4
VOL_DANGER = 20.0
DANGER_QV = 2_000_000
PERIOD = {"15m": 900000, "60m": 3600000, "4h": 14400000}

SIG_COLS = ["signal_id", "symbol", "signal_ts", "signal_time_jst",
            "detect_ts", "delay_sec", "price", "break_level",
            "hit_ts", "hit_time_jst", "qv24h_usdt", "chg24_pct",
            "label_at_hit", "label_now", "rsi_min_at_hit", "vmax_at_hit",
            "rsi_15m_now", "rsi_1h", "rsi_4h",
            "btc_price", "btc_chg24_pct", "complete", "missing"]
BAR_COLS = ["symbol", "bar_open_ms", "bar_open_jst", "open", "high",
            "low", "close", "volume", "recorded_ts"]


def fmt(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M")


def fnum(x):
    try:
        return float(x)
    except Exception:
        return None


def get_json(path, params=None):
    try:
        r = requests.get(BASE + path, params=params, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def open_ms(x):
    t = int(x[0])
    if t < 10**11:
        t = t * 1000
    return t


def get_bars(sym, interval, limit, now):
    k = get_json("/api/v3/klines",
                 {"symbol": sym, "interval": interval, "limit": limit})
    if not isinstance(k, list):
        return None
    out = []
    for x in k:
        try:
            t = open_ms(x)
            if t + PERIOD[interval] <= now * 1000 - 5000:
                out.append((t, float(x[1]), float(x[2]), float(x[3]),
                            float(x[4]), float(x[5])))
        except Exception:
            continue
    return out


def upto(bars, ts):
    return [b for b in bars if b[0] + 900000 <= ts * 1000 - 5000]


def calc_rsi(closes, n=14):
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def indicators(bars):
    if bars is None or len(bars) < 30:
        return None
    opens = [b[1] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    rsis = []
    ratios = []
    for j in range(1, LOOKBACK + 1):
        rsis.append(calc_rsi(closes[:len(closes) - (j - 1)]))
        avg = sum(vols[-j - 20:-j]) / 20
        ratios.append(vols[-j] / avg if avg > 0 else 0)
    return {
        "rmin": min(rsis), "rnow": rsis[0], "vmax": max(ratios),
        "bull": closes[-1] > opens[-1],
        "chg1h": (closes[-1] / closes[-5] - 1) * 100,
        "newlow": min(lows[-23:-3]) > min(lows[-3:]),
    }


def classify(ind, qv, chg24):
    if ind is None or qv is None or chg24 is None:
        return ""
    if ind["vmax"] >= VOL_DANGER:
        return "④ 危険（出来高が異常）"
    if qv < DANGER_QV:
        return "④ 危険（取引高が少ない）"
    if chg24 <= -30:
        return "④ 危険（24hで大暴落）"
    if ind["bull"] and ind["chg1h"] > 0 and not ind["newlow"]:
        return "③ 反発開始の可能性"
    if not ind["newlow"]:
        return "② 下げ止まりの兆候あり"
    return "① 急落中"


def rsi_tf(sym, interval, now):
    bars = get_bars(sym, interval, 100, now)
    if bars is None or len(bars) < 30:
        return None
    return calc_rsi([b[4] for b in bars])


def ensure_header(path, cols):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(cols)
        return True
    with open(path, newline="", encoding="utf-8") as f:
        head = next(csv.reader(f), [])
    if head != cols:
        print("既存の", path, "の列が想定と違うため、書き込みを中止:", head)
        return False
    return True


def read_rows(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def append_rows(path, cols, rows):
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        for r in rows:
            wr.writerow([r.get(c, "") for c in cols])


def load_open():
    if not os.path.exists(OPEN_FILE):
        return []
    try:
        with open(OPEN_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, list) else None
    except Exception:
        return None


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE + ".tmp", STATE)


def build_signal(p, now):
    miss = []
    sym = p["sym"]
    try:
        hit_ts = float(str(p["id"]).rsplit("_", 1)[1])
    except Exception:
        hit_ts = None
        miss.append("hit_ts")
    t24 = get_json("/api/v3/ticker/24hr", {"symbol": sym})
    qv = chg24 = None
    if isinstance(t24, dict):
        qv = fnum(t24.get("quoteVolume"))
        c = fnum(t24.get("priceChangePercent"))
        chg24 = c * 100 if c is not None else None
    if qv is None:
        miss.append("qv24h")
    if chg24 is None:
        miss.append("chg24")
    bt = get_json("/api/v3/ticker/24hr", {"symbol": "BTCUSDT"})
    btc = btc_c = None
    if isinstance(bt, dict):
        btc = fnum(bt.get("lastPrice"))
        c = fnum(bt.get("priceChangePercent"))
        btc_c = c * 100 if c is not None else None
    if btc is None:
        miss.append("btc")
    b15 = get_bars(sym, "15m", 100, now)
    ind_now = indicators(b15)
    ind_hit = None
    if b15 is not None and hit_ts is not None:
        ind_hit = indicators(upto(b15, hit_ts))
    if ind_now is None:
        miss.append("rsi_15m")
    if ind_hit is None:
        miss.append("at_hit")
    r1 = rsi_tf(sym, "60m", now)
    r4 = rsi_tf(sym, "4h", now)
    if r1 is None:
        miss.append("rsi_1h")
    if r4 is None:
        miss.append("rsi_4h")

    def v(x, nd=4):
        return "" if x is None else round(x, nd)

    row = {
        "signal_id": p["id"], "symbol": sym,
        "signal_ts": p["buy_ts"], "signal_time_jst": fmt(p["buy_ts"]),
        "detect_ts": round(now, 1),
        "delay_sec": round(now - p["buy_ts"], 1),
        "price": p.get("buy_price", ""),
        "break_level": p.get("break_level", ""),
        "hit_ts": "" if hit_ts is None else hit_ts,
        "hit_time_jst": "" if hit_ts is None else fmt(hit_ts),
        "qv24h_usdt": v(qv, 0), "chg24_pct": v(chg24, 2),
        "label_at_hit": classify(ind_hit, qv, chg24),
        "label_now": classify(ind_now, qv, chg24),
        "rsi_min_at_hit": v(ind_hit["rmin"], 2) if ind_hit else "",
        "vmax_at_hit": v(ind_hit["vmax"], 2) if ind_hit else "",
        "rsi_15m_now": v(ind_now["rnow"], 2) if ind_now else "",
        "rsi_1h": v(r1, 2), "rsi_4h": v(r4, 2),
        "btc_price": v(btc, 2), "btc_chg24_pct": v(btc_c, 2),
        "complete": 0 if miss else 1, "missing": "|".join(miss),
    }
    critical = qv is None or btc is None or b15 is None
    return row, critical


def track_bars(now, t0):
    sigs = read_rows(SIGNALS)
    start = {}
    for r in sigs:
        ts = fnum(r.get("signal_ts"))
        if ts is None or now - ts > TRACK_SECONDS:
            continue
        s = int(ts // 900 * 900 * 1000)
        sym = r["symbol"]
        start[sym] = min(start.get(sym, s), s)
    seen = set((r["symbol"], r["bar_open_ms"]) for r in read_rows(BARS))
    total = 0
    for sym, s in start.items():
        if time.time() - t0 > TIME_BUDGET:
            print("時間切れ。残りは次回")
            break
        bars = get_bars(sym, "15m", 100, now)
        if bars is None:
            print("バー取得失敗:", sym)
            continue
        rows = []
        for b in bars:
            if b[0] < s or (sym, str(b[0])) in seen:
                continue
            rows.append({
                "symbol": sym, "bar_open_ms": b[0],
                "bar_open_jst": fmt(b[0] / 1000),
                "open": b[1], "high": b[2], "low": b[3], "close": b[4],
                "volume": b[5], "recorded_ts": round(now, 1)})
        append_rows(BARS, BAR_COLS, rows)
        total += len(rows)
        time.sleep(0.1)
    print("追跡銘柄数:", len(start), "追加バー数:", total)


def main(now=None):
    t0 = time.time()
    now = now or time.time()
    opens = load_open()
    if opens is None:
        print("paper_open.json を読めないため今回は何もしません")
        return
    if not ensure_header(SIGNALS, SIG_COLS):
        return
    if not ensure_header(BARS, BAR_COLS):
        return
    state = load_state()
    ids_now = set(p["id"] for p in opens)
    if not state.get("initialized"):
        base = [p["id"] for p in opens
                if now - p["buy_ts"] >= NEW_WINDOW]
        state = {"initialized": True, "baseline": base}
        save_state(state)
        print("初期化: 既存の保有", len(base), "件は記録対象外にしました")
    baseline = set(state.get("baseline", []))
    seen = set(r["signal_id"] for r in read_rows(SIGNALS))
    new = [p for p in opens
           if p["id"] not in seen and p["id"] not in baseline]
    done = 0
    for p in new:
        if time.time() - t0 > TIME_BUDGET:
            break
        row, critical = build_signal(p, now)
        if critical and now - p["buy_ts"] < RETRY_LIMIT:
            print("取得失敗のため次回再試行:", p["id"])
            continue
        append_rows(SIGNALS, SIG_COLS, [row])
        done += 1
        print("シグナル記録:", p["id"], "complete=", row["complete"],
              row["missing"])
    print("新規シグナル:", done, "/", len(new))
    track_bars(now, t0)
    keep = [i for i in state.get("baseline", []) if i in ids_now]
    if keep != state.get("baseline", []):
        state["baseline"] = keep
        save_state(state)


if __name__ == "__main__":
    main()
# END