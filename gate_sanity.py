import pandas as pd
sig = pd.read_csv("results/v1_dev_datafix1/signals.csv.gz", parse_dates=["date"],
                  usecols=["ticker", "date", "close", "mcap_est", "dollar_vol", "grade", "ret_3m", "ret_6m"])
pd.set_option("display.width", 200)
g = sig.groupby("ticker").agg(rows=("close", "size"), max_close=("close", "max"),
                              max_mcap_B=("mcap_est", lambda s: s.max() / 1e9),
                              med_dvol_M=("dollar_vol", lambda s: s.median() / 1e6),
                              mean_ret3m=("ret_3m", "mean"))
print(f"Gate通過行: {len(sig)} / 銘柄数: {sig.ticker.nunique()}")
a = g[g.max_close > 1000].sort_values("max_close", ascending=False)
b = g[g.max_mcap_B > 1000].sort_values("max_mcap_B", ascending=False)
print(f"\n[A] 分割調整後の株価が$1,000超の行がある銘柄: {len(a)}銘柄 / {int(a.rows.sum())}行")
print(a.head(20).round(2).to_string())
print(f"\n[B] 推定時価総額が$1兆超の行がある銘柄: {len(b)}銘柄 / {int(b.rows.sum())}行")
print(b.head(20).round(2).to_string())
bad = set(a.index) | set(b.index)
rest = sig[~sig.ticker.isin(bad)]
print(f"\n[C] 参考（除外ではなく感度の確認）: A・Bの銘柄 {len(bad)}銘柄 / {len(sig) - len(rest)}行")
for c in ("ret_3m", "ret_6m"):
    print(f"  {c}: 全体 平均{sig[c].mean():.2%} 中央値{sig[c].median():.2%} / A・B除く 平均{rest[c].mean():.2%} 中央値{rest[c].median():.2%}")
