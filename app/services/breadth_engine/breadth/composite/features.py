"""Composite inputs: one frame of raw metrics, then rolling-percentile family scores (0-100).

Rolling percentile (no look-ahead):
  pct_T = 100 * rank of x_T among {x_{T-W+1} .. x_T}  (W = rank_window, min_periods = rank_min_periods)
  pandas rolling(...).rank(pct=True) — the window includes T itself and nothing after T.
Family score = mean of its metrics' percentiles (NaN metrics ignored, all NaN -> NaN).

Raw metrics assembled here (all from storage/features):
  pct_above_sma200, pct_above_sma50, hl_index          (A)
  mcclellan_osc, ad_line_chg20 = ad_line - ad_line[t-20] (B)
  pct_healthy, median_score                              (E)
  spread_60                                              (F)
  confirmation: 100 if index at 60d high with 0 diverging metrics, 0 if >=2 diverging,
                50 otherwise (1 diverging or no index high)                       (G)
Overlay (oversold / thrust, families C+D):
  trin_10 (pct), ratio_10_inv = -ratio_10 (pct), pct_down_x_10d = 10d mean of pct_down_x (pct),
  lowry95_down_recent = 100 if a 95% down day in the last `lowry_lookback` days else 0,
  thrust_recent = 100 if Zweig / Whaley / pct50 thrust fired in the last `thrust_lookback` days else 0
  overlay = mean of the five (0-100). High = oversold / capitulation / thrust context.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth import ROOT


def load_raw(cfg: dict) -> pd.DataFrame:
    st = ROOT / cfg["paths"]["storage_dir"] / "features"
    part = pd.read_parquet(st / "participation.parquet").set_index("date")
    ad = pd.read_parquet(st / "advance_decline.parquet").set_index("date")
    th = pd.read_parquet(st / "thrusts.parquet").set_index("date")
    mom = pd.read_parquet(st / "momentum.parquet").set_index("date")
    sec = pd.read_parquet(st / "sectors_summary.parquet").set_index("date")
    conc = pd.read_parquet(st / "concentration.parquet").set_index("date")
    div = pd.read_parquet(st / "divergences.parquet").set_index("date")
    idx = part.index
    raw = pd.DataFrame(index=idx)
    raw["pct_above_sma200"] = part["pct_above_sma200"]
    raw["pct_above_sma50"] = part["pct_above_sma50"]
    raw["hl_index"] = part["hl_index"]
    raw["mcclellan_osc"] = ad["mcclellan_osc"]
    raw["ad_line_chg20"] = ad["ad_line"] - ad["ad_line"].shift(20)
    raw["pct_healthy"] = sec["pct_healthy"].reindex(idx)
    raw["median_score"] = sec["median_score"].reindex(idx)
    raw["spread_60"] = conc["spread_60"].reindex(idx)
    conf = pd.Series(50.0, index=idx)
    hi = div["index_high"].reindex(idx).fillna(False).astype(bool)
    nb = div["n_bearish"].reindex(idx)
    conf[hi & (nb == 0)] = 100.0
    conf[hi & (nb >= 2)] = 0.0
    raw["confirmation"] = conf
    # overlay inputs
    raw["trin_10"] = ad["trin_10"]
    raw["ratio_10_inv"] = -mom["ratio_10"].reindex(idx)
    raw["pct_down_x_10d"] = mom["pct_down_x"].reindex(idx).rolling(10, min_periods=5).mean()
    ov = cfg["composite"]["overlay"]
    l95 = th["lowry_down_signal"].reindex(idx).fillna(False).astype(float)
    raw["lowry95_down_recent"] = 100 * (l95.rolling(ov["lowry_lookback"], min_periods=1).max())
    thr = (th["zweig_signal"] | th["whaley_signal"] | th["pct50_thrust_signal"]).reindex(idx).fillna(False).astype(float)
    raw["thrust_recent"] = 100 * thr.rolling(ov["thrust_lookback"], min_periods=1).max()
    raw.index.name = "date"
    return raw


def rolling_pct(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    return 100 * s.rolling(window, min_periods=min_periods).rank(pct=True)


def family_scores(raw: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = cfg["composite"]
    W, mp = c["rank_window"], c["rank_min_periods"]
    pct = pd.DataFrame(index=raw.index)
    for fam, cols in c["families"].items():
        for col in cols:
            pct[col] = raw[col] if col == "confirmation" else rolling_pct(raw[col], W, mp)
    fam = pd.DataFrame(index=raw.index)
    for f, cols in c["families"].items():
        fam[f] = pct[cols].mean(axis=1)
    # overlay
    ov_cols = []
    for col in c["overlay"]["metrics"]:
        if col in ("lowry95_down_recent", "thrust_recent"):
            pct[col] = raw[col]
        else:
            pct[col] = rolling_pct(raw[col], W, mp)
        ov_cols.append(col)
    fam["overlay"] = pct[ov_cols].mean(axis=1)
    return fam, pct
