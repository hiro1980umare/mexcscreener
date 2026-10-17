#!/usr/bin/env python3
"""1つのバックテスト結果を、95%信頼区間・暦年別の安定性・コホート最大DDつきで表示する（比較元が無いとき用）。
    python report_ci.py --run results/v1_dev_datafix1
"""
import argparse
import sys

import pandas as pd

sys.path.insert(0, ".")
from compare_runs import BASE, HS, SHOW, load, pc, rel_series, stats  # noqa: E402
from src.backtest import cohort_maxdd, make_groups  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run", required=True)
a = ap.parse_args()
sig, meta = load(a.run)
g = make_groups(sig)
L = [f"# {a.run}（95%信頼区間つき）", "",
     f"- 再実行の理由: {meta.get('rerun_reason') or '-'}",
     f"- ルールsha256: `{meta['rules_sha256'][:12]}`（凍結: d40847b9f968）",
     f"- 使用銘柄数: {meta.get('n_tickers_used', meta.get('n_tickers_in_data'))} / Gate通過行数 {meta['n_gate_rows']} / シグナル日数 {meta['n_signal_dates']}",
     f"- 承認済みデータ修正の適用: {len(meta.get('approved_fixes_applied', []))}件", "",
     "『対ベースライン超過』= 同じ日のGate通過全銘柄平均との差(日単位で平均)。信頼区間が0をまたぐなら優位性は示されていない。"
     "3m/6mは期間が重なるため信頼区間は実際より狭い可能性。", ""]
for h in HS:
    L += [f"## {h}後", "", "| グループ | n | 日数 | 勝率 | 平均 | 中央値 | 対SPY超過 | 対ベースライン超過 [95%CI] | プラスの日 | 最悪の保有中下落 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for nm in [BASE] + SHOW:
        s = stats(g[nm], g[BASE], h)
        if s is None:
            L.append(f"| {nm} | 0 |  |  |  |  |  |  |  |  |")
            continue
        ci = "-" if nm == BASE else f"{pc(s['rel'])} [{pc(s['lo'])}, {pc(s['hi'])}]"
        L.append(f"| {nm} | {s['n']} | {s['n_dates']} | {pc(s['win'], 0)} | {pc(s['mean'])} | {pc(s['median'])} | "
                 f"{pc(s['excess_spy'])} | {ci} | {'-' if nm == BASE else pc(s['pos_dates'], 0)} | {pc(s['worst_mae'])} |")
    L.append("")
years = sorted(sig.date.dt.year.unique())
L += ["## 暦年別の安定性（対ベースライン超過 / 日数）", "", "| グループ | 期間 | " + " | ".join(map(str, years)) + " |",
      "|---|---|" + "---|" * len(years)]
for nm in ["grade_A", "grade_A+B", "type_PULLBACK(含む)", "type_OVERSOLD(含む)", "type_MARKET_DIP(含む)"]:
    for h in ("1m", "3m"):
        rel = rel_series(g[nm], g[BASE], h)
        L.append(f"| {nm} | {h} | " + " | ".join(
            (f"{pc(rel[rel.index.year == y].mean())} ({int((rel.index.year == y).sum())}日)" if (rel.index.year == y).any() else "-")
            for y in years) + " |")
dates = sorted(sig.date.unique())
L += ["", "## コホート最大ドローダウン（1か月保有・等金額・全期間）", "", "| グループ | 最大DD |", "|---|---|"]
L += [f"| {nm} | {pc(cohort_maxdd(g[nm], dates))} |" for nm in [BASE] + SHOW]
print("\n".join(L))
