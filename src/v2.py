"""V2のシグナル収集。Timingはv1のevaluate_atをそのまま使い、Gate(時価総額・価格)だけ差し替え、Qualityを付ける。"""
import warnings

import numpy as np
import pandas as pd

from .backtest import cohort_maxdd, forward
from .quality_v2b import quality_raw_v2b, rank_quality_v2b
from .sec_pit import market_cap_at, quality_at, v2_grade
from .signals import evaluate_at


def check_gate_consistency(R1, R2):
    G2 = R2["gate"]
    for k in ("min_market_cap", "min_avg_dollar_volume_50d"):
        assert G2[k] == R1["gate"][k], f"V2のGate({k})がv1と違います"
    assert G2["min_price_unadjusted"] == R1["gate"]["min_price"]


def evaluate_day_v2(panel, ind, R1, R2, sec, i):
    """i番目の日付について、Gate -> Timing(v1) -> Quality -> V2判定表 を計算する(先の成績は含まない)。
    ★バックテスト(collect_signals_v2)と通知Botの両方が、この同じ関数を使う。"""
    G2, Q = R2["gate"], R2["quality"]
    RAW, DV = panel.close_raw.values, ind.a["dollar_vol"]
    tick = ind.tickers
    asof = str(ind.dates[i].date())
    mcap = np.full(len(tick), np.nan)
    gp = np.full(len(tick), np.nan)
    pre = np.flatnonzero(np.isfinite(RAW[i]) & (DV[i] >= G2["min_avg_dollar_volume_50d"]))
    for j in pre:
        s = sec.get(tick[j])
        if s is None:
            continue
        mcap[j], gp[j] = market_cap_at(s, asof, RAW[i, j], panel.splits.get(tick[j]), G2["shares_max_age_days"])
    df = evaluate_at(ind, i, R1, overrides={"mcap": mcap, "gate_price": gp})
    if df.empty:
        return df
    if Q.get("mode") == "rank_v2b":      # V2b: 3指標の同日順位平均(上位1/3=HIGH)。Timing・Gate・判定表はV2と同じ
        raw = pd.DataFrame([quality_raw_v2b(sec[t], asof, panel.splits.get(t), Q) for t in df.index], index=df.index)
        q = pd.concat([raw.drop(columns="q_reason"), rank_quality_v2b(raw, Q)], axis=1)
    else:                                # V2: 絶対値の点数(従来どおり・変更なし)
        q = pd.DataFrame([quality_at(sec[t], asof, panel.splits.get(t), Q) for t in df.index], index=df.index)
    df = df.join(q)
    df["v2_grade"] = [v2_grade(a, b) for a, b in zip(df.q_status, df.grade)]   # df.grade = v1のTiming判定
    return df


def collect_signals_v2(panel, ind, R1, R2, sec, start, end, log=print):
    B = R2["backtest"]
    H = B["horizons_trading_days"]
    cost = B["cost_per_side_bps"] / 1e4
    N, maxh = B["signal_every_n_trading_days"], max(H.values())
    check_gate_consistency(R1, R2)
    O, C = panel.open.values, panel.close.values
    SO, SC = panel.spy_open.reindex(ind.dates).values, panel.spy_close.reindex(ind.dates).values
    tick, tpos = ind.tickers, ind.tindex
    cand = np.flatnonzero((ind.dates >= pd.Timestamp(start)) & (ind.dates <= pd.Timestamp(end)))[::N]
    rows, spy_rows, cov = [], [], []
    for n_done, i in enumerate(cand):
        if i + 1 + maxh >= len(ind.dates):
            continue
        spy_row = {"date": ind.dates[i]}
        for nm, h in H.items():
            spy_row[f"ret_{nm}"] = SC[i + 1 + h] / SO[i + 1] - 1 - 2 * cost
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                spy_row[f"mae_{nm}"] = np.nanmin(SC[i + 1:i + 2 + h]) / SO[i + 1] - 1
        spy_rows.append(spy_row)
        df = evaluate_day_v2(panel, ind, R1, R2, sec, i)
        if df.empty:
            continue
        cols = np.array([tpos[t] for t in df.index])
        df["date"] = ind.dates[i]
        for nm, h in H.items():
            ret, mae = forward(O, C, i, h, cost)
            df[f"ret_{nm}"], df[f"mae_{nm}"] = ret[cols], mae[cols]
            df[f"excess_{nm}"] = df[f"ret_{nm}"] - spy_row[f"ret_{nm}"]
        rows.append(df.reset_index())
        if (n_done + 1) % 20 == 0:
            log(f"  {n_done + 1}/{len(cand)}日 処理済み")
    sig = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return sig, pd.DataFrame(spy_rows)


def make_groups_v2(sig):
    th = sig.grade.isin(["A", "B"])          # Timing高 = v1判定のA/B
    q = sig.q_status
    return {
        "ALL_GATE": sig,
        "SIMPLE_RSI": sig[sig.simple_rsi],
        "V2_A": sig[sig.v2_grade == "A"], "V2_B": sig[sig.v2_grade == "B"],
        "V2_C": sig[sig.v2_grade == "C"], "V2_D": sig[sig.v2_grade == "D"],
        "Q_HIGH_ONLY": sig[q == "HIGH"], "T_HIGH_ONLY": sig[th],
        "Q_HIGH_T_NOT_HIGH": sig[(q == "HIGH") & ~th],
        "Q_MID_T_HIGH": sig[(q == "MID") & th], "Q_LOW_T_HIGH": sig[(q == "LOW") & th],
        "Q_UNDET_T_HIGH": sig[(q == "UNDET") & th], "DETERMINABLE_ALL": sig[q != "UNDET"],
    }


def coverage_table(sig):
    """年ごとのGate通過行数とQualityの判定可能率。判定不能の理由の内訳も返す。"""
    d = sig.assign(year=sig.date.dt.year)
    t = d.groupby("year").agg(rows=("q_status", "size"),
                              determinable=("q_status", lambda s: (s != "UNDET").mean()),
                              high=("q_status", lambda s: (s == "HIGH").mean()),
                              mid=("q_status", lambda s: (s == "MID").mean()),
                              low=("q_status", lambda s: (s == "LOW").mean()))
    reasons = d[d.q_status == "UNDET"].q_reason.str.split(":").str[0].value_counts()
    return t, reasons
