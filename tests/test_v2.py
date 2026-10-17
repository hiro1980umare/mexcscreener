"""V2の部品テスト（ネットワーク不要）。 python tests/test_v2.py"""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import evaluate_criteria as EC  # noqa: E402
from src import data_v2 as D2  # noqa: E402
from src.data_v2 import PanelV2  # noqa: E402
from src.indicators import build  # noqa: E402
from src.rules import load_rules  # noqa: E402
from src.sec_pit import market_cap_at, quality_at, v2_grade  # noqa: E402
from src.v2 import collect_signals_v2, make_groups_v2  # noqa: E402
from test_synthetic import make_panel  # noqa: E402

R2 = yaml.safe_load((ROOT / "config/rules_v2.yaml").read_text(encoding="utf-8"))
R1 = load_rules(ROOT / "config/rules_v1.yaml")
K = yaml.safe_load((ROOT / "config/criteria_v2.yaml").read_text(encoding="utf-8"))
Q = R2["quality"]


def row(start, end, val, filed, form="10-K"):
    d = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        d["start"] = start
    return d


def make_sec(rev=(100, 120), op=30, ocf=40, capex=10, assets=1000, equity=600, shares=(1e8, 1e8), extra_filed=None):
    """FY2018(売上rev[0]) と FY2019(売上rev[1])。FY2019は2020-02-28提出。"""
    sec = {"flow": {}, "inst": {}}
    sec["flow"]["revenue"] = [("Revenues", [row("2018-01-01", "2018-12-31", rev[0], "2019-02-28"),
                                            row("2019-01-01", "2019-12-31", rev[1], "2020-02-28")])]
    sec["flow"]["op_income"] = [("OperatingIncomeLoss", [row("2019-01-01", "2019-12-31", op, "2020-02-28")])] if op is not None else []
    sec["flow"]["ocf"] = [("NetCashProvidedByUsedInOperatingActivities", [row("2019-01-01", "2019-12-31", ocf, "2020-02-28")])]
    sec["flow"]["capex"] = [("PaymentsToAcquirePropertyPlantAndEquipment", [row("2019-01-01", "2019-12-31", capex, "2020-02-28")])] if capex is not None else []
    sec["inst"]["assets"] = [("Assets", [row(None, "2019-12-31", assets, "2020-02-28")])]
    sec["inst"]["equity"] = [("StockholdersEquity", [row(None, "2019-12-31", equity, "2020-02-28")])]
    sec["inst"]["shares"] = [("EntityCommonStockSharesOutstanding", [row(None, "2019-02-15", shares[0], "2019-02-28"),
                                                                      row(None, "2020-02-15", shares[1], "2020-02-28")])]
    if extra_filed:  # 後から提出された修正・新しい期
        sec["flow"]["revenue"][0][1].append(extra_filed)
    return sec


def test_quality_scores_and_bins():
    q = quality_at(make_sec(), "2020-06-01", None, Q)
    assert q["q_status"] == "HIGH" and q["q_score"] == 100, q
    assert abs(q["q_rev_growth"] - 0.2) < 1e-12 and abs(q["q_op_margin"] - 0.25) < 1e-12
    low = quality_at(make_sec(rev=(100, 90), op=-5, ocf=1, capex=10, equity=-50, shares=(1e8, 1.4e8)), "2020-06-01", None, Q)
    assert low["q_status"] == "LOW" and low["q_score"] == 0, low
    mid = quality_at(make_sec(rev=(100, 107), op=8, ocf=14, capex=5, equity=300, shares=(1e8, 1.02e8)), "2020-06-01", None, Q)
    assert mid["q_status"] == "MID", mid


def test_missing_items_are_undetermined_not_estimated():
    for kw, word in ((dict(capex=None), "営業CF/設備投資"), (dict(op=None), "営業利益")):
        q = quality_at(make_sec(**kw), "2020-06-01", None, Q)
        assert q["q_status"] == "UNDET" and word in q["q_reason"] and np.isnan(q["q_score"]), q
    assert quality_at(make_sec(), "2019-03-15", None, Q)["q_status"] == "UNDET"          # 売上2期分が揃っていない時点
    assert quality_at(make_sec(), "2021-09-01", None, Q)["q_status"] == "UNDET"          # 最新の10-Kが456日より古い


