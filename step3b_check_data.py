#!/usr/bin/env python3
"""
STEP3b: 追加テスト（本体ではありません）
  1) SEC EDGAR: 過去の任意日時点で「すでに提出済みだった財務」だけを取り出せるか（point-in-time）
  2) Finnhub財務指標の欠損率（CROX/DECK含む実際の対象銘柄で）
  3) yfinanceの時価総額取得・欠損率
  4) 1,500銘柄の株価取得時間
  5) FMP limit=5 での決算履歴・四半期財務
  6) SEC のレート制限・User-Agent・速度

方針: 欠損は欠損のまま数える。推定値で埋めない。

使い方（Colab例は別途説明）:
    python step3b_check_data.py --quick     # まず小さく試す(数分)
    python step3b_check_data.py             # 本番(10〜15分)
必要な環境変数: FMP_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT（例: "Taro Yamada taro@example.com"）
"""
import argparse
import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FMP_KEY = os.getenv("FMP_API_KEY", "")
FH_KEY = os.getenv("FINNHUB_API_KEY", "")
SEC_UA = os.getenv("SEC_USER_AGENT", "")  # SECは連絡先入りUser-Agentが必須

FMP = "https://financialmodelingprep.com/stable"
FH = "https://finnhub.io/api/v1"
OUT = Path("api_check_output_b")
OUT.mkdir(exist_ok=True)
MUST = ["CROX", "DECK", "AAPL", "NVDA", "MSFT", "ENPH", "ETSY"]
TODAY = date.today()
LATEST = (TODAY + timedelta(days=1)).isoformat()
ASOFS = ["2023-03-01", "2025-03-01", LATEST]
LOG = []  # レポートに出す文章


def redact(s):
    s = str(s)
    for k in (FMP_KEY, FH_KEY):
        if k:
            s = s.replace(k, "***")
    return s


def pct(n, d):
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a"


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def say(*lines):
    for ln in lines:
        LOG.append(ln)
        print(ln)


# =====================================================
# 0. ユニバース（NASDAQ Trader公開ファイル）
# =====================================================
BAD_NAME = re.compile(r"Warrant|Right|Unit|Preferred|Depositary|Notes|ETF|Fund|Acquisition", re.I)


def load_universe():
    rows = []
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
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
            if not re.fullmatch(r"[A-Z]{1,5}", sym) or BAD_NAME.search(f.get("Security Name", "")):
                continue
            rows.append(sym)
    return sorted(set(rows))


# =====================================================
# 1. yfinance: 時価総額
# =====================================================
def get_mcap(t):
    try:
        import yfinance as yf
        v = getattr(yf.Ticker(t).fast_info, "market_cap", None)
        return t, float(v) if v else None
    except Exception:  # noqa: BLE001
        return t, None


# =====================================================
# 2. Finnhub 財務指標
# =====================================================
GROUPS = {
    "ROE": r"^roe", "営業利益率": r"^operatingMargin", "純利益率": r"^netProfitMargin",
    "売上成長": r"^revenueGrowth", "EPS成長": r"^epsGrowth", "FCF": r"freeCashFlow|pfcf",
    "負債(D/E等)": r"debt.*equity|^currentRatio", "PER": r"^pe[A-Z]", "PSR": r"^ps[A-Z]",
    "PEG": r"peg", "52週高値": r"^52WeekHigh$",
}
CORE = ["ROE", "営業利益率", "売上成長", "EPS成長", "FCF", "負債(D/E等)", "PER"]


def finnhub_metric(sym):
    for attempt in range(2):
        t0 = time.time()
        try:
            r = requests.get(f"{FH}/stock/metric", params={"symbol": sym, "metric": "all"},
                             headers={"X-Finnhub-Token": FH_KEY}, timeout=30)
        except Exception as e:  # noqa: BLE001
            return None, "error", redact(e)[:80], time.time() - t0
        if r.status_code == 429 and attempt == 0:
            time.sleep(5)
            continue
        if r.status_code != 200:
            return None, f"http{r.status_code}", "", time.time() - t0
        m = (r.json() or {}).get("metric") or {}
        return (m if m else None), ("ok" if m else "no_data"), "", time.time() - t0
    return None, "http429", "", 0


