"""通知文の組み立て。取得できない情報は推測で補わず、『判定不能』『取得できず』と書く。"""
import math

import numpy as np
import pandas as pd

from .signals import downgrade
from .tgram import split_message

TYPE_TEXT = {
    "PULLBACK": "押し目(200日線が上向きのまま高値から調整中)",
    "OVERSOLD": "売られ過ぎ(RSIが30未満)",
    "MARKET_DIP": "市場全体の調整に連れた下落",
    "RECOVERY": "下落後に戻りの兆し(RSIが反転)",
}
RF_TEXT = {
    "rf_long_downtrend": "長期下降トレンド(終値が200日線の下で、200日線も下向き)",
    "rf_idiosyncratic": "銘柄固有の弱さ(高値からの下落がSPYより25pt以上大きい)",
    "rf_deep_drawdown": "高値から40%以上の下落",
    "rf_crash_5d": "5日間で15%以上の急落",
}


def check_language(text, forbidden):
    return [w for w in forbidden if w in text]


def _pct(x, d=1, sign=True):
    return "取得できず" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:{'+' if sign else ''}.{d}f}%"


def _num(x, d=1):
    return "取得できず" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def top_percent(q_score):
    """同日・判定可能銘柄の中での合成スコアの順位 -> 『上位X%』。最良がちょうど 1/n。"""
    s = q_score.dropna()
    if s.empty:
        return pd.Series(np.nan, index=q_score.index)
    r = s.rank(ascending=False, method="min")            # 1が最良
    return np.ceil(r / len(s) * 100).reindex(q_score.index)


def quality_rank(q_score):
    """同日・判定可能銘柄の中でのQualityの順位(1が最良、同値は同順位)。判定不能はNaN(並びでは最後)。top_percentと同じ順位の付け方。"""
    return q_score.rank(ascending=False, method="min")


def sort_candidates(x):
    """表示順: Timingスコアの降順 → Quality順位(良い順。判定不能は最後) → ティッカー順。
    銘柄の選定・スコアは変えず、並びだけを決める。timing_tie_n = 同じ区分内でTimingスコアが同じ銘柄数(自分を含む)。"""
    y = x.assign(_t=x.index).sort_values(["score", "q_rank", "_t"], ascending=[False, True, True], na_position="last", kind="mergesort").drop(columns="_t")
    y["timing_tie_n"] = y.groupby("score")["score"].transform("size").astype(int) if len(y) else pd.Series(dtype=int)
    return y


def earnings_view(info, today, R1):
    """info: earnings.reconcileの結果。戻り値: (表示文, 日数, 決算前か)"""
    if info is None or info.get("date") is None:
        return "決算: 取得できず(判定不能)", None, False
    d = info["date"]
    days = (d - today).days
    near = days <= R1["earnings"]["block_days_before"]
    txt = f"決算: {d} (あと{days}日・{info['note']})"
    if near:
        txt += f" ⚠決算まで{R1['earnings']['block_days_before']}日以内"
    return txt, days, near


def grade_after_earnings(v2_grade, near):
    return downgrade(v2_grade) if near else v2_grade


def format_candidate(n, r, ein, today, R1, tie_n=1):
    """r: 1銘柄の行(Series)。ein: 決算情報。"""
    et, days, near = earnings_view(ein, today, R1)
    g2 = grade_after_earnings(r.v2_grade, near)
    types = [TYPE_TEXT[k] for k in ("PULLBACK", "OVERSOLD", "MARKET_DIP", "RECOVERY") if bool(r.get(f"t_{k}", False))]
    L = [f"{n}) {r.name}  終値 ${r.close_raw:,.2f}",
         f"   判定: V2判定{r.v2_grade}" + (f" → 決算前のため1段階下げて{g2}" if near and g2 != r.v2_grade else "") +
         f" / Timing: v1判定{r.grade}(スコア{r.score:.0f}/100" + (f"・同点{tie_n}銘柄" if tie_n and tie_n > 1 else "") + f") / 種別: {'、'.join(types) if types else '種別なし(スコアのみ)'}",
         f"   根拠: RSI {_num(r.rsi)} / 高値から{_pct(r.dd_high)} / 50日線比{_pct(r.vs_sma50)} / 200日線比{_pct(r.vs_sma200)} / "
         f"出来高比{_num(r.vol_ratio, 2)} / 対SPY(20日){_pct(r.rel20)}"]
    if r.q_status == "UNDET":
        L.append(f"   Quality: 判定不能(理由: {r.q_reason or '不明'})")
    else:
        L.append(f"   Quality: {r.q_status}(同日の判定可能銘柄の中で上位{_num(r.q_top_pct, 0)}%) / 営業利益/総資産 {_pct(r.qb_prof)} / "
                 f"アクルーアル {_pct(r.qb_accr)}(低いほど良い) / 株式数の変化 {_pct(r.qb_dil)}(低いほど良い)")
    rfs = [RF_TEXT[k] for k in RF_TEXT if bool(r.get(k, False))]
    risks = rfs + (["Quality判定不能(財務の良し悪しは評価できていない)"] if r.q_status == "UNDET" else [])
    L.append("   リスク: " + ("; ".join(risks) if risks else "Red Flagなし(技術的な警告条件に該当せず。リスクが無いという意味ではありません)") +
             (" / 出来高比が1.5超(売りが続いている可能性)" if (r.vol_ratio == r.vol_ratio and r.vol_ratio > 1.5) else ""))
    L.append("   " + et)
    return "\n".join(L)