def test_point_in_time_no_lookahead():
    """基準日より後(または当日)に提出されたデータは、結果に一切影響しない。"""
    base = make_sec()
    fut = make_sec()
    fut["flow"]["revenue"][0][1].append(row("2019-01-01", "2019-12-31", 999, "2020-06-01", "10-K/A"))   # 6/1に修正再提出
    fut["flow"]["revenue"][0][1].append(row("2020-01-01", "2020-12-31", 5, "2021-02-28"))             # 未来の10-K
    a = quality_at(base, "2020-06-01", None, Q)
    b = quality_at(fut, "2020-06-01", None, Q)             # 2020-06-01当日の提出は使わない
    assert a["q_rev_growth"] == b["q_rev_growth"] and a["q_score"] == b["q_score"]
    c = quality_at(fut, "2020-06-02", None, Q)             # 翌日からは修正値(999)が見える
    assert c["q_rev_growth"] > 5
    s0 = market_cap_at(base, "2020-03-01", 50.0, None, 548)
    base["inst"]["shares"][0][1].append(row(None, "2020-05-15", 7e8, "2020-05-20", "10-Q"))
    assert market_cap_at(base, "2020-03-01", 50.0, None, 548) == s0                    # 未来の株式数は使わない
    assert market_cap_at(base, "2020-06-01", 50.0, None, 548)[0] > s0[0]


def test_market_cap_uses_unadjusted_price_and_split_correctly():
    sec = make_sec(shares=(1e8, 1e8))
    # (1) 基準日の後(2021-01-01)に2分割: Yahooの調整済み終値は実際の半分(40)。当時の時価総額は 80 x 1e8 = 8e9
    sp = (np.array(["2021-01-01"], dtype="datetime64[D]"), np.array([2.0]))
    m, p = market_cap_at(sec, "2020-06-01", 40.0, sp, 548)
    assert abs(m - 8e9) < 1 and abs(p - 80.0) < 1e-9
    # (2) 株数の基準日(2020-02-15)の後、基準日の前(2020-04-01)に2分割: 分割後の株価20 x 分割後の株数2e8 = 4e9
    sp2 = (np.array(["2020-04-01"], dtype="datetime64[D]"), np.array([2.0]))
    m2, p2 = market_cap_at(sec, "2020-06-01", 20.0, sp2, 548)
    assert abs(m2 - 4e9) < 1 and abs(p2 - 20.0) < 1e-9
    # (3) 株式数が古すぎる/無い -> NaN(推定しない)
    assert np.isnan(market_cap_at(sec, "2022-06-01", 20.0, None, 548)[0])
    assert np.isnan(market_cap_at({"flow": {}, "inst": {}}, "2020-06-01", 20.0, None, 548)[0])


def test_dilution_is_split_adjusted():
    sec = make_sec(shares=(1e8, 2e8))                       # 株数が2倍に見える
    sp = (np.array(["2019-09-01"], dtype="datetime64[D]"), np.array([2.0]))   # でも間に2分割があった
    q = quality_at(sec, "2020-06-01", sp, Q)
    assert abs(q["q_dilution"]) < 1e-12 and q["q_status"] == "HIGH", q


def test_v2_grade_table():
    g = v2_grade
    assert g("HIGH", "A") == "A" and g("HIGH", "B") == "A"
    assert g("HIGH", "C") == "B" and g("MID", "A") == "B" and g("MID", "B") == "B"
    assert g("UNDET", "A") == "C" and g("UNDET", "B") == "C" and g("UNDET", "C") == "C"   # 判定不能は最高でもC
    assert g("UNDET", "D") == "D" and g("LOW", "A") == "D" and g("HIGH", "D") == "D" and g("MID", "C") == "C"
    # 判定不能(UNDET)は上限C。判定可能な中程度(MID)より上にはならない
    order = "ABCD"
    assert order.index(g("UNDET", "C")) >= order.index(g("MID", "C")) and order.index(g("UNDET", "A")) >= order.index(g("MID", "A"))


