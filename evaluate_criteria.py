#!/usr/bin/env python3
"""事前登録した成功基準(config/criteria_v2.yaml)を機械的に判定する。★このファイルは凍結対象。
    python evaluate_criteria.py --dev results/v2_dev_xxx/signals.csv.gz [--final results/v2_final/signals.csv.gz]
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.backtest import cohort_maxdd
from src.v2 import make_groups_v2

HD = {"1m": 21, "3m": 63, "6m": 126}


def daily_diff(g, b, h, min_n):
    col = f"ret_{h}"
    cnt = g.groupby("date")[col].count()
    keep = cnt[cnt >= min_n].index
    gg, bb = g[g.date.isin(keep)], b[b.date.isin(keep)]
    mean = (gg.groupby("date")[col].mean() - bb.groupby("date")[col].mean()).dropna().sort_index()
    med = (gg.groupby("date")[col].median() - bb.groupby("date")[col].median()).dropna().sort_index()
    return mean, med


def block_ci(x, L, B, seed, ci):
    x = np.asarray(x, float)
    n = len(x)
    if n < 6:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / L))
    m = np.empty(B)
    for k in range(B):
        idx = (rng.integers(0, n, nb)[:, None] + np.arange(L)[None, :]) % n
        m[k] = x[idx.ravel()[:n]].mean()
    a = (1 - ci) / 2
    return float(np.percentile(m, 100 * a)), float(np.percentile(m, 100 * (1 - a)))


def measure(sig, base_name, K):
    P, S = K["primary_test"], K["bootstrap"]
    G = make_groups_v2(sig)
    g, b = G[P["group"]], G[base_name]
    h = P["horizon"]
    mean, med = daily_diff(g, b, h, P["min_signals_per_day"])
    lo, hi = block_ci(mean.values, S["block_length_dates"], S["n_resamples"], S["seed"], S["ci"])
    C = K["criteria"]
    yr = mean.groupby(mean.index.year).agg(["mean", "count"])
    yr = yr[yr["count"] >= C["c4_min_days_per_year"]]
    alld = sorted(sig.date.unique())
    dd_g, dd_b = cohort_maxdd(g, alld), cohort_maxdd(b, alld)
    return dict(n_days=len(mean), mean=float(mean.mean()) if len(mean) else np.nan, ci_lo=lo, ci_hi=hi,
                median_diff=float(med.mean()) if len(med) else np.nan,
                year_pos_share=float((yr["mean"] > 0).mean()) if len(yr) else np.nan, n_years=len(yr),
                dd_group=dd_g, dd_base=dd_b, dd_diff_pt=(dd_g - dd_b) * 100)


def judge_dev(m, K):
    C, P = K["criteria"], K["primary_test"]
    ok = lambda v: bool(v) if v == v else False  # noqa: E731  NaNはFalse
    r = {"c1": ok(m["mean"] >= C["c1_mean_diff_min"]),
         "c2": ok(m["ci_lo"] > C["c2_ci_lower_above"]) and m["n_days"] >= P["min_days_required"],
         "c3": ok(m["median_diff"] >= C["c3_median_diff_min"]),
         "c4": ok(m["year_pos_share"] >= C["c4_positive_year_share_min"]),
         "c5": ok(m["dd_diff_pt"] > -C["c5_max_dd_worse_than_baseline_pt"])}   # ちょうど5ポイント悪化も不合格
    r["dev_pass"] = all(r.values())
    return r


def judge_final(mf, K):
    C = K["criteria"]
    if mf["n_days"] < C["c6_min_days_final"]:
        return {"c6": None, "note": f"最終検証の採用日数が{mf['n_days']}日(<{C['c6_min_days_final']})で判定不能"}
    return {"c6": bool(mf["mean"] > 0)}


def decide(K, t1_dev, t1_c6, t2_dev, t2_c6, final_given):
    """二段階評価の結論。戻り値: (コード, 文章)。
    Tier2は『Tier1を満たした場合にだけ』評価する。t2_dev/t2_c6 は Tier1を満たさない場合は None でよい。
    t1_c6/t2_c6: True / False / None(判定不能)"""
    O = K["verdict"]["outcomes"]
    if not t1_dev:
        return "tier1_not_met", O["tier1_not_met"]
    if not final_given:
        return "tier1_dev_met_final_pending", O["tier1_dev_met_final_pending"]
    if t1_c6 is None:
        return "undetermined", O["undetermined"]
    if t1_c6 is False:
        return "tier1_not_met", O["tier1_not_met"]
    # ---- Tier1を満たした: ここで初めてTier2を評価する ----
    if t2_c6 is None:
        return "undetermined", "Tier1は満たした。Tier2(RSI単独との比較)は最終検証の採用日数が不足で判定不能: " + O["tier1_met_tier2_not_met"]
    if t2_dev and t2_c6:
        return "tier1_met_tier2_met", O["tier1_met_tier2_met"]
    return "tier1_met_tier2_not_met", O["tier1_met_tier2_not_met"]


def _lines(tag, K, base, m, j):
    h = K["primary_test"]["horizon"]
    return [f"- 採用日数 {m['n_days']} / 平均差 {m['mean']:+.2%} / 95%区間 [{m['ci_lo']:+.2%}, {m['ci_hi']:+.2%}] / 中央値差 {m['median_diff']:+.2%}",
            f"- プラスだった年 {m['year_pos_share']:.0%}（対象{m['n_years']}年） / 最大DD 群{m['dd_group']:.1%} vs 基準{m['dd_base']:.1%}（差 {m['dd_diff_pt']:+.1f}pt）",
            "- 判定: " + " ".join(f"{k}={'○' if v else '×'}" for k, v in j.items() if k.startswith("c")) + f"  → dev合格: {'はい' if j['dev_pass'] else 'いいえ'}"]


def report(K, dev_sig, final_sig=None):
    P, B = K["primary_test"], K["baselines"]
    out = ["# 二段階評価（Tier1が主要な成功判定。Tier2はTier1を満たした場合にだけ評価）", ""]
    # ---- Tier1: 全銘柄ベースライン ----
    m1 = measure(dev_sig, B["tier1"], K)
    j1 = judge_dev(m1, K)
    out += [f"## Tier1: {P['group']} vs {B['tier1']}（{P['horizon']}後・dev期間）"] + _lines("t1", K, B["tier1"], m1, j1)
    c6_1 = None
    if final_sig is not None:
        mf1 = measure(final_sig, B["tier1"], K)
        jf1 = judge_final(mf1, K)
        c6_1 = jf1["c6"]
        out.append(f"- 最終検証(シグナル2011-01〜2015-11): 採用日数 {mf1['n_days']} / 平均差 {mf1['mean']:+.2%} [{mf1['ci_lo']:+.2%}, {mf1['ci_hi']:+.2%}] / c6={c6_1}" + (f"（{jf1['note']}）" if 'note' in jf1 else ""))
    out.append("")
    # ---- Tier2: RSI単独。Tier1(dev)を満たした場合だけ判定する ----
    t2_dev = t2_c6 = None
    if not j1["dev_pass"]:
        m2 = measure(dev_sig, B["tier2"], K)
        out += [f"## Tier2: {P['group']} vs {B['tier2']}  ※Tier1(dev)を満たしていないため評価しません（下は参考値・判定対象外）",
                f"- 参考: 採用日数 {m2['n_days']} / 平均差 {m2['mean']:+.2%} / 95%区間 [{m2['ci_lo']:+.2%}, {m2['ci_hi']:+.2%}]", ""]
    else:
        m2 = measure(dev_sig, B["tier2"], K)
        j2 = judge_dev(m2, K)
        t2_dev = j2["dev_pass"]
        out += [f"## Tier2: {P['group']} vs {B['tier2']}（{P['horizon']}後・dev期間。Tier1(dev)を満たしたので評価）"] + _lines("t2", K, B["tier2"], m2, j2)
        if m2["mean"] < 0:
            out.append(f"- ★RSI単独のほうが平均で {abs(m2['mean']):.2%} 上回っています")
        if final_sig is not None:
            mf2 = measure(final_sig, B["tier2"], K)
            jf2 = judge_final(mf2, K)
            t2_c6 = jf2["c6"]
            out.append(f"- 最終検証: 採用日数 {mf2['n_days']} / 平均差 {mf2['mean']:+.2%} [{mf2['ci_lo']:+.2%}, {mf2['ci_hi']:+.2%}] / c6={t2_c6}")
        out.append("")
    code, text = decide(K, j1["dev_pass"], c6_1, t2_dev, t2_c6, final_sig is not None)
    out += ["## 結論", f"- **{text}**", f"- (コード: {code})"]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default="config/criteria_v2.yaml")
    ap.add_argument("--dev", required=True)
    ap.add_argument("--final", default="")
    a = ap.parse_args()
    K = yaml.safe_load(Path(a.criteria).read_text(encoding="utf-8"))
    dev = pd.read_csv(a.dev, parse_dates=["date"])
    fin = pd.read_csv(a.final, parse_dates=["date"]) if a.final else None
    print(report(K, dev, fin))
