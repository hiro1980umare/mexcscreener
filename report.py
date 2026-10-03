import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
import paper

TOKEN = os.environ.get("TG_TOKEN", "").strip()
CHAT = os.environ.get("TG_CHAT", "").strip()
BASE = "https://api.mexc.com"
STATE = "report_state.json"
START_CASH = 100.0
INTERVAL = 12 * 3600
JST = timezone(timedelta(hours=9))


def send(text):
    r = requests.post(
        "https://api.telegram.org/bot" + TOKEN + "/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=20)
    return r.status_code


def get_price(sym):
    try:
        r = requests.get(BASE + "/api/v3/ticker/price",
                         params={"symbol": sym}, timeout=20).json()
        return float(r["price"])
    except Exception:
        return None


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"start_cash": START_CASH, "last_report_ts": 0}


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def main():
    st = load_state()
    now = time.time()
    if now - st.get("last_report_ts", 0) < INTERVAL:
        print("レポートはまだ送りません")
        return
    start = st.get("start_cash", START_CASH)
    opens = paper.load_open()
    trades = paper.load_trades()
    pnls = [float(t["pnl"]) for t in trades]
    realized = sum(pnls)
    wins = len([p for p in pnls if p > 0])
    losses = len(pnls) - wins
    rate = wins / len(pnls) * 100 if pnls else 0

    cash = start + realized
    hold_value = 0.0
    unreal = 0.0
    blocks = []
    for p in opens:
        price = get_price(p["sym"])
        if price is None:
            price = p.get("last_price", p["buy_price"])
        value = p["qty"] * price
        u = value - paper.PAPER_USD - p["buy_fee"]
        cash -= paper.PAPER_USD + p["buy_fee"]
        hold_value += value
        unreal += u
        hrs = (now - p["buy_ts"]) / 3600
        pct = u / paper.PAPER_USD * 100
        blocks.append("\n".join([
            p["sym"][:-4] + "/USDT",
            f"買値：${p['buy_price']}",
            f"現在値：${price}",
            f"数量：{p['qty']:.8f}",
            f"評価額：${value:.4f}",
            f"損益：{money(u)} ({pct:+.1f}%)",
            f"保有時間：{hrs:.0f}時間",
        ]))

    total = cash + hold_value
    pnl = total - start
    pnl_pct = pnl / start * 100
    nowtxt = datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d %H:%M")

    lines = [
        "📊 ペーパートレード途中経過",
        "日時：" + nowtxt,
        "",
        "【資産状況】",
        f"開始資金：${start:.2f}",
        f"現在の現金：${cash:.2f}",
        f"保有銘柄評価額：${hold_value:.2f}",
        f"現在の総資産：${total:.2f}",
        f"損益：{money(pnl)}",
        f"損益率：{pnl_pct:+.2f}%",
        "",
        "【現在の保有銘柄】",
    ]
    if blocks:
        lines.append("\n\n".join(blocks))
    else:
        lines.append("なし")
    lines += [
        "",
        "【取引状況】",
        f"累計取引：{len(pnls)}",
        f"勝ち：{wins}",
        f"負け：{losses}",
        f"勝率：{rate:.1f}%",
        f"確定損益：{money(realized)}",
        f"含み損益：{money(unreal)}",
        f"総合損益：{money(realized + unreal)}",
        "",
        "※仮想取引です。実際の注文は出していません",
    ]
    code = send("\n".join(lines))
    print("途中経過レポート:", code)
    if code == 200:
        st["start_cash"] = start
        st["last_report_ts"] = now
        with open(STATE, "w") as f:
            json.dump(st, f)


main()
# END
