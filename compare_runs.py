#!/usr/bin/env python3
"""2つのバックテスト結果（修正前 / 修正後）を、同じ方法で再集計して比較する。
signals.csv.gz から直接計算するので、集計方法の違いによるズレは出ない。

    python compare_runs.py --before results/v1_dev --after results/v1_dev_datafix1

指標:
  対ベースライン超過 = 『同じ日のGate通過全銘柄の平均』との差を日ごとに出して平均（95%信頼区間つき）
  期間別安定性       = 暦年ごとの対ベースライン超過と、プラスだった日の割合
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest import cohort_maxdd, make_groups

SHOW = ["grade_A", "grade_B", "grade_A+B", "type_PULLBACK(含む)", "type_OVERSOLD(含む)",
        "type_MARKET_DIP(含む)", "SIMPLE_RSI(RSI<30だけ)"]
BASE = "ALL_GATE(全銘柄ベースライン)"
HS = ("1m", "3m", "6m")


def load(d):
    d = Path(d)
    sig = pd.read_csv(d / "signals.csv.gz", parse_dates=["date"])
    meta = json.loads((d / "meta.json").read_text())
    return sig, meta


def rel_series(df, base, h):
    """日ごとの (グループ平均 - ベースライン平均)"""
    g = df.groupby("date")[f"ret_{h}"].mean()
    b = base.groupby("date")[f"ret_{h}"].mean()
    return (g - b).dropna()


def stats(df, base, h):
    r = df[f"ret_{h}"].dropna()
    if r.empty:
        return None
    rel = rel_series(df, base, h)
    se = rel.std(ddof=1) / np.sqrt(len(rel)) if len(rel) > 1 else np.nan
    m = rel.mean()
    return dict(n=len(r), n_dates=len(rel), win=(r > 0).mean(), mean=r.mean(), median=r.median(),
                excess_spy=df[f"excess_{h}"].mean(), rel=m, se=se, lo=m - 1.96 * se, hi=m + 1.96 * se,
                pos_dates=(rel > 0).mean(), worst_mae=df[f"mae_{h}"].min())


HDAYS = {"1m": 21, "3m": 63, "6m": 126}


def robust_stats(df, base, h, min_n=10, B=2000, seed=0):
    """極端な値と『シグナルが少ない日』に引きずられない評価（参考指標・ルールは変えない）。
    - 1日のシグナルが min_n 件未満の日は除く
    - 日ごとの『グループ平均 - ベースライン平均』を、日付ブロックのブートストラップで95%区間にする
      （3m/6mはリターン期間が重なるのでブロック長 = 保有期間/21日）
    - 中央値の差と、上下1%を丸めた(winsorize)平均の差も併記"""
    col = f"ret_{h}"
    cnt = df.groupby("date")[col].count()
    keep = cnt[cnt >= min_n].index
    d, b = df[df.date.isin(keep)], base[base.date.isin(keep)]
    if len(keep) < 6:
        return None
    gm, bm = d.groupby("date")[col].mean(), b.groupby("date")[col].mean()
    diff = (gm - bm).dropna().sort_index().values
    gmed, bmed = d.groupby("date")[col].median(), b.groupby("date")[col].median()
    mdiff = (gmed - bmed).dropna().mean()
    lo, hi = np.nanpercentile(base[col].dropna(), [1, 99])
    w = lambda x: x[col].clip(lo, hi).groupby(x.date).mean()  # noqa: E731
    wdiff = (w(d) - w(b)).dropna().mean()
    L = max(HDAYS[h] // 21, 1)
    rng = np.random.default_rng(seed)
    n = len(diff)
    nb = int(np.ceil(n / L))
    means = np.empty(B)
    for k in range(B):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(L)[None, :]) % n
        means[k] = diff[idx.ravel()[:n]].mean()
    return dict(n_dates=n, mean=diff.mean(), lo=np.percentile(means, 2.5), hi=np.percentile(means, 97.5),
                med=mdiff, wins=wdiff, pos=(diff > 0).mean())


def pc(x, d=1):
    return "-" if x is None or pd.isna(x) else f"{x * 100:.{d}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="", help="省略すると修正後(after)だけの集計になる")
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", default="results/compare_before_after.md")
    a = ap.parse_args()
    sa, ma = load(a.after)
    ga = make_groups(sa)
    has_b = bool(a.before)
    if has_b:
        sb, mb = load(a.before)
        gb = make_groups(sb)
    L = ["# データ修正によるv1再実行：" + ("修正前後の比較（dev期間のみ）" if has_b else "修正後の成績（95%信頼区間・年別安定性つき）"), "",
         f"- 再実行の理由: {ma.get('rerun_reason') or '(記録なし)'}",
         f"- ルールsha256(修正後): `{ma['rules_sha256'][:12]}`  凍結時と一致: {ma.get('rules_matches_frozen_sha')}",
         f"- 使用銘柄数(修正後): {ma.get('n_tickers_used', ma.get('n_tickers_in_data'))} / Gate通過行数 {ma['n_gate_rows']} / シグナル日数 {ma['n_signal_dates']} / データ末日 {ma['data_end']}",
         f"- 承認済みデータ修正の適用: {len(ma.get('approved_fixes_applied', []))}件", ""]
    if has_b:
        L += [f"- ルールsha256: 修正前 `{mb['rules_sha256'][:12]}` → "
              f"{'**一致（ルール変更なし）**' if mb['rules_sha256'] == ma['rules_sha256'] else '★不一致！ルールが変わっています'}",
              f"- 使用銘柄数: 修正前 {mb.get('n_tickers_in_data')} → 修正後 {ma.get('n_tickers_used', ma.get('n_tickers_in_data'))}", ""]

    L += ["## 1. 修正後の成績（95%信頼区間つき）", "",
          "読み方: 『対ベースライン超過』= 同じ日のGate通過全銘柄平均との差（日単位で平均、日付ごとにまとめて標準誤差を計算）。"
          "信頼区間が0をまたぐ場合、優位性は示されていません。3m/6mはリターン期間が重なるため、信頼区間は実際より狭い可能性があります。", ""]
    for h in HS:
        L += [f"### {h}後", "",
              "| グループ | n | 日数 | 勝率 | 平均 | 中央値 | 対SPY超過 | 対ベースライン超過 [95%CI] | プラスだった日 | 最悪の保有中下落 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for nm in [BASE] + SHOW:
            st = stats(ga[nm], ga[BASE], h)
            if st is None:
                L.append(f"| {nm} | 0 |  |  |  |  |  |  |  |  |")
                continue
            ci = "-" if nm == BASE else f"{pc(st['rel'])} [{pc(st['lo'])}, {pc(st['hi'])}]"
            L.append(f"| {nm} | {st['n']} | {st['n_dates']} | {pc(st['win'], 0)} | {pc(st['mean'])} | {pc(st['median'])} | "
                     f"{pc(st['excess_spy'])} | {ci} | {'-' if nm == BASE else pc(st['pos_dates'], 0)} | {pc(st['worst_mae'])} |")
        L.append("")

    L += ["## 1b. 頑健な評価（参考）: 1日10件以上のシグナルがある日だけ / ブロック・ブートストラップ95%区間 / 中央値差 / 上下1%を丸めた平均差", "",
          "| グループ | 期間 | 採用日数 | 平均差 [95%区間] | 中央値差 | 丸め平均差 | プラスの日 |", "|---|---|---|---|---|---|---|"]
    for nm in SHOW:
        for h in HS:
            rs = robust_stats(ga[nm], ga[BASE], h)
            if rs is None:
                L.append(f"| {nm} | {h} | 6日未満 |  |  |  |  |")
                continue
            L.append(f"| {nm} | {h} | {rs['n_dates']} | {pc(rs['mean'])} [{pc(rs['lo'])}, {pc(rs['hi'])}] | {pc(rs['med'])} | {pc(rs['wins'])} | {pc(rs['pos'], 0)} |")
    L.append("")

    if has_b:
        L += ["## 2. 修正前→修正後の変化（対ベースライン超過 / 平均リターン）", "",
              "| グループ | 期間 | n 前→後 | 対ベースライン超過 前→後 | 平均 前→後 |", "|---|---|---|---|---|"]
        for nm in SHOW:
            for h in HS:
                s1, s2 = stats(gb[nm], gb[BASE], h), stats(ga[nm], ga[BASE], h)
                if s1 is None or s2 is None:
                    continue
                L.append(f"| {nm} | {h} | {s1['n']}→{s2['n']} | {pc(s1['rel'])}→{pc(s2['rel'])} | {pc(s1['mean'])}→{pc(s2['mean'])} |")
        L.append("")

    L += ["## 3. 期間別の安定性（暦年ごとの対ベースライン超過 / 日数）— 修正後", ""]
    years = sorted(sa.date.dt.year.unique())
    L += ["| グループ | 期間 | " + " | ".join(str(y) for y in years) + " |", "|---|---|" + "---|" * len(years)]
    for nm in ["grade_A", "grade_A+B", "type_PULLBACK(含む)", "type_OVERSOLD(含む)"]:
        for h in ("1m", "3m"):
            rel = rel_series(ga[nm], ga[BASE], h)
            cells = []
            for y in years:
                ry = rel[rel.index.year == y]
                cells.append(f"{pc(ry.mean())} ({len(ry)}日)" if len(ry) else "-")
            L.append(f"| {nm} | {h} | " + " | ".join(cells) + " |")
    L.append("")

    da = sorted(sa.date.unique())
    if has_b:
        db = sorted(sb.date.unique())
        L += ["## 4. コホート最大ドローダウン（1か月保有・等金額・全期間）— 修正前→修正後", "",
              "| グループ | 修正前 | 修正後 |", "|---|---|---|"]
        for nm in [BASE] + SHOW:
            L.append(f"| {nm} | {pc(cohort_maxdd(gb[nm], db))} | {pc(cohort_maxdd(ga[nm], da))} |")
    else:
        L += ["## 4. コホート最大ドローダウン（1か月保有・等金額・全期間）", "", "| グループ | 最大DD |", "|---|---|"]
        for nm in [BASE] + SHOW:
            L.append(f"| {nm} | {pc(cohort_maxdd(ga[nm], da))} |")
    Path(a.out).parent.mkdir(exist_ok=True)
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n保存: {a.out}")


if __name__ == "__main__":
    main()
