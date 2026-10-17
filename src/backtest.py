"""バックテスト。
- シグナルは「その日の終値までのデータ」で計算 -> 翌営業日の始値で買う
- h営業日後の終値で売る。往復コスト = 片道bps x 2 を控除
- 欠損(取引なし等)は落とすだけで、推定値では埋めない
"""
import warnings

import numpy as np
import pandas as pd

from .signals import evaluate_at

HNAMES = ("1m", "3m", "6m")


def forward(O, C, i, h, cost):
    """i日にシグナル -> i+1の始値で買い、i+1+hの終値で売る。戻り値: (リターン, 保有中の最大下落)"""
    entry = O[i + 1]
    with np.errstate(invalid="ignore", divide="ignore"):   # 始値0などの異常行はNaN/inf→集計時に除外（警告の連発を防ぐ）
        ret = C[i + 1 + h] / entry - 1 - 2 * cost
        ret = np.where(np.isfinite(ret), ret, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(invalid="ignore", divide="ignore"):
            mae = np.nanmin(C[i + 1:i + 2 + h], axis=0) / entry - 1
    return ret, mae


def collect_signals(panel, ind, R, start, end, evaluator=None):
    B = R["backtest"]
    H = B["horizons_trading_days"]
    cost = B["cost_per_side_bps"] / 1e4
    N = B["signal_every_n_trading_days"]
    maxh = max(H.values())
    O, C = panel.open.values, panel.close.values
    SO, SC = panel.spy_open.reindex(ind.dates).values, panel.spy_close.reindex(ind.dates).values
    tpos = ind.tindex
    cand = np.flatnonzero((ind.dates >= pd.Timestamp(start)) & (ind.dates <= pd.Timestamp(end)))[::N]
    rows, spy_rows = [], []
    for i in cand:
        if i + 1 + maxh >= len(ind.dates):
            continue  # 6か月先まで見えない日は使わない(全期間で同じ標本にそろえる)
        spy_row = {"date": ind.dates[i]}
        for nm, h in H.items():
            spy_row[f"ret_{nm}"] = SC[i + 1 + h] / SO[i + 1] - 1 - 2 * cost
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                spy_row[f"mae_{nm}"] = np.nanmin(SC[i + 1:i + 2 + h]) / SO[i + 1] - 1
        spy_rows.append(spy_row)
        df = (evaluator or evaluate_at)(ind, i, R)
        if df.empty:
            continue
        cols = np.array([tpos[t] for t in df.index])
        df["date"] = ind.dates[i]
        for nm, h in H.items():
            ret, mae = forward(O, C, i, h, cost)
            df[f"ret_{nm}"], df[f"mae_{nm}"] = ret[cols], mae[cols]
            df[f"excess_{nm}"] = df[f"ret_{nm}"] - spy_row[f"ret_{nm}"]
        rows.append(df.reset_index())
    sig = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return sig, pd.DataFrame(spy_rows)


def make_groups(sig):
    g = {"ALL_GATE(全銘柄ベースライン)": sig, "SIMPLE_RSI(RSI<30だけ)": sig[sig.simple_rsi]}
    for k in ("A", "B", "C", "D"):
        g[f"grade_{k}"] = sig[sig.grade == k]
    g["grade_A+B"] = sig[sig.grade.isin(["A", "B"])]
    for k in ("PULLBACK", "OVERSOLD", "MARKET_DIP", "RECOVERY"):
        g[f"type_{k}(含む)"] = sig[sig[f"t_{k}"]]
    g["primary_MIXED"] = sig[sig.primary_type == "MIXED"]
    g["score<45"] = sig[sig.score < 45]
    g["score45-65"] = sig[(sig.score >= 45) & (sig.score < 65)]
    g["score65-80"] = sig[(sig.score >= 65) & (sig.score < 80)]
    g["score>=80"] = sig[sig.score >= 80]
    return g


def cohort_maxdd(df, all_dates, col="ret_1m"):
    """毎回の等金額コホート(1か月保有)を順に繋いだ資産曲線の最大ドローダウン。シグナル無しの月は現金(0%)。"""
    s = df.groupby("date")[col].mean().reindex(all_dates).fillna(0.0)
    eq = (1 + s).cumprod()
    return float((eq / eq.cummax() - 1).min())


def summarize(groups, spy_df):
    all_dates = pd.DatetimeIndex(spy_df["date"])
    rows = []
    for nm in HNAMES:
        r = spy_df[f"ret_{nm}"]
        rows.append(dict(group="SPY", horizon=nm, n=len(r), n_dates=len(r), win_rate=(r > 0).mean(),
                         mean=r.mean(), median=r.median(), mean_excess=0.0, beat_spy=np.nan,
                         avg_mae=spy_df[f"mae_{nm}"].mean(), worst_mae=spy_df[f"mae_{nm}"].min(),
                         se_mean=r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else np.nan,
                         cohort_maxdd=cohort_maxdd(pd.DataFrame({"date": all_dates, "ret_1m": spy_df["ret_1m"].values}),
                                                   all_dates) if nm == "1m" else np.nan))
    for gname, df in groups.items():
        for nm in HNAMES:
            r = df[f"ret_{nm}"].dropna() if len(df) else pd.Series(dtype=float)
            if r.empty:
                rows.append(dict(group=gname, horizon=nm, n=0))
                continue
            by_date = df.groupby("date")[f"ret_{nm}"].mean()
            ex = df[f"excess_{nm}"].dropna()
            rows.append(dict(group=gname, horizon=nm, n=len(r), n_dates=by_date.size,
                             win_rate=(r > 0).mean(), mean=r.mean(), median=r.median(),
                             mean_excess=ex.mean(), beat_spy=(ex > 0).mean(),
                             avg_mae=df[f"mae_{nm}"].mean(), worst_mae=df[f"mae_{nm}"].min(),
                             se_mean=by_date.std(ddof=1) / np.sqrt(by_date.size) if by_date.size > 1 else np.nan,
                             cohort_maxdd=cohort_maxdd(df, all_dates) if nm == "1m" else np.nan))
    return pd.DataFrame(rows)


def to_markdown(summary, meta):
    f = lambda x, p=1: "-" if pd.isna(x) else f"{x * 100:.{p}f}%"  # noqa: E731
    out = [f"# バックテスト結果  {meta['rules_version']} / {meta['period']}", ""]
    for k in ("rules_sha256", "git_commit", "signal_range", "data_end", "n_tickers_in_data",
              "n_signal_dates", "n_gate_rows"):
        out.append(f"- {k}: {meta.get(k)}")
    out += ["", "## 読み方の注意（必ず読む）"] + [f"- {n}" for n in meta["notes"]] + [""]
    for nm in HNAMES:
        out += [f"## {nm}後", "",
                "| グループ | n | 日数 | 勝率 | 平均 | 中央値 | 対SPY超過(平均) | SPYに勝つ率 | 保有中最大下落(平均) | 同(最悪) | 平均の標準誤差 | コホート最大DD(1m) |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in summary[summary.horizon == nm].iterrows():
            if r["n"] == 0:
                out.append(f"| {r['group']} | 0 | - | - | - | - | - | - | - | - | - | - |")
                continue
            out.append(f"| {r['group']} | {int(r['n'])} | {int(r['n_dates'])} | {f(r['win_rate'], 0)} | {f(r['mean'])} | "
                       f"{f(r['median'])} | {f(r['mean_excess'])} | {f(r['beat_spy'], 0)} | {f(r['avg_mae'])} | "
                       f"{f(r['worst_mae'])} | {f(r['se_mean'])} | {f(r['cohort_maxdd'])} |")
        out.append("")
    return "\n".join(out)
