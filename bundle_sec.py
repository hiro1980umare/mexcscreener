#!/usr/bin/env python3
"""SEC財務(1銘柄1ファイル)を、1つのファイルにまとめる。Colab+ドライブで、読み込みが25分→数秒になる。
    python bundle_sec.py --sec-dir data_v2/sec_v2b --out /content/drive/MyDrive/us_stock_bot_work/sec_v2b_bundle.pkl
  使う側: python run_bot.py ... --sec-bundle <上のファイル>      (SEC財務を取得し直したら、まとめ直す)
"""
import argparse
import time
from pathlib import Path

from src.bot_data import load_sec_dir, save_sec_bundle

ap = argparse.ArgumentParser()
ap.add_argument("--sec-dir", default="data_v2/sec_v2b")
ap.add_argument("--out", required=True)
a = ap.parse_args()
t0 = time.time()
tick = sorted(f.stem for f in Path(a.sec_dir).glob("*.pkl"))
print(f"{len(tick)}ファイルを読み込みます(ドライブ上だと数十分かかることがあります。1回だけです)", flush=True)
sec = load_sec_dir(a.sec_dir, tick, progress=lambda m: print(f"  {m}  {time.time() - t0:.0f}秒", flush=True))
save_sec_bundle(sec, a.out)
print(f"保存しました: {a.out} ({len(sec)}銘柄, {Path(a.out).stat().st_size / 1e6:.0f}MB, {time.time() - t0:.0f}秒)")
