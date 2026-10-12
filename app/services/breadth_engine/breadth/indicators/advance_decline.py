"""Family B — Advance / Decline.

Per day T over stocks with a valid return r (traded on T, traded before, not an ex-day):
  advances A = #(r > 0), declines D = #(r < 0), unchanged U = #(r == 0)
  ad_line   = cumulative sum of (A - D)
  ad_ratio  = A / D
  up_volume UV = sum volume of advancers, down_volume DV = sum volume of decliners
  ud_volume_ratio = UV / DV ; net_up_volume = UV - DV
  up_points / down_points = sum of |close_T - close_{T-1}| (adjusted) over advancers / decliners

McClellan (ratio-adjusted, as StockCharts):
  rana = 1000 * (A - D) / (A + D)
  mcclellan_osc = EMA19(rana) - EMA39(rana)  (EMA alpha = 2/(n+1), seeded with SMA of first n values)
  mcclellan_sum = cumulative sum of the oscillator (starts at 0)

Arms Index:
  trin = (A / D) / (UV / DV) ; trin_10 = 10-day SMA of trin
  trin_open10 = (sum10 A / sum10 D) / (sum10 UV / sum10 DV)  ("open" 10-day TRIN)

Lowry 90% days:
  up_volume_pct = UV / (UV + DV) ; up_points_pct = up_points / (up_points + down_points)
  day90_up    = up_volume_pct >= 0.90 AND up_points_pct >= 0.90
  day90_down  = up_volume_pct <= 0.10 AND up_points_pct <= 0.10
  also reported separately: day80/90 by volume only.
Days with fewer than `min_stocks` valid returns are NaN (never scored).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels


def ema_sma_seeded(s: pd.Series, n: int) -> pd.Series:
    """EMA seeded with the SMA of the first n valid values (classic McClellan convention)."""
    s = s.astype(float)
    out = pd.Series(np.nan, index=s.index)
    vals = s.to_numpy()
    valid = ~np.isnan(vals)
    idx = np.flatnonzero(valid)
    if len(idx) < n:
        return out
    alpha = 2.0 / (n + 1)
    start = idx[n - 1]
    e = np.nanmean(vals[idx[:n]])
    out.iloc[start] = e
    for i in range(start + 1, len(vals)):
        if valid[i]:
            e = e + alpha * (vals[i] - e)
        out.iloc[i] = e
    return out


def compute_advance_decline(p: Panels, cfg: dict) -> pd.DataFrame:
    c = cfg["indicators"]["advance_decline"]
    r = p.ret
    valid = r.notna()
    adv = r > 0; dec = r < 0
    A = adv.sum(axis=1).astype(float); D = dec.sum(axis=1).astype(float)
    U = ((r == 0) & valid).sum(axis=1).astype(float)
    n = valid.sum(axis=1)
    ok = n >= c["min_stocks"]

    vol = p.volume
    UV = vol.where(adv, 0.0).sum(axis=1); DV = vol.where(dec, 0.0).sum(axis=1)
    prev_close = p.close.ffill().shift()
    pts = (p.close - prev_close).abs()
    UP = pts.where(adv, 0.0).sum(axis=1); DP = pts.where(dec, 0.0).sum(axis=1)

    out = pd.DataFrame({"advances": A, "declines": D, "unchanged": U, "n_ad": n,
                        "up_volume": UV, "down_volume": DV, "up_points": UP, "down_points": DP})
    out.loc[~ok, ["advances", "declines", "unchanged", "up_volume", "down_volume", "up_points", "down_points"]] = np.nan
    A, D, UV, DV, UP, DP = out["advances"], out["declines"], out["up_volume"], out["down_volume"], out["up_points"], out["down_points"]

    out["ad_net"] = A - D
    out["ad_line"] = out["ad_net"].fillna(0).cumsum()
    out["ad_ratio"] = A / D.replace(0, np.nan)
    out["ud_volume_ratio"] = UV / DV.replace(0, np.nan)
    out["net_up_volume"] = UV - DV
    out["up_volume_pct"] = UV / (UV + DV).replace(0, np.nan)
    out["up_points_pct"] = UP / (UP + DP).replace(0, np.nan)

    rana = 1000.0 * (A - D) / (A + D).replace(0, np.nan)
    out["rana"] = rana
    out["mcclellan_osc"] = ema_sma_seeded(rana, c["mcclellan_fast"]) - ema_sma_seeded(rana, c["mcclellan_slow"])
    out["mcclellan_sum"] = out["mcclellan_osc"].fillna(0).cumsum()

    trin = (A / D.replace(0, np.nan)) / (UV / DV.replace(0, np.nan)).replace(0, np.nan)
    out["trin"] = trin
    k = c["trin_ma"]
    out["trin_10"] = trin.rolling(k, min_periods=k).mean()
    out["trin_open10"] = ((A.rolling(k).sum() / D.rolling(k).sum().replace(0, np.nan))
                          / (UV.rolling(k).sum() / DV.rolling(k).sum().replace(0, np.nan)))

    th = c["lowry_threshold"]
    out["day90_up"] = (out["up_volume_pct"] >= th) & (out["up_points_pct"] >= th)
    out["day90_down"] = (out["up_volume_pct"] <= 1 - th) & (out["up_points_pct"] <= 1 - th)
    out["day90_up_vol"] = out["up_volume_pct"] >= th
    out["day90_down_vol"] = out["up_volume_pct"] <= 1 - th
    out["day80_up_vol"] = out["up_volume_pct"] >= 0.80
    out["day80_down_vol"] = out["up_volume_pct"] <= 0.20
    out.index.name = "date"
    return out
