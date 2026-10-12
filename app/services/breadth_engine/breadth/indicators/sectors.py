"""Family E — Sector breadth (per TASI industry group).

For every group g and day T (same stock-level definitions as families A, B, D):
  n_g            eligible & active stocks in g (age >= 200, traded in last 10 days)
  pct50_g        100 * #(level > SMA50) / n50_g
  pct200_g       100 * #(level > SMA200) / n200_g
  nhnl_g         (NH - NL) / n_nhnl_g  * 100          (52-week, strict prior-window definition)
  ad10_g         100 * sum_10(A_g) / sum_10(A_g + D_g)
  mom10_g        100 * sum_10(up4_g) / sum_10(up4_g + down4_g)   (NaN when both are 0)
Sector health score (0-100) = mean of [pct50_g, pct200_g, 50*(1+nhnl_g/100), ad10_g]
  (mom10 is reported but kept out of the score: Phase 3 showed family D is contrarian on TASI)
healthy_g = score >= healthy_threshold ; weak_g = score <= weak_threshold
Groups with fewer than `min_stocks` eligible names on a day are pooled into "Other".
Summary per day: n_sectors, n_healthy, n_weak, pct_healthy, median_score.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels, eligible
from breadth.indicators.momentum import daily_movers  # noqa: F401  (same definition, re-used below)
from breadth.indicators.participation import ACTIVE_WINDOW

# Unified-environment compatibility (2026-10-07): `future_stack` exists only in
# pandas>=2.1 (kept through 3.x). On older pandas (project venv = 1.5.3) the same
# NaN-preserving behaviour for single-level columns is `stack(dropna=False)`.
_STACK_HAS_FUTURE = "future_stack" in inspect.signature(pd.DataFrame.stack).parameters


def _group_sum(panel: pd.DataFrame, groups: pd.Series) -> pd.DataFrame:
    """Sum a (date x symbol) panel into (date x group)."""
    g = groups.reindex(panel.columns)
    return panel.T.groupby(g.values).sum().T


def stock_flags(p: Panels, cfg: dict) -> dict[str, pd.DataFrame]:
    """Per-stock boolean/float panels shared by the sector module."""
    lvl = p.level
    traded = p.close.notna()
    active = traded.rolling(ACTIVE_WINDOW, min_periods=1).max().astype(bool) & p.listed
    sma50 = lvl.rolling(50, min_periods=50).mean()
    sma200 = lvl.rolling(200, min_periods=200).mean()
    w = cfg["indicators"]["participation"]["new_high_low_window"]
    rmax = lvl.rolling(w, min_periods=w).max().shift(); rmin = lvl.rolling(w, min_periods=w).min().shift()
    thr = cfg["momentum"]["move_threshold"]
    r = p.ret.round(8)
    vol_up = p.volume > p.volume.shift(1)
    e50 = eligible(p.age, 50) & active; e200 = eligible(p.age, 200) & active; eh = eligible(p.age, w + 1) & traded
    return {
        "e50": e50, "above50": (lvl > sma50) & e50,
        "e200": e200, "above200": (lvl > sma200) & e200,
        "ehl": eh, "nh": (p.close > rmax) & eh, "nl": (p.close < rmin) & eh,
        "adv": r > 0, "dec": r < 0, "valid": r.notna(),
        "up4": (r >= thr) & vol_up, "dn4": (r <= -thr) & vol_up,
    }


def compute_sectors(p: Panels, master: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    sc = cfg["sectors"]
    groups = master.set_index("symbol")[sc["group_field"]].astype("string").fillna("Unknown")
    f = stock_flags(p, cfg)
    n50 = _group_sum(f["e50"].astype(float), groups)
    # pool small groups into "Other" per day
    small = n50 < sc["min_stocks"]
    sums = {}
    for k, v in f.items():
        gs = _group_sum(v.astype(float), groups)
        other = gs.where(small, 0.0).sum(axis=1)
        gs = gs.where(~small, 0.0); gs["Other"] = other
        sums[k] = gs
    n50 = sums["e50"]; n200 = sums["e200"]; nh = sums["ehl"]
    w = sc["ad_window"]; mw = sc["mom_window"]
    pct50 = 100 * sums["above50"] / n50.replace(0, np.nan)
    pct200 = 100 * sums["above200"] / n200.replace(0, np.nan)
    nhnl = 100 * (sums["nh"] - sums["nl"]) / nh.replace(0, np.nan)
    A = sums["adv"].rolling(w, min_periods=w).sum(); D = sums["dec"].rolling(w, min_periods=w).sum()
    ad10 = 100 * A / (A + D).replace(0, np.nan)
    U = sums["up4"].rolling(mw, min_periods=mw).sum(); Dn = sums["dn4"].rolling(mw, min_periods=mw).sum()
    mom10 = 100 * U / (U + Dn).replace(0, np.nan)
    score = pd.concat([pct50, pct200, 50 * (1 + nhnl / 100), ad10]).groupby(level=0).mean()
    score = score.where(n50 >= sc["min_stocks"])                 # no score for empty / pooled-away groups

    def melt(df, name):
        return (df.stack(future_stack=True) if _STACK_HAS_FUTURE
                else df.stack(dropna=False)).rename(name)
    long = pd.concat([melt(n50, "n"), melt(pct50, "pct50"), melt(pct200, "pct200"), melt(nhnl, "nhnl"),
                      melt(ad10, "ad10"), melt(mom10, "mom10"), melt(score, "score")], axis=1)
    long.index.names = ["date", "sector"]
    long = long.reset_index()
    long = long[long["n"] >= sc["min_stocks"]].copy()
    long["healthy"] = long["score"] >= sc["healthy_threshold"]
    long["weak"] = long["score"] <= sc["weak_threshold"]
    summ = long.groupby("date").agg(n_sectors=("sector", "size"), n_healthy=("healthy", "sum"),
                                    n_weak=("weak", "sum"), median_score=("score", "median"))
    summ["pct_healthy"] = 100 * summ["n_healthy"] / summ["n_sectors"]
    summ["pct_weak"] = 100 * summ["n_weak"] / summ["n_sectors"]
    return long, summ