def header_lines(ctx):
    """通知の冒頭: 実行日・候補数・データ品質の注意事項。"""
    st, secs = ctx["stats"], ctx["sections"]
    n_all, n_shown = sum(len(df) for _, df, _ in secs), sum(len(sh) for _, _, sh in secs)
    L = [f"実行日 {ctx['today']} / 判定日 {ctx['asof']} / 候補{n_all}銘柄(" + " / ".join(f"{t.split('(')[0].replace(' ', '')} {len(df)}" for t, df, _ in secs) + f")・うち表示{n_shown}銘柄"]
    warn = []
    if ctx.get("sec_old"):
        warn.append(f"SEC財務キャッシュが古い(更新 {ctx['sec_updated']})")
    lag = (ctx["today"] - ctx["asof"]).days
    if lag > 3:
        warn.append(f"株価の最終日 {ctx['asof']} は実行日の{lag}日前")
    if st["n_no_earn"]:
        warn.append(f"決算日を取得できなかった表示銘柄 {st['n_no_earn']}銘柄")
    L.append(f"データ品質: Quality判定可能 {st['n_q']}/{st['n_gate']}銘柄({st['q_rate']:.0%})" + ("" if warn else " / 注意事項なし"))
    L += [f"   ⚠{w}" for w in warn]
    return L


def build_units(ctx):
    """通知文を、分割してはいけない塊(冒頭・各銘柄のブロック・集計)のリストにする。改行で連結したものが通知文の全文。"""
    sp, R1, today = ctx["spec"], ctx["R1"], ctx["today"]
    st = ctx["stats"]
    head = ([f"【リハーサル】過去のデータ(判定日 {ctx['asof']})による送信テストです。本番の通知ではありません。", ""] if ctx.get("rehearsal") else [])
    head += [f"【米国株 候補リスト(監視用)】判定日 {ctx['asof']}(米国市場の終値ベース)"] + header_lines(ctx) + [sp["disclaimer"]["top"]]
    units = ["\n".join(head), ""]
    for title, df, shown in ctx["sections"]:
        t = f"■ {title}: 該当{len(df)}銘柄" + (f"(上位{len(shown)}銘柄を表示)" if len(shown) < len(df) else "")
        blocks = [format_candidate(k, r, ctx["earn"].get(tk), today, R1, int(r.get("timing_tie_n", 1) or 1)) for k, (tk, r) in enumerate(shown.iterrows(), 1)] or ["   該当なし"]
        units.append(t + "\n" + blocks[0])           # 見出しだけが前の通に取り残されないよう、最初の銘柄と一緒にする
        units += blocks[1:] + [""]
    units.append("\n".join([
        f"■ 集計: Gate通過{st['n_gate']}銘柄 / Quality判定可能{st['n_q']}銘柄({st['q_rate']:.0%}) / Timing高(v1判定A・B){st['n_timing']}銘柄",
        f"   決算まで7日以内の表示銘柄: {st['n_near']}銘柄 / 決算日を取得できなかった表示銘柄: {st['n_no_earn']}銘柄",
        f"■ データ: 株価の最終日 {ctx['price_last']} / SEC財務キャッシュ更新 {ctx['sec_updated']}" + (" ⚠古いデータ" if ctx.get("sec_old") else ""),
        f"   ルール: V2b(rules {ctx['meta']['rules_v2b_sha8']}) / Timing v1({ctx['meta']['rules_v1_sha8']}) / 研究報告書 {ctx['meta']['report_sha8']}",
        "   並びはTimingスコア順で、期待リターンの順位ではありません。同点はQuality順位、それも同じならティッカー順です。", sp["disclaimer"]["bottom"]]))
    return units


def build_message(ctx):
    """ctx: asof(date), today, R1, spec, sections[(title, DataFrame, shown)], earn{ticker:info}, stats{...}, meta{...}"""
    return "\n".join(build_units(ctx))
