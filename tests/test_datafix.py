"""データ修正（STEP4b）の部品テスト。ネットワーク不要。 python tests/test_datafix.py"""
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_synthetic import R, make_panel  # noqa: E402
from src import data as D  # noqa: E402
from src.audit import classify, find_anomalies, split_like_ratio  # noqa: E402
from src.backtest import collect_signals  # noqa: E402
from src.indicators import build  # noqa: E402


def fake_yf(calls):
    """1回目は一部の銘柄をNaNで返し(レート制限の再現)、2回目以降は返す。永久に失敗する銘柄と短い履歴の銘柄も用意。"""
    idx = pd.bdate_range("2020-01-01", periods=300)
    mod = types.ModuleType("yfinance")
    mod.shared = types.SimpleNamespace(_ERRORS={"DEAD": "possibly delisted"})

    def download(part, **kw):
        calls.append((tuple(part), kw.get("threads")))
        n = len(calls)
        cols = {}
        for t in part:
            L = 300
            bad = (t.startswith("FLAKY") and n == 1) or t == "DEAD"
            if t == "SHORT":
                L = 100
            s = pd.Series(np.nan if bad else 10.0, index=idx)
            if t == "SHORT":
                s.iloc[:200] = np.nan
            for f in ("Open", "Close", "Volume"):
                cols[(f, t)] = s.copy()
        df = pd.DataFrame(cols)
        df.columns = pd.MultiIndex.from_tuples(df.columns)
        return df

    mod.download = download
    return mod


def test_download_robust_retries_and_reports():
    calls = []
    sys.modules["yfinance"] = fake_yf(calls)
    tick = ["A", "B", "FLAKY1", "FLAKY2", "DEAD", "SHORT"]
    o, c, v, cov = D.download_robust(tick, "2020-01-01", chunk=4, rounds=3, sleeper=lambda s: None, log=lambda s: None)
    st = cov.set_index("ticker").status.to_dict()
    assert st["A"] == st["B"] == st["FLAKY1"] == st["FLAKY2"] == "ok", st      # 再取得で回収できている
    assert st["DEAD"] == "failed" and st["SHORT"] == "short_history"
    assert "possibly delisted" in cov.set_index("ticker").loc["DEAD", "reason"]
    assert set(c.columns) == {"A", "B", "FLAKY1", "FLAKY2"}                    # 失敗銘柄を推定で埋めていない
    assert len(calls) > 2


def test_mcap_robust_recovers_transient_failures():
    """一時的な失敗(制限)は回収し、恒久的に取れない銘柄は推定で埋めず、無限に粘らない。"""
    state = {"calls": 0}
    rate = [f"RATE{i}" for i in range(30)]
    dead = [f"DEAD{i}" for i in range(5)]

    def fetcher(ts, **kw):
        state["calls"] += 1
        bad = lambda t: t.startswith("DEAD") or (t.startswith("RATE") and state["calls"] == 1)  # noqa: E731
        return pd.Series({t: (float("nan") if bad(t) else 5e9) for t in ts}, dtype=float)

    waits = []
    m = pd.Series(float("nan"), index=rate + dead + ["OK1"])
    m.loc["OK1"] = 1e9
    f = Path("/tmp/mcap_cache_test.csv")
    out = D.fetch_mcaps_robust(m, cache_file=f, batch=100, fetcher=fetcher, sleeper=waits.append, log=lambda s: None)
    assert out[rate].notna().all()                        # 一時的な失敗(制限)は回収できる
    assert out[dead].isna().all()                         # 取れないものは推定で埋めない
    assert waits and waits[0] == 120                      # 回収が半分未満のバッチは制限中とみなして待つ
    assert state["calls"] <= 6                            # 無限に粘らない
    assert f.exists() and "RATE1" in f.read_text()        # 進捗がキャッシュに保存される


def test_basic_checks_runs_on_dataframe():
    """audit_data.pyが落ちた不具合の再発防止（2次元のgroupbyを使わない）。"""
    from src.audit import basic_checks
    idx = pd.bdate_range("2021-01-01", periods=40)
    c = pd.DataFrame({"A": np.arange(40) + 10.0, "B": 10.0}, index=idx)
    c.loc[idx[20], "A"] = np.nan
    o = c.copy()
    o.iloc[3, 0] = 0
    bc = basic_checks(c, c * 0 + 100, o)
    assert bc.loc["B", "max_flat_run_days"] == 39 and bc.loc["A", "max_flat_run_days"] == 0
    assert bc.loc["A", "inner_missing_days"] == 1 and bc.loc["A", "nonpositive_open_days"] == 1


