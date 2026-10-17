"""合成データで『バグ』と『先読み』を検査する(ネットワーク不要)。 python tests/test_synthetic.py"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.backtest import collect_signals, forward, make_groups, summarize  # noqa: E402
from src.data import Panel  # noqa: E402
from src.indicators import build, rsi_wilder  # noqa: E402
from src.rules import load_rules, max_score  # noqa: E402
from src.signals import downgrade, evaluate_at, grade_from  # noqa: E402

R = load_rules(Path(__file__).resolve().parents[1] / "config" / "rules_v1.yaml")


def make_panel(n_days=700, n_t=60, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-01", periods=n_days)
    ret = rng.normal(0.0004, 0.02, (n_days, n_t))
    close = pd.DataFrame(50 * np.exp(np.cumsum(ret, 0)), index=idx, columns=[f"T{i}" for i in range(n_t)])
    opn = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    vol = pd.DataFrame(rng.uniform(1e6, 3e6, close.shape), index=idx, columns=close.columns)
    spy = pd.Series(300 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n_days))), index=idx)
    shares = pd.Series(5e7, index=close.columns)  # 時価総額は常に$1B超
    return Panel(opn, close, vol, spy.shift(1).fillna(spy), spy, shares)


def test_score_total_is_100():
    assert max_score(R) == 100


def test_rsi_extremes():
    up = pd.DataFrame({"a": np.arange(1, 100, dtype=float)})
    down = pd.DataFrame({"a": np.arange(100, 1, -1, dtype=float)})
    assert rsi_wilder(up, 14).iloc[-1, 0] == 100
    assert rsi_wilder(down, 14).iloc[-1, 0] < 1


def test_no_lookahead():
    """日付iの判定は、i以降のデータを書き換えても変わらない(=未来を見ていない)。"""
    p = make_panel()
    i = 500
    full = evaluate_at(build(p, R), i, R)
    assert len(full) > 0, "テスト用データでGate通過が0件"
    q = p.upto(i + 1)
    trunc = evaluate_at(build(q, R), i, R)
    pd.testing.assert_frame_equal(full, trunc)
    rng = np.random.default_rng(1)
    for df in (p.close, p.open, p.volume):
        df.iloc[i + 1:] = df.iloc[i + 1:].values * rng.uniform(0.2, 5, df.iloc[i + 1:].shape)
    p.spy_close.iloc[i + 1:] *= 3
    pd.testing.assert_frame_equal(full, evaluate_at(build(p, R), i, R))


def test_forward_return_and_mae():
    O = np.full((10, 1), 100.0)
    C = np.array([100, 100, 90, 95, 110, 110, 110, 110, 110, 110.0]).reshape(-1, 1)
    O[3, 0] = 100.0  # i=2 -> 翌日(3)始値100で買い
    ret, mae = forward(O, C, 2, 3, 0.001)  # 3営業日後 = 6日目の終値110
    assert abs(ret[0] - (110 / 100 - 1 - 0.002)) < 1e-12
    assert abs(mae[0] - (95 / 100 - 1)) < 1e-12  # 保有中の最安終値は95(3日目)


def test_grade_logic():
    g = lambda s, rf, nt: grade_from([s], [rf], [nt], R)[0]  # noqa: E731
    assert g(85, 0, 1) == "A" and g(70, 0, 1) == "B"
    assert g(90, 0, 0) == "C"          # シグナルタイプ無しはA/Bにならない
    assert g(95, 1, 2) == "C"          # Red Flag 1つは最高C
    assert g(95, 2, 2) == "D"          # 2つ以上は必ずD
    assert g(30, 0, 0) == "D"
    assert [downgrade(x) for x in "ABCD"] == ["B", "C", "D", "D"]


def test_pipeline_smoke():
    p = make_panel()
    ind = build(p, R)
    sig, spy_df = collect_signals(p, ind, R, "2020-03-02", "2020-12-31")
    assert len(sig) > 0 and len(spy_df) > 0
    s = summarize(make_groups(sig), spy_df)
    assert {"SPY", "grade_A", "SIMPLE_RSI(RSI<30だけ)"} <= set(s.group)
    # 全銘柄ベースラインの件数 = Gate通過数
    assert int(s[(s.group.str.startswith("ALL_GATE")) & (s.horizon == "1m")].n.iloc[0]) == sig["ret_1m"].notna().sum()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK ", fn.__name__)
    print(f"{len(fns)}件すべて成功")
