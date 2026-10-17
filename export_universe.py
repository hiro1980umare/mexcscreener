#!/usr/bin/env python3
"""対象銘柄リスト(config/bot_universe.csv)を、取得済みデータ(data_v2/coverage.csv)から出力する。週1回の更新を想定。
    python export_universe.py
"""
import sys
from pathlib import Path

import pandas as pd

src = Path("data_v2/coverage.csv")
if not src.exists():
    sys.exit("data_v2/coverage.csv がありません。先に python fetch_data_v2.py を実行してください。")
cov = pd.read_csv(src)
t = sorted(cov[(cov.status == "ok") & (cov.ticker != "SPY")].ticker)
Path("config").mkdir(exist_ok=True)
pd.DataFrame({"ticker": t}).to_csv("config/bot_universe.csv", index=False)
print(f"config/bot_universe.csv に {len(t)}銘柄を出力しました。git add config/bot_universe.csv でコミットしてください。")
