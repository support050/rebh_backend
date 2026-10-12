"""Corporate-action detection and price adjustment.

Why detection instead of a reference file: no corporate-action feed is available for TASI.
The source history is back-adjusted up to `detect_from_date` (verified in Phase 1: no
unadjusted jump between 2010 and Nov-2025). After that date the daily feed is raw, so we
detect ex-dates from the data itself and splice the gap.

Definitions
-----------
prev_close : last valid close before the ex-day (suspended days skipped)
ex_open    : the ex-day open (falls back to ex-day close if open is missing)
factor     : prev_close / ex_open            ("gap splice")
             All prices strictly BEFORE ex_date are divided by `factor`; volumes multiplied.
share_ratio: shares(ex_day) / shares(prev day) from Market Cap / Close, when Market Cap exists.
             |share_ratio - factor| <= tol  -> bonus_split   (factor snapped to common ratio)
             share_ratio > factor + tol     -> rights_issue  (factor kept as splice; TERP unknown)
             no market cap                  -> inferred      (factor kept as splice)

The ex-day return of the stock is set to NaN downstream (`adj_event` flag) because the
true economic return of that day is unknown without the subscription price.

Manual overrides: config/corporate_actions_override.csv with columns
    symbol, ex_date, action_type, factor   (action_type 'ignore' drops a detected event)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

EVENT_COLS = ["symbol", "ex_date", "action_type", "prev_close", "ex_open", "raw_factor",
              "share_ratio", "factor", "confidence", "source"]


def _parse_ratio(v) -> float:
    if isinstance(v, str) and "/" in v:
        a, b = v.split("/")
        return float(a) / float(b)
    return float(v)


def _snap(x: float, ratios: list, tol: float) -> float:
    r = np.asarray([_parse_ratio(v) for v in ratios], dtype=float)
    i = int(np.argmin(np.abs(r / x - 1)))
    return float(r[i]) if abs(r[i] / x - 1) <= tol else float(x)


def detect_events(clean: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Scan cleaned (unadjusted) prices for ex-day gaps from `detect_from_date` onward."""
    ca = cfg["corporate_actions"]
    start = pd.Timestamp(ca["detect_from_date"])
    df = clean.sort_values(["symbol", "date"]).copy()
    g = df.groupby("symbol")
    df["rank"] = g.cumcount()
    df["prev_close"] = g["close"].ffill().groupby(df["symbol"]).shift()
    df["ref_open"] = df["open"].fillna(df["close"])
    shares = (df["market_cap"] / df["close"]).where(df["close"] > 0)
    df["shares_prev"] = shares.groupby(df["symbol"]).ffill().groupby(df["symbol"]).shift()
    df["share_ratio"] = shares / df["shares_prev"]

    cand = df[(df["date"] >= start) & (~df["is_suspended"]) & (df["rank"] >= ca["ignore_first_n_days"])
              & (df["prev_close"] >= ca["min_price"])
              & ((df["ref_open"] / df["prev_close"] - 1).abs() > ca["jump_threshold"])].copy()
    if cand.empty:
        return pd.DataFrame(columns=EVENT_COLS)

    cand["raw_factor"] = cand["prev_close"] / cand["ref_open"]
    tol = ca["share_ratio_tolerance"]
    typ, fac, conf = [], [], []
    for _, r in cand.iterrows():
        rf, sr = float(r["raw_factor"]), r["share_ratio"]
        if pd.notna(sr) and abs(sr - rf) <= tol * max(rf, 1):
            typ.append("bonus_split"); fac.append(_snap(rf, ca["snap_ratios"], ca["snap_tolerance"])); conf.append("high")
        elif pd.notna(sr) and sr > rf + tol:
            typ.append("rights_issue"); fac.append(rf); conf.append("medium")
        elif pd.notna(sr):
            typ.append("capital_change"); fac.append(rf); conf.append("medium")
        else:
            typ.append("inferred"); fac.append(_snap(rf, ca["snap_ratios"], ca["snap_tolerance"])); conf.append("low")
    ev = pd.DataFrame({
        "symbol": cand["symbol"].values, "ex_date": cand["date"].values, "action_type": typ,
        "prev_close": cand["prev_close"].values, "ex_open": cand["ref_open"].values,
        "raw_factor": cand["raw_factor"].values, "share_ratio": cand["share_ratio"].values,
        "factor": fac, "confidence": conf, "source": "detected",
    })
    return ev[EVENT_COLS].reset_index(drop=True)


def apply_overrides(events: pd.DataFrame, overrides_path: str | Path | None) -> pd.DataFrame:
    if not overrides_path or not Path(overrides_path).exists():
        return events
    ov = pd.read_csv(overrides_path)
    if ov.empty:
        return events
    ov["ex_date"] = pd.to_datetime(ov["ex_date"])
    ov["symbol"] = ov["symbol"].astype(int)
    key = ["symbol", "ex_date"]
    ev = events.merge(ov[key].assign(_drop=True), on=key, how="left")
    ev = ev[ev["_drop"].isna()].drop(columns="_drop")
    keep = ov[ov["action_type"].str.lower() != "ignore"].copy()
    keep["source"] = "override"
    for c in EVENT_COLS:
        if c not in keep.columns:
            keep[c] = np.nan
    return pd.concat([ev, keep[EVENT_COLS]], ignore_index=True).sort_values(key).reset_index(drop=True)


def apply_adjustments(clean: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Add cumulative `adj_factor` per row and adjusted OHLC / volume / last_close columns.

    adj_factor(row) = product of factors of all events of that symbol with ex_date > row.date
    price_adj = price / adj_factor ; volume_adj = volume * adj_factor
    """
    df = clean.sort_values(["symbol", "date"]).copy()
    df["adj_factor"] = 1.0
    df["adj_event"] = False
    if events is not None and not events.empty:
        for sym, ev in events.groupby("symbol"):
            idx = df.index[df["symbol"] == sym]
            dates = df.loc[idx, "date"].to_numpy()
            f = np.ones(len(idx))
            for _, e in ev.iterrows():
                f[dates < np.datetime64(pd.Timestamp(e["ex_date"]))] *= float(e["factor"])
                df.loc[idx[dates == np.datetime64(pd.Timestamp(e["ex_date"]))], "adj_event"] = True
            df.loc[idx, "adj_factor"] = f
    for c in ["open", "high", "low", "close", "last_close"]:
        df[f"{c}_adj"] = df[c] / df["adj_factor"]
    df["volume_adj"] = df["volume"] * df["adj_factor"]
    return df
