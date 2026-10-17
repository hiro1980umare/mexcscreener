#!/usr/bin/env python3
"""V2用の株価データ取得。途中で止まっても同じコマンドで続きから再開できる。

    python fetch_data_v2.py                       # dev用(2016-06以降)。30〜60分
    python fetch_data_v2.py --max-tickers 150     # お試し
    python fetch_data_v2.py --period final        # ★最終検証用(2009〜)。最終候補を凍結するまで拒否される

★封印: dev用のデータには、最終検証期間(2011〜2016-05)の株価を一切入れない。
        最終検証用の株価は、最終候補ルールを freeze_v2.py --final-candidate で凍結するまで取得できない。
前提: data/mcap.csv（v1で作った時価総額の表）があること。
"""
import argparse
import json
import random
from pathlib import Path

import pandas as pd
import yaml

from src.data_v2 import assemble, download_v2, save_v2
from src.rules import load_rules

ap = argparse.ArgumentParser()
ap.add_argument("--period", choices=["dev", "final"], default="dev")
ap.add_argument("--max-tickers", type=int, default=0)
ap.add_argument("--out", default="")
a = ap.parse_args()
R2 = yaml.safe_load(open("config/rules_v2.yaml", encoding="utf-8"))
D = R2["data"][a.period]
out = a.out or D["dir"]

if a.period == "final":
    mf = Path("config/v2_manifest.json")
    fc = json.loads(mf.read_text()).get("final_candidate") if mf.exists() else None
    if not fc:
        raise SystemExit("【中止】最終検証用の株価は、最終候補ルールを凍結するまで取得できません（封印）。\n"
                         "  dev期間でルールが確定したら: python freeze_v2.py --final-candidate <ルール> → git commit → 再実行")
    print(f"最終候補が凍結済みです: {fc['file']}  → 最終検証用の株価を取得します")
    if a.max_tickers:
        raise SystemExit("【中止】最終検証用データはお試し取得できません（全銘柄を取得してください）。")
else:
    start = pd.Timestamp(D["price_history_start"])
    if start <= pd.Timestamp(R2["periods"]["final"]["outcome_end"]):
        raise SystemExit("【中止】dev用の取得開始日が、封印された最終検証期間に入っています。rules_v2.yamlを確認してください。")

R1 = load_rules("config/rules_v1.yaml")
mc = pd.read_csv("data/mcap.csv", index_col=0).iloc[:, 0]
tick = sorted(mc[mc >= R1["universe"]["current_market_cap_min"]].index)
if a.max_tickers and len(tick) > a.max_tickers:
    tick = sorted(random.Random(42).sample(tick, a.max_tickers))
    print(f"⚠ お試し: {len(tick)}銘柄に制限（このデータでdevの本番判断はしない）")
print(f"期間: {a.period} / 対象: {len(tick)}銘柄 + SPY / 取得開始 {D['price_history_start']} / 保存先 {out}")
cache = download_v2(tick + ["SPY"], D["price_history_start"], f"{out}/chunks")
mats, cov = assemble(cache)
save_v2(mats, cov, out)
st = cov[cov.ticker != "SPY"].status.value_counts().to_dict()
n = len(tick)
print(f"\n# V2 株価データ取得レポート ({a.period})\n- 対象 {n} / 成功(252日以上) {st.get('ok', 0)} / 履歴不足 {st.get('short_history', 0)} / 失敗 {st.get('failed', 0)}"
      f"\n- 成功率 {st.get('ok', 0) / max(n, 1):.1%}（履歴不足を除いた取得成功率 {(st.get('ok', 0) + st.get('short_history', 0)) / max(n, 1):.1%}）"
      f"\n- 期間 {mats['close'].index.min().date()} 〜 {mats['close'].index.max().date()} / 分割記録のある銘柄 {sum(len(v[0]) > 0 for v in mats['splits'].values())}"
      f"\n- 失敗一覧: {out}/coverage.csv (status=failed)")
