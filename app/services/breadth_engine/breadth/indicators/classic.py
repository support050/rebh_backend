"""Classic breadth library — the indicators the wider breadth literature uses that are not in families A–G.
Every one is tested on TASI (breadth.backtest / expert review Sep-2026) and carries its verdict in `VERDICTS`.

  hlli        Fosback High-Low Logic Index: 50-day mean of min(NH, NL) / issues, in %.  >1 = split market
  abi         Fosback Absolute Breadth Index: 10-day mean of |A - D| / issues, in %
  hindenburg  Miekka's Hindenburg Omen (TASI-adapted): NH/issues >= 2.2% AND NL/issues >= 2.2% on the same day,
              TASI above its close 50 days ago, McClellan Oscillator < 0, NH <= 2 x NL.  `hindenburg_confirmed`
              = a second omen within 36 days.
  titanic     Ohama's Titanic Syndrome: NL > NH within 7 days of a 52-week closing high of TASI
  sto_b       Swenlin Trading Oscillator (breadth): 4-day EMA of the 5-day sum of (A - D)
  itbm        DecisionPoint ITBM: 20-day EMA of 100 (A - D)/(A + D);  itbm_signal = 10-day EMA of itbm
  mvo         McClellan Volume Oscillator: EMA19 - EMA39 of 1000 (UV - DV)/(UV + DV);  mvo_sum cumulative
  cvi         Cumulative Volume Index: cumulative (UV - DV)
  zweig_91    Zweig 9:1 up-volume day (UV share >= 0.90); zweig_double_91 = second 9:1 day within 60 days
  nh_pct_10   10-day mean of NH / issues, in %  (Delwiche-style new-high list expansion)
  haurlan_st  Haurlan short-term index: 3-day EMA of (A - D)
  ad_pct_21   21-day mean of 100 (A - D)/(A + D)  (breadth momentum / A-D percent)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.thrusts import first_of_run

VERDICTS = {  # filled from the TASI tests (2007-2026); shown on the dashboard
    "hlli": ("Fosback", "HLLI > 1 (split market): n=190 days, −3.2% / 60d, 33% hit — the strongest NEGATIVE state found; scored as a warning", "warning"),
    "abi": ("Fosback", "ABI 10–20% (quiet tape): +2.4% / 60d (66%); >30% no edge — context", "context"),
    "hindenburg": ("Miekka", "n=20 omens: −1.9% / 60d, 35% hit — consistent with HLLI; too few to score, shown as a warning flag", "warning"),
    "titanic": ("Ohama", "n=18: +0.2% / 60d — no edge on TASI; shown for audit", "no edge"),
    "sto_b": ("Swenlin / DecisionPoint", "< 5th percentile: +2.3% / 60d (61%) — oversold bounce, similar to McClellan", "context"),
    "itbm": ("Swenlin / DecisionPoint", "above signal +0.7% / 20d vs 0.0% below — mild trend filter", "context"),
    "mvo": ("McClellan (volume)", "< 5th pct: +1.6% / 20d, +2.3% / 60d — volume confirms the price oscillator", "context"),
    "cvi": ("classic", "below its 50-day MA: −0.2% / 60d vs +1.2% above — mild", "context"),
    "zweig_91": ("Zweig", "double 9:1 within 60d: n=56, +1.3% / 60d (55%) — no edge on TASI (90% days are common here)", "no edge"),
    "nh_pct_10": ("Delwiche / Stack", "1–3% (thin leadership): −0.4% / 60d; >6%: 66% hit — new-high expansion matters more than its level", "context"),
    "haurlan_st": ("Haurlan", "sign: +0.7% vs 0.0% / 20d — mild", "context"),
    "ad_pct_21": ("Appel / classic", ">10: +1.3% / 20d then fades — short-horizon only", "context"),
    "bpi": ("Dorsey / Chartcraft", "level buckets no edge; status 'Bull Alert' +1.5% / 20d (60%), 'Bear Alert' −0.8% / 20d — use the status, not the level", "context"),
}


def compute_classic(ad: pd.DataFrame, part: pd.DataFrame, tasi: pd.Series, cfg: dict) -> pd.DataFrame:
    c = cfg["indicators"].get("classic", {})
    A, D = ad["advances"], ad["declines"]; n = ad["n_ad"]
    NH = part["new_highs"].reindex(ad.index); NL = part["new_lows"].reindex(ad.index); nn = part["n_nhnl"].reindex(ad.index)
    tc = tasi.reindex(ad.index)
    out = pd.DataFrame(index=ad.index)
    out["hlli"] = (np.minimum(NH, NL) / nn.replace(0, np.nan)).rolling(c.get("hlli_window", 50), min_periods=20).mean() * 100
    out["abi"] = ((A - D).abs() / n.replace(0, np.nan)).rolling(10, min_periods=5).mean() * 100
    th = c.get("hindenburg_pct", 0.022)
    omen = (NH / nn >= th) & (NL / nn >= th) & (tc > tc.shift(50)) & (ad["mcclellan_osc"] < 0) & (NH <= 2 * NL)
    out["hindenburg"] = omen.fillna(False)
    out["hindenburg_confirmed"] = (omen & (omen.shift(1).rolling(36, min_periods=1).sum() >= 1)).fillna(False)
    out["hindenburg_signal"] = first_of_run(out["hindenburg"], 36)
    hi52 = tc >= tc.rolling(252, min_periods=252).max()
    out["titanic"] = ((NL > NH) & hi52.rolling(7, min_periods=1).max().astype(bool)).fillna(False)
    out["titanic_signal"] = first_of_run(out["titanic"], 20)
    out["sto_b"] = (A - D).rolling(5, min_periods=5).sum().ewm(span=4, adjust=False).mean()
    out["itbm"] = (100 * (A - D) / (A + D).replace(0, np.nan)).ewm(span=20, adjust=False).mean()
    out["itbm_signal"] = out["itbm"].ewm(span=10, adjust=False).mean()
    uv, dv = ad["up_volume"], ad["down_volume"]
    rv = 1000 * (uv - dv) / (uv + dv).replace(0, np.nan)
    out["mvo"] = rv.ewm(span=19, adjust=False).mean() - rv.ewm(span=39, adjust=False).mean()
    out["mvo_sum"] = out["mvo"].cumsum()
    out["cvi"] = (uv - dv).fillna(0).cumsum()
    out["cvi_ma50"] = out["cvi"].rolling(50, min_periods=50).mean()
    d91 = ad["up_volume_pct"] >= 0.90
    out["zweig_91"] = d91.fillna(False)
    out["zweig_double_91"] = (d91 & (d91.shift(1).rolling(60, min_periods=1).sum() >= 1)).fillna(False)
    out["zweig_double_91_signal"] = first_of_run(out["zweig_double_91"], 60)
    out["nh_pct_10"] = (100 * NH / nn.replace(0, np.nan)).rolling(10, min_periods=5).mean()
    out["haurlan_st"] = (A - D).ewm(span=3, adjust=False).mean()
    out["ad_pct_21"] = (100 * (A - D) / (A + D).replace(0, np.nan)).rolling(21, min_periods=21).mean()
    out.index.name = "date"
    return out
