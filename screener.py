import os
import csv
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

# ---- 第4段階（ペーパートレード）の設定 ----
PAPER_USD = 1.0                 # 1回の仮想購入額（USD）
FEE_RATE = 0.001                # 片道の手数料率（0.001 = 0.1%）。MEXCの料率を確認して変更
HOLD_DAYS = 7                   # 保有日数
HOLD_SECONDS = HOLD_DAYS * 86400
PAPER_GRACE = 2 * 86400         # 期限後も価格が取れないとき、この期間を過ぎたら最後の観測値で決済
ONE_OPEN_PER_SYMBOL = False     # True=同じ銘柄の保有中は新しく買わない
PAPER_OPEN_FILE = "paper_open.json"
TRADES_FILE = "paper_trades.csv"
STATS_FILE = "paper_stats.json"
TRADE_COLS = ["id","symbol","buy_time","buy_price","qty","sell_time",
              "sell_price","hold_hours","buy_fee","sell_fee","pnl",
              "pnl_pct","high","low","note"]
try:
    with open(PAPER_OPEN_FILE) as f:
        paper_open = json.load(f)
except Exception:
    paper_open = []
if not os.path.exists(TRADES_FILE):
    with open(TRADES_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(TRADE_COLS)

def fmt_time(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")

def fmt_full(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M")
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

# ---- ローソク足の補助 ----
def open_ms(x):
    t = int(x[0])
    if t < 10**11:
        t = t * 1000
    return t

def confirmed(k, now):
    # 15分が経過して確定した足だけを返す（形成中の足は除く）
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
            open_paper(sym, w, price, level, now)

# ---- 第4段階：ペーパートレード（仮想取引・実注文なし） ----
def load_trades():
    try:
        with open(TRADES_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def calc_stats(trades):
    pnls = [float(t["pnl"]) for t in trades]
    n = len(pnls)
    if n == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate_pct": 0,
                "total_pnl": 0, "avg_pnl": 0, "max_win": 0, "max_loss": 0}
    wins = len([p for p in pnls if p > 0])
    return {
        "total": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(wins / n * 100, 1),
        "total_pnl": round(sum(pnls), 4),
        "avg_pnl": round(sum(pnls) / n, 4),
        "max_win": round(max(pnls), 4),
        "max_loss": round(min(pnls), 4),
    }

def calc_pnl(p, price):
    value = p["qty"] * price
    sell_fee = value * FEE_RATE
    pnl = value - sell_fee - PAPER_USD - p["buy_fee"]
    return value, sell_fee, pnl, pnl / PAPER_USD * 100

def get_price(sym):
    try:
        r = requests.get(BASE+"/api/v3/ticker/price",
            params={"symbol": sym}, timeout=20).json()
        return float(r["price"])
    except Exception:
        return None

def open_paper(sym, w, price, level, now):
    pid = sym + "_" + str(int(w["hit_ts"]))
    if any(p["id"] == pid for p in paper_open):
        print("ペーパー購入済みのためスキップ:", sym)
        return
    if pid in set(t["id"] for t in load_trades()):
        print("ペーパー決済済みのためスキップ:", sym)
        return
    if ONE_OPEN_PER_SYMBOL and any(p["sym"] == sym for p in paper_open):
        print("同銘柄を保有中のためスキップ:", sym)
        return
    qty = PAPER_USD / price
    p = {
        "id": pid,
        "sym": sym,
        "buy_ts": now,
        "buy_price": price,
        "qty": qty,
        "buy_fee": PAPER_USD * FEE_RATE,
        "break_level": level,
        "high": price,
        "low": price,
        "last_price": price,
    }
    paper_open.append(p)
    text = "\n".join([
        "🟢 ペーパー購入",
        "",
        "銘柄: " + sym[:-4] + "/USDT",
        f"購入価格: {price}",
        f"仮想購入額: ${PAPER_USD:g}",
        f"数量: {qty:.8f}",
        "購入時刻: " + fmt_time(now),
        f"購入手数料（想定）: ${p['buy_fee']:.4f}",
        f"決済予定: {HOLD_DAYS}日後",
        "※仮想取引です。実際の注文は出していません",
    ])
    code = send(text)
    print("ペーパー購入:", sym, code)

def close_paper(p, price, now, sell_fee, pnl, pct, note):
    hold_h = (now - p["buy_ts"]) / 3600
    row = [p["id"], p["sym"], fmt_full(p["buy_ts"]), p["buy_price"],
           p["qty"], fmt_full(now), price, round(hold_h, 2),
           round(p["buy_fee"], 6), round(sell_fee, 6), round(pnl, 6),
           round(pct, 4), p["high"], p["low"], note]
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)
    paper_open.remove(p)
    st = calc_stats(load_trades())
    lines = [
        "🔵 ペーパー決済",
        "",
        "銘柄: " + p["sym"][:-4] + "/USDT",
        f"購入価格: {p['buy_price']}",
        f"決済価格: {price}",
        f"保有期間: {hold_h / 24:.1f}日",
        f"損益額: {pnl:+.4f} USD（手数料込み）",
        f"損益率: {pct:+.2f}%",
        f"期間中の最高値: {p['high']}（観測値）",
        f"期間中の最安値: {p['low']}（観測値）",
    ]
    if note:
        lines.append("備考: " + note)
    lines += [
        "",
        "【累計成績】",
        f"取引数 {st['total']} / 勝ち {st['wins']} / 負け {st['losses']} / 勝率 {st['win_rate_pct']}%",
        f"累計損益 {st['total_pnl']:+.4f} USD / 平均 {st['avg_pnl']:+.4f} USD",
        "※仮想取引です。実際の注文は出していません",
    ]
    code = send("\n".join(lines))
    print("ペーパー決済:", p["sym"], code)

def check_paper(now):
    for p in list(paper_open):
        age = now - p["buy_ts"]
        price = get_price(p["sym"])
        note = ""
        if price is not None:
            p["last_price"] = price
            p["high"] = max(p["high"], price)
            p["low"] = min(p["low"], price)
        due = age >= HOLD_SECONDS
        if price is None:
            if due and age >= HOLD_SECONDS + PAPER_GRACE:
                price = p["last_price"]
                note = "価格取得失敗のため最後の観測値で決済"
            else:
                print("価格取得失敗:", p["sym"])
                continue
        value, sell_fee, pnl, pct = calc_pnl(p, price)
        p["now"] = {
            "price": price,
            "value": round(value, 6),
            "pnl": round(pnl, 6),
            "pnl_pct": round(pct, 4),
            "elapsed_hours": round(age / 3600, 2),
        }
        if due:
            close_paper(p, price, now, sell_fee, pnl, pct, note)
        else:
            print(f"ペーパー保有中: {p['sym']} {pct:+.2f}% {age/3600:.1f}h")
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
        rati
