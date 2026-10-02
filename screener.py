import os
import time
import requests

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

def make_text(r):
    lines = [
        r["sym"][:-4] + "/USDT",
        "判定: " + r["label"],
        f"価格: {r['price']}",
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

sent = 0
for r in hits[:MAX_NOTIFY]:
    code = send(make_text(r))
    print("通知:", r["sym"], code)
    sent += 1

if TEST_MODE and sent == 0:
    code = send(f"スキャン完了：対象{len(rows)}銘柄、該当{len(hits)}銘柄（通知なし）")
    print("テスト通知:", code)
