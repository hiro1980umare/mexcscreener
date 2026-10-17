"""株価データの異常値検出。★検出と分類だけ。除外・補正は一切しない（人が確認してから approved_fixes.csv に書く）。"""
import numpy as np
import pandas as pd

SPLIT_K = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]


def split_like_ratio(ratio, tol=0.15):
    """1日の価格比 ratio(=close/前日close) が 1/k(通常分割) か k(併合) に近ければ k を返す。"""
    for k in SPLIT_K:
        if abs(ratio * k - 1) < tol:
            return f"1:{k}分割に似た比率"
        if abs(ratio / k - 1) < tol:
            return f"{k}:1併合に似た比率"
    return ""


def find_anomalies(close, open_, volume, high=0.8, watch=0.5, revert_tol=0.25):
    """日次リターンの絶対値が watch(50%) 以上の日を全て一覧にする。high(80%)以上は level=HIGH。"""
    r = close.pct_change(fill_method=None)
    nxt = r.shift(-1)
    back = close.shift(-1) / close.shift(1) - 1   # 翌日終値 / 前日終値 - 1  (0付近 = 元に戻った)
    vmed = volume.shift(1).rolling(20, min_periods=10).median()   # 直前20日の出来高の中央値
    vspike = volume / vmed.replace(0, np.nan)                      # 当日の出来高 / 平常時
    back_prev = close / close.shift(2) - 1       # 当日終値 / 2日前終値 - 1（前日の異常が当日戻った形）
    gap = open_ / close.shift(1) - 1
    idx = np.argwhere((r.abs() >= watch).values)
    rows = []
    for i, j in idx:
        t, d = close.columns[j], close.index[i]
        ret = float(r.iat[i, j])
        prev = float(close.iat[i - 1, j])
        rows.append(dict(
            ticker=t, date=d.date(), prev_close=round(prev, 4), close=round(float(close.iat[i, j]), 4),
            ret=ret, next_ret=float(nxt.iat[i, j]) if not np.isnan(nxt.iat[i, j]) else np.nan,
            ret_2day_back=float(back.iat[i, j]) if not np.isnan(back.iat[i, j]) else np.nan,
            open_gap=float(gap.iat[i, j]) if not np.isnan(gap.iat[i, j]) else np.nan,
            volume=float(volume.iat[i, j]) if not np.isnan(volume.iat[i, j]) else np.nan,
            vol_spike=float(vspike.iat[i, j]) if np.isfinite(vspike.iat[i, j]) else np.nan,
            level="HIGH" if abs(ret) >= high else "WATCH",
            spike_revert=bool((not np.isnan(back.iat[i, j]) and abs(back.iat[i, j]) < revert_tol)
                              or (not np.isnan(back_prev.iat[i, j]) and abs(back_prev.iat[i, j]) < revert_tol)),
            split_like=split_like_ratio(1 + ret)))
    cols = ["ticker", "date", "prev_close", "close", "ret", "next_ret", "ret_2day_back", "open_gap", "volume", "vol_spike",
            "level", "spike_revert", "split_like"]
    return pd.DataFrame(rows, columns=cols)


def _max_flat_run(s):
    """同じ終値が連続した日数の最大値（前日と同値の日が何日続いたか）。"""
    same = s.diff().eq(0)
    if not same.any():
        return 0
    return int(same.groupby((~same).cumsum()).sum().max())


def basic_checks(close, volume, open_=None):
    """銘柄ごとの基本品質: 0以下の価格、出来高0の日、同値が続く期間、途中の欠損。"""
    out = pd.DataFrame({
        "nonpositive_price_days": (close <= 0).sum(),
        "zero_volume_days": ((volume == 0) & close.notna()).sum(),
    })
    if open_ is not None:
        out["nonpositive_open_days"] = (open_ <= 0).sum()
    out["max_flat_run_days"] = close.apply(_max_flat_run)
    first, last = close.apply(lambda s: s.first_valid_index()), close.apply(lambda s: s.last_valid_index())
    inner_na = []
    for t in close.columns:
        s_ = close[t].loc[first[t]:last[t]] if first[t] is not None else close[t].iloc[:0]
        inner_na.append(int(s_.isna().sum()))
    out["inner_missing_days"] = inner_na
    return out


def classify(row, splits):
    """splits: その銘柄の分割記録 Series（日付index, 値=分割比。2.0=2分割, 0.333=3併合）または None（取得失敗）。
    ★分類は『確認の優先順位』を付けるためのもの。除外・補正の根拠にはしない。"""
    d = pd.Timestamp(row["date"])
    near, val = False, np.nan
    if splits is not None and len(splits):
        sidx = pd.DatetimeIndex(splits.index).tz_localize(None)
        m = np.abs((sidx - d).days) <= 3
        if m.any():
            near, val = True, float(splits.values[np.argmax(m)])
    row = dict(row)
    row["split_recorded_near"], row["split_ratio_recorded"] = near, val
    ratio = 1 + row["ret"]
    adj = near and np.isfinite(val) and val > 0 and abs(ratio * val - 1) < 0.10   # 分割比どおりに価格が動いている
    vs = row.get("vol_spike", np.nan)
    big_vol = vs is not None and np.isfinite(vs) and vs >= 3
    note = "" if splits is not None else "(分割情報の取得に失敗)"
    if adj:
        cls = "ADJUSTMENT_SUSPECT 分割記録と同じ比率で価格が動いている: 調整が反映されていない疑い"
    elif near:
        cls = "SPLIT_NEAR_RATIO_DIFFERS 分割記録の近くだが比率が合わない: 要確認"
    elif big_vol:
        cls = "REAL_MOVE_LIKELY 出来高が平常の3倍以上を伴う急変動: 実需(ニュース等)の可能性が高い"
    elif row["spike_revert"]:
        cls = "GLITCH_SUSPECT 出来高の急増なしで翌日までに元に戻っている: データ誤りの疑い"
    else:
        cls = "UNEXPLAINED 分割記録も出来高急増もない: 要手動確認"
    row["classification"] = cls + note
    return row
