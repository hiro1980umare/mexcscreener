"""ある1日(i番目)について、Gate -> スコア -> Red Flag -> シグナル分類 -> A/B/C/D を計算する。
閾値はすべて rules_*.yaml から読む。コード側に数値を直書きしない。"""
import numpy as np
import pandas as pd

NEEDED = ["close", "rsi", "sma20", "sma50", "sma200", "dd", "vol_ratio", "slope200",
          "rel20", "rel_dd", "ret5", "mcap", "dollar_vol"]
TYPES = ["PULLBACK", "OVERSOLD", "MARKET_DIP", "RECOVERY", "VALUE"]
GRADE_ORDER = ["A", "B", "C", "D"]


def bin_points(x, bins):
    """lo <= x < hi（None は無限大）。最初に当てはまった行の点数。"""
    out = np.zeros(len(x))
    done = np.zeros(len(x), bool)
    for b in bins:
        lo = -np.inf if b.get("lo") is None else b["lo"]
        hi = np.inf if b.get("hi") is None else b["hi"]
        m = (x >= lo) & (x < hi) & ~done
        out[m] = b["points"]
        done |= m
    return out


def grade_from(score, rf_count, n_types, R):
    """満たす最上位のグレード。どれも満たさなければD。"""
    score, rf_count, n_types = map(np.asarray, (score, rf_count, n_types))
    grade = np.full(score.shape, "D", dtype=object)
    for g in ("C", "B", "A"):
        spec = R["grades"][g]
        ok = (score >= spec["min_score"]) & (rf_count <= spec["max_red_flags"])
        if spec.get("require_signal_type"):
            ok &= n_types >= 1
        grade[ok] = g
    return grade


def downgrade(grade):
    """決算前などで1段階下げる。A->B, B->C, C->D, D->D"""
    i = GRADE_ORDER.index(grade)
    return GRADE_ORDER[min(i + 1, 3)]


def evaluate_at(ind, i, R, overrides=None):
    """overrides: V2用。{'mcap': 配列, 'gate_price': 配列} を渡すと、Gateの時価総額・価格だけ差し替える
    （渡さなければv1と完全に同じ動作）。"""
    A, G = ind.a, R["gate"]
    row = {k: v[i] for k, v in A.items()}
    if overrides:
        row.update(overrides)
    ok = np.ones(len(ind.tickers), bool)
    for k in NEEDED:
        ok &= np.isfinite(row[k])
    gate_price = row.get("gate_price", row["close"])
    ok &= np.isfinite(gate_price)
    ok &= (gate_price >= G["min_price"]) & (row["mcap"] >= G["min_market_cap"])
    ok &= row["dollar_vol"] >= G["min_avg_dollar_volume_50d"]
    idx = np.flatnonzero(ok)
    if idx.size == 0:
        return pd.DataFrame()
    r = {k: v[idx] for k, v in row.items()}
    spy_dd = ind.spy["dd"][i]
    S = R["score"]

    # ---- スコア ----
    s_trend = ((r["close"] > r["sma200"]) * S["trend"]["above_sma200"]
               + (r["slope200"] > 0) * S["trend"]["sma200_rising"]
               + (r["sma50"] > r["sma200"]) * S["trend"]["sma50_above_sma200"]).astype(float)
    s_depth = bin_points(r["dd"], S["depth_bins"])
    s_rsi = bin_points(r["rsi"], S["rsi_bins"])
    s_vol = bin_points(r["vol_ratio"], S["volume_bins"])
    s_rel = bin_points(r["rel20"], S["rel_bins"])
    score = s_trend + s_depth + s_rsi + s_vol + s_rel

    # ---- Red Flag ----
    F = R["red_flags"]
    rf = {
        "rf_long_downtrend": (r["close"] < r["sma200"]) & (r["slope200"] < F["long_downtrend"]["sma200_slope_below"]),
        "rf_idiosyncratic": r["rel_dd"] <= F["idiosyncratic_weakness"]["rel_dd_at_or_below"],
        "rf_deep_drawdown": r["dd"] <= F["deep_drawdown"]["dd_at_or_below"],
        "rf_crash_5d": r["ret5"] <= F["crash_5d"]["ret5_at_or_below"],
    }
    rf_count = sum(v.astype(int) for v in rf.values())

    # ---- シグナル分類 ----
    T = R["signal_types"]
    n = len(idx)
    t = {k: np.zeros(n, bool) for k in TYPES}
    if T["PULLBACK"]["enabled"]:
        P = T["PULLBACK"]
        m = (r["dd"] >= P["dd_min"]) & (r["dd"] < P["dd_max"]) & (r["rsi"] <= P["rsi_max"])
        if P["require_above_sma200"]:
            m &= r["close"] > r["sma200"]
        if P["require_sma200_rising"]:
            m &= r["slope200"] > 0
        if P["require_sma50_above_sma200"]:
            m &= r["sma50"] > r["sma200"]
        if P["require_below_sma20"]:
            m &= r["close"] <= r["sma20"]
        t["PULLBACK"] = m
    if T["OVERSOLD"]["enabled"]:
        t["OVERSOLD"] = r["rsi"] < T["OVERSOLD"]["rsi_below"]
    if T["MARKET_DIP"]["enabled"]:
        M = T["MARKET_DIP"]
        t["MARKET_DIP"] = ((spy_dd <= M["spy_dd_at_or_below"]) & (r["dd"] <= M["stock_dd_at_or_below"])
                           & (r["rel_dd"] >= M["rel_dd_at_or_above"]))
    if T["RECOVERY"]["enabled"]:
        Rv = T["RECOVERY"]
        m = (r["dd"] <= Rv["dd_at_or_below"]) & (r["rsi_min10"] < Rv["rsi_min10_below"])
        if Rv["require_above_sma20"]:
            m &= r["close"] > r["sma20"]
        if Rv["require_rsi_rising_5d"]:
            m &= r["rsi"] > r["rsi_lag5"]
        t["RECOVERY"] = m
    if T["require_no_red_flags"]:
        for k in t:
            t[k] = t[k] & (rf_count == 0)
    n_types = sum(v.astype(int) for v in t.values())
    names = [k for k in TYPES if t[k].any()]
    primary = np.full(n, "NONE", dtype=object)
    primary[n_types >= 2] = "MIXED"
    for k in names:
        primary[(n_types == 1) & t[k]] = k
    types_str = ["|".join(k for k in TYPES if t[k][j]) for j in range(n)]

    grade = grade_from(score, rf_count, n_types, R)

    df = pd.DataFrame({
        "close": r["close"], "rsi": r["rsi"], "dd_high": r["dd"],
        "vs_sma20": r["close"] / r["sma20"] - 1, "vs_sma50": r["close"] / r["sma50"] - 1,
        "vs_sma200": r["close"] / r["sma200"] - 1, "slope200": r["slope200"],
        "vol_ratio": r["vol_ratio"], "rel20": r["rel20"], "rel_dd": r["rel_dd"],
        "mcap_est": r["mcap"], "dollar_vol": r["dollar_vol"],
        "score": score, "s_trend": s_trend, "s_depth": s_depth, "s_rsi": s_rsi,
        "s_vol": s_vol, "s_rel": s_rel, "rf_count": rf_count, **rf,
        **{f"t_{k}": v for k, v in t.items()}, "n_types": n_types,
        "primary_type": primary, "types": types_str, "grade": grade,
        "simple_rsi": r["rsi"] < R["baselines"]["simple_rsi"]["rsi_below"],
    }, index=[ind.tickers[j] for j in idx])
    df.index.name = "ticker"
    return df
