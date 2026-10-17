"""SECの財務データを『基準日より前に提出されたものだけ』で取り出す(point-in-time)。
  * 提出日(filed)が基準日【より前】のものだけを使う（当日提出は使わない=保守的）
  * 同じ期間が後から修正(再提出)されている場合は、基準日時点で最新の提出分を使う
  * 取れない項目は推定で埋めず、判定不能にする
"""
from datetime import date

import numpy as np

from .signals import bin_points

ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT")
BS_FORMS = ANNUAL_FORMS + ("10-Q", "10-Q/A")

FLOW = {  # 名前: (タグ候補(優先順), 単位)
    "revenue": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                 "SalesRevenueGoodsNet"], "USD"),
    "op_income": (["OperatingIncomeLoss"], "USD"),
    "net_income": (["NetIncomeLoss", "ProfitLoss"], "USD"),   # V2bで追加(アクルーアル用)。V2のQualityは使わない
    "ocf": (["NetCashProvidedByUsedInOperatingActivities"], "USD"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], "USD"),
}
INST = {
    "assets": ([("us-gaap", "Assets")], "USD"),
    "equity": ([("us-gaap", "StockholdersEquity"),
                ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")], "USD"),
    "shares": ([("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding")], "shares"),
}
KEEP = ("start", "end", "val", "filed", "form")


def compact_facts(facts):
    """companyfacts(数MB)から、V2で使う項目だけを小さく取り出す。"""
    g = facts.get("facts", {})
    out = {"flow": {}, "inst": {}}
    for name, (tags, unit) in FLOW.items():
        lst = []
        for tag in tags:
            d = g.get("us-gaap", {}).get(tag)
            if d and unit in d.get("units", {}):
                rows = []
                for r in d["units"][unit]:
                    if "start" not in r or r.get("form") not in ANNUAL_FORMS:
                        continue
                    try:
                        dd = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
                    except (KeyError, ValueError):
                        continue
                    if 340 <= dd <= 380:
                        rows.append({k: r[k] for k in KEEP if k in r})
                if rows:
                    lst.append((tag, rows))
        out["flow"][name] = lst
    for name, (tags, unit) in INST.items():
        lst = []
        for taxo, tag in tags:
            d = g.get(taxo, {}).get(tag)
            if d and unit in d.get("units", {}):
                rows = [{k: r[k] for k in KEEP if k in r} for r in d["units"][unit]
                        if "start" not in r and r.get("form") in BS_FORMS]
                if rows:
                    lst.append((tag, rows))
        out["inst"][name] = lst
    return out


# ------------------------------------------------------------------ 取り出し
def pit_annual(cs, asof):
    """asofより前に提出された10-Kの通期データを、期末の新しい順に返す（最新の期末を持つタグを採用）。"""
    best = None
    for _tag, rs in cs:
        keep = {}
        for r in rs:
            if r.get("filed", "9999") >= asof:
                continue
            k = (r["start"], r["end"])
            if k not in keep or r["filed"] > keep[k]["filed"]:
                keep[k] = r
        rows = sorted(keep.values(), key=lambda r: r["end"], reverse=True)
        if rows and (best is None or rows[0]["end"] > best[0]["end"]):
            best = rows
    return best or []


def pit_instant_rows(cs, asof, forms=BS_FORMS):
    """asofより前に提出された時点データを {期末: 行(最新の提出)} で返す。タグは優先順に最初に取れたものを使う。"""
    for _tag, rs in cs:
        keep = {}
        for r in rs:
            if r.get("filed", "9999") >= asof or r.get("form") not in forms:
                continue
            if r["end"] not in keep or r["filed"] > keep[r["end"]]["filed"]:
                keep[r["end"]] = r
        if keep:
            return keep
    return {}


def _d(s):
    return date.fromisoformat(s)


def split_factor(splits, d_from, d_to):
    """d_from < 分割日 <= d_to の分割比の積。splits=(日付配列(datetime64[D]), 比率配列)。2.0=2分割(株数2倍)。"""
    if splits is None or len(splits[0]) == 0:
        return 1.0
    dates, ratios = splits
    m = (dates > np.datetime64(d_from)) & (dates <= np.datetime64(d_to))
    return float(np.prod(ratios[m])) if m.any() else 1.0


# ------------------------------------------------------------------ 株式数・時価総額
def shares_at(sec, asof, max_age_days):
    """基準日より前に提出された最新の発行済株式数。(株数, 株数の基準日) / 取れなければ None。"""
    rows = pit_instant_rows(sec["inst"].get("shares", []), asof)
    if not rows:
        return None
    end = max(rows)
    if (_d(asof) - _d(end)).days > max_age_days:
        return None
    return float(rows[end]["val"]), end


def market_cap_at(sec, asof, close_raw_today_terms, splits, max_age_days):
    """推定ではなく『提出済みの株式数 x 当時の株価』。
    close_raw_today_terms: Yahooの分割調整済み終値(今日の株数基準)。
    当時の株価(分割調整前)  = close_raw x (基準日より後の分割比の積)
    当時の株数(基準日時点) = 提出株数 x (株数の基準日〜基準日の分割比の積)
    → 掛け合わせると、基準日より後の分割は打ち消し合い、未来の分割情報は結果に影響しない。"""
    s = shares_at(sec, asof, max_age_days)
    if s is None or not np.isfinite(close_raw_today_terms):
        return np.nan, np.nan
    shares, end = s
    f_after_asof = split_factor(splits, asof, "2100-01-01")
    price_unadj = close_raw_today_terms * f_after_asof
    shares_asof = shares * split_factor(splits, end, asof)
    return price_unadj * shares_asof, price_unadj


# ------------------------------------------------------------------ Quality
def _match(rows, r0):
    for r in rows:
        if r["end"] == r0["end"] and r["start"] == r0["start"]:
            return r
    return None


def quality_at(sec, asof, splits, Q):
    """Quality判定。戻り値 dict: q_status(HIGH/MID/LOW/UNDET), q_score, 各項目の値, q_reason。"""
    res = {"q_status": "UNDET", "q_score": np.nan, "q_reason": "",
           "q_rev_growth": np.nan, "q_op_margin": np.nan, "q_fcf_margin": np.nan,
           "q_equity_ratio": np.nan, "q_dilution": np.nan}
    F = sec["flow"]
    rev = pit_annual(F.get("revenue", []), asof)
    if len(rev) < 2:
        res["q_reason"] = "売上の2期分が無い"
        return res
    r0, r1 = rev[0], rev[1]
    if (_d(asof) - _d(r0["filed"])).days > Q["max_annual_age_days"]:
        res["q_reason"] = "最新の10-Kが古い"
        return res
    if not 350 <= (_d(r0["end"]) - _d(r1["end"])).days <= 380:
        res["q_reason"] = "前年の期間が連続していない"
        return res
    if r0["val"] <= 0 or r1["val"] <= 0:
        res["q_reason"] = "売上が0以下"
        return res
    miss = []
    growth = r0["val"] / r1["val"] - 1
    op = _match(pit_annual(F.get("op_income", []), asof), r0)
    ocf = _match(pit_annual(F.get("ocf", []), asof), r0)
    cap = _match(pit_annual(F.get("capex", []), asof), r0)
    opm = op["val"] / r0["val"] if op else None
    fcfm = (ocf["val"] - cap["val"]) / r0["val"] if (ocf and cap) else None
    if opm is None:
        miss.append("営業利益")
    if fcfm is None:
        miss.append("営業CF/設備投資")
    # 自己資本比率: 総資産と株主資本が同じ期末で揃う最新のもの
    A = pit_instant_rows(sec["inst"].get("assets", []), asof)
    E = pit_instant_rows(sec["inst"].get("equity", []), asof)
    both = sorted(set(A) & set(E))
    eqr = None
    if both:
        e = both[-1]
        if (_d(asof) - _d(e)).days <= Q["balance_sheet_max_age_days"] and A[e]["val"] > 0:
            eqr = E[e]["val"] / A[e]["val"]
    if eqr is None:
        miss.append("総資産/株主資本")
    # 希薄化: 10-Kの表紙の株式数 最新 vs 1つ前(分割は調整)
    S = pit_instant_rows(sec["inst"].get("shares", []), asof, forms=ANNUAL_FORMS)
    dil = None
    ends = sorted(S)
    if len(ends) >= 2:
        e0, e1 = ends[-1], ends[-2]
        if 300 <= (_d(e0) - _d(e1)).days <= 430 and S[e1]["val"] > 0:
            dil = S[e0]["val"] / (S[e1]["val"] * split_factor(splits, e1, e0)) - 1
    if dil is None:
        miss.append("発行済株式数(2期)")
    res.update(q_rev_growth=growth, q_op_margin=opm if opm is not None else np.nan,
               q_fcf_margin=fcfm if fcfm is not None else np.nan,
               q_equity_ratio=eqr if eqr is not None else np.nan,
               q_dilution=dil if dil is not None else np.nan)
    if miss:
        res["q_reason"] = "欠損: " + "/".join(miss)   # ★推定で埋めない
        return res
    I = Q["items"]
    pts = [float(bin_points(np.array([v]), I[k]["bins"])[0])
           for k, v in (("revenue_growth", growth), ("operating_margin", opm), ("fcf_margin", fcfm),
                        ("equity_ratio", eqr), ("share_dilution", dil))]
    score = sum(pts)
    G = Q["grades"]
    res["q_score"] = score
    res["q_status"] = "HIGH" if score >= G["high_min"] else ("LOW" if score < G["low_below"] else "MID")
    return res


def v2_grade(q_status, t_grade):
    """V2のA/B/C/D。Timing高 = v1判定のA/B。
    判定不能は最高でもC(上限)。ただし判定可能な中程度(MID)より上にはしない。"""
    th = t_grade in ("A", "B")
    if t_grade == "D" or q_status == "LOW":
        return "D"
    if q_status == "UNDET":
        return "C"
    if q_status == "HIGH":
        return "A" if th else "B"
    return "B" if th else "C"   # MID: Timing高ならB、そうでなければC
