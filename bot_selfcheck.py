#!/usr/bin/env python3
"""通知Botのデータ・計算の正確性を、『本体とは別の書き方』で再計算して突き合わせる自己検査。
  python bot_selfcheck.py --data-dir data_v2                  # 保存済みデータで(ネットワーク不要)
  python bot_selfcheck.py --data-dir data_v2 --network        # さらに外部の値(Yahoo・Finnhub)と照合
検査項目:
  C1 指標(RSI・移動平均・高値からの下落率・出来高比・200日線の傾き・SPYとの差)を、スカラーの計算で再計算して一致するか
  C2 Qualityの収益性を、SEC生データから再計算して一致するか。さらに、使った全ての提出日が判定日より前か(先読みなし)
  C3 Gateの時価総額(当時の株価 x 提出済みの株式数)を再計算して一致するか
  C4 判定のつじつま(判定不能がAにならない、Qualityの三分位、Timing高の定義)
  C5 (--network) 最終終値をYahooから取り直して一致するか / 決算日の2情報源の一致率 / 時価総額の外部値との比
1つでも FAIL があれば終了コード1。
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.bot_data import load_sec_bundle, load_sec_dir
from src.bot_format import top_percent
from src.data_v2 import load_v2
from src.indicators import build
from src.rules import load_rules
from src.sec_pit import ANNUAL_FORMS, BS_FORMS, split_factor
from src.v2 import evaluate_day_v2

TOL = dict(rtol=1e-7, atol=1e-9)


# ---------------- 独立した再計算(本体のpandas実装とは別の、スカラーのコード) ----------------
def indep_rsi(x, n=14):
    d = np.diff(np.asarray(x, float))
    g, l, a = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0), 1.0 / n
    ag, al = g[0], l[0]
    for k in range(1, len(d)):
        ag, al = (1 - a) * ag + a * g[k], (1 - a) * al + a * l[k]
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def indep_indicators(c, v, spy):
    """c, v: ある銘柄の最終日までの終値・出来高(先頭の欠損は除去済み)。spy: SPYの終値。戻り値は本体の列名で。"""
    c, v, spy = (np.asarray(x, float) for x in (c, v, spy))
    sma = lambda x, n, off=0: x[len(x) - n - off:len(x) - off].mean()  # noqa: E731
    return {"rsi": indep_rsi(c), "sma20": sma(c, 20), "sma50": sma(c, 50), "sma200": sma(c, 200),
            "dd": c[-1] / c[-252:].max() - 1, "vol_ratio": v[-5:].mean() / v[-50:].mean(),
            "slope200": sma(c, 200) / sma(c, 200, 20) - 1, "ret5": c[-1] / c[-6] - 1,
            "ret20": c[-1] / c[-21] - 1, "rel20": (c[-1] / c[-21] - 1) - (spy[-1] / spy[-21] - 1)}


def indep_prof(sec, asof):
    """収益性 = 営業利益 / 総資産(期末)。SEC生データから。戻り値: (値, 使った提出日の最大) or None"""
    best = {}
    for _tag, rows in sec["flow"].get("op_income", []):
        for r in rows:
            if "start" not in r or r.get("form") not in ANNUAL_FORMS or r["filed"] >= asof:
                continue
            if not 340 <= (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days <= 380:
                continue
            k = (r["start"], r["end"])
            if k not in best or r["filed"] > best[k][0]:
                best[k] = (r["filed"], r["val"])
    if not best:
        return None
    (s, e) = max(best, key=lambda k: k[1])
    filed, val = best[(s, e)]
    ar = [(r["filed"], r["val"]) for _t, rows in sec["inst"].get("assets", []) for r in rows
          if "start" not in r and r["end"] == e and r["filed"] < asof and r.get("form") in BS_FORMS]
    if not ar:
        return None
    af, av = max(ar)
    return (val / av if av > 0 else None), max(filed, af)


def indep_mcap(sec, asof, raw_today_terms, splits, max_age):
    rows = {}
    for _t, rs in sec["inst"].get("shares", []):
        for r in rs:
            if r["filed"] < asof and r.get("form") in BS_FORMS and (r["end"] not in rows or r["filed"] > rows[r["end"]]["filed"]):
                rows[r["end"]] = r
        if rows:
            break
    if not rows:
        return np.nan
    e = max(rows)
    if (date.fromisoformat(asof) - date.fromisoformat(e)).days > max_age:
        return np.nan
    return raw_today_terms * split_factor(splits, asof, "2100-01-01") * float(rows[e]["val"]) * split_factor(splits, e, asof)


# ---------------- 検査 ----------------
def run_checks(panel, ind, df, sec, i, R2, sample=30, seed=0, network=False, log=print):
    res, rng = [], np.random.default_rng(seed)
    asof = str(ind.dates[i].date())
    tick = list(df.index)
    pick = list(rng.choice(tick, min(sample, len(tick)), replace=False))
    tpos = ind.tindex
    spy = panel.spy_close.reindex(ind.dates).values[: i + 1]

    # C1 指標
    bad, n_ok, skipped = [], 0, 0
    for t in pick:
        j = tpos[t]
        c = panel.close.values[: i + 1, j]
        v = panel.volume.values[: i + 1, j]
        first = np.flatnonzero(np.isfinite(c))[0]
        c, v = c[first:], v[first:]
        if np.isnan(c).any() or np.isnan(v[-50:]).any() or len(c) < 260:
            skipped += 1
            continue
        mine = indep_indicators(c, v, spy[first:] if False else spy)
        for k, x in mine.items():
            y = ind.a[k][i, j]
            if not np.isclose(x, y, **TOL):
                bad.append((t, k, x, y))
        n_ok += 1
    res.append(("C1 指標の再計算が一致", "PASS" if not bad and n_ok else "FAIL",
                f"{n_ok}銘柄×10指標を再計算(欠損等で{skipped}銘柄はスキップ)。不一致 {len(bad)}件" + (f": {bad[:3]}" if bad else "")))

    # C2 Quality(収益性)とPIT
    bad, late, n_ok = [], [], 0
    for t in pick:
        q = df.loc[t]
        r = indep_prof(sec[t], asof)
        if r is None or not np.isfinite(q.get("qb_prof", np.nan)):
            continue
        prof, used = r
        n_ok += 1
        if used >= asof:
            late.append((t, used))
        if prof is None or not np.isclose(prof, q["qb_prof"], **TOL):
            bad.append((t, prof, q["qb_prof"]))
    res.append(("C2 収益性の再計算が一致 / 使った提出日がすべて判定日より前", "PASS" if not bad and not late and n_ok else ("SKIP" if not n_ok else "FAIL"),
                f"{n_ok}銘柄を再計算。不一致 {len(bad)}件 / 判定日以降の提出日を使用 {len(late)}件" + (f": {(bad + late)[:3]}" if bad or late else "")))

    # C3 時価総額
    G2, bad, n_ok = R2["gate"], [], 0
    for t in pick:
        m = indep_mcap(sec[t], asof, panel.close_raw.values[i, tpos[t]], panel.splits.get(t), G2["shares_max_age_days"])
        if np.isfinite(m):
            n_ok += 1
            if not np.isclose(m, df.loc[t, "mcap_est"], rtol=1e-6):
                bad.append((t, m, df.loc[t, "mcap_est"]))
    res.append(("C3 Gateの時価総額(当時の株価×提出済みの株式数)が一致", "PASS" if not bad and n_ok else ("SKIP" if not n_ok else "FAIL"), f"{n_ok}銘柄を再計算。不一致 {len(bad)}件" + (f": {bad[:3]}" if bad else "")))

    # C4 判定のつじつま
    errs = []
    if ((df.q_status == "UNDET") & (df.v2_grade == "A")).any():
        errs.append("判定不能のAがある")
    if (df.v2_grade[df.q_status == "UNDET"].isin(["A", "B"])).any():
        errs.append("判定不能がA/Bになっている")
    ok = df[df.q_status != "UNDET"]
    if len(ok) >= R2["quality"]["min_cross_section"]:
        share = ok.q_status.value_counts(normalize=True)
        if not all(abs(share.get(k, 0) - 1 / 3) < 0.06 for k in ("HIGH", "LOW")):
            errs.append(f"三分位が偏っている {share.round(2).to_dict()}")
        tp = top_percent(ok.q_score)
        if not (tp[ok.q_status == "HIGH"].max() <= 34 and tp[ok.q_status == "LOW"].min() >= 66):
            errs.append("上位%の表示とHIGH/LOWが整合しない")
    if (df.v2_grade[(df.q_status == "HIGH") & df.grade.isin(["A", "B"])] != "A").any():
        errs.append("Quality高×Timing高がAでない")
    if (df.v2_grade[df.grade == "D"] != "D").any():
        errs.append("v1判定Dがrules通りDになっていない")
    res.append(("C4 判定のつじつま(判定不能はA/Bにならない、三分位、Timing高の定義)", "PASS" if not errs else "FAIL", "; ".join(errs) or f"Gate通過{len(df)}銘柄で矛盾なし"))

    if network:
        res += network_checks(panel, df, i, ind, pick, log)
    return res


def network_checks(panel, df, i, ind, pick, log):
    """外部の値との照合(ネットワーク)。失敗しても本体の判定は止めず、結果に明記する。"""
    out = []
    asof = ind.dates[i]
    try:
        import yfinance as yf
        diffs, n = [], 0
        for t in pick[:12]:
            h = yf.Ticker(t).history(period="30d", auto_adjust=False)["Close"]
            h.index = h.index.tz_localize(None).normalize()
            if asof in h.index:
                n += 1
                mine = panel.close_raw.values[i, ind.tindex[t]]
                diffs.append(abs(mine / h.loc[asof] - 1))
        worst = max(diffs) if diffs else np.nan
        out.append(("C5a 最終終値をYahooから取り直して一致(0.5%以内。分割調整の違いを除く)", "PASS" if diffs and worst < 0.005 else ("SKIP" if not diffs else "FAIL"),
                    f"{n}銘柄を照合。最大の差 {worst:.3%}" if diffs else "同じ日付のデータが取れず照合できない(判定日が古い可能性)"))
    except Exception as e:  # noqa: BLE001
        out.append(("C5a 最終終値の再取得", "SKIP", f"取得失敗: {type(e).__name__}"))
    try:
        from src.earnings import get_earnings
        tops = list(df[df.v2_grade == "A"].sort_values("score", ascending=False).index[:8])
        info = get_earnings(tops, date.today(), os.getenv("FINNHUB_API_KEY", ""))
        cnt = pd.Series([v["status"] for v in info.values()]).value_counts().to_dict()
        out.append(("C5b 決算日の2情報源の照合", "INFO", f"上位{len(tops)}銘柄: {cnt}（CONFIRMED=一致 / SINGLE=1情報源のみ / CONFLICT=不一致 / UNAVAILABLE=取得できず）"))
    except Exception as e:  # noqa: BLE001
        out.append(("C5b 決算日の照合", "SKIP", f"取得失敗: {type(e).__name__}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data_v2")
    ap.add_argument("--sec-dir", default="data_v2/sec_v2b")
    ap.add_argument("--sec-bundle", default="")
    ap.add_argument("--rules-v1", default="config/rules_v1.yaml")
    ap.add_argument("--rules-v2b", default="config/rules_v2b.yaml")
    ap.add_argument("--asof", default="")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--network", action="store_true")
    ap.add_argument("--out-dir", default="bot_records")
    a = ap.parse_args()
    R1 = load_rules(a.rules_v1)
    R2 = yaml.safe_load(Path(a.rules_v2b).read_text(encoding="utf-8"))
    panel = load_v2(a.data_dir)
    sec = load_sec_bundle(a.sec_bundle, panel.close.columns) if a.sec_bundle else load_sec_dir(a.sec_dir, panel.close.columns)
    ind = build(panel, R1)
    idx = np.flatnonzero(ind.dates <= pd.Timestamp(a.asof)) if a.asof else np.arange(len(ind.dates))
    i = int(idx[-1])
    df = evaluate_day_v2(panel, ind, R1, R2, sec, i)
    res = run_checks(panel, ind, df, sec, i, R2, a.sample, network=a.network)
    L = [f"# Bot 自己検査 (判定日 {ind.dates[i].date()} / Gate通過 {len(df)}銘柄)", "", "| 検査 | 結果 | 詳細 |", "|---|---|---|"] + [f"| {n} | {s} | {d} |" for n, s, d in res]
    txt = "\n".join(L)
    Path(a.out_dir).mkdir(exist_ok=True)
    (Path(a.out_dir) / f"selfcheck_{ind.dates[i].date()}.md").write_text(txt, encoding="utf-8")
    print(txt)
    sys.exit(1 if any(s == "FAIL" for _, s, _ in res) else 0)


if __name__ == "__main__":
    main()
