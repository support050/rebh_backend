"""Index series: official TASI (from file) + Equal-Weight TASI built from the universe.

Equal-Weight TASI
-----------------
EW return on day T = simple mean of (close_adj_T / prev_valid_close_adj - 1) over stocks that
  (a) are in the universe (common stocks, no REITs),
  (b) are not suspended on T,
  (c) have a valid previous close (i.e. not their first row), and
  (d) are not on an adjustment ex-day (adj_event) — that day's return is unknown.
Chained from `equal_weight_base_value` at `equal_weight_base_date`. Days with fewer than
`min_stocks_per_day` eligible stocks carry the previous level (return 0, flagged).

Note: TASI is cap-weighted with free-float. Because no share-count history exists before
2026-01-29, no cap-weighted reconstruction is attempted here (see Phase 1 report, family F).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def stock_returns(adj: pd.DataFrame) -> pd.DataFrame:
    """Per-row daily return on adjusted closes; NaN on suspended / first / ex-day rows."""
    df = adj.sort_values(["symbol", "date"]).copy()
    prev = df.groupby("symbol")["close_adj"].ffill().groupby(df["symbol"]).shift()
    df["ret"] = df["close_adj"] / prev - 1
    df.loc[df["is_suspended"] | df["adj_event"] | prev.isna(), "ret"] = np.nan
    df["prev_close_adj"] = prev
    return df


def build_equal_weight(adj: pd.DataFrame, master: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ic = cfg["index"]
    uni = set(master.loc[master["in_universe"], "symbol"])
    df = stock_returns(adj[adj["symbol"].isin(uni)])
    daily = df.groupby("date")["ret"].agg(ew_ret="mean", n_stocks="count").reset_index()
    base = pd.Timestamp(ic["equal_weight_base_date"])
    daily = daily[daily["date"] >= base].copy()
    thin = daily["n_stocks"] < ic["min_stocks_per_day"]
    daily.loc[thin | daily["ew_ret"].isna(), "ew_ret"] = 0.0
    daily["thin_day"] = thin
    daily["close"] = ic["equal_weight_base_value"] * (1 + daily["ew_ret"]).cumprod()
    daily["index_code"] = "EW_TASI"
    return daily[["index_code", "date", "close", "ew_ret", "n_stocks", "thin_day"]].reset_index(drop=True)


def combine_indices(official: pd.DataFrame, ew: pd.DataFrame) -> pd.DataFrame:
    off = official[["index_code", "date", "open", "high", "low", "close", "volume", "value", "trades"]].copy()
    e = ew.copy()
    for c in ["open", "high", "low", "volume", "value", "trades"]:
        e[c] = np.nan
    e = e[["index_code", "date", "open", "high", "low", "close", "volume", "value", "trades", "n_stocks", "thin_day"]]
    out = pd.concat([off, e], ignore_index=True)
    return out.sort_values(["index_code", "date"]).reset_index(drop=True)