def run_finnhub(syms):
    cov = {g: 0 for g in GROUPS}
    keyc = {g: Counter() for g in GROUPS}
    status, times, allcore, per = Counter(), [], 0, {}
    sample_keys = None
    for i, s in enumerate(syms, 1):
        m, st, _, sec = finnhub_metric(s)
        status[st] += 1
        times.append(sec)
        if m:
            sample_keys = sample_keys or sorted(m.keys())
            hit = {}
            for g, rx in GROUPS.items():
                ks = [k for k, v in m.items() if re.search(rx, k, re.I)
                      and isinstance(v, (int, float)) and not isinstance(v, bool)]
                hit[g] = bool(ks)
                if ks:
                    cov[g] += 1
                    keyc[g][ks[0]] += 1
            allcore += all(hit[g] for g in CORE)
            per[s] = hit
        if i % 25 == 0:
            print(f"  Finnhub {i}/{len(syms)}")
        time.sleep(1.05)  # 60回/分
    if sample_keys:
        (OUT / "finnhub_metric_keys.txt").write_text("\n".join(sample_keys), encoding="utf-8")
    return cov, keyc, status, times, allcore, per


# =====================================================
# 3. SEC EDGAR (point-in-time)
# =====================================================
ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT")
FLOW = {  # name: (tags, unit)
    "売上": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
              "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
              "SalesRevenueGoodsNet"], "USD"),
    "営業利益": (["OperatingIncomeLoss"], "USD"),
    "純利益": (["NetIncomeLoss", "ProfitLoss"], "USD"),
    "EPS(希薄化後)": (["EarningsPerShareDiluted"], "USD/shares"),
    "営業CF": (["NetCashProvidedByUsedInOperatingActivities"], "USD"),
    "設備投資": (["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], "USD"),
}
INST = {
    "総資産": (["Assets"], "USD"),
    "総負債": (["Liabilities"], "USD"),
    "株主資本": (["StockholdersEquity",
                  "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "USD"),
    "長期債務": (["LongTermDebtNoncurrent", "LongTermDebt"], "USD"),
    "短期債務": (["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"], "USD"),
    "現金": (["CashAndCashEquivalentsAtCarryingValue"], "USD"),
    "発行済株式数": (["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"], "shares"),
}
SEC_METRICS = list(FLOW) + ["FCF(営業CF-設備投資)"] + list(INST) + ["売上成長(前年比)", "EPS成長(前年比)"]


def cands(facts, tags, unit):
    out = []
    for tag in tags:
        for taxo in ("us-gaap", "dei"):
            d = facts.get("facts", {}).get(taxo, {}).get(tag)
            if d and unit in d.get("units", {}):
                out.append((tag, d["units"][unit]))
    return out


def pit_annual(cs, asof):
    """asof より前に提出された10-Kの通期データだけを、決算期末の新しい順に返す。
    同じ期間が修正(再提出)されている場合は、asof時点で最新の提出分を使う。"""
    best = None
    for _tag, rs in cs:
        keep = {}
        for r in rs:
            if "start" not in r or r.get("form") not in ANNUAL_FORMS:
                continue
            if r.get("filed", "9999") >= asof:  # 当日以降の提出は使わない(保守的)
                continue
            try:
                dd = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
            except (KeyError, ValueError):
                continue
            if not 340 <= dd <= 380:
                continue
            k = (r["start"], r["end"])
            if k not in keep or r["filed"] > keep[k]["filed"]:
                keep[k] = r
        rows = sorted(keep.values(), key=lambda r: r["end"], reverse=True)
        if rows and (best is None or rows[0]["end"] > best[0]["end"]):
            best = rows
    return best or []


def pit_instant(cs, asof):
    best = None
    for _tag, rs in cs:
        for r in rs:
            if "start" in r or r.get("filed", "9999") >= asof or r.get("form") not in ANNUAL_FORMS + ("10-Q", "10-Q/A"):
                continue
            key = (r["end"], r["filed"])
            if best is None or key > (best["end"], best["filed"]):
                best = r
    return best


def sec_metrics(facts, asof):
    m, used = {}, []
    ann = {}
    for name, (tags, unit) in FLOW.items():
        rows = pit_annual(cands(facts, tags, unit), asof)
        ann[name] = rows
        m[name] = rows[0]["val"] if rows else None
        if rows:
            used.append(rows[0]["filed"])
    ocf, capex = m["営業CF"], m["設備投資"]
    m["FCF(営業CF-設備投資)"] = (ocf - capex) if (ocf is not None and capex is not None) else None
    for name, (tags, unit) in INST.items():
        r = pit_instant(cands(facts, tags, unit), asof)
        m[name] = r["val"] if r else None
        if r:
            used.append(r["filed"])

    def growth(rows):
        if len(rows) >= 2 and rows[1]["val"] and rows[1]["val"] > 0 and rows[0]["val"] is not None:
            return rows[0]["val"] / rows[1]["val"] - 1
        return None
    m["売上成長(前年比)"] = growth(ann["売上"])
    m["EPS成長(前年比)"] = growth(ann["EPS(希薄化後)"])
    last10k = ann["売上"][0]["filed"] if ann["売上"] else None
    return m, (max(used) if used else None), last10k


class Sec:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
        self.codes = Counter()
        self.last = 0.0

    def get(self, url):
        for attempt in range(2):
            wait = 0.12 - (time.time() - self.last)  # 上限10回/秒より余裕を持たせる
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(url, timeout=60)
            except Exception as e:  # noqa: BLE001
                self.codes["exception"] += 1
                return None, str(e)[:80]
            self.codes[r.status_code] += 1
            if r.status_code in (403, 429) and attempt == 0:
                time.sleep(10)
                continue
            return r, ""
        return r, ""


def run_sec(syms):
    if not SEC_UA or "@" not in SEC_UA:
        say("- SEC: SEC_USER_AGENT 未設定（例: `Taro Yamada taro@example.com`）のため未実行")
        return None
    sec = Sec()
    r, _ = sec.get("https://www.sec.gov/files/company_tickers.json")
    if r is None or r.status_code != 200:
        say(f"- SEC: ticker→CIK表の取得失敗 (HTTP {getattr(r, 'status_code', None)})")
        return None
    cik = {v["ticker"]: v["cik_str"] for v in r.json().values()}
    res = {"nocik": [], "fail": [], "ok": 0, "secs": [], "mb": [], "cov": {a: Counter() for a in ASOFS},
           "viol": 0, "differs": 0, "cmp": 0, "samples": {}, "n": 0, "stale": []}
    for i, s in enumerate(syms, 1):
        if s not in cik:
            res["nocik"].append(s)
            continue
        t0 = time.time()
        r, err = sec.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik[s]:010d}.json")
        if r is None or r.status_code != 200:
            res["fail"].append((s, getattr(r, "status_code", err)))
            continue
        facts = r.json()
        res["mb"].append(len(r.content) / 1e6)
        res["ok"] += 1
        vals = {}
        for a in ASOFS:
            m, maxf, last10k = sec_metrics(facts, a)
            vals[a] = m
            for k, v in m.items():
                res["cov"][a][k] += v is not None
            if maxf and maxf >= a:
                res["viol"] += 1  # 先読み違反(0であるべき)
            if a == "2025-03-01" and last10k:
                res["stale"].append((date.fromisoformat(a) - date.fromisoformat(last10k)).days)
        res["cmp"] += 1
        res["differs"] += vals["2025-03-01"]["売上"] != vals[LATEST]["売上"]
        if s in MUST:
            res["samples"][s] = vals["2025-03-01"]
        res["secs"].append(time.time() - t0)
        if i % 25 == 0:
            print(f"  SEC {i}/{len(syms)}")
    res["n"] = res["ok"]
    res["codes"] = dict(sec.codes)
    # 提出一覧(8-K等)の確認
    sub = []
    for s in [x for x in syms if x in cik][:5]:
        r, _ = sec.get(f"https://data.sec.gov/submissions/CIK{cik[s]:010d}.json")
        if r is not None and r.status_code == 200:
            rec = r.json().get("filings", {}).get("recent", {})
            forms = Counter(rec.get("form", []))
            items = Counter(it for x, it in zip(rec.get("form", []), rec.get("items", [])) if x == "8-K"
                            for it in (it.split(",") if it else []) if it == "2.02")
            sub.append((s, len(rec.get("form", [])), forms.get("10-K", 0), forms.get("10-Q", 0),
                        forms.get("8-K", 0), items.get("2.02", 0), min(rec.get("filingDate", [""]) or [""]),
                        "acceptanceDateTime" in rec))
    res["sub"] = sub
    return res


# =====================================================
# 4. FMP limit=5
# =====================================================
def fmp(path, **p):
    p["apikey"] = FMP_KEY
    try:
        r = requests.get(f"{FMP}/{path}", params=p, timeout=30)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:100]
    except Exception as e:  # noqa: BLE001
        return None, redact(e)[:100]


def run_fmp():
    rows = []
    for sym in ("AAPL", "NVDA", "CROX"):
        for label, path, p, need in (
            ("決算履歴", "earnings", {"limit": 5}, ["date", "epsActual", "epsEstimated", "revenueActual", "revenueEstimated"]),
            ("四半期損益", "income-statement", {"period": "quarter", "limit": 5}, ["date", "filingDate", "revenue", "operatingIncome", "epsDiluted"]),
        ):
            code, body = fmp(path, symbol=sym, **p)
            if code == 200 and isinstance(body, list) and body:
                miss = [f for f in need if f not in body[0]]
                dates = sorted(str(x.get("date")) for x in body)
                nn = sum(1 for x in body if x.get("epsActual") is not None) if label == "決算履歴" else "-"
                rows.append((sym, label, "✅" if not miss else "⚠️", len(body), f"{dates[0]}〜{dates[-1]}",
                             f"欠落{miss}" if miss else f"実績あり行={nn}"))
            else:
                msg = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                rows.append((sym, label, "❌", 0, "-", f"HTTP {code}: {redact(msg)[:70]}"))
        time.sleep(0.4)
    return rows


# =====================================================
# main
# =====================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="小規模で動作確認")
    ap.add_argument("--cap-sample", type=int, default=400, help="時価総額を調べる銘柄数")
    ap.add_argument("--n", type=int, default=150, help="Finnhub/SECを調べる銘柄数(時価総額$1B以上)")
    ap.add_argument("--price-n", type=int, default=1500, help="株価取得時間テストの銘柄数")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    if a.quick:
        a.cap_sample, a.n, a.price_n = 120, 20, 300
    say(f"# STEP3b レポート ({TODAY})", f"設定: cap_sample={a.cap_sample}, n={a.n}, price_n={a.price_n}, seed={a.seed}", "")

    # --- ユニバース & 時価総額 ---
    uni = load_universe()
    rnd = random.Random(a.seed)
    sample = rnd.sample(uni, min(a.cap_sample, len(uni)))
    sample = list(dict.fromkeys(MUST + sample))
    say("## 1. 銘柄リスト・時価総額(yfinance)", f"- 銘柄リスト(ETF/ワラント等除外後): {len(uni)}銘柄")
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        caps = dict(ex.map(get_mcap, sample))
    sec_cap = time.time() - t0
    got = {k: v for k, v in caps.items() if v}
    big = [k for k, v in got.items() if v >= 1e9]
    say(f"- 時価総額を取得できた率: {pct(len(got), len(caps))}（{sec_cap:.0f}秒, 1銘柄あたり{sec_cap / len(caps):.2f}秒"
        f" → 全{len(uni)}銘柄なら約{sec_cap / len(caps) * len(uni) / 60:.0f}分）",
        f"- うち$1B以上: {len(big)}銘柄 ({pct(len(big), len(got))})", "")
    deep = [s for s in MUST if s in big] + [s for s in big if s not in MUST]
    deep = deep[: a.n]
    say(f"- 以降の詳細テスト対象: {len(deep)}銘柄（$1B以上, MUST銘柄含む: {[s for s in MUST if s in deep]}）", "")

    # --- 株価取得時間 ---
    say("## 2. 株価・出来高の一括取得時間(yfinance, 2年)")
    try:
        import yfinance as yf
        psyms = rnd.sample(uni, min(a.price_n, len(uni)))
        ok = 0
        t0 = time.time()
        for i in range(0, len(psyms), 250):
            chunk = psyms[i:i + 250]
            df = yf.download(chunk, period="2y", interval="1d", auto_adjust=True, group_by="ticker",
                             threads=True, progress=False)
            for t in chunk:
                try:
                    sub = df[t].dropna(subset=["Close"])
                    ok += len(sub) >= 200 and (sub["Volume"] > 0).mean() > 0.9
                except KeyError:
                    pass
        sec_p = time.time() - t0
        say(f"- {len(psyms)}銘柄: {sec_p:.0f}秒（{sec_p / 60:.1f}分）、十分なデータが取れた率 {pct(ok, len(psyms))}",
            "  （失敗には上場1年未満・出来高なし等も含む）", "")
    except Exception as e:  # noqa: BLE001
        say(f"- 失敗: {redact(e)[:100]}", "")

    # --- Finnhub ---
    say("## 3. Finnhub 財務指標の欠損率")
    fh_res = None
    if FH_KEY:
        t0 = time.time()
        cov, keyc, status, times, allcore, per = run_finnhub(deep)
        fh_res = (cov, per)
        n_ok = status.get("ok", 0)
        say(f"- 取得結果: {dict(status)}（所要{(time.time() - t0) / 60:.1f}分、1,500銘柄なら約{1500 * 1.05 / 60:.0f}分＋通信時間）",
            f"- 必須7項目({'/'.join(CORE)})が全部そろう銘柄: {pct(allcore, n_ok)}", "")
        rows = [(g, pct(cov[g], n_ok), ", ".join(f"{k}:{c}" for k, c in keyc[g].most_common(2))) for g in GROUPS]
        LOG.extend(table(["項目", "取得率", "使われたキー"], rows))
        say("", "### MUST銘柄(FMPで取れなかったCROX/DECK等)の取得状況")
        mrows = [(s, *["✅" if per[s][g] else "❌" for g in CORE]) if s in per else (s, *["取得なし"] * len(CORE))
                 for s in MUST if s in deep]
        LOG.extend(table(["銘柄"] + CORE, mrows))
        say("")
    else:
        say("- FINNHUB_API_KEY未設定のためスキップ", "")

    # --- FMP ---
    say("## 4. FMP limit=5 の実用性")
    if FMP_KEY:
        LOG.extend(table(["銘柄", "項目", "判定", "件数", "期間", "備考"], run_fmp()))
        say("")
    else:
        say("- FMP_API_KEY未設定のためスキップ", "")

    # --- SEC ---
    say("## 5. SEC EDGAR (point-in-time)")
    res = run_sec(deep)
    if res:
        n = res["n"]
        say(f"- 取得成功 {n}銘柄 / CIK対応なし {len(res['nocik'])}銘柄 {res['nocik'][:8]} / 失敗 {len(res['fail'])} {res['fail'][:5]}",
            f"- HTTPステータス内訳: {res['codes']}",
            f"- 速度: 1銘柄あたり平均 {sum(res['secs']) / max(n, 1):.2f}秒, 平均サイズ {sum(res['mb']) / max(n, 1):.1f}MB"
            f" → 1,500銘柄なら約{sum(res['secs']) / max(n, 1) * 1500 / 60:.0f}分, 約{sum(res['mb']) / max(n, 1) * 1.5:.1f}GB(都度取得の場合)",
            f"- **先読み違反(提出日が基準日以降のデータを使った件数): {res['viol']}**（0であること）",
            f"- 2025-03-01時点と最新で売上が異なる銘柄: {pct(res['differs'], res['cmp'])}"
            "（=基準日を指定しないと未来の情報が混ざる銘柄の割合）")
        if res["stale"]:
            st = sorted(res["stale"])
            say(f"- 2025-03-01時点で使える最新10-Kの古さ: 中央値{st[len(st) // 2]}日, 最大{st[-1]}日", "")
        hdr = ["指標"] + [f"{a_} 取得率" for a_ in ASOFS]
        LOG.extend(table(hdr, [(k, *[pct(res["cov"][a_][k], n) for a_ in ASOFS]) for k in SEC_METRICS]))
        say("", "### 2025-03-01時点で見えていた値(MUST銘柄・通期)")
        keys = ["売上", "営業利益", "純利益", "EPS(希薄化後)", "営業CF", "FCF(営業CF-設備投資)", "総資産", "株主資本", "発行済株式数"]
        srows = []
        for s, m in res["samples"].items():
            srows.append([s] + [("-" if m[k] is None else f"{m[k]:,.2f}" if abs(m[k]) < 1000 else f"{m[k]:,.0f}") for k in keys])
        LOG.extend(table(["銘柄"] + keys, srows))
        say("", "### 提出一覧(submissions API)")
        LOG.extend(table(["銘柄", "直近提出数", "10-K", "10-Q", "8-K", "うち決算(Item2.02)", "最古の提出日", "受理日時あり"], res["sub"]))
        say("", "- 注: 営業CF/設備投資は通期(10-K)のみ使用。10-Qは累計(YTD)表記のため四半期単独値の作り方は別途設計が必要。")
    say("")
    (OUT / "step3b_report.md").write_text("\n".join(LOG), encoding="utf-8")
    print(f"\nレポート: {OUT / 'step3b_report.md'}")


if __name__ == "__main__":
    main()
