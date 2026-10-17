#!/usr/bin/env python3
"""
STEP3: API疎通・仕様確認テスト（本体ではありません）

目的: 「取得できるはず」ではなく「実際に取得できた項目」だけを正式仕様に採用するため、
      FMP / yfinance / Finnhub を実際に叩いて、結果を api_check_output/ に保存する。

使い方:
    1) .env.example を .env にコピーしてAPIキーを書く（.envはGitに上げない）
    2) pip install -r requirements.txt
    3) python step3_check_apis.py
    4) api_check_output/api_check_report.md を確認

APIキーはコードに書かない。環境変数（.env / GitHub Secrets / Colabシークレット）から読む。
"""
import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # GitHub Actionsなど環境変数だけでも動く
    pass

FMP_KEY = os.getenv("FMP_API_KEY", "")
FH_KEY = os.getenv("FINNHUB_API_KEY", "")

OUT = Path("api_check_output")
SAMPLES = OUT / "samples"
SAMPLES.mkdir(parents=True, exist_ok=True)

FMP = "https://financialmodelingprep.com/stable"
FH = "https://finnhub.io/api/v1"
MAIN = "AAPL"
EXTRA_SYMBOLS = ["NVDA", "MSFT", "CROX", "DECK"]  # 銘柄制限の有無を見る
YF_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "XOM", "UNH",
    "HD", "COST", "LLY", "AVGO", "CRM", "NFLX", "AMD", "ADBE", "PEP", "KO",
    "CROX", "DECK", "ENPH", "ETSY", "ZS", "DOCU", "PINS", "SNAP", "RIVN", "PLTR",
    "NKE", "SBUX", "DIS", "BA", "INTC", "CSCO", "ORCL", "QCOM", "TXN", "MU",
    "PYPL", "SQ", "UBER", "ABNB", "SHOP", "ROKU", "LULU", "TGT", "LOW", "CAT",
]

TODAY = date.today()
RESULTS = []
CALLS = {"fmp": 0, "finnhub": 0}


# ---------- 共通ユーティリティ ----------
def redact(s):
    """エラー文などにAPIキーが混ざっても出力しない"""
    s = str(s)
    for k in (FMP_KEY, FH_KEY):
        if k:
            s = s.replace(k, "***")
    return s


def http_get(provider, url, params=None):
    CALLS[provider] += 1
    p = dict(params or {})
    headers = {}
    if provider == "fmp":
        p["apikey"] = FMP_KEY
    elif provider == "finnhub":
        headers["X-Finnhub-Token"] = FH_KEY
    t0 = time.time()
    try:
        r = requests.get(url, params=p, headers=headers, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"err": redact(e), "status": None}
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    rl = {k: v for k, v in r.headers.items()
          if "ratelimit" in k.lower() or "rate-limit" in k.lower() or k.lower() == "retry-after"}
    return {"status": r.status_code, "body": body, "sec": round(time.time() - t0, 2), "rl": rl}


def classify(resp):
    if resp.get("err"):
        return "ERROR", resp["err"]
    s, b = resp["status"], resp["body"]
    if s == 429:
        return "RATE_LIMIT", "HTTP 429"
    if s in (401, 402, 403):
        return "RESTRICTED", f"HTTP {s}: {redact(str(b)[:140])}"
    if s == 404:
        return "NOT_FOUND", "HTTP 404（エンドポイント名要確認）"
    if s != 200:
        return "ERROR", f"HTTP {s}: {redact(str(b)[:140])}"
    if isinstance(b, dict) and ("Error Message" in b or "error" in b):
        return "RESTRICTED", redact(str(b)[:140])
    if b in ([], {}, None, ""):
        return "EMPTY", "空レスポンス（銘柄/期間制限の可能性）"
    return "OK", ""


def first_row(b):
    if isinstance(b, list):
        return b[0] if b and isinstance(b[0], dict) else {}
    return b if isinstance(b, dict) else {}


def record(pid, name, status, rows="-", note="", alt="", found=None, missing=None):
    RESULTS.append({"id": pid, "name": name, "status": status, "rows": rows,
                    "note": note, "alt": alt, "found": found or [], "missing": missing or []})
    mark = {"OK": "✅", "PARTIAL": "⚠️"}.get(status, "❌" if status != "EMPTY" else "⚠️")
    print(f"{mark} [{pid}] {name}: {status} {note[:100]}")


