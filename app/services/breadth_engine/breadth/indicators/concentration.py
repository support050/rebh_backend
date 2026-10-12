"""Family F — Concentration.

Inputs: index.parquet (TASI official 2007+, EW_TASI 2002+), clean prices (market cap 2026-01-29+).

  ew_ret / tasi_ret        daily returns
  spread_N                 100 * [(EW_T/EW_{T-N}) - (TASI_T/TASI_{T-N})]   in pct points, N in spread_windows
  ratio                    EW / TASI, rebased to 1.0 on the first common date
  ratio_ma                 SMA(ratio_ma) of ratio ; ratio_trend = +1 if ratio > ratio_ma else -1
  ew_lead_60               sign(spread_60): +1 broad market leads (healthy), -1 mega-caps mask weakness

Top-N contribution (only where market cap exists, 2026-01-29 onwards):
  weight_i,T-1 = mcap_i,T-1 / sum(mcap_T-1) over the universe, then each weight is capped at
                 `weight_cap` (15%) with the excess redistributed pro-rata (iterated). Free-float
                 weights are unavailable; uncapped full-cap weights give Aramco ~67% and only 0.57
                 correlation with TASI daily returns, the 15% cap gives 0.99 (validated Phase 4).
  contrib_i,T  = weight_i,T-1 * ret_i,T
  top5_contrib = sum of the N largest |contrib| names' contributions
  top5_share   = |top5_contrib| / sum |contrib_i|   (share of the day's move explained by N names)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels


def cap_weights(w: pd.DataFrame, cap: float, iters: int = 10) -> pd.DataFrame:
    """Cap each row's weights at `cap`, redistributing the excess pro-rata to uncapped names."""
    w = w.copy()
    for _ in range(iters):
        excess = (w - cap).clip(lower=0).sum(axis=1)
        if (excess <= 1e-12).all():
            break
        clipped = w.clip(upper=cap)
        rest = clipped.where(clipped < cap, 0.0)
        can = rest.sum(axis=1) > 0                    # rows where the cap is feasible
        redistributed = clipped + rest.div(rest.sum(axis=1).replace(0, np.nan), axis=0).mul(excess, axis=0).fillna(0)
        w = redistributed.where(can, w)               # infeasible rows (n*cap < 1) are left uncapped
    return w


def compute_concentration(index_df: pd.DataFrame, p: Panels, prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c = cfg["concentration"]
    tasi = index_df[index_df["index_code"] == "TASI"].set_index("date")["close"].sort_index()
    ew = index_df[index_df["index_code"] == "EW_TASI"].set_index("date")["close"].sort_index()
    df = pd.DataFrame({"tasi": tasi, "ew": ew}).sort_index()
    df["tasi_ret"] = df["tasi"].pct_change(); df["ew_ret"] = df["ew"].pct_change()
    for n in c["spread_windows"]:
        df[f"spread_{n}"] = 100 * (df["ew"] / df["ew"].shift(n) - df["tasi"] / df["tasi"].shift(n))
    both = df.dropna(subset=["tasi", "ew"])
    base = both.index.min()
    df["ratio"] = (df["ew"] / df["tasi"]) / (df.loc[base, "ew"] / df.loc[base, "tasi"])
    df["ratio_ma"] = df["ratio"].rolling(c["ratio_ma"], min_periods=c["ratio_ma"]).mean()
    df["ratio_trend"] = np.sign(df["ratio"] - df["ratio_ma"])
    df["ew_lead_60"] = np.sign(df["spread_60"]) if "spread_60" in df else np.nan

    # ---- top-N contribution where market cap exists
    mc = prices[prices["market_cap"].notna() & prices["in_universe"]].pivot(index="date", columns="symbol", values="market_cap")
    if len(mc):
        mc = mc.reindex(columns=p.ret.columns)
        w = mc.shift(1)
        w = cap_weights(w.div(w.sum(axis=1), axis=0), c["weight_cap"])
        contrib = (w * p.ret.reindex(mc.index)).where(w.notna())
        n = c["top_n"]
        absc = contrib.abs()
        top = absc.apply(lambda row: row.nlargest(n).index.tolist() if row.notna().any() else [], axis=1)
        top_sum = pd.Series([contrib.loc[d, cols].sum() if cols else np.nan for d, cols in top.items()], index=top.index)
        tot = absc.sum(axis=1)
        df.loc[top_sum.index, "top5_contrib"] = 100 * top_sum
        df.loc[top_sum.index, "top5_share"] = (top_sum.abs() / tot.replace(0, np.nan))
        df.loc[top_sum.index, "cw_proxy_ret"] = 100 * contrib.sum(axis=1)
        df.loc[top_sum.index, "top5_names"] = [",".join(map(str, cols)) for cols in top.values]
    df.index.name = "date"
    return df
