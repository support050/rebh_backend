"""Preliminary state -> forward-return tables (full validation is Phase 6).

For an indicator state S(T) (a categorical label per day), we report over all days T >= start:
  n           number of days in that state
  mean_h      mean TASI return from close T to close T+h, h in HORIZONS (in %)
  hit_h       share of those days with a positive h-day forward return
  base_h      unconditional mean over the same period (for comparison)
Overlapping windows: the n days are NOT independent; treat this as descriptive, not a test.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS = (5, 10, 20, 60)


def forward_returns(index_close: pd.Series) -> pd.DataFrame:
    out = {}
    for h in HORIZONS:
        out[f"fwd_{h}"] = 100 * (index_close.shift(-h) / index_close - 1)
    return pd.DataFrame(out)


def state_table(state: pd.Series, fwd: pd.DataFrame, name: str) -> pd.DataFrame:
    df = fwd.join(state.rename("state"), how="inner").dropna(subset=["state"])
    rows = []
    base = {h: df[f"fwd_{h}"].mean() for h in HORIZONS}
    for s, g in df.groupby("state", observed=True):
        r = {"indicator": name, "state": str(s), "n": int(g[f"fwd_{HORIZONS[0]}"].notna().sum())}
        for h in HORIZONS:
            x = g[f"fwd_{h}"].dropna()
            r[f"mean_{h}"] = round(x.mean(), 2); r[f"hit_{h}"] = round((x > 0).mean(), 2); r[f"base_{h}"] = round(base[h], 2)
        rows.append(r)
    return pd.DataFrame(rows)


def bucket(s: pd.Series, edges: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(s, bins=[-np.inf] + edges + [np.inf], labels=labels)
