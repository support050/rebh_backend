"""Family D — Momentum breadth (Stockbee Market Monitor adapted to TASI).

Per day T on the universe (r = daily adjusted return, NaN on suspended / ex-day):
  up_X    = #(round(r,8) >= X  AND volume_T > volume_{T-1})   X = move_threshold (4% default)
  down_X  = #(r <= -X AND volume_T > volume_{T-1})
  (volume condition switchable in config; Stockbee's original counts price only for the daily
   4% list but uses volume for the "with volume" breakouts — we keep the stricter version)
  ratio_5  = sum_5(up_X)  / sum_5(down_X)
  ratio_10 = sum_10(up_X) / sum_10(down_X)
  up_25_q  = #(level_T / level_{T-63} - 1 >= 0.25),   down_25_q analog
  up_25_m  = #(level_T / level_{T-21} - 1 >= 0.25),   down_25_m
  up_50_m  = #(level_T / level_{T-21} - 1 >= 0.50),   down_50_m
  up_13_34 = #(level_T / level_{T-34} - 1 >= 0.13),   down_13_34
  n_mom    = # stocks with a valid return on T (denominator for pct_ columns)
A stock enters an N-day move only with age > N (full lookback since listing).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels


def daily_movers(p: Panels, thr: float, require_volume_up: bool) -> tuple[pd.Series, pd.Series]:
    r = p.ret.round(8)          # avoid floating-point boundary cases (e.g. 9.6/10-1 vs -0.04)
    up = r >= thr
    dn = r <= -thr
    if require_volume_up:
        vol_up = p.volume > p.volume.shift(1)
        up &= vol_up; dn &= vol_up
    return up.sum(axis=1), dn.sum(axis=1)


def _nday_counts(p: Panels, n: int, pct: float) -> tuple[pd.Series, pd.Series]:
    chg = p.level / p.level.shift(n) - 1
    ok = (p.age > n) & p.close.notna()
    return ((chg >= pct) & ok).sum(axis=1), ((chg <= -pct) & ok).sum(axis=1)


def compute_momentum(p: Panels, cfg: dict, thr: float | None = None) -> pd.DataFrame:
    m = cfg["momentum"]
    thr = m["move_threshold"] if thr is None else thr
    out = pd.DataFrame(index=p.ret.index)
    out["n_mom"] = p.ret.notna().sum(axis=1)
    up, dn = daily_movers(p, thr, m["require_volume_up"])
    out["up_x"], out["down_x"] = up, dn
    for w in m["ratio_windows"]:
        out[f"ratio_{w}"] = up.rolling(w, min_periods=w).sum() / dn.rolling(w, min_periods=w).sum().replace(0, np.nan)
        out[f"up_{w}"] = up.rolling(w, min_periods=w).sum(); out[f"down_{w}"] = dn.rolling(w, min_periods=w).sum()
    out["up_25_q"], out["down_25_q"] = _nday_counts(p, m["quarter_days"], m["quarter_pct"])
    out["up_25_m"], out["down_25_m"] = _nday_counts(p, m["month_days"], m["month_pct"])
    out["up_50_m"], out["down_50_m"] = _nday_counts(p, m["month_days"], m["month_big_pct"])
    out["up_13_34"], out["down_13_34"] = _nday_counts(p, m["d34_days"], m["d34_pct"])
    out["pct_up_x"] = 100 * up / out["n_mom"].replace(0, np.nan)
    out["pct_down_x"] = 100 * dn / out["n_mom"].replace(0, np.nan)
    out["threshold"] = thr
    out.index.name = "date"
    return out