def test_c5_boundary_exactly_five_points_fails():
    import evaluate_criteria as EC
    import yaml
    K = yaml.safe_load(open("config/criteria_v2.yaml", encoding="utf-8"))
    base = dict(n_days=60, mean=0.02, ci_lo=0.005, ci_hi=0.03, median_diff=0.01, year_pos_share=0.8, n_years=7,
                dd_group=-0.35, dd_base=-0.30)
    assert EC.judge_dev({**base, "dd_diff_pt": -4.99}, K)["c5"] is True
    assert EC.judge_dev({**base, "dd_diff_pt": -5.00}, K)["c5"] is False     # 「5ポイント以上悪化しない」→ちょうど5.0は不合格
    assert EC.judge_dev({**base, "dd_diff_pt": -6.0}, K)["c5"] is False
    # 6つの基準が、指定どおりの値で登録されている
    C = K["criteria"]
    assert (C["c1_mean_diff_min"], C["c2_ci_lower_above"], C["c3_median_diff_min"], C["c4_positive_year_share_min"],
            C["c5_max_dd_worse_than_baseline_pt"], C["c6_final_same_sign"]) == (0.01, 0.0, 0.0, 0.6, 5.0, True)
    assert K["primary_test"]["group"] == "V2_A" and K["primary_test"]["horizon"] == "3m" and K["primary_test"]["min_days_required"] == 40


def test_timing_is_identical_to_v1_and_periods_are_sealed():
    import yaml
    R1 = yaml.safe_load(open("config/rules_v1.yaml", encoding="utf-8"))
    R2 = yaml.safe_load(open("config/rules_v2.yaml", encoding="utf-8"))
    import hashlib
    assert hashlib.sha256(open("config/rules_v1.yaml", "rb").read()).hexdigest() == R2["timing_source"]["sha256"]   # Timingはv1そのまま
    assert R2["gate"]["min_market_cap"] == R1["gate"]["min_market_cap"] and R2["gate"]["min_price_unadjusted"] == R1["gate"]["min_price"]
    P, D = R2["periods"], R2["data"]
    assert P["dev"]["signal_start"] >= "2017-01-01"                                    # devは2017年以降だけ
    assert P["final"]["outcome_end"] < D["dev"]["price_history_start"]                 # 最終検証の結果期間はdevデータに存在しない
    assert P["final"]["signal_end"] < P["final"]["outcome_end"] < P["dev"]["signal_start"]
    assert D["dev"]["dir"] != D["final"]["dir"]
    # 最終検証のシグナル終了日 + 保有6か月(126営業日, 祝日余裕込みで+135営業日)が outcome_end を超えない
    end = pd.bdate_range(P["final"]["signal_end"], periods=136)[-1]
    assert end <= pd.Timestamp(P["final"]["outcome_end"]), (end, P["final"]["outcome_end"])


def fake_yf():
    idx = pd.bdate_range("2020-01-01", periods=300)
    mod = types.ModuleType("yfinance")
    mod.shared = types.SimpleNamespace(_ERRORS={"DEAD": "possibly delisted"})
    mod.calls = []

    def download(part, **kw):
        mod.calls.append(tuple(part))
        assert kw["auto_adjust"] is False and kw["actions"] is True
        cols = {}
        for t in part:
            bad = t == "DEAD"
            close = pd.Series(np.nan if bad else 20.0, index=idx)
            adj = close * 0.5
            split = pd.Series(0.0, index=idx)
            if t == "SPLT":
                split.iloc[100] = 2.0
            for f, s in (("Open", close * 1.0), ("Close", close), ("Adj Close", adj), ("Volume", close * 0 + 1000), ("Stock Splits", split)):
                cols[(f, t)] = s
        df = pd.DataFrame(cols)
        df.columns = pd.MultiIndex.from_tuples(df.columns)
        return df
    mod.download = download
    return mod


def test_download_v2_resume_and_assemble():
    sys.modules["yfinance"] = fake_yf()
    d = Path(tempfile.mkdtemp())
    tick = ["A", "B", "SPLT", "DEAD", "SPY"]
    D2.download_v2(tick, "2020-01-01", d / "chunks", chunk=3, sleeper=lambda s: None, log=lambda s: None)
    n_calls = len(sys.modules["yfinance"].calls)
    D2.download_v2(tick, "2020-01-01", d / "chunks", chunk=3, sleeper=lambda s: None, log=lambda s: None)
    assert len(sys.modules["yfinance"].calls) == n_calls                        # 保存済みは再取得しない(再開可能)
    mats, cov = D2.assemble(d / "chunks", min_rows=252)
    st = cov.set_index("ticker").status.to_dict()
    assert st["A"] == "ok" and st["DEAD"] == "failed"
    assert "DEAD" not in mats["close"].columns                                    # 失敗銘柄を推定で埋めない
    assert abs(mats["open"]["A"].iloc[5] - 20.0 * 0.5 / 20.0 * 20.0) < 1e-9       # 調整済み始値 = Open x (Adj/Close)
    assert len(mats["splits"]["SPLT"][0]) == 1 and mats["splits"]["SPLT"][1][0] == 2.0
    D2.save_v2(mats, cov, d / "out")
    P = D2.load_v2(d / "out")
    assert "SPY" not in P.close.columns and len(P.spy_close) == len(P.close)


