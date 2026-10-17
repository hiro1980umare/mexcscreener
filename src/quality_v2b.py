"""V2b の Quality: 確立された3指標の『同日・銘柄間の順位』の平均。
  1. 収益性      : 営業利益 / 総資産(最新10-Kの期末)           高いほど良い  Novy-Marx(2013), Fama-French(2015)
  2. 利益の質    : (純利益 - 営業CF) / 総資産 = アクルーアル     低いほど良い  Sloan(1996)
  3. 株式の希薄化: 発行済株式数の1年変化率(分割は調整)           低いほど良い  Pontiff & Woodgate(2008)
調整できる数値は『3分割(上位3分の1=HIGH, 下位3分の1=LOW)』だけ。点数のしきい値は無い。
どれか1つでも取れなければ判定不能(推定で埋めない)。先読み防止はV2と同じ部品(提出日が基準日より前のものだけ)。"""
import numpy as np
import pandas as pd

from .sec_pit import ANNUAL_FORMS, _d, _match, pit_annual, pit_instant_rows, split_factor

ITEMS = ("qb_prof", "qb_accr", "qb_dil")
EPS = 1e-9


def _match_any(cs, asof, r0):
    """同じ期間(開始日・終了日)の値を、タグ候補の優先順に探す。"""
    for tag_rows in cs:
        m = _match(pit_annual([tag_rows], asof), r0)
        if m is not None:
            return m
    return None


def quality_raw_v2b(sec, asof, splits, Q):
    """基準日(asof)より前に提出されたデータだけから、3つの生の値を計算する。取れない項目はNaN(推定しない)。"""
    res = {"qb_prof": np.nan, "qb_accr": np.nan, "qb_dil": np.nan, "q_reason": ""}
    F = sec["flow"]
    op = pit_annual(F.get("op_income", []), asof)
    if not op:
        res["q_reason"] = "営業利益が無い"
        return res
    r0 = op[0]
    if (_d(asof) - _d(r0["filed"])).days > Q["max_annual_age_days"]:
        res["q_reason"] = "最新の10-Kが古い"
        return res
    ni = _match_any(F.get("net_income", []), asof, r0)
    ocf = _match(pit_annual(F.get("ocf", []), asof), r0)
    a = pit_instant_rows(sec["inst"].get("assets", []), asof).get(r0["end"])
    miss = []
    if ni is None:
        miss.append("純利益")
    if ocf is None:
        miss.append("営業CF")
    if a is None or not a["val"] > 0:
        miss.append("総資産(期末)")
    # 希薄化: 直近2回の10-K表紙の株式数(V2と同じ定義) + 古すぎないこと
    S = pit_instant_rows(sec["inst"].get("shares", []), asof, forms=ANNUAL_FORMS)
    ends = sorted(S)
    dil = np.nan
    if len(ends) >= 2 and 300 <= (_d(ends[-1]) - _d(ends[-2])).days <= 430 and S[ends[-2]]["val"] > 0 \
            and (_d(asof) - _d(ends[-1])).days <= Q["max_annual_age_days"]:
        e1, e0 = ends[-2], ends[-1]
        dil = S[e0]["val"] / (S[e1]["val"] * split_factor(splits, e1, e0)) - 1
    else:
        miss.append("発行済株式数(2期)")
    if miss:
        res["q_reason"] = "欠損: " + "/".join(miss)
        return res
    res.update(qb_prof=r0["val"] / a["val"], qb_accr=(ni["val"] - ocf["val"]) / a["val"], qb_dil=float(dil))
    return res


def rank_quality_v2b(raw, Q):
    """同じ日のGate通過銘柄のうち3要素が揃う銘柄で、各要素を0〜1の順位にして等金額平均 → 上位1/3=HIGH, 下位1/3=LOW。
    判定可能な銘柄がその日 min_cross_section 未満なら、その日は全て判定不能。"""
    out = pd.DataFrame({"q_status": "UNDET", "q_score": np.nan, "q_reason": raw["q_reason"]}, index=raw.index)
    ok = raw[list(ITEMS)].notna().all(axis=1)
    if ok.sum() < Q["min_cross_section"]:
        out.loc[ok, "q_reason"] = f"その日の判定可能銘柄が{int(ok.sum())}(<{Q['min_cross_section']})"
        return out
    r = pd.DataFrame({"p": raw.loc[ok, "qb_prof"].rank(pct=True),
                      "a": (-raw.loc[ok, "qb_accr"]).rank(pct=True),     # 低いほど良い → 符号を反転して順位
                      "d": (-raw.loc[ok, "qb_dil"]).rank(pct=True)})
    comp = r.mean(axis=1)
    cr = comp.rank(pct=True)
    k = Q["n_groups"]
    out.loc[ok, "q_score"] = comp
    out.loc[ok, "q_status"] = np.where(cr > 1 - 1 / k + EPS, "HIGH", np.where(cr <= 1 / k + EPS, "LOW", "MID"))
    out.loc[ok, "q_reason"] = ""
    return out