def test_anomaly_detection_and_classification():
    idx = pd.bdate_range("2021-01-01", periods=30)
    close = pd.DataFrame({"SPLT": 100.0, "GLCH": 50.0, "REAL": 20.0, "OK": 10.0}, index=idx)
    close.loc[idx[10]:, "SPLT"] = 50.0          # 2:1分割が調整されていない形(-50%)
    close.loc[idx[10], "GLCH"] = 5.0            # 1日だけ-90%で翌日戻る
    close.loc[idx[10]:, "REAL"] = 50.0          # +150%のまま(買収提案など実際の急変動の可能性。分割比に似ない)
    an = find_anomalies(close, close, close * 0 + 1000)
    assert "OK" not in set(an.ticker)
    got = an.drop_duplicates("ticker", keep="first").set_index("ticker")   # 各銘柄の最初の異常日
    assert (an[an.ticker == "GLCH"].spike_revert).all()                      # 下落日も戻った日も『反転』と判定
    assert got.loc["GLCH", "spike_revert"] and not got.loc["REAL", "spike_revert"]
    assert "2" in split_like_ratio(0.5) and split_like_ratio(0.9) == ""
    spl = pd.Series([2.0], index=pd.DatetimeIndex([idx[10]], tz="America/New_York"))
    assert classify(got.loc["SPLT"].to_dict() | {"ticker": "SPLT"}, spl)["classification"].startswith("ADJUSTMENT_SUSPECT")
    assert classify(got.loc["GLCH"].to_dict() | {"ticker": "GLCH"}, pd.Series(dtype=float))["classification"].startswith("GLITCH_SUSPECT")
    assert classify(got.loc["REAL"].to_dict() | {"ticker": "REAL"}, pd.Series(dtype=float))["classification"].startswith("UNEXPLAINED")
    assert "取得に失敗" in classify(got.loc["REAL"].to_dict() | {"ticker": "REAL"}, None)["classification"]
    big = got.loc["REAL"].to_dict() | {"ticker": "REAL", "vol_spike": 8.0}
    assert classify(big, pd.Series(dtype=float))["classification"].startswith("REAL_MOVE_LIKELY")   # 出来高急増=実需の可能性
    # 分割記録(2分割=2.0)があっても、価格が分割比どおりに動いていなければ ADJUSTMENT_SUSPECT にしない
    off = got.loc["SPLT"].to_dict() | {"ticker": "SPLT", "ret": 0.9}
    assert classify(off, spl)["classification"].startswith("SPLIT_NEAR_RATIO_DIFFERS")


def test_approved_fixes_need_reason_and_are_opt_in(tmp_path=Path("/tmp/fx_test")):
    tmp_path.mkdir(exist_ok=True)
    p = make_panel()
    same, applied = D.apply_approved_fixes(p, tmp_path / "none.csv")   # ファイルが無ければ何も変えない
    assert applied == [] and same.close.shape == p.close.shape
    f = tmp_path / "fx.csv"
    pd.DataFrame([dict(ticker="T1", action="nan_range", date_from="2020-06-01", date_to="2020-06-10", reason="分割未調整を確認"),
                  dict(ticker="T2", action="drop_ticker", date_from="", date_to="", reason="上場廃止後の異常値")]).to_csv(f, index=False)
    q, applied = D.apply_approved_fixes(p, f)
    assert len(applied) == 2 and "T2" not in q.close.columns
    assert q.close.loc["2020-06-03", "T1"] != q.close.loc["2020-06-03", "T1"]   # NaN
    assert "T2" in p.close.columns                                             # 元データは変更されない
    pd.DataFrame([dict(ticker="T1", action="drop_ticker", date_from="", date_to="", reason="")]).to_csv(f, index=False)
    try:
        D.apply_approved_fixes(p, f)
        raise SystemExit("理由なしの修正が通ってしまった")
    except AssertionError:
        pass


def test_compare_runs_end_to_end():
    import compare_runs
    p = make_panel()
    ind = build(p, R)
    sig, spy = collect_signals(p, ind, R, "2020-03-02", "2020-12-31")
    base = Path("/tmp/cmp_test")
    for name in ("before", "after"):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        sig.to_csv(d / "signals.csv.gz", index=False)
        (d / "meta.json").write_text(json.dumps({"rules_sha256": "x" * 64, "n_tickers_in_data": 60, "n_gate_rows": len(sig),
                                                  "n_signal_dates": len(spy), "data_end": "2026-10-02", "rerun_reason": "テスト"}))
    sys.argv = ["compare_runs.py", "--before", str(base / "before"), "--after", str(base / "after"), "--out", str(base / "out.md")]
    compare_runs.main()
    txt = (base / "out.md").read_text()
    assert "ルール変更なし" in txt and "95%CI" in txt


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK ", fn.__name__)
    print(f"{len(fns)}件すべて成功")
