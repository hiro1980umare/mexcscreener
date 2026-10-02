import os
import csv
import json
from datetime import datetime, timezone, timedelta

PAPER_USD = 1.0
FEE_RATE = 0.001
HOLD_DAYS = 7
HOLD_SECONDS = HOLD_DAYS * 86400
PAPER_GRACE = 2 * 86400
ONE_OPEN_PER_SYMBOL = False

OPEN_FILE = "paper_open.json"
TRADES_FILE = "paper_trades.csv"
STATS_FILE = "paper_stats.json"
COLS = ["id", "symbol", "buy_time", "buy_price", "qty", "sell_time",
        "sell_price", "hold_hours", "buy_fee", "sell_fee", "pnl",
        "pnl_pct", "high", "low", "note"]
JST = timezone(timedelta(hours=9))


def fmt_time(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")


def fmt_full(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M")


def load_open():
    try:
        with open(OPEN_FILE) as f:
            return json.load(f)
    except Exception:
        return []


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


def open_paper(sym, w, price, level, now, send):
    paper_open = load_open()
    pid = sym + "_" + str(int(w["hit_ts"]))
    if any(p["id"] == pid for p in paper_open):
        return
    if pid in set(t["id"] for t in load_trades()):
        return
    if ONE_OPEN_PER_SYMBOL and any(p["sym"] == sym for p in paper_open):
        return
    qty = PAPER_USD / price
    p = {
        "id": pid, "sym": sym, "buy_ts": now, "buy_price": price,
        "qty": qty, "buy_fee": PAPER_USD * FEE_RATE,
        "break_level": level, "high": price, "low": price,
        "last_price": price,
    }
    paper_open.append(p)
    save_open(paper_open)
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
    print("ペーパー購入:", sym, send(text))


def save_open(paper_open):
    with open(OPEN_FILE, "w") as f:
        json.dump(paper_open, f, ensure_ascii=False, indent=1)


def close_paper(paper_open, p, price, now, sell_fee, pnl, pct, note, send):
    hold_h = (now - p["buy_ts"]) / 3600
    row = [p["id"], p["sym"], fmt_full(p["buy_ts"]), p["buy_price"],
           p["qty"], fmt_full(now), price, round(hold_h, 2),
           round(p["buy_fee"], 6), round(sell_fee, 6), round(pnl, 6),
           round(pct, 4), p["high"], p["low"], note]
    new_file = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        if new_file:
            wr.writerow(COLS)
        wr.writerow(row)
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
    print("ペーパー決済:", p["sym"], send("\n".join(lines)))


def check_paper(now, get_price, send):
    paper_open = load_open()
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
                continue
        value, sell_fee, pnl, pct = calc_pnl(p, price)
        p["now"] = {
            "price": price, "value": round(value, 6),
            "pnl": round(pnl, 6), "pnl_pct": round(pct, 4),
            "elapsed_hours": round(age / 3600, 2),
        }
        if due:
            close_paper(paper_open, p, price, now, sell_fee, pnl, pct,
                        note, send)
    save_open(paper_open)
    with open(STATS_FILE, "w") as f:
        json.dump(calc_stats(load_trades()), f, ensure_ascii=False, indent=1)
# END
