"""Shared panel construction for breadth indicators.

A *panel* is a wide DataFrame: index = trading date, columns = symbol.
All indicator maths run on panels so that day T only ever sees rows <= T.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Panels:
    close: pd.DataFrame        # adjusted close, NaN on suspended days
    level: pd.DataFrame        # adjusted last valid close (ffilled) — for MAs / NH-NL
    ret: pd.DataFrame          # daily return on adjusted close, NaN on suspended / ex-day / first row
    volume: pd.DataFrame       # adjusted volume (0 on suspended)
    age: pd.DataFrame          # trading days since listing (1 on listing day), NaN before listing / after delisting
    listed: pd.DataFrame       # True between list and delist date


def build_panels(prices: pd.DataFrame, universe: set[int] | None = None) -> Panels:
    """From the clean/prices parquet (long) to wide panels restricted to `universe`."""
    df = prices
    if universe is not None:
        df = df[df["symbol"].isin(universe)]
    df = df.sort_values(["symbol", "date"])
    # daily return (same definition as breadth.data.index.stock_returns)
    prev = df.groupby("symbol")["close_adj"].ffill().groupby(df["symbol"]).shift()
    ret = df["close_adj"] / prev - 1
    ret[df["is_suspended"] | df["adj_event"] | prev.isna()] = np.nan
    df = df.assign(_ret=ret.values, _age=df.groupby("symbol").cumcount().values + 1)

    def piv(col):
        return df.pivot(index="date", columns="symbol", values=col).sort_index()

    close = piv("close_adj")
    level = piv("last_close_adj")
    listed = piv("is_listed").fillna(False).astype(bool)
    vol = piv("volume_adj").fillna(0.0)
    age = piv("_age")
    return Panels(close=close, level=level, ret=piv("_ret"), volume=vol, age=age, listed=listed)


def eligible(age: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """True where the stock has at least `lookback` trading days of history (listing day = 1)."""
    return age >= lookback


def pct_true(mask: pd.DataFrame, elig: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """% of eligible stocks where mask is True; returns (pct, n_eligible)."""
    m = mask & elig
    n = elig.sum(axis=1)
    pct = 100.0 * m.sum(axis=1) / n.replace(0, np.nan)
    return pct, n
