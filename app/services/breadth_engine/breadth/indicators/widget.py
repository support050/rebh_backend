"""Daily "Market Breadth widget" counts (TradingView-style panel), computed on the universe.

  new_highs / new_lows           52-week (from participation.py)
  advances / declines            close vs previous valid close (from advance_decline.py)
  up_from_open / down_from_open  close_adj > open_adj / close_adj < open_adj (traded days with a valid open)
  up_on_volume / down_on_volume  advancing / declining stocks whose volume_adj > 20-day average volume (prior 20 days)
  up4 / down4                    ≥ +4% / ≤ −4% with volume above the prior day (from momentum.py)
Weinstein stages on the 150-day SMA (slope over 20 days), eligible = age ≥ 170 and active:
  stage1  level <  MA and MA not falling   (basing)
  stage2  level >  MA and MA rising        (advancing)
  stage3  level >  MA and MA not rising    (topping)
  stage4  level <  MA and MA falling       (declining)
"""
from __future__ import annotations

import pandas as pd

from breadth.indicators.common import Panels, eligible
from breadth.indicators.participation import ACTIVE_WINDOW


def compute_widget(p: Panels, open_adj: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c = cfg["indicators"]["participation"]
    traded = p.close.notna()
    out = pd.DataFrame(index=p.close.index)
    # up / down from open
    o = open_adj.reindex_like(p.close)
    valid_o = traded & o.notna()
    out["up_from_open"] = ((p.close > o) & valid_o).sum(axis=1)
    out["down_from_open"] = ((p.close < o) & valid_o).sum(axis=1)
    out["n_open"] = valid_o.sum(axis=1)
    # up / down on volume (volume above prior-20-day average)
    avg20 = p.volume.where(traded).rolling(20, min_periods=10).mean().shift(1)
    hi_vol = (p.volume > avg20) & traded
    out["up_on_volume"] = ((p.ret > 0) & hi_vol).sum(axis=1)
    out["down_on_volume"] = ((p.ret < 0) & hi_vol).sum(axis=1)
    # stages
    w, sl = c["weinstein_ma"], c["weinstein_slope_lookback"]
    lvl = p.level
    ma = lvl.rolling(w, min_periods=w).mean()
    rising, falling = ma > ma.shift(sl), ma < ma.shift(sl)
    active = traded.rolling(ACTIVE_WINDOW, min_periods=1).max().astype(bool) & p.listed
    elig = eligible(p.age, w + sl) & active & ma.notna()
    above = lvl > ma
    out["stage1"] = (~above & ~falling & elig).sum(axis=1)
    out["stage2"] = (above & rising & elig).sum(axis=1)
    out["stage3"] = (above & ~rising & elig).sum(axis=1)
    out["stage4"] = (~above & falling & elig).sum(axis=1)
    out["n_stage"] = elig.sum(axis=1)
    out.index.name = "date"
    return out
