import os
import time
import json
import requests
import paper
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

# ---- 第3段階（反発確認）の設定 ----
REBOUND_FILE = "rebound.json"
REBOUND_EXPIRE = 3 * 3600
BREAK_BY = "high"
try:
    with open(REBOUND_FILE) as f:
        rebound = json.load(f)
except Exception:
    rebound = {}

def fmt_time(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")

API = "https://api.telegram.org/bot" + TOKEN

def send(text):
    r = requests.post(API + "/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=20)
    return r.status_code

def get_price(sym):
    try:
        r = requests.get(BASE+"/api/v3/ticker/price",
            params={"symbol": sym}, timeout=20).json()
        return float(r["price"])
    except Exception:
        return None

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

# ---- ローソク足の補助 ----
def open_ms(x):
    t = int(x[0])
    if t < 10**11:
        t = t * 1000
    return t

def confirmed(k, now):
    limit = now * 1000 - 5000
    return [x for x in k if open_ms(x) + 900000 <= limit]

# ---- 第2段階：ヒット後の追跡 ----
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
                conf = confirmed(k, now)
                if len(conf) > 0:
                    ref = conf[-1]
                    rebound[sym] = {
                        "t2": now,
                        "hit_ts": w["hit_ts"],
                        "hit_low": w["hit_low"],
                        "rmin": w.get("rmin"),
                        "vmax": w.get("vmax"),
                        "ref_high": float(ref[2]),
                        "ref_open_ms": open_ms(ref),
                    }
                del watch[sym]
        else:
            print("監視中:", sym)

# ---- 第3段階：反発確認 ----
def check_rebound(now):
    for sym in list(rebound.keys()):
        w = rebound[sym]
        if now - w["t2"] >= REBOUND_EXPIRE:
            print("反発待ち期限切れ:", sym)
            del rebound[sym]
            continue
        try:
            k = requests.get(BASE+"/api/v3/klines",
                params={"symbol": sym, "interval": "15m", "limit": 100},
                timeout=20).json()
            if not isinstance(k, list):
                continue
            if len(k) == 0:
                continue
            conf = confirmed(k, now)
            price = float(k[-1][4])
        except Exception as e:
            print("反発追跡エラー:", sym, e)
            continue

        found = None
        for i, x in enumerate(conf):
            if open_ms(x) <= w["ref_open_ms"]:
                continue
            if BREAK_BY == "close":
                level = float(x[4])
            else:
                level = float(x[2])
            if level > w["ref_high"]:
                found = (i, x, level)
                break

        if found is None:
            print("反発待ち:", sym)
            continue

        i, x, level = found
        vol = float(x[5])
        if i >= 20:
            avg = sum(float(y[5]) for y in conf[i-20:i]) / 20
        else:
            avg = 0
        ratio = vol / avg if avg > 0 else 0

        r_rsi = w.get("rmin")
        r_vol = w.get("vmax")
        rsi_text = f"{r_rsi:.1f}" if r_rsi is not None else "不明"
        vol_text = f"{r_vol:.2f}x" if r_vol is not None else "不明"

        text = "\n".join([
            "🟢 反発確認",
            "",
            "銘柄: " + sym[:-4] + "/USDT",
            "",
            "第1段階:",
            f"RSI: {rsi_text}",
            f"出来高倍率: {vol_text}",
            "ヒット時刻: " + fmt_time(w["hit_ts"]),
            "",
            "第2段階:",
            "下げ止まり確認: " + fmt_time(w["t2"]),
            "監視時間: 30分",
            "",
            "第3段階:",
            f"突破価格: {level}",
            f"直前高値: {w['ref_high']}",
            f"現在価格: {price}",
            "",
            f"突破時出来高: {vol:,.0f}",
            f"20本平均: {avg:,.0f}",
            f"出来高倍率: {ratio:.2f}x",
            "",
            "判定: 反発確認・買い候補",
            "※注文は出していません。買いシグナルではなく確認材料です",
        ])
        code = send(text)
        print("反発確認通知:", sym, code)
        if code == 200:
            del rebound[sym]
            paper.open_paper(sym, w, price, level, now, send)
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
check_rebound(now)
paper.check_paper(now, get_price, send)

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
            "rmin": r["rmin"],
            "vmax": r["vmax"],
        }
    sent += 1

with open(STATE_FILE, "w") as f:
    json.dump(notified, f)
with open(WATCH_FILE, "w") as f:
    json.dump(watch, f)
with open(REBOUND_FILE, "w") as f:
    json.dump(rebound, f)

if TEST_MODE and sent == 0:
    code = send(f"スキャン完了：対象{len(rows)}銘柄、該当{len(hits)}銘柄（通知なし）")
    print("テスト通知:", code)
# END
