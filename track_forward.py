#!/usr/bin/env python3
"""通知(記録)した銘柄の『その後の実際の成績』を、バックテストと同じ方法で追跡する。
  判定日の翌営業日の始値で買い、1・3・6か月(21・63・126営業日)後の終値で売る。片道0.10%を往復で控除。
  比較対象: 同じ日のGate通過の全銘柄(ALL_GATE)・SPY。期間が経過していないものは NaN(未経過)。
    python track_forward.py --records bot_records --data-dir data_v2       # 保存済みの株価で
    python track_forward.py --records bot_records                          # 株価を取得して(ネットワーク)
※件数が少ないうちの数字は、ほとんど意味を持たない(研究の事前登録基準は適用しない・参考値)。
"""
import argparse
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.backtest import forward
from src.bot_data import get_panel_since
from src.data_v2 import load_v2


def load_records(records_dir, include_dryrun=False):
    fs = sorted(Path(records_dir).glob("*/signals.csv.gz"))
    fs = [f for f in fs if include_dryrun or not f.parent.name.startswith(("dryrun_", "rehearsal_"))]   # 試運転・リハーサルは成績の追跡に含めない
    cols = ["ticker", "asof", "section", "notified", "v2_grade", "t_grade", "q_status", "close_raw", "score"]
    return pd.concat([pd.read_csv(f, usecols=lambda c: c in cols) for f in fs], ignore_index=True) if fs else pd.DataFrame(columns=cols)


def compute_forward(rec, panel, R2):
    """記録の各行について、1・3・6か月後の成績を計算する。判定日がデータに無い/期間が未経過なら NaN。"""
    H, cost = R2["backtest"]["horizons_trading_days"], R2["backtest"]["cost_per_side_bps"] / 1e4
    dates, cols = panel.close.index, {t: j for j, t in enumerate(panel.close.columns)}
    O, C = panel.open.values, panel.close.values
    SO, SC = panel.spy_open.reindex(dates).values, panel.spy_close.reindex(dates).values
    out = rec.copy()
    for nm in H:
        for k in ("ret", "mae", "spy", "excess"):
            out[f"{k}_{nm}"] = np.nan
    for asof, g in rec.groupby("asof"):
        i = dates.get_indexer([pd.Timestamp(asof)])[0]
        if i < 0:
            continue
        j = np.array([cols.get(t, -1) for t in g.ticker])
        for nm, h in H.items():
            if i + 1 + h >= len(dates):
                continue
            ret, mae = forward(O, C, i, h, cost)
            spy = SC[i + 1 + h] / SO[i + 1] - 1 - 2 * cost
            ok = j >= 0
            r = np.where(ok, ret[np.where(ok, j, 0)], np.nan)
            out.loc[g.index, f"ret_{nm}"] = r
            out.loc[g.index, f"mae_{nm}"] = np.where(ok, mae[np.where(ok, j, 0)], np.nan)
            out.loc[g.index, f"spy_{nm}"] = spy
            out.loc[g.index, f"excess_{nm}"] = r - spy
    return out


def review(fwd, min_days=1):
    """区分ごとの成績。Aの表示銘柄と、同じ日のGate通過の全銘柄を、日ごとの平均差で比べる。"""
    L = ["# 通知銘柄のその後の成績(参考値)", "", "※件数が少ないうちは、数字にほとんど意味はありません。研究の事前登録基準は適用していません。", ""]
    for nm in ("1m", "3m", "6m"):
        col = f"ret_{nm}"
        d = fwd[fwd[col].notna()]
        if d.empty:
            L.append(f"- {nm}後: まだ期間が経過した記録がありません")
            continue
        a = d[d.section == "A_QualityHigh_TimingHigh"]
        base = d.groupby("asof")[col].mean()
        ga = a.groupby("asof")[col].mean()
        diff = (ga - base.reindex(ga.index)).dropna()
        L.append(f"- {nm}後: 判定日{d["asof"].nunique()}日 / Gate通過の全銘柄 平均 {d[col].mean():+.2%}(n={len(d)}) / "
                 f"A区分 平均 {a[col].mean():+.2%}(n={len(a)}、中央値 {a[col].median():+.2%}) / 日ごとの差(A−全銘柄) 平均 "
                 + (f"{diff.mean():+.2%}({len(diff)}日)" if len(diff) else "算出できず"))
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default="bot_records")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--rules", default="config/rules_v2b.yaml")
    ap.add_argument("--include-dryrun", action="store_true")
    a = ap.parse_args()
    R2 = yaml.safe_load(Path(a.rules).read_text(encoding="utf-8"))
    rec = load_records(a.records, a.include_dryrun)
    if rec.empty:
        raise SystemExit("記録がありません。")
    panel = load_v2(a.data_dir) if a.data_dir else get_panel_since(sorted(rec.ticker.unique()), (pd.Timestamp(rec["asof"].min()) - pd.Timedelta(days=10)).date().isoformat(), tempfile.mkdtemp())[0]
    fwd = compute_forward(rec, panel, R2)
    out = Path(a.records) / "forward_returns.csv"
    fwd.to_csv(out, index=False)
    txt = review(fwd)
    (Path(a.records) / "review_summary.md").write_text(txt, encoding="utf-8")
    print(txt)
    print(f"\n保存: {out}")
