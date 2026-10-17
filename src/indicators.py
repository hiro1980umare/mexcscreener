"""テクニカル指標。すべて「その日までのデータ」だけで計算する（rolling/ewm/shiftのみ。未来は見ない）。"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Ind:
    dates: pd.DatetimeIndex
    tickers: list
    a: dict      # 指標名 -> 2次元配列 (日付 x 銘柄)
    spy: dict    # 指標名 -> 1次元配列 (日付)

    @property
    def tindex(self):
        return {t: i for i, t in enumerate(self.tickers)}


def rsi_wilder(close, period):
    d = close.diff()
    gain, loss = d.clip(lower=0), (-d).clip(lower=0)
    ag = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rsi = 100 - 100 / (1 + ag / al)
    rsi = rsi.where(al > 0, 100.0)  # 下落が全く無ければ100
    return rsi.where(ag.notna())


def build(panel, R):
    p = R["indicators"]
    c, v = panel.close, panel.volume
    roll = lambda x, n, f: getattr(x.rolling(n, min_periods=n), f)()  # noqa: E731

    rsi = rsi_wilder(c, p["rsi_period"])
    sma20, sma50, sma200 = (roll(c, p[k], "mean") for k in ("sma_short", "sma_mid", "sma_long"))
    high = roll(c, p["high_lookback"], "max")
    dd = c / high - 1
    vol_ratio = roll(v, p["volume_fast"], "mean") / roll(v, p["volume_slow"], "mean")
    slope200 = sma200 / sma200.shift(p["slope_lookback"]) - 1
    ret5 = c / c.shift(p["ret_short"]) - 1
    ret20 = c / c.shift(p["ret_mid"]) - 1
    dollar_vol = roll(c * v, p["volume_slow"], "mean")
    mcap = c.mul(panel.shares.reindex(c.columns), axis=1)  # 推定時価総額(現在の株式数を固定)

    spy = panel.spy_close.reindex(c.index)
    spy_high = roll(spy, p["high_lookback"], "max")
    spy_dd = (spy / spy_high - 1).values
    spy_ret20 = (spy / spy.shift(p["ret_mid"]) - 1).values

    a = {
        "close": c.values, "rsi": rsi.values, "sma20": sma20.values, "sma50": sma50.values,
        "sma200": sma200.values, "dd": dd.values, "vol_ratio": vol_ratio.values,
        "slope200": slope200.values, "ret5": ret5.values, "ret20": ret20.values,
        "rel20": ret20.values - spy_ret20[:, None], "rel_dd": dd.values - spy_dd[:, None],
        "rsi_min10": roll(rsi, 10, "min").values, "rsi_lag5": rsi.shift(5).values,
        "dollar_vol": dollar_vol.values, "mcap": mcap.values,
    }
    return Ind(c.index, list(c.columns), a, {"dd": spy_dd, "ret20": spy_ret20})
