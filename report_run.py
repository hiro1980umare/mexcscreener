"""1つのバックテスト結果を、95%信頼区間・暦年別・コホートDDつきで表示する（比較相手が無いとき用）。
    python report_run.py results/v1_dev_datafix1
"""
import sys

sys.path.insert(0, ".")
import pandas as pd  # noqa: E402

from compare_runs import BASE, HS, SHOW, load, pc, rel_series, stats  # noqa: E402
from src.backtest import cohort_maxdd, make_groups  # noqa: E402

d = sys.argv[1]
sig, meta = load(d)
g = make_groups(sig)
L = [f"# {d} 詳細レポート", f"- {meta.get('rerun_reason', '')}",
     f"- 使用銘柄数: {meta.get('n_tickers_used', meta.get('n_tickers_in_data'))} / Gate通過行数: {meta['n_gate_rows']} / シグナル日数: {meta['n_signal_dates']}",
     f"- ルールsha256: {meta['rules_sha256'][:12]}", ""]
for h in HS:
    L += [f"## {h}後", "", "| グループ | n | 日数 | 勝率 | 平均 | 中央値 | 対SPY超過 | 対ベースライン超過 [95%CI] | プラスの日 | 最悪の保有中下落 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for nm in [BASE] + SHOW:
        st = stats(g[nm], g[BASE], h)
        if st is None:
            L.append(f"| {nm} | 0 |  |  |  |  |  |  |  |  |")
            continue
        ci = "-" if nm == BASE else f"{pc(st['rel'])} [{pc(st['lo'])}, {pc(st['hi'])}]"
        L.append(f"| {nm} | {st['n']} | {st['n_dates']} | {pc(st['win'], 0)} | {pc(st['mean'])} | {pc(st['median'])} | {pc(st['excess_spy'])} | "
                 f"{ci} | {'-' if nm == BASE else pc(st['pos_dates'], 0)} | {pc(st['worst_mae'])} |")
    L.append("")
years = sorted(sig.date.dt.year.unique())
L += ["## 暦年別の対ベースライン超過（日数）", "", "| グループ | 期間 | " + " | ".join(map(str, years)) + " |", "|---|---|" + "---|" * len(years)]
for nm in ["grade_A", "grade_A+B", "type_PULLBACK(含む)", "type_OVERSOLD(含む)"]:
    for h in ("1m", "3m"):
        rel = rel_series(g[nm], g[BASE], h)
        L.append(f"| {nm} | {h} | " + " | ".join(
            (f"{pc(rel[rel.index.year == y].mean())} ({(rel.index.year == y).sum()}日)" if (rel.index.year == y).any() else "-") for y in years) + " |")
dates = sorted(sig.date.unique())
L += ["", "## コホート最大DD(1か月保有・等金額)", "", "| グループ | 最大DD |", "|---|---|"]
L += [f"| {nm} | {pc(cohort_maxdd(g[nm], dates))} |" for nm in [BASE] + SHOW]
print("\n".join(L))
