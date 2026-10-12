"""Family G — Automatic divergence detection.

Index extreme:  hi_T = TASI_T >= max(TASI[T-N..T])   (new N-day closing high, N = index_window)
                lo_T = TASI_T <= min(TASI[T-N..T])
Metric confirmation (for each breadth metric m, window N = confirm_window, tolerance k):
  confirmed_high_T = max(m[T-k..T]) >= max(m[T-N..T])   (m made its own N-day high within the last k days)
  confirmed_low_T  = min(m[T-k..T]) <= min(m[T-N..T])
Bearish divergence on T: hi_T AND NOT confirmed_high_T(m)    (index new high, breadth fails to confirm)
Bullish divergence on T: lo_T AND NOT confirmed_low_T(m)     (index new low, breadth holds up)
One event per metric per `min_gap_days`; `n_bearish` / `n_bullish` = number of metrics diverging on T
(0..4). Metrics: ad_line, net_nh_nl_10 (10-day sum of NH-NL), pct_above_sma50, mcclellan_sum.
Lookback only: every quantity on T uses data up to T. The event is known on T (no future confirmation).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.thrusts import first_of_run


def build_metrics(ad: pd.DataFrame, part: pd.DataFrame) -> pd.DataFrame:
    m = pd.DataFrame(index=ad.index)
    m["ad_line"] = ad["ad_line"]
    m["net_nh_nl_10"] = part["net_nh_nl"].reindex(ad.index).rolling(10, min_periods=10).sum()
    m["pct_above_sma50"] = part["pct_above_sma50"].reindex(ad.index)
    m["mcclellan_sum"] = ad["mcclellan_sum"]
    return m


def compute_divergences(tasi: pd.Series, metrics: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    d = cfg["divergences"]
    N, M, k, gap = d["index_window"], d["confirm_window"], d["tolerance_days"], d["min_gap_days"]
    idx = tasi.reindex(metrics.index)
    hi = idx >= idx.rolling(N, min_periods=N).max()
    lo = idx <= idx.rolling(N, min_periods=N).min()
    out = pd.DataFrame({"index_high": hi, "index_low": lo}, index=metrics.index)
    nb = pd.Series(0, index=metrics.index); nu = pd.Series(0, index=metrics.index)
    for c in d["metrics"]:
        m = metrics[c]
        conf_hi = m.rolling(k + 1, min_periods=1).max() >= m.rolling(M, min_periods=M).max()
        conf_lo = m.rolling(k + 1, min_periods=1).min() <= m.rolling(M, min_periods=M).min()
        bear = hi & ~conf_hi & m.notna()
        bull = lo & ~conf_lo & m.notna()
        out[f"bear_{c}"] = bear; out[f"bull_{c}"] = bull
        out[f"bear_{c}_signal"] = first_of_run(bear, gap)
        out[f"bull_{c}_signal"] = first_of_run(bull, gap)
        nb += bear.astype(int); nu += bull.astype(int)
    out["n_bearish"] = nb.where(hi, 0); out["n_bullish"] = nu.where(lo, 0)
    out["bearish_signal"] = first_of_run(out["n_bearish"] >= 2, gap)
    out["bullish_signal"] = first_of_run(out["n_bullish"] >= 2, gap)
    out.index.name = "date"
    return out