def probe(pid, name, provider, url, params=None, expect=(), alt="", unwrap=None,
          hint=None, delay=0.4, save=True):
    resp = http_get(provider, url, params)
    st, note = classify(resp)
    body = resp.get("body")
    if unwrap and isinstance(body, dict):
        body = body.get(unwrap, body)
    rows = len(body) if isinstance(body, list) else (1 if st == "OK" else 0)
    row = first_row(body)
    found = [f for f in expect if f in row]
    missing = [f for f in expect if f not in row]
    if st == "OK" and missing:
        st = "PARTIAL"
        note = f"欠落項目: {missing}"
    if st in ("OK", "PARTIAL") and hint:
        hits = [k for k in row if re.search(hint, k, re.I)]
        note += f" 関連キー: {hits[:12]}"
    if st in ("OK", "PARTIAL") and isinstance(body, list) and body and "date" in body[0]:
        dates = sorted(str(x.get("date")) for x in body if isinstance(x, dict) and x.get("date"))
        if dates:
            note += f" 期間: {dates[0]}〜{dates[-1]}"
    if resp.get("rl"):
        note += f" RL:{resp['rl']}"
    if save and st in ("OK", "PARTIAL"):
        sample = body[:3] if isinstance(body, list) else body
        (SAMPLES / f"{pid}.json").write_text(
            redact(json.dumps(sample, ensure_ascii=False, indent=1, default=str)[:20000]),
            encoding="utf-8")
        (SAMPLES / f"{pid}_keys.txt").write_text("\n".join(row.keys()) if row else "", encoding="utf-8")
    record(pid, name, st, rows, note, alt, found, missing)
    time.sleep(delay)
    return resp


