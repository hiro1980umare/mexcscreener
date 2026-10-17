"""Botのテスト用の『小さな世界』(合成データ)。ネットワーク不要。"""
import pickle
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from src import data_v2 as D2  # noqa: E402
from src.indicators import build  # noqa: E402
from src.rules import load_rules  # noqa: E402
from src.v2 import evaluate_day_v2  # noqa: E402
from test_v2b_cli import sec_for, sec_for_b  # noqa: E402

COPY = ("config", "docs", "src", "run_bot.py", "freeze_bot.py", "track_forward.py", "bot_selfcheck.py", "export_universe.py", "bundle_sec.py", "resend_bot.py")
N_UNDET = 25                     # Quality判定不能にする銘柄数(純利益の項目が無い)


def make_world(n_t=140, seed=2, end="2018-09-28", run_freeze=True):
    """戻り値: (tmpフォルダ, 候補が両方の区分に出る判定日, 全データ)"""
    tmp = Path(tempfile.mkdtemp())
    for item in COPY:
        if (ROOT / item).exists():
            (shutil.copytree if (ROOT / item).is_dir() else shutil.copy)(ROOT / item, tmp / item)
    shutil.rmtree(tmp / "config" / "__pycache__", ignore_errors=True)
    (tmp / "config" / "bot_manifest.json").unlink(missing_ok=True)       # 本番で登録済みの凍結ファイルは持ち込まない(テスト用に登録し直すため)
    shutil.rmtree(tmp / "config" / "bot_manifest_history", ignore_errors=True)
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2016-06-01", end)
    tick = [f"T{i}" for i in range(n_t)]
    ret = rng.normal(0.0005, 0.022, (len(idx), n_t))
    close = pd.DataFrame(40 * np.exp(np.cumsum(ret, 0)), index=idx, columns=tick)
    opn = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    vol = pd.DataFrame(rng.uniform(2e6, 5e6, close.shape), index=idx, columns=tick)
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(idx)))), index=idx)
    mats = dict(open=pd.concat([opn, spy.shift(1).fillna(spy).rename("SPY")], axis=1), close=pd.concat([close, spy.rename("SPY")], axis=1),
                close_raw=pd.concat([close, spy.rename("SPY")], axis=1), volume=pd.concat([vol, vol.iloc[:, 0].rename("SPY")], axis=1),
                splits={t: (np.array([], dtype="datetime64[D]"), np.array([])) for t in tick})
    cov = pd.DataFrame({"ticker": tick + ["SPY"], "status": "ok", "n_rows": len(idx), "reason": "", "first_date": "", "last_date": ""})
    D2.save_v2(mats, cov, tmp / "data_v2")
    (tmp / "data_v2/sec_v2b").mkdir()
    for i, t in enumerate(tick):
        sec = sec_for(i) if i < N_UNDET else sec_for_b(i)
        with (tmp / f"data_v2/sec_v2b/{t}.pkl").open("wb") as fh:
            pickle.dump(sec, fh)
    pd.DataFrame({"ticker": tick}).to_csv(tmp / "config/bot_universe.csv", index=False)
    if run_freeze:
        subprocess.run([sys.executable, "freeze_bot.py"], cwd=tmp, check=True, capture_output=True)
    # 両方の区分に候補が出る判定日を探す
    R1 = load_rules(ROOT / "config/rules_v1.yaml")
    R2 = yaml.safe_load((ROOT / "config/rules_v2b.yaml").read_text(encoding="utf-8"))
    panel = D2.load_v2(tmp / "data_v2")
    from src.bot_data import load_sec_dir
    sec = load_sec_dir(tmp / "data_v2/sec_v2b", panel.close.columns)
    ind = build(panel, R1)
    best = None
    for i in range(len(ind.dates) - 1, len(ind.dates) - 260, -1):
        df = evaluate_day_v2(panel, ind, R1, R2, sec, i)
        if df.empty:
            continue
        na, nu = (df.v2_grade == "A").sum(), ((df.q_status == "UNDET") & df.grade.isin(["A", "B"])).sum()
        if na >= 3 and nu >= 2:
            best = (str(ind.dates[i].date()), int(na), int(nu))
            break
    return tmp, best, (panel, ind, sec, R1, R2)
