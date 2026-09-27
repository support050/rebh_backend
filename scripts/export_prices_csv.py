"""
╔══════════════════════════════════════════════════════════════════════════╗
║          TASI Historical Prices Exporter — Script 1                      ║
║          بيانات التداول الأساسية فقط (OHLCV)                             ║
║                                                                          ║
║  الأعمدة المُصدَّرة:                                                       ║
║    Industry Group | Symbol | Company Name | Date |                       ║
║    Open | High | Low | Close | Change | % Change |                       ║
║    Volume Traded | Value Traded (SAR) | No. of Trades                   ║
║    (+ Market Cap | Sector | Industry | Sub Industry)                     ║
║                                                                          ║
║  المصدر: جدول prices فقط — لا JOIN                                       ║
║                                                                          ║
║  طريقة الاستخدام:                                                         ║
║    python scripts/export_prices_csv.py                    ← كل الأسهم   ║
║    python scripts/export_prices_csv.py --symbol 1120     ← سهم واحد     ║
║    python scripts/export_prices_csv.py --split-by-symbol ← ملف لكل سهم ║
║    python scripts/export_prices_csv.py --from 2020-01-01 ← من تاريخ    ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
import pandas as pd
from sqlalchemy import text

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine

DEFAULT_OUTPUT_DIR = BACKEND_ROOT / "output" / "prices_export"

# ─── أعمدة الأسعار بترتيبها المطلوب في الملف النهائي ────────────────────────
PRICE_COLUMNS_ORDERED = [
    "industry_group",
    "symbol",
    "company_name",
    "date",
    "open",
    "high",
    "low",
    "close",
    "change",
    "change_percent",
    "volume_traded",
    "value_traded_sar",
    "no_of_trades",
    "market_cap",
    "sector",
    "industry",
    "sub_industry",
]

# ─── أسماء الأعمدة المقروءة في ملف CSV ──────────────────────────────────────
COLUMN_RENAME_MAP = {
    "industry_group":   "Industry Group",
    "symbol":           "Symbol",
    "company_name":     "Company Name",
    "date":             "Date",
    "open":             "Open",
    "high":             "High",
    "low":              "Low",
    "close":            "Close",
    "change":           "Change",
    "change_percent":   "% Change",
    "volume_traded":    "Volume Traded",
    "value_traded_sar": "Value Traded (SAR)",
    "no_of_trades":     "No. of Trades",
    "market_cap":       "Market Cap",
    "sector":           "Sector",
    "industry":         "Industry",
    "sub_industry":     "Sub Industry",
}


def build_query(symbol=None, from_date=None, to_date=None):
    """بناء استعلام SQL لجدول prices مع التصفية الاختيارية."""
    cols_sql = ",\n    ".join(PRICE_COLUMNS_ORDERED)
    where_clauses = ["1=1"]
    params = {}

    if symbol:
        where_clauses.append("symbol = :symbol")
        params["symbol"] = str(symbol)
    if from_date:
        where_clauses.append("date >= :from_date")
        params["from_date"] = str(from_date)
    if to_date:
        where_clauses.append("date <= :to_date")
        params["to_date"] = str(to_date)

    where_sql = " AND ".join(where_clauses)
    query = f"""
    SELECT
        {cols_sql}
    FROM prices
    WHERE {where_sql}
    ORDER BY symbol ASC, date ASC
    """
    return query, params


def export_data(symbol=None, from_date=None, to_date=None,
                split_by_symbol=False, output_dir=DEFAULT_OUTPUT_DIR):

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(" 📊 تصدير بيانات التداول الأساسية (OHLCV) — Script 1")
    print("=" * 70)
    if symbol:
        print(f" • السهم: {symbol}")
    if from_date:
        print(f" • من تاريخ: {from_date}")
    if to_date:
        print(f" • إلى تاريخ: {to_date}")
    print(f" • المجلد: {out_dir}")
    print(f" • الأعمدة: {len(PRICE_COLUMNS_ORDERED)} عمود من جدول prices")

    start_time = time.time()
    query, params = build_query(symbol, from_date, to_date)

    print("\n⚡ جاري سحب البيانات...")
    chunk_size = 100_000  # prices أخف بكثير من الجدول المدمج

    if split_by_symbol:
        print(" 📁 النمط: ملف CSV منفصل لكل سهم")
        total_rows = 0
        symbols_seen = set()

        with engine.connect().execution_options(stream_results=True) as conn:
            for chunk in pd.read_sql(text(query), conn, params=params, chunksize=chunk_size):
                total_rows += len(chunk)
                symbols_seen.update(chunk["symbol"].dropna().unique())

                chunk.rename(columns=COLUMN_RENAME_MAP, inplace=True)
                for sym, group in chunk.groupby("Symbol"):
                    fpath = out_dir / f"TASI_prices_{sym}.csv"
                    write_header = not fpath.exists()
                    group.to_csv(fpath, mode="a", index=False,
                                 header=write_header, encoding="utf-8-sig")

                elapsed = round(time.time() - start_time, 1)
                print(f"   ↳ {total_rows:,} صف... ({elapsed}s)")

        duration = round(time.time() - start_time, 2)
        print("\n" + "=" * 70)
        print(f" ✅ تم!")
        print(f" • إجمالي الصفوف: {total_rows:,}")
        print(f" • عدد الأسهم: {len(symbols_seen)}")
        print(f" • الوقت: {duration} ثانية")
        print(f" • المجلد: {out_dir}")
        print("=" * 70)

    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"TASI_prices_{symbol or 'ALL'}_{ts}.csv"
        fpath = out_dir / fname
        print(f" 📄 النمط: ملف واحد → {fname}")

        first_chunk = True
        total_rows = 0

        with engine.connect().execution_options(stream_results=True) as conn:
            for chunk in pd.read_sql(text(query), conn, params=params, chunksize=chunk_size):
                total_rows += len(chunk)
                chunk.rename(columns=COLUMN_RENAME_MAP, inplace=True)
                chunk.to_csv(fpath,
                             mode="w" if first_chunk else "a",
                             index=False,
                             header=first_chunk,
                             encoding="utf-8-sig")
                first_chunk = False
                elapsed = round(time.time() - start_time, 1)
                print(f"   ↳ {total_rows:,} صف... ({elapsed}s)")

        duration = round(time.time() - start_time, 2)
        print("\n" + "=" * 70)
        print(f" ✅ تم!")
        print(f" • إجمالي الصفوف: {total_rows:,}")
        print(f" • الوقت: {duration} ثانية")
        print(f" • الملف: {fpath}")
        print("=" * 70)


def main():
    p = argparse.ArgumentParser(
        description="تصدير بيانات التداول الأساسية (OHLCV) من جدول prices"
    )
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--from", dest="from_date", type=str, default=None)
    p.add_argument("--to", dest="to_date", type=str, default=None)
    p.add_argument("--split-by-symbol", action="store_true")
    p.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR))
    args = p.parse_args()

    export_data(
        symbol=args.symbol,
        from_date=args.from_date,
        to_date=args.to_date,
        split_by_symbol=args.split_by_symbol,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