# ---------- A. FMP ----------
def check_fmp():
    if not FMP_KEY:
        record("A0", "FMP APIキー", "SKIPPED", note="FMP_API_KEY未設定")
        return
    probe("A1", "FMP: キー動作確認(profile)", "fmp", f"{FMP}/profile", {"symbol": MAIN},
          expect=["symbol", "marketCap", "exchange", "isEtf", "price", "sector"])

    probe("A2a", "FMP: 時価総額スクリーナー", "fmp", f"{FMP}/company-screener",
          {"marketCapMoreThan": 1_000_000_000, "exchange": "NASDAQ", "isEtf": "false",
           "isFund": "false", "isActivelyTrading": "true", "limit": 1000},
          expect=["symbol", "marketCap", "exchange", "isEtf", "volume", "sector"],
          alt="NASDAQ Trader公開リスト + yfinanceで時価総額判定")
    probe("A2b", "FMP: 全銘柄リスト(stock-list)", "fmp", f"{FMP}/stock-list",
          expect=["symbol", "companyName"], alt="NASDAQ Trader公開リスト")

    # FMP以外の銘柄リスト候補（無料・キー不要）
    for pid, nm, url in [("A2c", "NASDAQ Trader: nasdaqlisted",
                          "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"),
                         ("A2d", "NASDAQ Trader: otherlisted(NYSE等)",
                          "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")]:
        try:
            r = requests.get(url, timeout=30)
            lines = r.text.strip().splitlines()
            record(pid, nm, "OK" if r.status_code == 200 and len(lines) > 100 else "ERROR",
                   len(lines), f"HTTP {r.status_code}, ヘッダ: {lines[0][:80] if lines else ''}")
        except Exception as e:  # noqa: BLE001
            record(pid, nm, "ERROR", note=redact(e))

    probe("A3a", "FMP: 損益(年次5期)", "fmp", f"{FMP}/income-statement",
          {"symbol": MAIN, "period": "annual", "limit": 5},
          expect=["date", "filingDate", "acceptedDate", "revenue", "operatingIncome",
                  "netIncome", "eps", "epsDiluted"])
    probe("A3b", "FMP: 損益(四半期12期)", "fmp", f"{FMP}/income-statement",
          {"symbol": MAIN, "period": "quarter", "limit": 12},
          expect=["date", "filingDate", "revenue", "operatingIncome", "epsDiluted"],
          alt="yfinance quarterly_income_stmt(直近約4-5期)")
    probe("A3c", "FMP: 貸借対照表", "fmp", f"{FMP}/balance-sheet-statement",
          {"symbol": MAIN, "period": "annual", "limit": 5},
          expect=["date", "filingDate", "totalDebt", "totalStockholdersEquity",
                  "cashAndCashEquivalents", "totalCurrentAssets", "totalCurrentLiabilities"])
    probe("A3d", "FMP: キャッシュフロー", "fmp", f"{FMP}/cash-flow-statement",
          {"symbol": MAIN, "period": "annual", "limit": 5},
          expect=["date", "filingDate", "operatingCashFlow", "capitalExpenditure", "freeCashFlow"])
    probe("A3e", "FMP: 財務比率(ratios)", "fmp", f"{FMP}/ratios",
          {"symbol": MAIN, "period": "annual", "limit": 5},
          hint=r"earnings|peg|sales|debt|margin", alt="Finnhub stock/metric")
    probe("A3f", "FMP: key-metrics", "fmp", f"{FMP}/key-metrics",
          {"symbol": MAIN, "period": "annual", "limit": 5}, hint=r"roe|return|freeCash|ev")
    probe("A3g", "FMP: 成長率(income-statement-growth)", "fmp", f"{FMP}/income-statement-growth",
          {"symbol": MAIN, "period": "annual", "limit": 5}, hint=r"growth(revenue|eps)",
          alt="損益から自前で計算")

    probe("A4a", "FMP: 決算履歴(予想/実績)", "fmp", f"{FMP}/earnings", {"symbol": MAIN, "limit": 12},
          expect=["date", "epsActual", "epsEstimated", "revenueActual", "revenueEstimated"],
          alt="yfinance get_earnings_dates / Finnhub stock/earnings")
    probe("A4b", "FMP: 決算カレンダー(今後14日)", "fmp", f"{FMP}/earnings-calendar",
          {"from": TODAY.isoformat(), "to": (TODAY + timedelta(days=14)).isoformat()},
          expect=["symbol", "date", "epsEstimated", "revenueEstimated"],
          alt="Finnhub calendar/earnings")
    probe("A4c", "FMP: アナリスト予想(estimates)", "fmp", f"{FMP}/analyst-estimates",
          {"symbol": MAIN, "period": "annual", "limit": 5},
          expect=["date", "revenueAvg", "epsAvg"], alt="Finnhub")
    probe("A4d", "FMP: 格上げ/格下げ(grades)", "fmp", f"{FMP}/grades", {"symbol": MAIN, "limit": 10},
          expect=["date", "gradingCompany", "previousGrade", "newGrade", "action"],
          alt="Finnhub upgrade-downgrade")
    probe("A4e", "FMP: 目標株価サマリ", "fmp", f"{FMP}/price-target-summary", {"symbol": MAIN},
          alt="Finnhub price-target")
    probe("A4f", "FMP: 日足履歴(10年)", "fmp", f"{FMP}/historical-price-eod/full",
          {"symbol": MAIN, "from": (TODAY - timedelta(days=3650)).isoformat(),
           "to": TODAY.isoformat()},
          expect=["date", "open", "high", "low", "close", "volume"], alt="yfinance")

    # 無料枠の「銘柄制限」確認
    okset, ngset = [], []
    for s in EXTRA_SYMBOLS:
        resp = http_get("fmp", f"{FMP}/income-statement", {"symbol": s, "period": "annual", "limit": 1})
        (okset if classify(resp)[0] == "OK" else ngset).append(s)
        time.sleep(0.4)
    record("A5", "FMP: 無料枠の銘柄制限確認", "OK" if not ngset else "PARTIAL",
           note=f"取得可: {okset} / 不可: {ngset}",
           alt="不可が出る場合: 対象はFMPで取れる銘柄に限定 or 有料プラン")


