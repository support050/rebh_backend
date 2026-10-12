"""Family A — Participation (trend breadth).

Definitions (all on adjusted prices, day T uses data <= T):

  pct_above_ema{w} / pct_above_sma{w}
      100 * #(level_T > MA_w(level)_T) / #eligible, eligible = age >= w AND active
      active = the stock traded at least once in the last `active_window` days (prolonged
      suspensions are excluded; their stale price would otherwise freeze the metric).

  Weinstein stage (30-week MA = 150d SMA, slope over 20 days):
      stage2 : level > MA150 AND MA150 > MA150[t-20]      (advancing)
      stage4 : level < MA150 AND MA150 < MA150[t-20]      (declining)
      stage1/3 otherwise (basing / topping — not separated here)

  Minervini trend template (8 criteria):
      1. level > MA150 and level > MA200      2. MA150 > MA200
      3. MA200 rising for >= 20 days           4. MA50 > MA150 > MA200
      5. level > MA50                          6. level >= 1.25 x 52w low
      7. level >= 0.75 x 52w high              8. RS percentile >= 70
         RS = 126-day return, percentile-ranked across eligible stocks each day.

  52-week new highs / lows (close-based, strictly above/below the PRIOR 252 days):
      NH_T = #(close_T > max(level[T-252..T-1]))   requires age >= 253 and traded on T
      NL_T = #(close_T < min(level[T-252..T-1]))
      (strict + prior-window: a flat stock is neither a new high nor a new low)
      net_nh_nl = NH - NL ; hl_index = 10d SMA of NH/(NH+NL) (NaN when NH+NL == 0)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels, eligible, pct_true

ACTIVE_WINDOW = 10


def _active(p: Panels) -> pd.DataFrame:
    traded = p.close.notna()
    return traded.rolling(ACTIVE_WINDOW, min_periods=1).max().astype(bool) & p.listed


def trend_template(p: Panels, cfg: dict, active: pd.DataFrame | None = None):
    """Minervini trend template per stock. Returns (passes, eligible, rs_percentile) panels."""
    m = cfg["indicators"]["participation"]["minervini"]
    lvl = p.level
    if active is None:
        active = _active(p)
    ma50 = lvl.rolling(m["ma_fast"], min_periods=m["ma_fast"]).mean()
    ma150 = lvl.rolling(m["ma_mid"], min_periods=m["ma_mid"]).mean()
    ma200 = lvl.rolling(m["ma_slow"], min_periods=m["ma_slow"]).mean()
    hi52 = lvl.rolling(252, min_periods=252).max()
    lo52 = lvl.rolling(252, min_periods=252).min()
    rs_ret = lvl / lvl.shift(m["rs_lookback"]) - 1
    elig_m = eligible(p.age, max(m["ma_slow"] + m["ma_slow_rising_days"], 252)) & active
    rs_pct = rs_ret.where(elig_m).rank(axis=1, pct=True) * 100
    tt = ((lvl > ma150) & (lvl > ma200) & (ma150 > ma200)
          & (ma200 > ma200.shift(m["ma_slow_rising_days"]))
          & (ma50 > ma150) & (lvl > ma50)
          & (lvl >= (1 + m["above_52w_low_pct"]) * lo52)
          & (lvl >= (1 - m["within_52w_high_pct"]) * hi52)
          & (rs_pct >= m["rs_min_percentile"]))
    return tt & elig_m, elig_m, rs_pct


def rs_rating(p: Panels, active: pd.DataFrame | None = None) -> pd.DataFrame:
    """IBD-style relative-strength rating (1-99): weighted return 2x3M + 6M + 9M + 12M, percentile-ranked
    across active stocks with >= 252 days of history, each day."""
    lvl = p.level
    if active is None:
        active = _active(p)
    r = lambda n: lvl / lvl.shift(n) - 1
    score = 2 * r(63) + r(126) + r(189) + r(252)
    elig = eligible(p.age, 253) & active
    return (score.where(elig).rank(axis=1, pct=True) * 99).round()


def compute_participation(p: Panels, cfg: dict) -> pd.DataFrame:
    c = cfg["indicators"]["participation"]
    lvl = p.level
    active = _active(p)
    out = {}

    mas = {}
    for w in c["ema_windows"]:
        mas[f"ema{w}"] = lvl.ewm(span=w, adjust=False, min_periods=w).mean()
    for w in c["sma_windows"]:
        mas[f"sma{w}"] = lvl.rolling(w, min_periods=w).mean()
    for name, ma in mas.items():
        w = int("".join(ch for ch in name if ch.isdigit()))
        elig = eligible(p.age, w) & active
        pct, n = pct_true(lvl > ma, elig)
        out[f"pct_above_{name}"] = pct
        out[f"n_{name}"] = n

    # ---- Weinstein
    wma = c["weinstein_ma"]; sl = c["weinstein_slope_lookback"]
    ma_w = lvl.rolling(wma, min_periods=wma).mean()
    rising = ma_w > ma_w.shift(sl)
    falling = ma_w < ma_w.shift(sl)
    elig_w = eligible(p.age, wma + sl) & active
    out["pct_stage2"], out["n_weinstein"] = pct_true((lvl > ma_w) & rising, elig_w)
    out["pct_stage4"], _ = pct_true((lvl < ma_w) & falling, elig_w)

    # ---- Minervini
    tt, elig_m, _ = trend_template(p, cfg, active)
    out["pct_minervini"], out["n_minervini"] = pct_true(tt, elig_m)

    # ---- 52-week highs / lows on actual closes
    w = c["new_high_low_window"]
    cl = p.close
    roll_max = p.level.rolling(w, min_periods=w).max().shift()
    roll_min = p.level.rolling(w, min_periods=w).min().shift()
    elig_h = eligible(p.age, w + 1) & cl.notna()
    nh = ((cl > roll_max) & elig_h).sum(axis=1)
    nl = ((cl < roll_min) & elig_h).sum(axis=1)
    out["new_highs"] = nh; out["new_lows"] = nl
    # new highs on volume expansion (Minervini): NH with volume > prior 20-day average volume
    avg20 = p.volume.where(cl.notna()).rolling(20, min_periods=10).mean().shift(1)
    out["new_highs_vol"] = ((cl > roll_max) & elig_h & (p.volume > avg20)).sum(axis=1)
    out["n_nhnl"] = elig_h.sum(axis=1)
    out["net_nh_nl"] = nh - nl
    ratio = nh / (nh + nl).replace(0, np.nan)
    out["hl_index"] = ratio.rolling(c["high_low_index_ma"], min_periods=1).mean()
    out["pct_new_highs"] = 100 * nh / out["n_nhnl"].replace(0, np.nan)
    out["pct_new_lows"] = 100 * nl / out["n_nhnl"].replace(0, np.nan)

    res = pd.DataFrame(out)
    res.index.name = "date"
    return res


def compute_weekly_stages(p: Panels, cfg: dict) -> pd.DataFrame:
    """Weinstein stages on WEEKLY bars (his original setting): weekly last close, 30-week SMA,
    slope over 4 weeks. Eligible = >= 34 weeks of history and traded in the last 2 weeks.
      stage1 : close < MA and MA not falling      stage2 : close > MA and MA rising
      stage3 : close > MA and MA not rising       stage4 : close < MA and MA falling
    Returns weekly rows (index = last trading day of the week) with counts and % per stage."""
    mkt_open = p.close.notna().any(axis=1).resample("W-THU").max().fillna(False).astype(bool)   # drop holiday weeks
    wk = p.level.resample("W-THU").last()[mkt_open]            # Saudi week ends Thursday
    traded = p.close.notna().resample("W-THU").max()[mkt_open].astype(bool)
    listed = p.listed.resample("W-THU").last()[mkt_open].fillna(False).astype(bool)
    active = traded.rolling(2, min_periods=1).max().astype(bool) & listed
    age_w = p.close.notna().astype(int).resample("W-THU").max()[mkt_open].cumsum()
    ma = wk.rolling(30, min_periods=30).mean()
    rising, falling = ma > ma.shift(4), ma < ma.shift(4)
    elig = (age_w >= 34) & active & ma.notna()
    above = wk > ma
    out = pd.DataFrame(index=wk.index)
    out["stage1_w"] = (~above & ~falling & elig).sum(axis=1)
    out["stage2_w"] = (above & rising & elig).sum(axis=1)
    out["stage3_w"] = (above & ~rising & elig).sum(axis=1)
    out["stage4_w"] = (~above & falling & elig).sum(axis=1)
    out["n_stage_w"] = elig.sum(axis=1)
    for k in (1, 2, 3, 4):
        out[f"pct_stage{k}_w"] = 100 * out[f"stage{k}_w"] / out["n_stage_w"].replace(0, np.nan)
    out = out[out["n_stage_w"] > 0]
    out.index.name = "date"
    return out
