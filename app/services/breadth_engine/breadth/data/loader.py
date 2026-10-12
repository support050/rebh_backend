"""Raw loaders: CSV -> typed DataFrames (no cleaning here, only parsing).

Zero look-ahead is a property of the *consumers*; the loader only guarantees
that every row carries the trading date it belongs to.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

RAW_COLUMNS = {
    "Industry Group": "industry_group",
    "Symbol": "symbol",
    "Company Name": "name",
    "Date": "date",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Change": "src_change",
    "% Change": "src_pct_change",
    "Volume Traded": "volume",
    "Value Traded (SAR)": "value",
    "No. of Trades": "trades",
    "Market Cap": "market_cap",
    "Sector": "sector",
    "Industry": "industry",
    "Sub Industry": "sub_industry",
}


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_prices_csv(path: str | Path) -> pd.DataFrame:
    """Parse the stock-level CSV. Keeps every row (REITs, delisted, etc.)."""
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    missing = set(RAW_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Price CSV is missing columns: {sorted(missing)}")
    df = df.rename(columns=RAW_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df["symbol"] = df["symbol"].astype("int32")
    for c in ["open", "high", "low", "close", "volume", "value", "trades", "market_cap",
              "src_change", "src_pct_change"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in ["industry_group", "name", "sector", "industry", "sub_industry"]:
        df[c] = df[c].astype("string").str.strip()
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["source_row_id"] = df.index.astype("int64")
    if df.duplicated(["symbol", "date"]).any():
        raise ValueError("Duplicate (symbol, date) rows in price CSV")
    return df


def load_index_csv(path: str | Path) -> pd.DataFrame:
    """Parse the TASI 'historical reports' CSV (thousand separators, '-' for missing)."""
    df = pd.read_csv(path, encoding="utf-8-sig", thousands=",", na_values=["-"])
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Date": "date", "Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume Traded": "volume",
                            "Value Traded": "value", "No. of Trades": "trades"})
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    for c in ["open", "high", "low", "close", "volume", "value", "trades"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df = df.sort_values("date").reset_index(drop=True)
    if df["date"].duplicated().any():
        raise ValueError("Duplicate dates in index CSV")
    df["index_code"] = "TASI"
    return df
