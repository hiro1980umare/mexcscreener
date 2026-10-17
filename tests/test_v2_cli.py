"""V2を端から端まで合成データで通す統合テスト（凍結 -> dev実行 -> 最終検証1回きり）。 python tests/test_v2_cli.py"""
import pickle
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import data_v2 as D2  # noqa: E402


def row(start, end, val, filed, form="10-K"):
    d = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        d["start"] = start
    return d


def sec_for(seed):
    rng = np.random.default_rng(seed)
    rev, rows = 100.0, {"revenue": [], "op": [], "ocf": [], "capex": [], "assets": [], "equity": [], "shares": []}
    for y in range(2009, 2018):
        rev *= 1 + rng.uniform(-0.05, 0.25)
        f, st, en = f"{y + 1}-02-28", f"{y}-01-01", f"{y}-12-31"
        rows["revenue"].append(row(st, en, rev, f))
        rows["op"].append(row(st, en, rev * rng.uniform(0.0, 0.3), f))
        rows["ocf"].append(row(st, en, rev * rng.uniform(0.05, 0.3), f))
        rows["capex"].append(row(st, en, rev * rng.uniform(0.01, 0.1), f))
        rows["assets"].append(row(None, en, 1000 * (1 + y - 2009), f))
        rows["equity"].append(row(None, en, 600 * (1 + y - 2009) * rng.uniform(0.4, 1.0), f))
        rows["shares"].append(row(None, f"{y + 1}-02-15", 1e8 * (1 + rng.uniform(-0.02, 0.1)) ** (y - 2008), f))
    return {"flow": {"revenue": [("Revenues", rows["revenue"])], "op_income": [("OperatingIncomeLoss", rows["op"])],
                     "ocf": [("NetCashProvidedByUsedInOperatingActivities", rows["ocf"])],
                     "capex": [("PaymentsToAcquirePropertyPlantAndEquipment", rows["capex"])]},
            "inst": {"assets": [("Assets", rows["assets"])], "equity": [("StockholdersEquity", rows["equity"])],
                     "shares": [("EntityCommonStockSharesOutstanding", rows["shares"])]}}


