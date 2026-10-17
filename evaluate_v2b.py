#!/usr/bin/env python3
"""V2b の判定。V2の判定コード(evaluate_criteria.py)は1文字も変えず、その上に乗せる。
  * load_K_v2b : V2の事前登録基準 + V2b専用の上書き(c2の区間を97.5%に厳格化)
  * eligibility: 最終検証に進む条件 (a)(b)(c) の機械的な判定
  * report_v2b : 二段階評価 + 最終検証へ進む条件 + 記述的な副次分析
"""
import copy
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import evaluate_criteria as EC
from src.v2 import make_groups_v2

CRIT_V2, CRIT_V2B = "config/criteria_v2.yaml", "config/criteria_v2b.yaml"


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _deep_update(d, u):
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            _deep_update(d[k], v)
        else:
            d[k] = v
    return d


def load_K_v2b(path=CRIT_V2B):
    b = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if _sha(b["base"]) != b["base_sha256"]:
        raise SystemExit("【中止】V2の事前登録基準(criteria_v2.yaml)が、V2b登録時から変わっています。")
    K = _deep_update(copy.deepcopy(yaml.safe_load(Path(b["base"]).read_text(encoding="utf-8"))), b["overrides"])
    K["v2b"] = b
    return K


def quality_increment(sig, K):
    """(b) Qualityの上乗せ: V2b_A - T_HIGH_ONLY(同日内の差)。V2b_Aが min_signals_per_day 件以上ある日だけ。"""
    P, S = K["primary_test"], K["bootstrap"]
    G = make_groups_v2(sig)
    mean, _ = EC.daily_diff(G[P["group"]], G["T_HIGH_ONLY"], P["horizon"], P["min_signals_per_day"])
    lo, hi = EC.block_ci(mean.values, S["block_length_dates"], S["n_resamples"], S["seed"], S["ci"])
    return dict(n_days=len(mean), mean=float(mean.mean()) if len(mean) else np.nan, ci_lo=lo, ci_hi=hi)


def eligibility(dev_sig, K):
    """最終検証に進む条件。a: c1,c3,c4,c5 / b: Quality上乗せ>0 / c: c2(97.5%区間, 40日以上)。"""
    m = EC.measure(dev_sig, K["baselines"]["tier1"], K)
    j = EC.judge_dev(m, K)
    inc = quality_increment(dev_sig, K)
    a = bool(j["c1"] and j["c3"] and j["c4"] and j["c5"])
    b = bool(inc["mean"] > 0) if inc["mean"] == inc["mean"] else False
    c = bool(j["c2"])
    return dict(a=a, b=b, c=c, eligible=a and b and c, measure=m, judge=j, increment=inc)


def final_eligibility(rules_file, dev_sig, M):
    """(適格か, 詳細)。V2bは (a)(b)(c)。V2(および登録外のルール)は、devでTier1(c1〜c5)を満たさなければ不適格。"""
    v2b = M.get("v2b")
    if v2b and _sha(rules_file) == v2b["rules"]["sha256"]:
        el = eligibility(dev_sig, load_K_v2b())
        return el["eligible"], el
    K = yaml.safe_load(Path(CRIT_V2).read_text(encoding="utf-8"))
    j = EC.judge_dev(EC.measure(dev_sig, K["baselines"]["tier1"], K), K)
    return bool(j["dev_pass"]), {"judge": j, "note": "V2(旧版): devでTier1を満たしていなければ最終検証の候補になれない"}


def paired_within(sig, mask, h, K, min_side=3):
    """記述的な副次分析: mask内で Quality HIGH と LOW の差(同日内)。各側 min_side 件以上の日だけ。"""
    d, col, S = sig[mask], f"ret_{h}", K["bootstrap"]
    hi = d[d.q_status == "HIGH"].groupby("date")[col].agg(["mean", "count"])
    lo = d[d.q_status == "LOW"].groupby("date")[col].agg(["mean", "count"])
    j = hi.join(lo, lsuffix="_h", rsuffix="_l", how="inner")
    j = j[(j["count_h"] >= min_side) & (j["count_l"] >= min_side)]
    diff = (j["mean_h"] - j["mean_l"]).sort_index()
    lo_, hi_ = EC.block_ci(diff.values, S["block_length_dates"], S["n_resamples"], S["seed"], S["ci"])
    return dict(n_days=len(diff), mean=float(diff.mean()) if len(diff) else np.nan, ci_lo=lo_, ci_hi=hi_)


def report_v2b(K, dev_sig, final_sig=None):
    ci_label = f"{K['bootstrap']['ci'] * 100:g}%区間"
    rep = EC.report(K, dev_sig, final_sig).replace("95%区間", ci_label)   # 区間の水準の表示だけ置き換える(計算はKの設定どおり)
    el = eligibility(dev_sig, K)
    mk = lambda x: "○" if x else "×"  # noqa: E731
    inc, h = el["increment"], K["primary_test"]["horizon"]
    out = [rep, "", "## V2bの最終検証へ進む条件（事前登録）",
           f"- (a) c1・c3・c4・c5: {mk(el['a'])}",
           f"- (b) Qualityの上乗せ V2b_A − Timing高のみ（{h}後・同日内）: 平均 {inc['mean']:+.2%} / {ci_label} [{inc['ci_lo']:+.2%}, {inc['ci_hi']:+.2%}] / {inc['n_days']}日 → 0超: {mk(el['b'])}",
           f"- (c) c2（{ci_label}の下限が0超・40日以上）: {mk(el['c'])}",
           f"- **最終検証に進める: {'はい' if el['eligible'] else 'いいえ（最終検証は行わず、封印のまま『優位性を示せなかった』と報告する）'}**", "",
           "## 記述的な副次分析（選択基準には使わない）"]
    G = dev_sig
    for name, mask in (("RSI<30の中で Quality HIGH − LOW", G.simple_rsi), ("Timing高(v1判定A/B)の中で Quality HIGH − LOW", G.grade.isin(["A", "B"]))):
        p = paired_within(G, mask, h, K)
        if p["n_days"] == 0:
            out.append(f"- {name}（{h}後・同日内）: 各側3件以上ある日が無く、算出できません")
        else:
            out.append(f"- {name}（{h}後・同日内、各側3件以上の{p['n_days']}日）: 平均 {p['mean']:+.2%} / {ci_label} [{p['ci_lo']:+.2%}, {p['ci_hi']:+.2%}]")
    out.append("- ※RSI<30は件数が少なく検出力が低い。方向の参考にとどめる。")
    return "\n".join(out)