# ---------- B. yfinance ----------
def check_yf():
    try:
        import yfinance as yf
    except ImportError:
        record("B0", "yfinance", "SKIPPED", note="yfinance未インストール")
        return

    # B1: 複数銘柄の一括取得（50銘柄・1年）
    t0 = time.time()
    try:
        df = yf.download(YF_TICKERS, period="1y", interval="1d", auto_adjust=True,
                         group_by="ticker", threads=True, progress=False)
        sec = round(time.time() - t0, 1)
        ok, bad = [], []
        for t in YF_TICKERS:
            try:
                sub = df[t].dropna(subset=["Close"])
                good = len(sub) > 200 and (sub["Volume"] > 0).mean() > 0.95
                (ok if good else bad).append(t)
            except KeyError:
                bad.append(t)
        record("B1", f"yfinance: {len(YF_TICKERS)}銘柄の株価・出来高 一括取得(1年)",
               "OK" if len(ok) >= len(YF_TICKERS) * 0.95 else "PARTIAL", len(df),
               f"{len(ok)}/{len(YF_TICKERS)}OK, {sec}秒, 最新日 {df.index.max().date()}, 不良: {bad}")
    except Exception as e:  # noqa: BLE001
        record("B1", "yfinance: 一括取得", "ERROR", note=redact(e)[:150])

    # B2: 長期履歴（10年）
    try:
        df = yf.download(["AAPL", "NVDA", "MSFT", "SPY"], period="10y", interval="1d",
                         auto_adjust=True, group_by="ticker", progress=False)
        info = {t: (len(df[t].dropna(subset=["Close"])), str(df[t].dropna(subset=["Close"]).index.min().date()))
                for t in ["AAPL", "NVDA", "MSFT", "SPY"]}
        record("B2", "yfinance: 10年履歴", "OK" if all(v[0] > 2400 for v in info.values()) else "PARTIAL",
               note=f"(行数, 開始日): {info}")
    except Exception as e:  # noqa: BLE001
        record("B2", "yfinance: 10年履歴", "ERROR", note=redact(e)[:150])

    # B3: 上場廃止銘柄が取れるか（生存者バイアス確認）
    try:
        dead = ["SIVB", "BBBY", "FRC"]
        df = yf.download(dead, period="max", interval="1d", auto_adjust=True,
                         group_by="ticker", progress=False)
        got = []
        for t in dead:
            try:
                if len(df[t].dropna(subset=["Close"])) > 0:
                    got.append(t)
            except KeyError:
                pass
        record("B3", "yfinance: 上場廃止銘柄の履歴(生存者バイアス確認)", "OK" if got else "EMPTY",
               note=f"取得できた銘柄: {got} / 試行: {dead}",
               alt="取得不可なら『生存者バイアスあり』と明記して結果を割り引いて解釈")
    except Exception as e:  # noqa: BLE001
        record("B3", "yfinance: 廃止銘柄", "ERROR", note=redact(e)[:150])

    # B4: 決算日・予想・実績（FMP代替候補）
    try:
        ed = yf.Ticker(MAIN).get_earnings_dates(limit=12)
        cols = list(ed.columns)
        need = ["EPS Estimate", "Reported EPS", "Surprise(%)"]
        miss = [c for c in need if c not in cols]
        record("B4", "yfinance: 決算日/EPS予想/実績", "OK" if not miss else "PARTIAL", len(ed),
               f"列: {cols} 欠落: {miss}（売上予想は無し）", alt="売上予想差はFMP earnings")
    except Exception as e:  # noqa: BLE001
        record("B4", "yfinance: 決算日", "ERROR", note=redact(e)[:150])

    # B5: 四半期財務（FMP代替候補）
    try:
        q = yf.Ticker(MAIN).quarterly_income_stmt
        record("B5", "yfinance: 四半期損益", "OK" if not q.empty else "EMPTY",
               f"{q.shape[1]}期", f"項目例: {list(q.index[:8])}", alt="長期推移はFMP年次")
    except Exception as e:  # noqa: BLE001
        record("B5", "yfinance: 四半期損益", "ERROR", note=redact(e)[:150])


