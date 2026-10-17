"""V2bを端から端まで合成データで通す統合テスト。 python tests/test_v2b_cli.py
  登録 -> V2実行 -> V2b登録 -> V2b実行 -> V2cの拒否 -> 変更の拒否 -> 最終検証の条件検査(本物のコード) -> 条件検査をスタブにして最終検証の1回きり
"""
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
sys.path.insert(0, str(ROOT / "tests"))
from src import data_v2 as D2  # noqa: E402
from test_v2_cli import row, sec_for  # noqa: E402


def sec_for_b(seed):
    s = sec_for(seed)
    rng = np.random.default_rng(seed + 7)
    s["flow"]["net_income"] = [("NetIncomeLoss", [row(f"{y}-01-01", f"{y}-12-31", float(rng.uniform(0, 60)), f"{y + 1}-02-28") for y in range(2009, 2018)])]
    return s


def main():
    tmp = Path(tempfile.mkdtemp())
    for item in ("config", "docs", "src", "evaluate_criteria.py", "evaluate_v2b.py", "freeze_v2.py", "run_v2.py", "fetch_data_v2.py"):
        (shutil.copytree if (ROOT / item).is_dir() else shutil.copy)(ROOT / item, tmp / item)
    shutil.rmtree(tmp / "config" / "__pycache__", ignore_errors=True)
    rng = np.random.default_rng(2)
    idx = pd.bdate_range("2009-06-01", "2018-09-28")
    n_t = 130                                                     # V2bは同日に判定可能な銘柄が100以上必要
    tick = [f"T{i}" for i in range(n_t)]
    ret = rng.normal(0.0005, 0.02, (len(idx), n_t))
    close = pd.DataFrame(40 * np.exp(np.cumsum(ret, 0)), index=idx, columns=tick)
    opn = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    vol = pd.DataFrame(rng.uniform(2e6, 5e6, close.shape), index=idx, columns=tick)
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(idx)))), index=idx)
    mats = dict(open=pd.concat([opn, spy.shift(1).fillna(spy).rename("SPY")], axis=1), close=pd.concat([close, spy.rename("SPY")], axis=1),
                close_raw=pd.concat([close, spy.rename("SPY")], axis=1), volume=pd.concat([vol, vol.iloc[:, 0].rename("SPY")], axis=1),
                splits={t: (np.array([], dtype="datetime64[D]"), np.array([])) for t in tick})
    cov = pd.DataFrame({"ticker": tick + ["SPY"], "status": "ok", "n_rows": len(idx), "reason": "", "first_date": "", "last_date": ""})
    D2.save_v2({k: (v.loc["2016-06-01":] if k != "splits" else v) for k, v in mats.items()}, cov, tmp / "data_v2")
    for name, fn in (("sec", sec_for), ("sec_v2b", sec_for_b)):        # V2は従来のデータ、V2bは純利益つきの別フォルダ
        (tmp / f"data_v2/{name}").mkdir()
        for i, t in enumerate(tick):
            with (tmp / f"data_v2/{name}/{t}.pkl").open("wb") as fh:
                pickle.dump(fn(i), fh)

    def run(*a, check=True):
        r = subprocess.run([sys.executable, *a], cwd=tmp, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SystemExit(f"失敗: {a}\n{r.stdout[-1800:]}\n{r.stderr[-1800:]}")
        return r

    out = lambda r: r.stdout + r.stderr  # noqa: E731
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp, capture_output=True, text=True)  # noqa: E731
    commit = lambda m: (git("add", "."), git("commit", "-q", "-m", m))  # noqa: E731
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    DEV = ("--period", "dev")

    # 1) V2の登録とdev実行
    run("freeze_v2.py"); commit("register v2")
    run("run_v2.py", "--rules", "config/rules_v2.yaml", *DEV, "--label", "v2")
    assert (tmp / "results/v2_dev_v2/signals.csv.gz").exists()

    # 2) V2bは、登録前は実行できない
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", *DEV, "--label", "v2b", check=False)
    assert r.returncode != 0 and "登録されていない" in out(r), out(r)                              # 理由は『未登録』(未コミットではない)

    # 3) V2bの登録(1回だけ)
    run("freeze_v2.py", "--register-v2b"); commit("register v2b")
    r = run("freeze_v2.py", "--register-v2b", check=False)
    assert r.returncode != 0 and "すでに登録済み" in out(r)

    # 4) V2b実行: 基準は97.5%、最終検証に進む条件が出る
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", *DEV, "--label", "v2b")
    assert "97.5%区間" in r.stdout and "最終検証へ進む条件" in r.stdout and (tmp / "results/v2_dev_v2b/signals.csv.gz").exists(), r.stdout[-2500:]
    sb = pd.read_csv(tmp / "results/v2_dev_v2b/signals.csv.gz", parse_dates=["date"])
    assert set(sb.q_status) & {"HIGH", "LOW"} and sb.date.min() >= pd.Timestamp("2017-07-03")      # 実際にHIGH/LOWが付く(判定可能銘柄が十分)

    # 5) V2cは作れない(登録された2つ以外のdev実行は拒否)。コミット済みにして、『未コミット』が理由にならないようにする
    (tmp / "config/rules_v2c.yaml").write_text((tmp / "config/rules_v2b.yaml").read_text().replace("n_groups: 3", "n_groups: 4"))
    commit("add v2c file")
    r = run("run_v2.py", "--rules", "config/rules_v2c.yaml", *DEV, "--label", "v2c", check=False)
    assert r.returncode != 0 and not (tmp / "results/v2_dev_v2c").exists(), out(r)
    assert "V2cは作れません" in out(r) or "登録されていない、または登録後に変更" in out(r), out(r)

    # 6) 登録後のV2b・事前登録文書・基準の変更は、コミットしても拒否される(結果を見てのルール変更の禁止)
    for f, old, new, why in (("config/rules_v2b.yaml", "n_groups: 3", "n_groups: 4", "登録されていない、または登録後に変更"),
                             ("docs/v2b_preregistration.md", "最大1版", "最大2版", "事前登録"),
                             ("config/criteria_v2b.yaml", "ci: 0.975", "ci: 0.95", "事前登録")):
        p = tmp / f
        orig = p.read_text()
        p.write_text(orig.replace(old, new)); commit(f"tamper {f}")
        r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", *DEV, "--label", "tamper", check=False)
        assert r.returncode != 0 and not (tmp / "results/v2_dev_tamper").exists(), (f, out(r))
        assert why in out(r) and "変更" in out(r), (f, out(r))                                        # 理由が『登録後の変更』であること
        p.write_text(orig); commit(f"restore {f}")
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", *DEV, "--label", "v2b_again")           # 元の内容に戻せば実行できる
    assert r.returncode == 0

    # 7) 最終検証に進む条件(本物のコード)。合成データは優位性がないので、V2もV2bも最終候補にできない
    for rules in ("config/rules_v2.yaml", "config/rules_v2b.yaml"):
        r = run("freeze_v2.py", "--final-candidate", rules, check=False)
        assert r.returncode != 0 and "最終検証に進む条件" in out(r), (rules, out(r))
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", "--period", "final", check=False)
    assert r.returncode != 0 and not (tmp / "results/v2_final_lock.json").exists(), out(r)

    # 8) 条件検査だけをスタブにして(一時コピーのみ)、V2bの最終検証が『1回きり』で動くことを確認
    with (tmp / "evaluate_v2b.py").open("a") as fh:
        fh.write("\n\ndef final_eligibility(rules_file, dev_sig, M):\n    return True, {}\n")
    run("freeze_v2.py", "--final-candidate", "config/rules_v2b.yaml"); commit("freeze final candidate")
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", "--period", "final", check=False)      # 最終検証用データが未取得
    assert r.returncode != 0 and "権利は消費されません" in out(r) and not (tmp / "results/v2_final_lock.json").exists(), out(r)
    D2.save_v2(mats, cov, tmp / "data_v2_final")
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", "--period", "final")
    assert "V2B 最終検証" in (tmp / "results/v2_final/verdict.md").read_text(encoding="utf-8")
    fs = pd.read_csv(tmp / "results/v2_final/signals.csv.gz", parse_dates=["date"])
    assert fs.date.min() >= pd.Timestamp("2011-01-03") and fs.date.max() <= pd.Timestamp("2015-11-13")
    r = run("run_v2.py", "--rules", "config/rules_v2b.yaml", "--period", "final", check=False)      # 2回目は拒否
    assert r.returncode != 0 and "使用済み" in out(r)
    print("V2b統合テスト OK: V2登録 -> V2b登録(1回だけ) -> V2b dev -> V2c拒否 -> 変更拒否 -> 最終検証の条件検査 -> 最終検証は1回きり")


if __name__ == "__main__":
    main()