def main():
    tmp = Path(tempfile.mkdtemp())
    for item in ("config", "src", "evaluate_criteria.py", "evaluate_v2b.py", "freeze_v2.py", "run_v2.py", "fetch_data_v2.py"):
        (shutil.copytree if (ROOT / item).is_dir() else shutil.copy)(ROOT / item, tmp / item)
    shutil.rmtree(tmp / "config" / "__pycache__", ignore_errors=True)
    # 最終検証の仕組み(ロック・封印・1回きり)だけを試すため、『最終検証に進む条件』の検査を、この一時コピーにだけスタブにする
    # (条件検査そのものは tests/test_v2b.py と tests/test_v2b_cli.py で本物のコードを検査している。本体のファイルは変更しない)
    (tmp / "evaluate_v2b.py").write_text("def final_eligibility(rules_file, dev_sig, M):\n    return True, {}\n")
    # 合成データ（2010-06〜2018-09）
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2009-06-01", "2018-09-28")
    n_t, tick = 40, [f"T{i}" for i in range(40)]
    ret = rng.normal(0.0005, 0.02, (len(idx), n_t))
    close = pd.DataFrame(40 * np.exp(np.cumsum(ret, 0)), index=idx, columns=tick)
    opn = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    vol = pd.DataFrame(rng.uniform(2e6, 5e6, close.shape), index=idx, columns=tick)
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(idx)))), index=idx)
    mats = dict(open=pd.concat([opn, spy.shift(1).fillna(spy).rename("SPY")], axis=1),
                close=pd.concat([close, spy.rename("SPY")], axis=1), close_raw=pd.concat([close, spy.rename("SPY")], axis=1),
                volume=pd.concat([vol, vol.iloc[:, 0].rename("SPY")], axis=1), splits={t: (np.array([], dtype="datetime64[D]"), np.array([])) for t in tick})
    cov = pd.DataFrame({"ticker": tick + ["SPY"], "status": "ok", "n_rows": len(idx), "reason": "", "first_date": "", "last_date": ""})
    def sliced(start):
        m = {k: (mats[k].loc[start:] if k != "splits" else mats[k]) for k in mats}
        return m
    # dev用データ = 2016-06-01以降だけ(封印期間の株価を含まない)。最終検証用は凍結後にだけ作る
    D2.save_v2(sliced("2016-06-01"), cov, tmp / "data_v2")
    (tmp / "data_v2/sec").mkdir()
    for i, t in enumerate(tick):
        with (tmp / f"data_v2/sec/{t}.pkl").open("wb") as fh:
            pickle.dump(sec_for(i), fh)

    def run(*a, check=True):
        r = subprocess.run([sys.executable, *a], cwd=tmp, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SystemExit(f"失敗: {a}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
        return r

    git = lambda *a: subprocess.run(["git", *a], cwd=tmp, capture_output=True, text=True)  # noqa: E731
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    shutil.copy(tmp / "config/rules_v2.yaml", tmp / "config/rules_v2x.yaml")   # 未凍結の別ルール(試験用)
    run("freeze_v2.py")
    git("add", "."); git("commit", "-q", "-m", "register")
    # ★封印: 最終候補の凍結前は、最終検証用の株価を取得できない
    r = run("fetch_data_v2.py", "--period", "final", check=False)
    assert r.returncode != 0 and "封印" in r.stdout + r.stderr, r.stdout + r.stderr
    assert not (tmp / "data_v2_final").exists()
    # ★封印: 封印期間の株価を含むデータをdevに使おうとすると拒否される
    D2.save_v2(mats, cov, tmp / "data_leaky")
    r = run("run_v2.py", "--rules", "config/rules_v2.yaml", "--period", "dev", "--data", "data_leaky", "--label", "leak", check=False)
    assert r.returncode != 0 and "封印" in r.stdout + r.stderr, r.stdout + r.stderr
    assert not (tmp / "results/v2_dev_leak").exists()
    # 未凍結のルールでfinalは実行できない
    r = run("run_v2.py", "--rules", "config/rules_v2x.yaml", "--period", "final", check=False)
    assert r.returncode != 0 and "凍結されていません" in r.stdout + r.stderr, r.stdout + r.stderr
    # dev実行
    r = run("run_v2.py", "--rules", "config/rules_v2.yaml", "--period", "dev", "--label", "t1")
    assert "成功基準" in r.stdout and (tmp / "results/v2_dev_t1/signals.csv.gz").exists()
    sig = pd.read_csv(tmp / "results/v2_dev_t1/signals.csv.gz", parse_dates=["date"])
    assert sig.date.min() >= pd.Timestamp("2017-07-03") and sig.date.max() <= pd.Timestamp("2026-03-31")   # devは2017年以降だけ
    assert not (tmp / "results/v2_final").exists()
    # 最終候補を凍結 -> コミット前は拒否 -> コミット後に1回だけ実行
    run("freeze_v2.py", "--final-candidate", "config/rules_v2.yaml")
    r = run("run_v2.py", "--rules", "config/rules_v2.yaml", "--period", "final", check=False)
    assert r.returncode != 0 and "コミット" in r.stdout + r.stderr, r.stdout + r.stderr
    git("add", "."); git("commit", "-q", "-m", "freeze final candidate")
    r = run("run_v2.py", "--rules", "config/rules_v2.yaml", "--period", "final", check=False)   # 最終検証用データが未取得
    assert r.returncode != 0 and "権利は消費されません" in r.stdout + r.stderr, r.stdout + r.stderr
    assert not (tmp / "results/v2_final_lock.json").exists()                                    # ★ロックは作られていない
    D2.save_v2(mats, cov, tmp / "data_v2_final")                                                # 凍結後に最終検証用データを置く(実運用ではfetch_data_v2.py --period final)
    r = run("run_v2.py", "--rules", "config/rules_v2.yaml", "--period", "final")
    assert "最終検証" in r.stdout and (tmp / "results/v2_final/verdict.md").exists(), r.stdout
    fs = pd.read_csv(tmp / "results/v2_final/signals.csv.gz", parse_dates=["date"])
    assert fs.date.min() >= pd.Timestamp("2011-01-03") and fs.date.max() <= pd.Timestamp("2015-11-13")     # finalは封印期間だけ
    r = run("run_v2.py", "--rules", "config/rules_v2.yaml", "--period", "final", check=False)             # 2回目は拒否
    assert r.returncode != 0 and "使用済み" in r.stdout + r.stderr
    # コードを変えるとfinalは実行できない(改ざん防止)
    print("統合テスト OK: 凍結 -> dev(2017-07〜, 封印期間のデータなし) -> final(2011-01〜2015-11, 凍結後にだけデータ取得, 1回きり)")


if __name__ == "__main__":
    main()
