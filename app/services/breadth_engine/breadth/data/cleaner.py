"""Cleaning rules (Phase 1). Every rule is driven by config['cleaning'] / config['universe'].

Output schema of `normalize_prices` (one row per symbol x trading day between the
symbol's first and last date):

    symbol, date, open, high, low, close, volume, value, trades, market_cap,
    is_suspended   : no trade that day (volume 0 / close 0 / row absent in source)
    is_listed      : True between first and last source date (inclusive)
    quality_flag   : "ok" | "open_missing" | "hl_clamped" | "row_inserted" | ...
    source_row_id  : row id in raw parquet (-1 for inserted rows)

`close` is NaN on suspended rows. `last_close` is the last valid close (forward-filled)
so that level-type indicators (%>50DMA) can still position the stock, while
movement-type indicators (A/D, NH/NL, 4% moves) must use `close` and skip NaN.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PRICE_COLS = ["open", "high", "low", "close", "volume", "value", "trades", "market_cap"]


# --------------------------------------------------------------------------- calendar
def build_calendar(prices: pd.DataFrame, index_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Trading calendar = every date on which at least one stock has a row, union index dates."""
    dates = pd.Index(prices["date"].unique())
    if index_df is not None:
        dates = dates.union(pd.Index(index_df["date"].unique()))
    dates = dates.sort_values()
    n_rows = prices.groupby("date").size().reindex(dates).fillna(0).astype(int)
    cal = pd.DataFrame({"date": dates, "n_symbol_rows": n_rows.values})
    cal["is_trading_day"] = True
    cal["weekday"] = cal["date"].dt.dayofweek  # Mon=0 ... Sun=6
    return cal.reset_index(drop=True)


# --------------------------------------------------------------------------- universe
def _excluded(symbols: pd.Series, cfg: dict) -> pd.Series:
    u = cfg["universe"]
    mask = symbols.isin(u.get("exclude_symbols", []))
    for lo, hi in u.get("exclude_symbol_ranges", []):
        mask |= symbols.between(lo, hi)
    return mask


def build_symbols_master(prices: pd.DataFrame, cfg: dict, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """One row per symbol: names, listing window, classification, security type."""
    u = cfg["universe"]
    as_of = as_of or prices["date"].max()
    g = prices.groupby("symbol")
    m = pd.DataFrame({
        "name_first": g["name"].first(),
        "name_last": g["name"].last(),
        "list_date": g["date"].min(),
        "last_date": g["date"].max(),
        "n_rows": g.size(),
        "industry_group": g["industry_group"].agg(lambda s: s.dropna().replace("Unknown", pd.NA).dropna().mode().iat[0]
                                                  if s.dropna().replace("Unknown", pd.NA).dropna().size else pd.NA),
        "sector_src": g["sector"].agg(lambda s: s.dropna().mode().iat[0] if s.dropna().size else pd.NA),
    }).reset_index()
    # source fixes / manual classifications
    for sym, ig in (u.get("industry_group_fixes") or {}).items():
        m.loc[m["symbol"] == int(sym), "industry_group"] = ig
    manual = u.get("industry_group_manual") or {}
    m["classification_source"] = "source"
    for sym, ig in manual.items():
        sel = (m["symbol"] == int(sym)) & m["industry_group"].isna()
        m.loc[sel, "industry_group"] = ig
        m.loc[sel, "classification_source"] = "manual_unverified"
    ig2s = u.get("industry_group_to_sector") or {}
    m["sector"] = m["sector_src"]
    m.loc[m["sector"].isna(), "sector"] = m.loc[m["sector"].isna(), "industry_group"].map(ig2s)
    # isin() instead of =="REITs": industry_group is a nullable StringDtype column, so
    # `==` yields pd.NA for a symbol the source never classified and the next line's
    # np.where() then dies with "TypeError: boolean value of NA is ambiguous", taking the
    # whole breadth run down with it. isin() returns plain bool (NA -> False), which is
    # the correct reading anyway: an unknown group is not a REIT. No effect on any
    # classified symbol; no formula touched.
    m["is_reit"] = m["symbol"].between(4330, 4359) | m["industry_group"].isin(["REITs"])
    m["security_type"] = np.where(m["is_reit"], "reit", "common")
    m["in_universe"] = ~_excluded(m["symbol"], cfg) & ~m["is_reit"]
    m["status"] = np.where(m["last_date"] >= as_of, "active", "delisted")
    m["delist_date"] = m["last_date"].where(m["status"] == "delisted")
    return m.sort_values("symbol").reset_index(drop=True)


# --------------------------------------------------------------------------- prices
def normalize_prices(prices: pd.DataFrame, calendar: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Apply the cleaning rules. Input: raw parsed prices (any symbols). Output: see module doc."""
    c = cfg["cleaning"]
    df = prices[["symbol", "date"] + PRICE_COLS + ["source_row_id"]].copy()
    df["quality_flag"] = "ok"

    # 1) zero OHL with volume => missing, not zero
    if c["zero_ohl_is_missing"]:
        for col in ["open", "high", "low"]:
            bad = df[col] <= 0
            df.loc[bad, col] = np.nan
        df.loc[df["open"].isna() & (df["volume"] > 0), "quality_flag"] = "open_missing"

    # 2) suspension / no-trade rows
    susp = pd.Series(False, index=df.index)
    if c["suspended_if_volume_zero"]:
        susp |= (df["volume"].fillna(0) <= 0)
    if c["suspended_if_close_zero"]:
        susp |= (df["close"].fillna(0) <= 0)
    df["is_suspended"] = susp
    df.loc[susp, ["open", "high", "low", "close"]] = np.nan
    df.loc[susp, ["volume", "value", "trades"]] = 0.0

    # 3) clamp High/Low to contain Open and Close (source rounding artefacts)
    if c["clamp_high_low"]:
        oc_max = df[["open", "close"]].max(axis=1)
        oc_min = df[["open", "close"]].min(axis=1)
        viol = ((df["high"] < oc_max) | (df["low"] > oc_min)) & ~susp
        df.loc[viol, "quality_flag"] = "hl_clamped"
        df["high"] = df[["high", "open", "close"]].max(axis=1)
        df["low"] = df[["low", "open", "close"]].min(axis=1)

    # 4) re-insert missing trading days between first and last date as suspended rows
    if c["fill_missing_days_as_suspended"]:
        cal_dates = calendar["date"].sort_values().to_numpy()
        span = df.groupby("symbol")["date"].agg(["min", "max"])
        pieces = []
        for sym, (d0, d1) in span.iterrows():
            d = cal_dates[(cal_dates >= np.datetime64(d0)) & (cal_dates <= np.datetime64(d1))]
            pieces.append(pd.DataFrame({"symbol": sym, "date": d}))
        full = pd.concat(pieces, ignore_index=True)
        full["symbol"] = full["symbol"].astype(df["symbol"].dtype)
        df = full.merge(df, on=["symbol", "date"], how="left")
        inserted = df["source_row_id"].isna()
        df.loc[inserted, "is_suspended"] = True
        df.loc[inserted, "quality_flag"] = "row_inserted"
        df.loc[inserted, ["volume", "value", "trades"]] = 0.0
        df["source_row_id"] = df["source_row_id"].fillna(-1).astype("int64")
        df["is_suspended"] = df["is_suspended"].astype(bool)

    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["is_listed"] = True
    df["last_close"] = df.groupby("symbol")["close"].ffill()
    return df
