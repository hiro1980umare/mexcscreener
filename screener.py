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

# ---- 第3段階（反発確認）の設定 ----
REBOUND_FILE = "rebound.json"
REBOUND_EXPIRE = 3 * 3600
BREAK_BY = "high"   # "high"=確定足の高値で判定 / "close"=確定足の終値で判定
try:
    with open(REBOUND_FILE) as f:
        rebound = json.load(f)
except Exception:
    rebound = {}

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
        f"RSI: 今 {r['rnow']
