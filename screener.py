import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("TG_TOKEN", "").strip()
CHAT = os.environ.get("TG_CHAT", "").strip()
TEST_MODE = os.environ.get("TEST_MODE", "1") == "1"

BASE = "https://api.mexc.com"
MIN_QUOTE_VOLUME = 1_000_000
STABLES = {"USDC","FDUSD","TUSD","DAI","USDD","USDE",
           "USDP","PYUSD","BUSD","USD1","EUR","AEUR","USDGO"}

RSI_MAX = 30
VOL_MULT = 2.0
LOOKBACK = 4
VOL_DANGER = 20.0
DANGER_QV = 2_000_000
MAX_NOTIFY = 5

STATE_FILE = "notified.json"
COOLDOWN = 2 * 3600
try:
    with open(STATE_FILE) as f:
        notified = json.load(f)
except Exception:
    notified = {}

# ---- 第2段階（下げ止まり判定）の設定 ----
WATCH_FILE = "watch.json"
WATCH_SECONDS = 30 * 60
WATCH_EXPIRE = 3 * 3600
try:
    with open(WATCH_FILE) as f:
        watch = json.load(f)
except Exception:
    watch = {}
JST = timezone(timedelta(hours=9))

def fmt_time(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")
# ------------------------------------------

API = "https://api.telegram.org/bot" + TOKEN

def send(text):
    r = requests.post(API + "/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=20)
    return r.status_code

def calc_rsi(closes, n=14):
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag*(n-1) + gains[i]) / n
        al = (al*(n-1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag/al)

def classify(r):
    if r["vmax"] >= VOL_DANGER:
        return "④ 危険（出来高が異常）"
    if r["qv"] < DANGER_QV:
        return "④ 危険（取引高が少ない）"
    if r["chg24"] <= -30:
        return "④ 危険（24hで大暴落）"
    if r["bull"] and r["chg1h"] > 0 and not r["newlow"]:
        return "③ 反発開始の可能性"
    if not r["newlow"]:
        return "② 下げ止まりの兆候あり"
    return "① 急落中"

def tf_info(sym, interval):
    try:
        k = requests.get(BASE+"/api/v3/klines",
            params={"symbol": sym, "interval": interval, "limit": 100},
            timeout=20).json()
        if not isinstance(k, list):
            return None
        if not len(k) >= 40:
            return None
        k = k[:-1]
        closes = [float(x[4]) for x in k]
        highs = [float(x[2]) for x in k]
        hi = max(highs[-20:])
        return {"rsi": calc_rsi(closes),
                "fromhi": (closes[-1] / hi - 1) * 100}
    except Exception:
        return None

def tf_text(name, info):
    if info is None:
        return name + ": データなし"
    return f"{name}: RSI {info['rsi']:.1f} / 高値から {info['fromhi']:+.1f}%"

def make_text(r, ts):
    lines = [
        "🔴 急落候補",
        "銘柄: " + r["sym"][:-4] + "/USDT",
        "判定: " + r["label"],
        f"現在価格: {r['price']}",
        f"ヒット時刻: {fmt_time(ts)}",
        f"RSI: 今 {r['rnow']:.1f} / 直近{LOOKBACK}本最低 {r['rmin']:.1f}",
        f"出来高倍率: 今 {r['vnow']:.2f}x / 最大 {r['vmax']:.2f}x",
        f"24h: {r['chg24']:+.1f}%  1h: {r['chg1h']:+.1f}%",
        "ローソク足: " + ("陽線" if r["bull"] else "陰線"),
        "安値更新: " + ("あり" if r["newlow"] else "なし"),
        f"24h取引高: {r['qv']:,.0f} USDT",
        tf_text("15分", tf_info(r["sym"], "15m")),
        tf_text("1時間", tf_info(r["sym"], "60m")),
        tf_text("4時間", tf_info(r["sym"], "4h")),
        "※買いシグナルではなく監視候補です",
    ]
    return "\n".join(lines)

# ---- 第2段階：ヒット後の追跡 ----
def open_ms(x):
    t = int(x[0])
    if t < 10**11:
        t = t * 1000
    return t

def check_watch(now):
    for sym in list(watch.keys()):
        w = watch[sym]
        if now - w["hit_ts"] >= WATCH_EXPIRE:
            print("監視期限切れ:", sym)
            del watch[sym]
            continue
        try:
            k = requests.get(BASE+"/api/v3/klines",
                params={"symbol": sym, "interval": "15m", "limit": 100},
                timeout=20).json()
            if not isinstance(k, list):
                continue
            if len(k) == 0:
                continue
            start_ms = int(w["hit_ts"] // 900 * 900 * 1000)
            lows_after = [float(x[3]) for x in k if open_ms(x) >= start_ms]
            if len(lows_after) == 0:
                continue
            low_after = min(lows_after)
            price = float(k[-1][4])
        except Exception as e:
            print("追跡エラー:", sym, e)
            continue

        if low_after < w["hit_low"]:
            print("まだ下落中（監視終了）:", sym)
            del watch[sym]
            continue

        if now - w["hit_ts"] >= WATCH_SECONDS:
            text = "\n".join([
                "🟡 下げ止まり候補",
                "銘柄: " + sym[:-4] + "/USDT",
                "第1段階ヒット時刻: " + fmt_time(w["hit_ts"]),
                "監視時間: 30分",
                f"ヒット時安値: {w['hit_low']}",
                f"現在価格: {price}",
                "その間の安値更新: なし",
                "※買いシグナルではなく監視候補です",
            ])
            code = send(text)
            print("下げ止まり通知:", sym, code)
            if code == 200:
                del watch[sym]
        else:
            print("監視中:", sym)
# ------------------------------------------

tick = requests.get(BASE+"/api/v3/ticker/24hr", timeout=20).json()
tmap = {}
symbols = []
for d in tick:
    s = d["symbol"]
    if not s.endswith("USDT"):
        continue
    if s[:-4] in STABLES:
        continue
    if float(d["quoteVolume"] or 0) >= MIN_QUOTE_VOLUME:
        symbols.append(s)
        tmap[s] = d

rows = []
for s in symbols:
    try:
        k = requests.get(BASE+"/api/v3/klines",
            params={"symbol": s, "interval": "15m", "limit": 100},
            timeout=20).json()
        if not isinstance(k, list):
            continue
        if not len(k) >= 50:
            continue
        forming_low = float(k[-1][3])
        k = k[:-1]
        opens = [float(x[1]) for x in k]
        lows = [float(x[3]) for x in k]
        closes = [float(x[4]) for x in k]
        vols = [float(x[5]) for x in k]
        rsis = []
        ratios = []
        for j in range(1, LOOKBACK + 1):
            rsis.append(calc_rsi(closes[:len(closes)-(j-1)]))
            avg = sum(vols[-j-20:-j]) / 20
            ratios.append(vols[-j] / avg if avg > 0 else 0)
        t = tmap[s]
        r = {
            "sym": s,
            "rmin": min(rsis),
            "rnow": rsis[0],
            "vmax": max(ratios),
            "vnow": ratios[0],
            "price": float(t["lastPrice"]),
            "chg24": float(t["priceChangePercent"]) * 100,
            "chg1h": (closes[-1] / closes[-5] - 1) * 100,
            "bull": closes[-1] > opens[-1],
            "newlow": min(lows[-23:-3]) > min(lows[-3:]),
            "qv": float(t["quoteVolume"]),
            "hitlow": min(min(lows[-LOOKBACK:]), forming_low),
        }
        r["label"] = classify(r)
        rows.append(r)
    except Exception as e:
        print("スキップ:", s, e)
    time.sleep(0.1)

hits = []
for r in rows:
    if r["rmin"] > RSI_MAX:
        continue
    if not r["vmax"] >= VOL_MULT:
        continue
    hits.append(r)
hits.sort(key=lambda x: x["rmin"])

print("対象銘柄数:", len(rows))
print("該当:", len(hits), "銘柄")

now = time.time()
notified = {k: v for k, v in notified.items() if now - v < COOLDOWN}

check_watch(now)

sent = 0
for r in hits:
    if sent >= MAX_NOTIFY:
        break
    if r["sym"] in notified:
        print("通知済みのためスキップ:", r["sym"])
        continue
    code = send(make_text(r, now))
    print("通知:", r["sym"], code)
    if code == 200:
        notified[r["sym"]] = now
        watch[r["sym"]] = {
            "hit_ts": now,
            "hit_low": r["hitlow"],
            "hit_price": r["price"],
        }
    sent += 1

with open(STATE_FILE, "w") as f:
    json.dump(notified, f)
with open(WATCH_FILE, "w") as f:
    json.dump(watch, f)

if TEST_MODE and sent == 0:
    code = send(f"スキャン完了：対象{len(rows)}銘柄、該当{len(hits)}銘柄（通知なし）")
    print("テスト通知:", code)