# ---------- C. Finnhub ----------
def check_finnhub():
    if not FH_KEY:
        record("C0", "Finnhub APIキー", "SKIPPED", note="FINNHUB_API_KEY未設定")
        return
    d = 1.1  # 60回/分を超えないための待機
    frm, to = (TODAY - timedelta(days=7)).isoformat(), TODAY.isoformat()
    probe("C1", "Finnhub: キー動作確認(quote)", "finnhub", f"{FH}/quote", {"symbol": MAIN},
          expect=["c", "h", "l", "o", "pc"], delay=d)
    probe("C2", "Finnhub: 企業ニュース(7日)", "finnhub", f"{FH}/company-news",
          {"symbol": MAIN, "from": frm, "to": to},
          expect=["headline", "datetime", "source", "url", "summary"], delay=d)
    probe("C3", "Finnhub: アナリスト推奨トレンド", "finnhub", f"{FH}/stock/recommendation",
          {"symbol": MAIN}, expect=["period", "strongBuy", "buy", "hold", "sell", "strongSell"], delay=d)
    probe("C4", "Finnhub: 格上げ/格下げ", "finnhub", f"{FH}/stock/upgrade-downgrade",
          {"symbol": MAIN}, expect=["company", "action", "fromGrade", "toGrade", "gradeTime"],
          alt="FMP grades", delay=d)
    probe("C5", "Finnhub: 目標株価", "finnhub", f"{FH}/stock/price-target", {"symbol": MAIN},
          expect=["targetMean", "targetHigh", "targetLow"], alt="FMP price-target-summary", delay=d)
    probe("C6", "Finnhub: EPSサプライズ履歴", "finnhub", f"{FH}/stock/earnings", {"symbol": MAIN},
          expect=["period", "actual", "estimate", "surprisePercent"], delay=d)
    probe("C7", "Finnhub: 決算カレンダー(今後14日)", "finnhub", f"{FH}/calendar/earnings",
          {"from": TODAY.isoformat(), "to": (TODAY + timedelta(days=14)).isoformat()},
          expect=["symbol", "date", "epsEstimate", "revenueEstimate"], unwrap="earningsCalendar", delay=d)
    probe("C8", "Finnhub: 財務指標(stock/metric)", "finnhub", f"{FH}/stock/metric",
          {"symbol": MAIN, "metric": "all"}, unwrap="metric",
          expect=["peTTM", "psTTM", "roeTTM", "netProfitMarginTTM", "52WeekHigh"],
          hint=r"peg|freeCash|debt|revenueGrowth|epsGrowth", alt="FMP ratios", delay=d)
    ts = lambda dt: int(time.mktime(dt.timetuple()))  # noqa: E731
    probe("C9", "Finnhub: 株価ローソク足(無料枠では不可の想定)", "finnhub", f"{FH}/stock/candle",
          {"symbol": MAIN, "resolution": "D", "from": ts(TODAY - timedelta(days=30)), "to": ts(TODAY)},
          alt="yfinance", delay=d)


# ---------- レポート ----------
VERDICT = {"OK": "✅ 採用可", "PARTIAL": "⚠️ 一部のみ", "EMPTY": "⚠️ 空", "RESTRICTED": "❌ 無料枠不可",
           "NOT_FOUND": "❌ 404", "RATE_LIMIT": "⚠️ レート制限", "ERROR": "❌ エラー", "SKIPPED": "➖ 未実行"}


def write_report():
    lines = [f"# STEP3 API確認レポート ({TODAY})", "",
             f"APIコール数: FMP {CALLS['fmp']}回 / Finnhub {CALLS['finnhub']}回（FMP無料枠は250/日）", "",
             "| ID | 項目 | 判定 | 件数 | 備考 | 代替手段 |", "|---|---|---|---|---|---|"]
    for r in RESULTS:
        note = r["note"].replace("|", "/").replace("\n", " ")[:220]
        lines.append(f"| {r['id']} | {r['name']} | {VERDICT.get(r['status'], r['status'])} | "
                     f"{r['rows']} | {note} | {r['alt']} |")
    lines += ["", "## 採用ルール", "- ✅のみ正式仕様に採用。⚠️は欠落項目を確認の上、代替手段で補う。",
              "- ❌は採用しない（代替手段を使う）。", "- samples/ に実レスポンス（キー除去済み）と項目一覧を保存。"]
    (OUT / "api_check_report.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "api_check_results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nレポート: {OUT / 'api_check_report.md'}")


if __name__ == "__main__":
    print("=== A. FMP ===")
    check_fmp()
    print("=== B. yfinance ===")
    check_yf()
    print("=== C. Finnhub ===")
    check_finnhub()
    write_report()
