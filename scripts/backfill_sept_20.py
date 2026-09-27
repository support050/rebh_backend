"""
backfill_sept_20.py
====================
يجلب بيانات OHLCV ليوم 2026-09-20 من yfinance ويحدث جدول prices.
ثم يعيد حساب المؤشرات الفنية.
"""
import sys
import os
import logging
from datetime import date, datetime
from pathlib import Path
from decimal import Decimal

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yfinance as yf
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app.core.database import SessionLocal
from app.models.price import Price

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TARGET_DATE = date(2026, 9, 20)
TARGET_DATE_STR = "2026-09-20"


def get_all_symbols_for_date(db, target_date) -> list:
    """Get all symbols that have data for target date."""
    result = db.execute(text(
        "SELECT DISTINCT symbol FROM prices WHERE date = :d ORDER BY symbol"
    ), {"d": target_date})
    return [r[0] for r in result.fetchall()]


def fetch_yfinance_ohlcv(symbol: str, target_date: date) -> dict | None:
    """Fetch OHLCV data for a single stock from yfinance for the target date."""
    ticker_symbol = f"{symbol}.SR"
    
    # Fetch a window around the target date
    start = target_date.isoformat()
    # yfinance end date is exclusive, so add one day
    end_date = date(2026, 9, 21)
    end = end_date.isoformat()
    
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(start=start, end=end, auto_adjust=True)
        
        if hist.empty:
            return None
        
        # Try to find the target date
        hist.index = pd.to_datetime(hist.index).normalize()
        target_ts = pd.Timestamp(target_date)
        
        if target_ts in hist.index:
            row = hist.loc[target_ts]
            return {
                "open": float(round(row["Open"], 4)),
                "high": float(round(row["High"], 4)),
                "low": float(round(row["Low"], 4)),
                "close": float(round(row["Close"], 4)),
                "volume": int(row["Volume"]),
            }
        else:
            logger.warning(f"  {symbol}: Sep 20 not found in yfinance (available: {list(hist.index.date)})")
            return None
            
    except Exception as e:
        logger.error(f"  {symbol}: yfinance error: {e}")
        return None


def update_prices_in_db(db, symbol: str, data: dict):
    """Update the prices row for symbol/date with new OHLCV data."""
    db.execute(text("""
        UPDATE prices
        SET open = :open, high = :high, low = :low, close = :close,
            volume_traded = :volume
        WHERE symbol = :symbol AND date = :date
    """), {
        "open": data["open"],
        "high": data["high"],
        "low": data["low"],
        "close": data["close"],
        "volume": data["volume"],
        "symbol": symbol,
        "date": TARGET_DATE_STR,
    })


def main():
    logger.info("=" * 60)
    logger.info(f"🗓️  Backfill Sept 20 Prices from yfinance")
    logger.info("=" * 60)

    db = SessionLocal()

    try:
        # Step 1: Get all symbols for Sep 20
        symbols = get_all_symbols_for_date(db, TARGET_DATE_STR)
        logger.info(f"📋 Found {len(symbols)} symbols with Sep 20 data to update")

        # Step 2: For each symbol, fetch yfinance data and compare
        updated = 0
        failed = 0
        identical_to_sep17 = 0

        for i, symbol in enumerate(symbols, 1):
            if i % 50 == 0:
                logger.info(f"  ... Processing {i}/{len(symbols)}")
            
            # Check current Sep 20 vs Sep 17
            result = db.execute(text("""
                SELECT p20.open as o20, p20.close as c20,
                       p17.open as o17, p17.close as c17
                FROM prices p20
                LEFT JOIN prices p17 ON p17.symbol = p20.symbol AND p17.date = '2026-09-17'
                WHERE p20.symbol = :sym AND p20.date = '2026-09-20'
            """), {"sym": symbol})
            row = result.fetchone()
            
            if not row:
                continue
            
            o20, c20, o17, c17 = row
            
            # Only update if Sep 20 is identical to Sep 17 (corrupt data)
            if o17 is not None and float(o20) == float(o17) and float(c20) == float(c17):
                identical_to_sep17 += 1
                
                # Fetch real data from yfinance
                yf_data = fetch_yfinance_ohlcv(symbol, TARGET_DATE)
                
                if yf_data:
                    update_prices_in_db(db, symbol, yf_data)
                    updated += 1
                    if updated <= 10:  # Print first 10 for verification
                        logger.info(f"  ✅ {symbol}: Sep17({o17},{c17}) → Sep20({yf_data['open']},{yf_data['close']})")
                else:
                    failed += 1

        db.commit()
        logger.info(f"\n{'=' * 60}")
        logger.info(f"📊 Summary:")
        logger.info(f"  Total symbols:          {len(symbols)}")
        logger.info(f"  Identical to Sep 17:    {identical_to_sep17}")
        logger.info(f"  Updated from yfinance:  {updated}")
        logger.info(f"  Failed/not found:       {failed}")
        logger.info(f"  Already correct:        {len(symbols) - identical_to_sep17}")
        logger.info(f"{'=' * 60}")

        # Step 3: Verify a sample
        logger.info("\n🔍 Verification - Checking 1010 (RIBL):")
        result = db.execute(text("""
            SELECT date, open, high, low, close FROM prices
            WHERE symbol = '1010' AND date >= '2026-09-17'
            ORDER BY date
        """))
        for r in result.fetchall():
            logger.info(f"  {dict(r._mapping)}")

    except Exception as e:
        db.rollback()
        logger.error(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

    logger.info("\n✅ Done! Now re-run daily_market_update.py --date 2026-09-20 to recalculate indicators.")


if __name__ == "__main__":
    main()