def make_sig(edge, n_days=60, per_day=40, seed=0, years=(2018, 2019, 2020)):
    rng = np.random.default_rng(seed)
    dates = pd.to_datetime([f"{years[k % len(years)]}-{(k // len(years)) % 12 + 1:02d}-{(k // 36) % 20 + 1:02d}" for k in range(n_days)])
    dates = pd.DatetimeIndex(sorted(set(dates)))
    rows = []
    for d in dates:
        for k in range(per_day):
            a = k < per_day // 2
            r1 = rng.normal(0.005, 0.05)
            r3 = rng.normal(0.02, 0.1) + (edge if a else 0)
            rows.append(dict(date=d, ticker=f"T{k}", grade="A" if a else "C", q_status="HIGH" if a else "MID",
                             v2_grade="A" if a else "C", simple_rsi=(k % 5 == 0), ret_1m=r1, ret_3m=r3, ret_6m=r3,
                             mae_1m=-0.05, mae_3m=-0.1, mae_6m=-0.1, excess_1m=r1, excess_3m=r3, excess_6m=r3,
                             q_reason=""))
    return pd.DataFrame(rows)


def test_criteria_evaluator_detects_edge_and_rejects_noise():
    K2 = copy.deepcopy(K)
    K2["bootstrap"]["n_resamples"] = 500
    good = EC.measure(make_sig(0.04, n_days=120), "ALL_GATE", K2)
    j = EC.judge_dev(good, K2)
    assert good["mean"] > 0.01 and j["c1"] and j["c2"] and j["c3"], (good, j)
    noise = EC.measure(make_sig(0.0, n_days=120, seed=3), "ALL_GATE", K2)
    assert not EC.judge_dev(noise, K2)["dev_pass"], noise
    few = EC.measure(make_sig(0.05, n_days=30), "ALL_GATE", K2)
    assert not EC.judge_dev(few, K2)["c2"]                                       # 採用日数40日未満は不合格
    assert EC.judge_final({"n_days": 5, "mean": 0.1}, K2)["c6"] is None          # 最終検証の日数不足は判定不能
    assert EC.judge_final({"n_days": 30, "mean": -0.01}, K2)["c6"] is False
    assert EC.measure(make_sig(0.04, n_days=120), "ALL_GATE", K2) == good        # 乱数固定で再現する


def test_freeze_blocks_changed_criteria():
    d = Path(tempfile.mkdtemp())
    for f in ("config/criteria_v2.yaml", "config/rules_v1.yaml", "config/rules_v2.yaml"):
        (d / f).parent.mkdir(exist_ok=True, parents=True)
        shutil.copy(ROOT / f, d / f)
    shutil.copy(ROOT / "freeze_v2.py", d / "freeze_v2.py")
    run = lambda *a: subprocess.run([sys.executable, "freeze_v2.py", *a], cwd=d, capture_output=True, text=True)  # noqa: E731
    assert run().returncode == 0
    m = json.loads((d / "config/v2_manifest.json").read_text())
    assert m["criteria"]["sha256"] and m["final_candidate"] is None
    r = run("--final-candidate", "config/rules_v2.yaml")                        # devの実行結果が無いルールは最終候補にできない
    assert r.returncode != 0 and "dev実行結果がありません" in (r.stdout + r.stderr)
    assert json.loads((d / "config/v2_manifest.json").read_text())["final_candidate"] is None
    (d / "config/criteria_v2.yaml").write_text((d / "config/criteria_v2.yaml").read_text() + "\n# 改ざん\n")
    r = run()
    assert r.returncode != 0 and "変更されています" in (r.stdout + r.stderr)    # 基準を変えると凍結できない


def _with_rsi_edge(sig, rsi_edge):
    sig = sig.copy()
    m = sig.simple_rsi
    for h in ("1m", "3m", "6m"):
        sig.loc[m, f"ret_{h}"] += rsi_edge
        sig.loc[m, f"excess_{h}"] += rsi_edge
    return sig


