"""V2bの部品テスト（ネットワーク不要）。 python tests/test_v2b.py"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import evaluate_criteria as EC  # noqa: E402
import evaluate_v2b as EB  # noqa: E402
from src.data_v2 import PanelV2  # noqa: E402
from src.indicators import build  # noqa: E402
from src.quality_v2b import quality_raw_v2b, rank_quality_v2b  # noqa: E402
from src.v2 import collect_signals_v2  # noqa: E402
from test_synthetic import make_panel  # noqa: E402
from test_v2 import R1, make_sec, make_sig, row  # noqa: E402

LOAD = lambda p: yaml.safe_load((ROOT / p).read_text(encoding="utf-8"))  # noqa: E731
R2, R2B, K2 = LOAD("config/rules_v2.yaml"), LOAD("config/rules_v2b.yaml"), LOAD("config/criteria_v2.yaml")
Q = R2B["quality"]
USER_SENTENCE = ("V2の事前登録基準は変更しない。V2bはV2結果を見た後に設計された2回目のdev試行なので、"
                 "偶然の合格を防ぐため、V2bのc2だけ97.5%区間という、より厳しい基準を追加する。")


def make_sec_b(i=0, op=30, ni=24, ocf=30, assets=1000, shares=(1e8, 1e8)):
    sec = make_sec(op=op, ocf=ocf, assets=assets, shares=shares)
    sec["flow"]["net_income"] = [("NetIncomeLoss", [row("2019-01-01", "2019-12-31", ni, "2020-02-28")])] if ni is not None else []
    return sec


def test_v2b_differs_from_v2_only_where_intended():
    diff = sorted(k for k in set(R2) | set(R2B) if R2.get(k) != R2B.get(k))
    assert diff == ["data", "derived_from", "quality", "version"], diff
    assert {k for k in R2["data"] if R2["data"][k] != R2B["data"][k]} == {"sec_dir"}
    assert R2B["derived_from"]["sha256"] == hashlib.sha256((ROOT / "config/rules_v2.yaml").read_bytes()).hexdigest()
    # Timing(v1)・Gate・判定表・期間は完全に同じ
    for k in ("timing_source", "gate", "periods", "grades_v2", "backtest", "timing_grades", "score", "red_flags", "signal_types"):
        assert R2.get(k) == R2B.get(k), k


def test_v2b_criteria_keep_six_criteria_and_only_tighten_c2():
    Kb = EB.load_K_v2b()
    assert Kb["bootstrap"]["ci"] == 0.975 and K2["bootstrap"]["ci"] == 0.95
    assert Kb["criteria"] == K2["criteria"]                                          # 6基準の数値は完全に同じ
    for k in ("primary_test", "baselines", "verdict"):
        assert Kb[k] == K2[k], k
    assert {k: v for k, v in Kb["bootstrap"].items() if k != "ci"} == {k: v for k, v in K2["bootstrap"].items() if k != "ci"}
    raw = (ROOT / "config/criteria_v2b.yaml").read_text(encoding="utf-8")
    rec = yaml.safe_load(raw)["record"]
    assert USER_SENTENCE in rec and "95%→97.5%への変更は基準を緩めるものではなく、V2bに対する追加の厳格化である。" in rec   # ご指定の記録文を一字一句そのまま
    assert "追加の厳格化" in raw


def test_c2_at_975_is_strictly_harder_than_95():
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.03, 87)
    S2, Sb = K2["bootstrap"], EB.load_K_v2b()["bootstrap"]
    found = None
    for m in np.linspace(0.002, 0.03, 120):
        x = noise + m
        lo95 = EC.block_ci(x, S2["block_length_dates"], S2["n_resamples"], S2["seed"], S2["ci"])[0]
        lo975 = EC.block_ci(x, Sb["block_length_dates"], Sb["n_resamples"], Sb["seed"], Sb["ci"])[0]
        assert lo975 <= lo95 + 1e-12                                                  # 97.5%の下限は、95%の下限以下(厳しい)
        if lo95 > 0 >= lo975 and found is None:
            found = (lo95, lo975)
    assert found, "95%では合格・97.5%では不合格になる例が見つからない"
    base = dict(n_days=87, mean=0.02, ci_hi=0.05, median_diff=0.01, year_pos_share=0.8, n_years=7, dd_diff_pt=0.0)
    assert EC.judge_dev({**base, "ci_lo": found[0]}, K2)["c2"] is True
    assert EC.judge_dev({**base, "ci_lo": found[1]}, EB.load_K_v2b())["c2"] is False


def test_quality_raw_formulas_and_directions():
    s = make_sec_b(op=30, ni=20, ocf=26, assets=1000, shares=(1e8, 1.05e8))
    r = quality_raw_v2b(s, "2020-03-02", None, Q)
    assert abs(r["qb_prof"] - 0.03) < 1e-12 and abs(r["qb_accr"] - (-0.006)) < 1e-12 and abs(r["qb_dil"] - 0.05) < 1e-12 and r["q_reason"] == ""
    # 株式分割は調整される(2分割なら株式数が2倍になっても希薄化ではない)
    r2 = quality_raw_v2b(make_sec_b(shares=(1e8, 2e8)), "2020-03-02", (np.array(["2019-09-01"], dtype="datetime64[D]"), np.array([2.0])), Q)
    assert abs(r2["qb_dil"]) < 1e-12


def test_quality_raw_missing_is_nan_not_estimated():
    r = quality_raw_v2b(make_sec_b(ni=None), "2020-03-02", None, Q)
    assert np.isnan(r["qb_prof"]) and "純利益" in r["q_reason"]                       # 取れない項目があれば、他の項目も使わない
    assert "営業利益" in quality_raw_v2b(make_sec_b(op=None), "2020-03-02", None, Q)["q_reason"]
    s = make_sec_b(); s["inst"]["assets"] = [("Assets", [row(None, "2019-09-30", 1000, "2019-11-01", "10-Q")])]   # 期末(FY末)の総資産が無い
    assert "総資産" in quality_raw_v2b(s, "2020-03-02", None, Q)["q_reason"]
    assert "古い" in quality_raw_v2b(make_sec_b(), "2021-12-01", None, Q)["q_reason"]                              # 最新の10-Kが15か月より古い
    s = make_sec_b(); s["inst"]["shares"] = [("EntityCommonStockSharesOutstanding", [row(None, "2020-02-15", 1e8, "2020-02-28")])]
    assert "発行済株式数" in quality_raw_v2b(s, "2020-03-02", None, Q)["q_reason"]                                # 株式数が1期分しかない


def test_quality_raw_point_in_time_no_lookahead():
    s = make_sec_b(op=30, ni=24, ocf=30)
    base = quality_raw_v2b(s, "2020-03-02", None, Q)
    # 基準日より後に提出された修正値(純利益を大きく変更)は、基準日より前には見えない
    s["flow"]["net_income"][0][1].append(row("2019-01-01", "2019-12-31", -500, "2020-03-10", "10-K/A"))
    assert quality_raw_v2b(s, "2020-03-02", None, Q) == base
    assert quality_raw_v2b(s, "2020-03-10", None, Q)["qb_accr"] == base["qb_accr"]      # 提出日と同じ日はまだ使わない(より前のみ)
    assert quality_raw_v2b(s, "2020-03-11", None, Q)["qb_accr"] != base["qb_accr"]      # 翌日から見える


def _raw(n=300, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"qb_prof": rng.normal(0.05, 0.05, n), "qb_accr": rng.normal(0, 0.03, n),
                         "qb_dil": rng.normal(0.02, 0.05, n), "q_reason": ""}, index=[f"T{i}" for i in range(n)])


def test_rank_terciles_directions_and_min_cross_section():
    raw = _raw()
    raw.loc["BEST"] = [0.5, -0.3, -0.5, ""]      # 収益性が最高・アクルーアル最低・希薄化最低(株を減らしている)
    raw.loc["WORST"] = [-0.5, 0.3, 0.5, ""]
    out = rank_quality_v2b(raw, Q)
    assert out.loc["BEST", "q_status"] == "HIGH" and out.loc["WORST", "q_status"] == "LOW"
    vc = out.q_status.value_counts()
    assert abs(vc["HIGH"] - len(raw) / 3) <= 2 and abs(vc["LOW"] - len(raw) / 3) <= 2 and vc["HIGH"] + vc["MID"] + vc["LOW"] == len(raw)
    # 3要素のどれかが無い銘柄は判定不能。推定しない
    raw.loc["T1", "qb_accr"] = np.nan; raw.loc["T1", "q_reason"] = "欠損: 純利益"
    out2 = rank_quality_v2b(raw, Q)
    assert out2.loc["T1", "q_status"] == "UNDET" and np.isnan(out2.loc["T1", "q_score"])
    # その日の判定可能銘柄が100未満なら、その日は全員判定不能
    small = rank_quality_v2b(_raw(60), Q)
    assert (small.q_status == "UNDET").all() and small.q_reason.str.contains("100").all()
    # 全く同じ値(同順位)なら、無理に三分割しない
    tie = _raw(150); tie[["qb_prof", "qb_accr", "qb_dil"]] = 0.0
    assert (rank_quality_v2b(tie, Q).q_status == "MID").all()


def test_timing_gate_and_returns_identical_between_v2_and_v2b():
    """V2とV2bで違うのはQualityだけ。Gate通過行・Timing・先の成績は完全に同じ。"""
    p = make_panel(n_days=700, n_t=140)
    pv = PanelV2(p.open, p.close, p.close.copy(), p.volume, p.spy_open, p.spy_close, {}, pd.Series(np.nan, index=p.close.columns))
    rng = np.random.default_rng(1)
    sec = {t: make_sec_b(i, op=float(rng.uniform(-10, 120)), ni=float(rng.uniform(-10, 100)), ocf=float(rng.uniform(-20, 120)),
                         shares=(1e8, 1e8 * float(1 + rng.uniform(-0.03, 0.15)))) for i, t in enumerate(p.close.columns[:130])}
    ind = build(pv, R1)
    a, _ = collect_signals_v2(pv, ind, R1, R2, sec, "2020-03-02", "2020-12-31", log=lambda s: None)
    ind = build(pv, R1)
    b, _ = collect_signals_v2(pv, ind, R1, R2B, sec, "2020-03-02", "2020-12-31", log=lambda s: None)
    keep = [c for c in a.columns if not c.startswith(("q_", "v2_grade", "qb_")) and c in b.columns]
    assert {"close", "rsi", "score", "grade", "ret_3m", "ret_6m"} <= set(keep)
    pd.testing.assert_frame_equal(a[keep].reset_index(drop=True), b[keep].reset_index(drop=True))
    assert set(b.q_status) <= {"HIGH", "MID", "LOW", "UNDET"} and set(b.v2_grade) <= set("ABCD")
    assert (b.q_status != a.q_status).any()                                              # Qualityの定義が実際に違う
    d = b[b.q_status != "UNDET"].groupby("date").q_status.value_counts().unstack(fill_value=0)
    assert ((d["HIGH"] / d.sum(axis=1)).between(0.25, 0.4)).all()                        # 毎日ほぼ三分の一がHIGH
    assert not ((b.q_status == "UNDET") & (b.v2_grade == "A")).any()                     # 判定不能がAになることはない


def make_sig_groups(r_a, r_t, r_o, n_days=150, seed=0):
    """20銘柄=V2_A(Timing高かつQuality HIGH) / 10銘柄=Timing高だがQualityはHIGHでない / 10銘柄=その他。3か月リターンの平均をそれぞれ指定。"""
    base = make_sig(0.0, n_days=n_days, per_day=40, seed=seed)
    rng = np.random.default_rng(seed + 100)
    k = base.ticker.str[1:].astype(int)
    grp = np.where(k < 20, "A", np.where(k < 30, "T", "O"))
    mean = np.select([grp == "A", grp == "T"], [r_a, r_t], r_o)
    r3 = rng.normal(0, 0.1, len(base)) + mean
    base["grade"] = np.where(grp == "O", "C", "A")
    base["q_status"] = np.where(grp == "A", "HIGH", "MID")
    base["v2_grade"] = np.select([grp == "A", grp == "T"], ["A", "B"], "C")
    for h in ("3m", "6m"):
        base[f"ret_{h}"], base[f"excess_{h}"] = r3, r3
    return base


def test_eligibility_requires_a_b_and_c():
    Kb = EB.load_K_v2b()
    Kb["bootstrap"]["n_resamples"] = 500
    good = EB.eligibility(make_sig_groups(0.05, 0.0, 0.0, seed=1), Kb)
    assert good["a"] and good["b"] and good["c"] and good["eligible"], good
    # (b) Qualityの上乗せが負(Timing高のQuality非HIGHのほうが高リターン) → (a)(c)を満たしても不適格
    bad_b = EB.eligibility(make_sig_groups(0.04, 0.06, -0.04, seed=1), Kb)
    assert bad_b["a"] and bad_b["c"] and not bad_b["b"] and not bad_b["eligible"], bad_b
    # 優位性なし → 不適格
    none = EB.eligibility(make_sig_groups(0.0, 0.0, 0.0, seed=3), Kb)
    assert not none["eligible"] and not none["a"]


def test_final_eligibility_v2_is_ineligible_and_v2b_follows_abc():
    M = {"v2b": {"rules": {"sha256": hashlib.sha256((ROOT / "config/rules_v2b.yaml").read_bytes()).hexdigest()}}}
    noise = make_sig(0.0, n_days=150, seed=3)
    assert EB.final_eligibility(str(ROOT / "config/rules_v2.yaml"), noise, M)[0] is False       # V2: devでTier1を満たさない → 不適格
    assert EB.final_eligibility(str(ROOT / "config/rules_v2b.yaml"), noise, M)[0] is False      # V2b: (a)(b)(c)を満たさない → 不適格


def test_report_v2b_labels_and_selection_section():
    Kb = EB.load_K_v2b()
    Kb["bootstrap"]["n_resamples"] = 300
    rep = EB.report_v2b(Kb, make_sig(0.04, n_days=150, seed=1))
    assert "97.5%区間" in rep and "95%区間" not in rep
    assert "最終検証へ進む条件" in rep and "Qualityの上乗せ" in rep and "記述的な副次分析" in rep
    assert "nan" not in rep


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK ", fn.__name__)
    print(f"{len(fns)}件すべて成功")
