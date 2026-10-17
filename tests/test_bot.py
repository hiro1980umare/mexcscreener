"""通知Botのテスト(ネットワーク不要)。 python tests/test_bot.py
  * バックテストと同じ計算か / 通知文の数値が独立した再計算と一致するか / 取れない情報を推測で補わないか
  * 禁止表現 / 凍結ファイルの検査 / 記録が送信より前に保存されるか / Telegram送信の失敗処理とトークンの秘匿
  * 実績の追跡 / 自己検査が不一致を検出できるか / ワークフローの構造
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import bot_selfcheck as SC  # noqa: E402
import track_forward as TF  # noqa: E402
from bot_fixtures import make_world  # noqa: E402
from src import earnings as E  # noqa: E402
from src.bot_format import check_language, earnings_view, format_candidate, grade_after_earnings, quality_rank, sort_candidates, top_percent  # noqa: E402
from src.bot_records import sha256_file, sha256_text  # noqa: E402
from src.rules import load_rules  # noqa: E402
from src.tgram import TelegramError, init_state, make_parts, pack_units, redact, send_message, send_parts, split_message, summarize_state  # noqa: E402
from src.v2 import collect_signals_v2, evaluate_day_v2  # noqa: E402

TOKEN = "123456:SECRET-TOKEN-XYZ"
_W = {}


def world():
    if not _W:
        _W["tmp"], _W["best"], _W["objs"] = make_world()
        net = Path(_W["tmp"]) / "fake_net"
        net.mkdir()
        (net / "sitecustomize.py").write_text('''
import json, os, time
import requests
time.sleep = lambda s: None
class R:
    def __init__(s, code, js): s.status_code = code; s._js = js; s.text = json.dumps(js)
    def json(s): return s._js
def post(url, json=None, timeout=None, **kw):
    log = os.environ["FAKE_TG_LOG"]
    n = len(open(log).read().splitlines()) if os.path.exists(log) else 0
    rec = {"records_exist": os.path.exists(f"bot_records/{os.environ['FAKE_ASOF']}/meta.json"), "text": json["text"], "chat_id": json["chat_id"]}
    open(log, "a").write(__import__("json").dumps(rec, ensure_ascii=False) + "\\n")
    mode = os.environ.get("FAKE_TG_MODE", "ok")
    if mode == "fail":
        return R(500, {"ok": False, "description": "boom " + url})
    if mode == "fail_after_1" and n >= 1:
        return R(500, {"ok": False, "description": "boom " + url})
    if mode == "429" and n == 0:
        return R(429, {"ok": False, "parameters": {"retry_after": 0}})
    return R(200, {"ok": True, "result": {"message_id": 100 + n}})
requests.post = post
''')
    return _W


def run(*args, cwd=None, env=None, check=False):
    e = dict(os.environ, **(env or {}))
    r = subprocess.run([sys.executable, *args], cwd=cwd or world()["tmp"], capture_output=True, text=True, env=e)
    if check and r.returncode != 0:
        raise SystemExit(f"失敗: {args}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    return r


def dry(asof, *extra):
    return run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--dry-run", "--no-earnings", *extra)


def strip_no(t):
    """送信された通の先頭の「[k/n]」行を除く(2通以上のときだけ付く)。"""
    return re.sub(r"\A\[\d+/\d+\]\n", "", t)


def body_lines(texts):
    return [x for t in texts for x in strip_no(t).splitlines() if x.strip()]


def sections(tmp, asof):
    d = Path(tmp) / "bot_records" / f"dryrun_{asof}"
    return pd.read_csv(d / "signals.csv.gz", index_col=0), pd.read_csv(d / "notified.csv", index_col=0), d


# ---------------------------------------------------------------- 計算の一致
def test_live_function_equals_backtest_function():
    """Botの1日分の判定は、バックテスト(collect_signals_v2)の同じ日の判定と、全ての列で完全に一致する。"""
    panel, ind, sec, R1, R2 = world()["objs"]
    i0 = len(ind.dates) - 300                                   # 6か月先まである日
    live = evaluate_day_v2(panel, ind, R1, R2, sec, i0)
    d0 = str(ind.dates[i0].date())
    bt, _ = collect_signals_v2(panel, ind, R1, R2, sec, d0, d0, log=lambda s: None)
    assert len(bt) == len(live) > 50
    bt = bt.set_index("ticker")
    cols = [c for c in live.columns if c in bt.columns]
    assert {"score", "grade", "q_status", "v2_grade", "mcap_est", "qb_prof"} <= set(cols)
    pd.testing.assert_frame_equal(live[cols].sort_index(), bt[cols].sort_index())


# ---------------------------------------------------------------- 通知文の正確性
def test_message_numbers_match_independent_recalculation():
    W = world()
    asof = W["best"][0]
    r = dry(asof, "--write-records")
    assert r.returncode == 0, r.stderr[-1500:]
    allr, shown, d = sections(W["tmp"], asof)
    msg = (d / "message.txt").read_text(encoding="utf-8")
    assert len(shown) >= 5 and {"A_QualityHigh_TimingHigh", "U_QualityUndetermined_TimingHigh"} <= set(shown.section)
    panel, ind, sec, R1, R2 = W["objs"]
    i = int(np.flatnonzero(ind.dates == pd.Timestamp(asof))[0])
    n_checked = 0
    for t, row in shown.iterrows():
        j = ind.tindex[t]
        c, v = panel.close.values[: i + 1, j], panel.volume.values[: i + 1, j]
        spy = panel.spy_close.reindex(ind.dates).values[: i + 1]
        m = SC.indep_indicators(c[np.isfinite(c)], v[np.isfinite(c)], spy)                     # 本体とは別の計算
        block = msg[msg.index(f") {t}  終値"):].split("\n   決算:")[0]
        px = panel.close_raw.values[i, j]
        for want in (f"終値 ${px:,.2f}", f"RSI {m['rsi']:.1f}", f"高値から{m['dd'] * 100:+.1f}%", f"50日線比{(c[-1] / m['sma50'] - 1) * 100:+.1f}%",
                     f"200日線比{(c[-1] / m['sma200'] - 1) * 100:+.1f}%", f"出来高比{m['vol_ratio']:.2f}", f"対SPY(20日){m['rel20'] * 100:+.1f}%", f"スコア{row.score:.0f}/100"):
            assert want in block, (t, want, block)
        if row.q_status == "UNDET":
            assert "Quality: 判定不能(理由:" in block and row.q_reason in block
        else:
            ind_prof = SC.indep_prof(sec[t], asof)[0]
            assert f"営業利益/総資産 {ind_prof * 100:+.1f}%" in block and f"Quality: {row.q_status}(" in block
        n_checked += 1
    assert n_checked == len(shown)


def test_missing_information_is_not_estimated():
    W = world()
    asof = W["best"][0]
    dry(asof, "--write-records")
    allr, shown, d = sections(W["tmp"], asof)
    msg = (d / "message.txt").read_text(encoding="utf-8")
    assert msg.count("決算: 取得できず(判定不能)") == len(shown)                       # 決算日を取得していないので、全て『取得できず』
    assert (shown.earnings_date.fillna("") == "").all() and shown.days_to_earnings.isna().all()   # 決算日を推測で入れていない
    und = allr[allr.q_status == "UNDET"]
    assert len(und) > 0 and und.qb_prof.isna().all() and und.q_score.isna().all()    # 判定不能の銘柄に、Qualityの値を入れていない
    assert (und.v2_grade.isin(["C", "D"])).all()                                       # 判定不能は最高でもC
    assert allr.loc[~allr.notified.astype(bool), "earnings_date"].fillna("").eq("").all()   # 表示していない銘柄の決算日は未取得のまま


def test_earnings_rule_and_display():
    W = world()
    asof = W["best"][0]
    allr, shown, _ = sections(W["tmp"], asof) if (Path(W["tmp"]) / "bot_records" / f"dryrun_{asof}").exists() else (None, None, None)
    dry(asof, "--write-records") if shown is None else None
    allr, shown, d = sections(W["tmp"], asof)
    a = [t for t in shown.index if shown.loc[t, "section"].startswith("A_")]
    ed = Path(W["tmp"]) / "earn.json"
    near, far = a[0], a[1]
    ed.write_text(json.dumps({near: str(date.fromisoformat(asof) + timedelta(days=3)), far: str(date.fromisoformat(asof) + timedelta(days=30))}))
    r = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--dry-run", "--earnings-file", "earn.json")
    assert r.returncode == 0, r.stderr[-800:]
    out = r.stdout
    blk = lambda t: out[out.index(f") {t}  終値"):].split("\n")  # noqa: E731
    nb, fb = "\n".join(blk(near)[:6]), "\n".join(blk(far)[:6])
    assert "⚠決算まで7日以内" in nb and "あと3日" in nb and "1段階下げて" in nb                  # v1に凍結した決算前ルール
    assert "あと30日" in fb and "⚠" not in fb and "1段階下げて" not in fb
    assert "決算まで7日以内の表示銘柄: 1銘柄" in out
    R1 = load_rules(ROOT / "config/rules_v1.yaml")
    assert grade_after_earnings("A", True) == "B" and grade_after_earnings("C", True) == "D" and grade_after_earnings("A", False) == "A"
    assert earnings_view(None, date(2026, 1, 1), R1)[0] == "決算: 取得できず(判定不能)"


def test_language_has_no_forbidden_expressions_and_has_disclaimers():
    W = world()
    spec = yaml.safe_load((ROOT / "config/bot_spec.yaml").read_text(encoding="utf-8"))
    assert {"勝てる", "高確率", "必ず", "おすすめ", "買い時"} <= set(spec["language"]["forbidden"])
    assert check_language("この銘柄は高確率で上がる", spec["language"]["forbidden"]) and not check_language("条件に該当した銘柄の一覧", spec["language"]["forbidden"])
    panel, ind, *_ = W["objs"]
    dates = [str(ind.dates[k].date()) for k in (len(ind.dates) - 1, len(ind.dates) - 8, len(ind.dates) - 40, len(ind.dates) - 120, len(ind.dates) - 200)]
    for asof in dates:                                                                  # 区分が空の日も含めて
        r = dry(asof)
        assert r.returncode == 0, (asof, r.stderr[-500:])
        assert not check_language(r.stdout, spec["language"]["forbidden"]), asof
        assert "将来リターンの優位性は確認されていません" in r.stdout and "投資判断はご自身で行ってください" in r.stdout
        assert "並びはTimingスコア順で、期待リターンの順位ではありません" in r.stdout


def test_format_handles_missing_values():
    R1 = load_rules(ROOT / "config/rules_v1.yaml")
    r = pd.Series({"close_raw": 10.0, "v2_grade": "C", "grade": "A", "score": 80.0, "rsi": np.nan, "dd_high": -0.1, "vs_sma50": np.nan, "vs_sma200": 0.1,
                   "vol_ratio": np.nan, "rel20": np.nan, "q_status": "UNDET", "q_reason": "欠損: 純利益", "t_PULLBACK": True}, name="ZZZ")
    t = format_candidate(1, r, None, date(2026, 1, 1), R1)
    assert t.count("取得できず") >= 4 and "nan" not in t.lower() and "判定不能(理由: 欠損: 純利益)" in t
    s = pd.Series([0.9, 0.5, np.nan, 0.1], index=list("abcd"))
    tp = top_percent(s)
    assert tp["a"] == 34 and tp["d"] == 100 and np.isnan(tp["c"])                       # 3銘柄中の1位 = 上位34%(切り上げ)


# ---------------------------------------------------------------- 記録・送信
def test_records_saved_before_send_and_chunking():
    W = world()
    asof = W["best"][0]
    log = Path(W["tmp"]) / "tg.log"
    log.unlink(missing_ok=True)
    env = {"PYTHONPATH": str(Path(W["tmp"]) / "fake_net"), "FAKE_TG_LOG": str(log), "FAKE_ASOF": asof, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "999"}
    r = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--no-earnings", env=env)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) >= 2 and all(c["records_exist"] for c in calls)                  # ★記録は、最初の送信より前に保存済み
    assert all(len(c["text"]) <= 3800 and c["chat_id"] == "999" for c in calls)
    d = Path(W["tmp"]) / "bot_records" / asof
    n_calls = len(calls)
    assert all(c["text"].startswith(f"[{k}/{n_calls}]\n") for k, c in enumerate(calls, 1))     # 2通以上は「[1/3]」の番号つき
    assert body_lines(c["text"] for c in calls) == [x for x in (d / "message.txt").read_text(encoding="utf-8").splitlines() if x.strip()]   # 番号と空行以外は全文と一致
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["send_status"] == "sent" and len(meta["telegram_message_ids"]) == len(calls) and meta["mode"] == "live"
    for f, h in meta["files"].items():                                                  # 記録の改ざん検知
        assert sha256_file(d / f) == h
    assert meta["message_sha256"] == sha256_text((d / "message.txt").read_text(encoding="utf-8"))
    for k in ("rules", "git_commit", "manifest_sha8", "price_last_date", "sec_updated", "n_gate", "n_quality_determinable", "spec_version"):
        assert k in meta
    assert meta["rules"]["rules_v2b_sha8"] == sha256_file(ROOT / "config/rules_v2b.yaml")[:8]
    # 後から成績を検証できる列が、全てのGate通過銘柄について保存されている
    allr = pd.read_csv(d / "signals.csv.gz", index_col=0)
    need = ["asof", "close_raw", "score", "t_grade", "v2_grade", "q_status", "q_reason", "qb_prof", "qb_accr", "qb_dil", "q_score", "q_top_pct", "rsi", "dd_high",
            "vs_sma50", "vs_sma200", "vol_ratio", "rel20", "rf_count", "types", "primary_type", "earnings_date", "earnings_status", "section", "notified",
            "rules_version", "rules_v2b_sha8", "rules_v1_sha8", "report_sha8"]
    assert not [c for c in need if c not in allr.columns] and len(allr) == meta["n_gate"] and (allr.rules_version == "v2b").all()
    assert allr.notified.astype(bool).sum() == meta["n_notified"] and (allr["asof"] == asof).all()
    assert TOKEN not in r.stdout + r.stderr + (d / "meta.json").read_text(encoding="utf-8") + (d / "message.txt").read_text(encoding="utf-8")
    # 同じ判定日は二重に通知しない
    n = len(log.read_text().splitlines())
    r2 = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--no-earnings", env=env)
    assert r2.returncode == 0 and "処理済み" in r2.stdout and len(log.read_text().splitlines()) == n


def test_rehearsal_sends_but_is_kept_separate():
    W = world()
    panel, ind, *_ = W["objs"]
    asof = str(ind.dates[len(ind.dates) - 70].date())
    log = Path(W["tmp"]) / "tg3.log"
    log.unlink(missing_ok=True)
    env = {"PYTHONPATH": str(Path(W["tmp"]) / "fake_net"), "FAKE_TG_LOG": str(log), "FAKE_ASOF": f"rehearsal_{asof}", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "999"}
    args = ["run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--no-earnings", "--rehearsal"]
    r = run(*args, env=env)
    assert r.returncode == 0, r.stderr[-600:]
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert calls and calls[0]["records_exist"] and strip_no(calls[0]["text"]).startswith("【リハーサル】過去のデータ")
    d = Path(W["tmp"]) / "bot_records" / f"rehearsal_{asof}"
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "rehearsal" and meta["send_status"] == "sent" and not (Path(W["tmp"]) / "bot_records" / asof).exists()   # 本番の記録(判定日名)は作らない
    assert not TF.load_records(Path(W["tmp"]) / "bot_records")["asof"].astype(str).eq(asof).any()      # 成績の追跡に含めない
    assert run(*args, env=env).returncode == 1 and len(log.read_text().splitlines()) == len(calls)   # 2回目の送信は止める
    assert run(*args[:-1], "--dry-run", "--rehearsal", env=env).returncode == 1                       # dry-runとの同時指定は不可


def test_send_failure_keeps_records_and_hides_token():
    W = world()
    panel, ind, *_ = W["objs"]
    asof = str(ind.dates[len(ind.dates) - 30].date())
    log = Path(W["tmp"]) / "tg2.log"
    log.unlink(missing_ok=True)
    env = {"PYTHONPATH": str(Path(W["tmp"]) / "fake_net"), "FAKE_TG_LOG": str(log), "FAKE_ASOF": asof, "FAKE_TG_MODE": "fail", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "999"}
    r = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--no-earnings", env=env)
    assert r.returncode == 5 and "送信失敗" in r.stderr
    d = Path(W["tmp"]) / "bot_records" / asof
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["send_status"].startswith("failed") and (d / "signals.csv.gz").exists()   # 送信に失敗しても記録は残る
    assert TOKEN not in r.stdout + r.stderr + json.dumps(meta, ensure_ascii=False)       # トークンは出力にも記録にも出ない


def test_telegram_unit():
    class Resp:
        def __init__(self, code, js): self.status_code, self._js, self.text = code, js, json.dumps(js)
        def json(self): return self._js
    class Sess:
        def __init__(self, seq): self.seq, self.calls = list(seq), []
        def post(self, url, json=None, timeout=None):
            self.calls.append((url, json)); x = self.seq.pop(0) if self.seq else Resp(200, {"ok": True, "result": {"message_id": 7}})
            if isinstance(x, Exception): raise x
            return x
    waits = []
    long_text = "\n".join(f"行{k} " + "あ" * 100 for k in range(120))
    assert all(len(c) <= 3800 for c in split_message(long_text)) and "\n".join(split_message(long_text)) == long_text
    assert split_message("x" * 9000, 3800) == ["x" * 3800, "x" * 3800, "x" * 1400]
    s = Sess([Resp(429, {"parameters": {"retry_after": 3}}), Resp(500, {"ok": False}), Resp(200, {"ok": True, "result": {"message_id": 5}})])
    assert send_message(TOKEN, "1", "hello", session=s, sleep=waits.append) == [5] and waits == [3, 4]       # 429のretry_afterに従う
    s = Sess([ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/sendMessage")] * 5)
    try:
        send_message(TOKEN, "1", "hello", session=s, sleep=lambda x: None)
        raise SystemExit("例外が出るはず")
    except TelegramError as e:
        assert TOKEN not in str(e) and "***" in str(e)
    try:
        send_message("", "1", "x")
        raise SystemExit("例外が出るはず")
    except TelegramError as e:
        assert "未設定" in str(e)
    assert redact(f"url/bot{TOKEN}/x", TOKEN) == "url/bot***/x"


# ---------------------------------------------------------------- 決算日の取得
def test_earnings_reconcile_and_sources():
    t = date(2026, 10, 10)
    assert E.pick_next(["2026-10-01", "2026-11-05", date(2026, 10, 20)], t) == date(2026, 10, 20) and E.pick_next([], t) is None and E.pick_next(None, t) is None
    assert E.to_date(pd.Timestamp("2026-10-20 09:00")) == date(2026, 10, 20) and E.to_date("2026/10") is None
    d1, d2 = date(2026, 10, 20), date(2026, 10, 21)
    assert E.reconcile(d1, d2)["status"] == "CONFIRMED" and E.reconcile(d1, None)["status"] == "SINGLE"
    c = E.reconcile(d1, date(2026, 11, 20))
    assert c["status"] == "CONFLICT" and c["date"] == d1                              # 不一致は早いほうを表示(決算前の注意を漏らさない)
    u = E.reconcile(None, None)
    assert u["date"] is None and u["status"] == "UNAVAILABLE" and "判定不能" in u["note"]
    out = E.get_earnings(["A", "B", "C"], t, "tok", fetch_yf_fn=lambda tk, today: ({"A": d1, "B": None, "C": None}[tk], None if tk == "A" else "yfinance: X"),
                         fetch_fh_fn=lambda tk, today, tok: ({"A": d1, "B": d2, "C": None}[tk], None), sleep=lambda s: None)
    assert [out[k]["status"] for k in "ABC"] == ["CONFIRMED", "SINGLE", "UNAVAILABLE"] and out["C"]["date"] is None and out["B"]["errors"] == ["yfinance: X"]

    summ = E.summarize_earnings(out)
    assert "CONFIRMED 1" in summ and "SINGLE 1" in summ and "UNAVAILABLE 1" in summ and "yfinance: X x" in summ     # 取得できなかった理由が分かる
    nokey = E.get_earnings(["A"], t, "", fetch_yf_fn=lambda tk, today: (d1, None), sleep=lambda s: None)
    assert nokey["A"]["status"] == "SINGLE" and "finnhub: トークン未設定" in nokey["A"]["errors"]               # キーが渡っていない場合は、理由に出る

    class FS:
        def __init__(self, code, js): self.code, self.js = code, js
        def get(self, url, params=None, headers=None, timeout=None):
            if isinstance(self.code, Exception): raise self.code
            return type("R", (), {"status_code": self.code, "json": lambda s: self.js})()
    assert E.fetch_finnhub("A", t, "tok", FS(200, {"earningsCalendar": [{"date": "2026-10-28"}, {"date": "2026-10-01"}]})) == (date(2026, 10, 28), None)
    assert E.fetch_finnhub("A", t, "tok", FS(403, {}))[0] is None and "403" in E.fetch_finnhub("A", t, "tok", FS(403, {}))[1]
    assert E.fetch_finnhub("A", t, "tok", FS(ConnectionError("x"), {}))[0] is None and E.fetch_finnhub("A", t, "", FS(200, {}))[1] == "finnhub: トークン未設定"


# ---------------------------------------------------------------- 凍結
def test_manifest_blocks_changes_to_frozen_files():
    W = world()
    tmp, asof = Path(W["tmp"]), W["best"][0]
    for f, add in (("docs/research_report.md", "\n追記"), ("config/rules_v2b.yaml", "\n# x"), ("config/bot_spec.yaml", "\n# x"), ("config/criteria_v2b.yaml", "\n# x")):
        p = tmp / f
        orig = p.read_text(encoding="utf-8")
        p.write_text(orig + add, encoding="utf-8")
        r = dry(asof)
        assert r.returncode == 2 and f in r.stderr and "変更されています" in r.stderr, (f, r.stderr[-300:])
        p.write_text(orig, encoding="utf-8")
    assert dry(asof).returncode == 0                                                    # 元に戻せば動く
    mf = tmp / "config/bot_manifest.json"
    txt = mf.read_text(encoding="utf-8"); mf.unlink()
    assert dry(asof).returncode != 0
    mf.write_text(txt, encoding="utf-8")


def test_freeze_bot_checks_report_and_supersede():
    tmp, _, _ = make_world(run_freeze=False)
    rep = tmp / "docs/research_report.md"
    orig = rep.read_text(encoding="utf-8")
    rep.write_text(orig.replace(sha256_file(ROOT / "config/rules_v1.yaml")[:8], "00000000"), encoding="utf-8")
    r = run("freeze_bot.py", cwd=tmp)
    assert r.returncode != 0 and "報告書と実ファイルが一致していません" in r.stdout + r.stderr
    rep.write_text(orig, encoding="utf-8")
    assert run("freeze_bot.py", cwd=tmp).returncode == 0
    r = run("freeze_bot.py", cwd=tmp)
    assert r.returncode != 0 and "登録済み" in r.stdout + r.stderr                       # 2回目は拒否
    spec = tmp / "config/bot_spec.yaml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('"買い時"', '"買い時", "候補リスト"', 1), encoding="utf-8")   # 必ず通知文に含まれる語を禁止語に加える
    assert run("freeze_bot.py", "--supersede", cwd=tmp).returncode == 0 and len(list((tmp / "config/bot_manifest_history").glob("*.json"))) == 1
    asof = "2018-09-28"
    r = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--dry-run", "--no-earnings", "--write-records", cwd=tmp)
    assert r.returncode == 4 and "禁止表現" in r.stderr and not (tmp / "bot_records").exists()   # 禁止表現があれば、送信も記録もしない


def test_stale_data_is_not_notified():
    W = world()
    r = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--today", "2019-03-01", "--dry-run", "--no-earnings")
    assert r.returncode == 3 and "古すぎ" in r.stderr


# ---------------------------------------------------------------- 実績の追跡
def test_forward_tracking_matches_manual_calculation():
    W = world()
    panel, ind, sec, R1, R2 = W["objs"]
    d_old, d_new = str(ind.dates[len(ind.dates) - 300].date()), str(ind.dates[len(ind.dates) - 60].date())
    for d in (d_old, d_new):
        assert dry(d, "--write-records").returncode == 0
    rec = TF.load_records(Path(W["tmp"]) / "bot_records", include_dryrun=True)
    assert rec["asof"].nunique() >= 2
    fwd = TF.compute_forward(rec, panel, R2)
    i = int(np.flatnonzero(ind.dates == pd.Timestamp(d_old))[0])
    row = fwd[(fwd["asof"] == d_old) & fwd.section.fillna("").str.startswith("A_")].iloc[0]
    j, cost = ind.tindex[row.ticker], R2["backtest"]["cost_per_side_bps"] / 1e4
    O, C = panel.open.values, panel.close.values
    for nm, h in (("1m", 21), ("3m", 63), ("6m", 126)):
        assert np.isclose(row[f"ret_{nm}"], C[i + 1 + h, j] / O[i + 1, j] - 1 - 2 * cost)       # 翌営業日の始値で買い、h営業日後の終値で売る
    new = fwd[fwd["asof"] == d_new]
    assert new.ret_1m.notna().all() and new.ret_6m.isna().all()                                 # 6か月が経過していない記録は『未経過』(NaN)
    txt = TF.review(fwd)
    assert "A区分" in txt and "参考" in txt and "意味はありません" in txt
    assert "まだ期間が経過した記録がありません" in TF.review(fwd.assign(ret_1m=np.nan, ret_3m=np.nan, ret_6m=np.nan))


# ---------------------------------------------------------------- 自己検査
def test_selfcheck_passes_and_detects_injected_errors():
    W = world()
    panel, ind, sec, R1, R2 = W["objs"]
    i = len(ind.dates) - 1
    df = evaluate_day_v2(panel, ind, R1, R2, sec, i)
    ok = SC.run_checks(panel, ind, df, sec, i, R2, sample=60)
    assert [s for _, s, _ in ok] == ["PASS", "PASS", "PASS", "PASS"], ok
    bad = copy.deepcopy(ind)
    bad.a["rsi"][i] += 0.5                                                              # 指標のずれ
    assert dict((n[:2], s) for n, s, _ in SC.run_checks(panel, bad, df, sec, i, R2, sample=60))["C1"] == "FAIL"
    df2 = df.copy(); df2["mcap_est"] *= 1.01                                           # 時価総額のずれ
    assert dict((n[:2], s) for n, s, _ in SC.run_checks(panel, ind, df2, sec, i, R2, sample=60))["C3"] == "FAIL"
    df3 = df.copy(); df3.loc[df3.q_status == "UNDET", "v2_grade"] = "A"                # 判定不能がAになる
    assert dict((n[:2], s) for n, s, _ in SC.run_checks(panel, ind, df3, sec, i, R2, sample=60))["C4"] == "FAIL"
    sec2 = copy.deepcopy(sec)                                                           # 営業利益の値を書き換える
    for t in list(sec2)[:140]:
        for _tag, rows in sec2[t]["flow"]["op_income"]:
            for r in rows:
                r["val"] = r["val"] * 1.5
    assert dict((n[:2], s) for n, s, _ in SC.run_checks(panel, ind, df, sec2, i, R2, sample=60))["C2"] == "FAIL"
    r = run("bot_selfcheck.py", "--data-dir", "data_v2", "--sample", "40")
    assert r.returncode == 0 and "FAIL" not in r.stdout


def test_live_price_path_with_mocked_yfinance():
    """本番の株価取得経路(download_v2 -> assemble -> PanelV2)を、yfinanceをモックして通す。"""
    import types
    from src.bot_data import get_panel_live
    idx = pd.bdate_range("2025-06-01", "2026-10-07")
    rng = np.random.default_rng(1)

    def download(tickers, start=None, **kw):
        tickers = [tickers] if isinstance(tickers, str) else list(tickers)
        cols = {}
        for t in tickers:
            p = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx))))
            for f in ("Open", "High", "Low", "Close", "Adj Close"):
                cols[(f, t)] = pd.Series(p, index=idx)
            cols[("Volume", t)] = pd.Series(3e6, index=idx)
        df = pd.DataFrame(cols)
        df.columns = pd.MultiIndex.from_tuples(df.columns)
        return df
    old = sys.modules.get("yfinance")
    mod = types.ModuleType("yfinance")
    mod.download, mod.Ticker, mod.shared = download, lambda t: types.SimpleNamespace(splits=pd.Series([], dtype=float)), types.SimpleNamespace(_ERRORS={})
    sys.modules["yfinance"] = mod
    try:
        panel, cov = get_panel_live(["AAA", "BBB"], date(2026, 10, 8), 450, tempfile.mkdtemp(), log=lambda s: None)
    finally:
        if old is not None:
            sys.modules["yfinance"] = old
        else:
            del sys.modules["yfinance"]
    assert list(panel.close.columns) == ["AAA", "BBB"] and panel.close.index.max() == pd.Timestamp("2026-10-07") and len(panel.close) > 300
    assert panel.close_raw.notna().all().all() and panel.spy_close.notna().all()


def test_sec_bundle_gives_identical_results():
    """SEC財務を1つのファイルにまとめても(読み込みを速くするため)、通知文は1文字も変わらない。"""
    W = world()
    asof = W["best"][0]
    r1 = run("bundle_sec.py", "--sec-dir", "data_v2/sec_v2b", "--out", "sec_bundle.pkl")
    assert r1.returncode == 0 and "保存しました" in r1.stdout, r1.stderr[-500:]
    a = dry(asof)
    b = run("run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--sec-bundle", "sec_bundle.pkl", "--asof", asof, "--today", asof, "--dry-run", "--no-earnings")
    assert a.returncode == 0 and b.returncode == 0, b.stderr[-600:]
    strip = lambda o: re.sub(r"SEC財務キャッシュ更新 \S+", "", o.split("[dry-run]")[0])  # noqa: E731
    assert strip(a.stdout) == strip(b.stdout)
    c = run("bot_selfcheck.py", "--data-dir", "data_v2", "--sec-bundle", "sec_bundle.pkl", "--sample", "30")
    assert c.returncode == 0 and "FAIL" not in c.stdout


def test_partial_sec_data_stops_the_bot():
    """SEC財務のコピーが途中で止まった状態(一部のファイルしかない)では、誤った通知を出さずに止まる。"""
    W = world()
    d = Path(W["tmp"]) / "data_v2/sec_v2b"
    files = sorted(d.glob("*.pkl"))
    moved = files[: int(len(files) * 0.7)]
    hold = Path(W["tmp"]) / "sec_hold"
    hold.mkdir(exist_ok=True)
    for f in moved:
        f.rename(hold / f.name)
    try:
        r = dry(W["best"][0])
        assert r.returncode == 1 and "SEC財務が対象銘柄の" in r.stderr and "途中で止まった" in r.stderr and "Traceback" not in r.stderr
    finally:
        for f in moved:
            (hold / f.name).rename(d / f.name)
    assert dry(W["best"][0]).returncode == 0


def test_missing_universe_file_stops_with_instructions():
    W = world()
    f = Path(W["tmp"]) / "config/bot_universe.csv"
    txt = f.read_text()
    f.unlink()
    r = dry(W["best"][0])
    f.write_text(txt)
    assert r.returncode == 1 and "export_universe.py" in r.stderr and "Traceback" not in r.stderr


# ---------------------------------------------------------------- ワークフロー
def test_workflows_structure_and_secrets():
    d = yaml.safe_load((ROOT / ".github/workflows/daily_bot.yml").read_text(encoding="utf-8"))
    w = yaml.safe_load((ROOT / ".github/workflows/weekly_sec.yml").read_text(encoding="utf-8"))
    on = d.get("on") or d.get(True)
    assert on["schedule"][0]["cron"] == "30 22 * * 1-5" and "workflow_dispatch" in on
    txt = (ROOT / ".github/workflows/daily_bot.yml").read_text(encoding="utf-8")
    for s in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY"):
        assert f"secrets.{s}" in txt
    assert not [l for l in txt.splitlines() if "echo" in l and "secrets" in l]                                   # シークレットをechoしない
    assert "dry_run" in txt and "python run_bot.py --dry-run --write-records" in txt and "bot_records" in txt
    assert "SEC_USER_AGENT" in (ROOT / ".github/workflows/weekly_sec.yml").read_text(encoding="utf-8") and "fetch_sec.py" in str(w)


# ---------------------------------------------------------------- 通知の改善(分割・番号・冒頭・同点・送信状況)
def test_parts_never_split_a_candidate_and_header_comes_first():
    W = world()
    asof = W["best"][0]
    assert dry(asof, "--write-records").returncode == 0
    allr, shown, d = sections(W["tmp"], asof)
    parts = json.loads((d / "message_parts.json").read_text(encoding="utf-8"))
    n = len(parts)
    assert n >= 2 and all(p.startswith(f"[{k}/{n}]\n") and len(p) <= 3800 for k, p in enumerate(parts, 1))
    head = strip_no(parts[0]).splitlines()[:6]
    assert any(x.startswith(f"実行日 {asof}") and "候補" in x for x in head) and any(x.startswith("データ品質:") for x in head)   # 冒頭: 実行日・候補数・データ品質
    for p in parts:                                                                       # どの通も、銘柄ブロック(見出し1行+5行)を途中で切らない
        L = strip_no(p).splitlines()
        assert not L[0].startswith(" "), L[0]
        k = 0
        while k < len(L):
            if re.match(r"\d+\) \S+  終値", L[k]):
                assert len(L) >= k + 6 and all(x.startswith("   ") for x in L[k + 1:k + 6]) and L[k + 5].startswith("   決算:"), (k, L[k:k + 6])
                k += 6
            else:
                k += 1
    assert sum(len(re.findall(r"^\d+\) \S+  終値", strip_no(p), flags=re.M)) for p in parts) == len(shown)   # 全ての表示銘柄が、ちょうど1回ずつ
    assert make_parts(["a", "b"]) == ["a\nb"]                                              # 1通なら番号は付けない


def test_pack_units_keeps_units_whole():
    units = [f"U{k}\n" + "x" * 90 for k in range(10)] + ["", "tail"]
    ch = pack_units(units, 300)
    assert all(len(c) <= 300 for c in ch) and len(ch) >= 4
    assert all(any(c.count(f"U{k}\n" + "x" * 90) == 1 for c in ch) for k in range(10))    # 各塊は丸ごと1通の中にある
    assert pack_units(["z" * 700], 300) == ["z" * 300, "z" * 300, "z" * 100]               # 1塊が上限超のときだけ強制分割


def test_tie_break_is_quality_rank_then_ticker_and_counts_ties():
    q = pd.Series([0.5, 0.5, 0.9, np.nan, 0.1, np.nan, np.nan, 0.7], index=list("BACDEFGH"))
    df = pd.DataFrame({"score": [80, 80, 80, 70, 80, 80, 80, 90], "q_score": q})
    df["q_rank"] = quality_rank(df.q_score)
    out = sort_candidates(df)
    # 90: H / 80: C(Q1位) → A,B(Q2位で同順位 → ティッカー順) → E(Q4位) → F,G(判定不能は最後 → ティッカー順) / 70: D
    assert list(out.index) == list("HCABEFGD")
    assert out.timing_tie_n.to_dict() == {"H": 1, "C": 6, "A": 6, "B": 6, "E": 6, "F": 6, "G": 6, "D": 1}
    assert list(sort_candidates(df.iloc[0:0].assign(q_rank=[])).index) == []                # 空でも落ちない


def test_ties_in_records_follow_the_rule():
    W = world()
    asof = W["best"][0]
    assert dry(asof, "--write-records").returncode == 0
    allr, shown, d = sections(W["tmp"], asof)
    assert {"q_rank", "timing_tie_n"} <= set(allr.columns)
    msg = (d / "message.txt").read_text(encoding="utf-8")
    ties_seen = 0
    for sec, mask in (("A_QualityHigh_TimingHigh", allr.v2_grade == "A"), ("U_QualityUndetermined_TimingHigh", (allr.q_status == "UNDET") & allr.t_grade.isin(["A", "B"]))):
        g = allr[mask]
        q = g.q_score.dropna()
        rk = lambda t: 1 + int((q > q[t]).sum()) if t in q.index else 10 ** 9                  # 本体とは別の書き方での順位  # noqa: E731
        want = sorted(g.index, key=lambda t: (-g.score[t], rk(t), t))[: int((shown.section == sec).sum())]
        seg = msg.split("■ Timing高だが")                                                   # 通知文での表示順(notified.csvは判定時の並びで保存される)
        seg = seg[0] if sec.startswith("A_") else seg[1].split("■ 集計")[0]
        assert re.findall(r"^\d+\) (\S+)  終値", seg, flags=re.M) == want, sec
        for t in want:
            n_tie = int((g.score == g.score[t]).sum())
            assert int(shown.loc[t, "timing_tie_n"]) == n_tie
            block = msg[msg.index(f") {t}  終値"):].split("\n   決算:")[0]
            assert (f"・同点{n_tie}銘柄)" in block) == (n_tie > 1)
            ties_seen += n_tie > 1
    assert ties_seen > 0 and "同点はQuality順位、それも同じならティッカー順です" in msg


def test_send_parts_records_state_and_resumes_only_unsent():
    class Resp:
        def __init__(self, code, js): self.status_code, self._js, self.text = code, js, json.dumps(js)
        def json(self): return self._js
    class Sess:
        def __init__(self, seq): self.seq, self.sent = list(seq), []
        def post(self, url, json=None, timeout=None):
            self.sent.append(json["text"]); return self.seq.pop(0)
    ok = lambda i: Resp(200, {"ok": True, "result": {"message_id": i}})  # noqa: E731
    parts, upd = ["[1/3]\np1", "[2/3]\np2", "[3/3]\np3"], []
    st = init_state(3)
    s1 = Sess([ok(11), Resp(500, {"ok": False}), Resp(500, {"ok": False}), Resp(500, {"ok": False})])
    st, err = send_parts(TOKEN, "1", parts, st, session=s1, sleep=lambda x: None, on_update=lambda x: upd.append([y["status"] for y in x]))
    assert err and [x["status"] for x in st] == ["sent", "failed", "pending"] and s1.sent == [parts[0]] + [parts[1]] * 3   # 失敗した通で止まる(順番を崩さない)
    status, ids = summarize_state(st)
    assert status.startswith("partial: 1/3通送信済み") and ids == [11] and TOKEN not in json.dumps(st, ensure_ascii=False) and upd[0] == ["sent", "pending", "pending"]
    s2 = Sess([ok(12), ok(13)])
    st, err = send_parts(TOKEN, "1", parts, st, session=s2, sleep=lambda x: None)
    assert err is None and s2.sent == parts[1:] and summarize_state(st) == ("sent", [11, 12, 13]) and st[0]["attempts"] == 1 and st[1]["attempts"] == 2   # 送信済みの通は再送しない
    st, err = send_parts("", "1", parts, init_state(3))
    assert "未設定" in err and summarize_state(st)[0].startswith("failed")


def test_partial_failure_is_recorded_and_only_unsent_parts_are_resent():
    W = world()
    tmp = Path(tempfile.mkdtemp()) / "w"
    shutil.copytree(W["tmp"], tmp)
    shutil.rmtree(tmp / "bot_records", ignore_errors=True)                              # 他のテストの記録を持ち込まない
    asof = W["best"][0]
    log = tmp / "tg_resend.log"
    env = lambda mode: {"PYTHONPATH": str(tmp / "fake_net"), "FAKE_TG_LOG": str(log), "FAKE_ASOF": asof, "FAKE_TG_MODE": mode, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "999"}  # noqa: E731
    calls = lambda: [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []  # noqa: E731
    base = ["run_bot.py", "--source", "dir", "--data-dir", "data_v2", "--asof", asof, "--today", asof, "--no-earnings"]
    d = tmp / "bot_records" / asof
    # (a) 1通目だけ届き、2通目で失敗 -> 記録が残り、送信状況が通ごとに残る
    r = run(*base, cwd=tmp, env=env("fail_after_1"))
    assert r.returncode == 5 and "送信失敗" in r.stderr and "resend_bot.py" in r.stderr
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    parts = json.loads((d / "message_parts.json").read_text(encoding="utf-8"))
    sp = [x["status"] for x in meta["send_parts"]]
    assert meta["n_parts"] == len(parts) >= 2 and sp[:2] == ["sent", "failed"] and set(sp[2:]) <= {"pending"}
    assert meta["send_status"].startswith(f"partial: 1/{len(parts)}通送信済み") and len(meta["telegram_message_ids"]) == 1 and TOKEN not in json.dumps(meta, ensure_ascii=False)
    assert [c["text"] for c in calls()][0] == parts[0] and sha256_file(d / "message_parts.json") == meta["files"]["message_parts.json"]
    n0 = len(calls())
    # (b) 同じ判定日をもう一度実行 -> 再計算も二重送信もしない。未送信があることを知らせる
    r = run(*base, cwd=tmp, env=env("ok"))
    assert r.returncode == 5 and "未送信の通が" in r.stderr and len(calls()) == n0
    # (c) 一覧だけ(送らない) / 保存した通が書き換えられていれば止まる
    r = run("resend_bot.py", "--dry-run", cwd=tmp, env=env("ok"))
    assert r.returncode == 0 and f"未送信 {len(parts) - 1}通" in r.stdout and len(calls()) == n0
    orig = (d / "message_parts.json").read_text(encoding="utf-8")
    (d / "message_parts.json").write_text(orig.replace("終値", "終価", 1), encoding="utf-8")
    r = run("resend_bot.py", cwd=tmp, env=env("ok"))
    assert r.returncode == 5 and "変更されています" in r.stderr and len(calls()) == n0
    (d / "message_parts.json").write_text(orig, encoding="utf-8")
    # (d) 再送 -> 未送信の通だけが、保存した文面のまま届く
    r = run("resend_bot.py", cwd=tmp, env=env("ok"))
    assert r.returncode == 0, r.stderr[-500:]
    assert [c["text"] for c in calls()][n0:] == parts[1:]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["send_status"] == "sent" and len(meta["telegram_message_ids"]) == len(parts) and all(x["status"] == "sent" for x in meta["send_parts"])
    assert meta["send_parts"][0]["attempts"] == 1 and TOKEN not in json.dumps(meta, ensure_ascii=False) + r.stdout + r.stderr
    n1 = len(calls())
    r = run("resend_bot.py", cwd=tmp, env=env("ok"))                                     # (e) もう未送信は無い
    assert r.returncode == 0 and "未送信の通知はありません" in r.stdout and len(calls()) == n1
    assert run(*base, cwd=tmp, env=env("ok")).returncode == 0 and len(calls()) == n1      # 全て送信済みなら、従来どおり「処理済み」で何もしない
    # (f) 旧形式の記録(通ごとの送信状況なし)は、何が届いたか分からないので触らない
    old = tmp / "bot_records" / "2018-01-02"
    old.mkdir()
    (old / "meta.json").write_text(json.dumps({"mode": "live", "send_status": "failed: boom"}), encoding="utf-8")
    before = (old / "meta.json").read_bytes()
    r = run("resend_bot.py", "--record", str(old.relative_to(tmp)), cwd=tmp, env=env("ok"))
    assert r.returncode == 1 and "旧形式" in r.stderr and (old / "meta.json").read_bytes() == before and len(calls()) == n1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK ", fn.__name__)
    print(f"{len(fns)}件すべて成功")