def test_two_stage_decision_branches():
    D = EC.decide
    Kx = K
    assert D(Kx, False, None, None, None, False)[0] == "tier1_not_met"                       # Tier1(dev)不合格 → Tier2は見ない
    assert D(Kx, True, None, None, None, False)[0] == "tier1_dev_met_final_pending"          # devだけ合格 → 最終検証待ち
    assert D(Kx, True, None, True, None, True)[0] == "undetermined"                          # 最終検証の日数不足
    assert D(Kx, True, False, None, None, True)[0] == "tier1_not_met"                        # 最終検証で符号が違う
    assert D(Kx, True, True, True, True, True)[0] == "tier1_met_tier2_met"
    assert D(Kx, True, True, False, True, True)[0] == "tier1_met_tier2_not_met"              # RSI単独を上回れない
    assert D(Kx, True, True, True, False, True)[0] == "tier1_met_tier2_not_met"
    assert D(Kx, True, True, True, None, True)[0] == "undetermined"
    assert "V2自体には優位性があるが、RSI単独を明確には上回らない" in D(Kx, True, True, False, True, True)[1]


def test_report_two_stage_end_to_end():
    K2 = copy.deepcopy(K)
    K2["bootstrap"]["n_resamples"] = 500
    # (1) V2_AはALL_GATEに勝つが、RSI単独のほうが強い → Tier1成功・Tier2不成功
    dev = _with_rsi_edge(make_sig(0.04, n_days=150, seed=1), 0.10)
    fin = _with_rsi_edge(make_sig(0.04, n_days=60, seed=2, years=(2012, 2013, 2014)), 0.10)
    t1 = EC.measure(dev, "ALL_GATE", K2)
    assert EC.judge_dev(t1, K2)["dev_pass"], t1
    rep = EC.report(K2, dev, fin)
    assert "V2自体には優位性があるが、RSI単独を明確には上回らない" in rep and "RSI単独のほうが平均で" in rep, rep
    assert "tier1_met_tier2_not_met" in rep
    # (2) V2_AがALL_GATEにもRSI単独にも勝つ → 両方成功
    dev2, fin2 = make_sig(0.04, n_days=150, seed=1), make_sig(0.04, n_days=60, seed=2, years=(2012, 2013, 2014))
    rep2 = EC.report(K2, dev2, fin2)
    assert "tier1_met_tier2_met" in rep2, rep2
    # (3) 優位性なし → Tier1不成功。Tier2は評価しない(参考値のみ)
    rep3 = EC.report(K2, make_sig(0.0, n_days=150, seed=3), make_sig(0.0, n_days=60, seed=4, years=(2012, 2013, 2014)))
    assert "tier1_not_met" in rep3 and "評価しません" in rep3 and "Tier2は評価しない" in rep3, rep3
    # (4) devのみ: Tier1(dev)合格なら最終検証待ち
    assert "tier1_dev_met_final_pending" in EC.report(K2, dev2)


def test_collect_signals_v2_smoke_and_sealing_columns():
    p = make_panel(n_days=700, n_t=40)
    pv = PanelV2(p.open, p.close, p.close.copy(), p.volume, p.spy_open, p.spy_close, {}, pd.Series(np.nan, index=p.close.columns))
    sec = {t: make_sec() for t in p.close.columns[:30]}                          # 後ろ10銘柄はSEC財務なし = Gateを通らない
    ind = build(pv, R1)
    sig, spy = collect_signals_v2(pv, ind, R1, R2, sec, "2020-03-02", "2020-12-31", log=lambda s: None)
    assert len(sig) > 0 and set(sig.v2_grade) <= set("ABCD")
    assert not set(sig.ticker) & set(p.close.columns[30:])                       # 株式数が無い銘柄は入らない
    assert set(sig.q_status) <= {"HIGH", "MID", "LOW", "UNDET"}
    assert not ((sig.q_status == "UNDET") & (sig.v2_grade == "A")).any()         # 判定不能がAになることはない
    g = make_groups_v2(sig)
    assert len(g["V2_A"]) + len(g["V2_B"]) + len(g["V2_C"]) + len(g["V2_D"]) == len(sig)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK ", fn.__name__)
    print(f"{len(fns)}件すべて成功")
