"""Family C — Breadth thrusts (rare, high-conviction).

All functions take the daily A/D frame (columns `advances`, `declines`) and/or the
participation frame (`pct_above_sma50`) and return a DataFrame of daily columns:
  <name>_value  : the underlying ratio
  <name>_state  : True while the condition holds
  <name>_signal : True on the first day of a new signal (deduplicated by `cooldown_days`)

Zweig Breadth Thrust
  zbt = EMA_span( A / (A + D) )      (alpha = 2/(span+1), pandas ewm adjust=False)
  signal on T if zbt_T > high AND min(zbt[T-window .. T-1]) < low
  (i.e. the ratio went from below `low` to above `high` within `window` trading days)

Whaley thrust
  w = sum_{days}(A) / sum_{days}(D) ; signal when w >= ratio

Deemer Breakaway Momentum
  bam = sum_{10}(A) / sum_{10}(D) ; signal when bam >= 1.97 (Deemer's NYSE value; recalibrated here)

%>50DMA thrust
  signal on T if pct50_T > high AND min(pct50[T-window .. T-1]) < low

Lowry 90% days are produced in advance_decline.py; the calibration grid re-tests their threshold.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def first_of_run(state: pd.Series, cooldown: int) -> pd.Series:
    """True on the first True day of a run, suppressing repeats within `cooldown` days."""
    s = state.fillna(False).to_numpy(dtype=bool)
    out = np.zeros(len(s), dtype=bool)
    last = -10**9
    for i, v in enumerate(s):
        if v and i - last > cooldown:
            out[i] = True
            last = i
    return pd.Series(out, index=state.index)


def low_to_high(x: pd.Series, low: float, high: float, window: int) -> pd.Series:
    prior_min = x.shift(1).rolling(window, min_periods=1).min()
    return (x > high) & (prior_min < low)


def zweig(ad: pd.DataFrame, span: int, low: float, high: float, window: int, cooldown: int) -> pd.DataFrame:
    A, D = ad["advances"], ad["declines"]
    ratio = A / (A + D)
    zbt = ratio.ewm(span=span, adjust=False, min_periods=span).mean()
    state = low_to_high(zbt, low, high, window)
    return pd.DataFrame({"zweig_value": zbt, "zweig_state": state, "zweig_signal": first_of_run(state, cooldown)})


def ratio_thrust(ad: pd.DataFrame, days: int, ratio: float, cooldown: int, name: str) -> pd.DataFrame:
    A, D = ad["advances"], ad["declines"]
    v = A.rolling(days, min_periods=days).sum() / D.rolling(days, min_periods=days).sum().replace(0, np.nan)
    state = v >= ratio
    return pd.DataFrame({f"{name}_value": v, f"{name}_state": state, f"{name}_signal": first_of_run(state, cooldown)})


def pct50_thrust(part: pd.DataFrame, low: float, high: float, window: int, cooldown: int) -> pd.DataFrame:
    x = part["pct_above_sma50"]
    state = low_to_high(x, low, high, window)
    return pd.DataFrame({"pct50_thrust_value": x, "pct50_thrust_state": state,
                         "pct50_thrust_signal": first_of_run(state, cooldown)})


def compute_thrusts(ad: pd.DataFrame, part: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    t = cfg["thrusts"]; cd = t["cooldown_days"]
    z = t["zweig"]; w = t["whaley"]; d = t["deemer"]; p = t["pct50_thrust"]
    out = pd.concat([
        zweig(ad, z["ema_span"], z["low"], z["high"], z["window"], cd),
        ratio_thrust(ad, w["days"], w["ratio"], cd, "whaley"),
        ratio_thrust(ad, d["days"], d["ratio"], cd, "deemer"),
        pct50_thrust(part.reindex(ad.index), p["low"], p["high"], p["window"], cd),
    ], axis=1)
    lt = t["lowry"]["threshold"]
    up_day = (ad["up_volume_pct"] >= lt) & (ad["up_points_pct"] >= lt)
    dn_day = (ad["up_volume_pct"] <= 1 - lt) & (ad["up_points_pct"] <= 1 - lt)
    out["lowry_up_signal"] = first_of_run(up_day, cd)
    out["lowry_down_signal"] = first_of_run(dn_day, cd)
    # separate conditions (points / volume) for display
    out["lowry_down_points"] = ad["up_points_pct"] <= 1 - lt
    out["lowry_down_volume"] = ad["up_volume_pct"] <= 1 - lt
    # Lowry sequence: 95% down day followed by a >= `sequence_up_threshold` up day within `sequence_window` days
    su, sw = t["lowry"]["sequence_up_threshold"], t["lowry"]["sequence_window"]
    up90 = (ad["up_volume_pct"] >= su) & (ad["up_points_pct"] >= su)
    recent_dn = dn_day.astype(float).rolling(sw, min_periods=1).max().shift(1).fillna(0).astype(bool)
    out["lowry_seq_signal"] = first_of_run(up90 & recent_dn, cd)
    # rare variants (expert review): see config `thrusts.rare`
    r = t.get("rare", {})
    if r:
        rcd = r["cooldown_days"]
        zr = low_to_high(out["zweig_value"], z["low"], r["zweig_high"], z["window"])
        out["zweig_rare_state"] = zr; out["zweig_rare_signal"] = first_of_run(zr, rcd)
        dr = out["deemer_value"] >= r["deemer_ratio"]
        out["deemer_rare_state"] = dr; out["deemer_rare_signal"] = first_of_run(dr, rcd)
    out.index.name = "date"
    return out
