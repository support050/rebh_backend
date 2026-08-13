import sys
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, date
from sqlalchemy import text
from sqlalchemy.orm import Session
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.services.rs_line import calculate_rs_line, df_to_response

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

def create_table_if_not_exists(db: Session):
    # Using text for raw SQL
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS stock_rs_line_metrics (
            symbol VARCHAR(20),
            date DATE,
            rs_line DECIMAL(20, 8),
            rs_ma1 DECIMAL(20, 8),
            rs_ma2 DECIMAL(20, 8),
            rs_direction VARCHAR(10),
            rs_position VARCHAR(20),
            rs_signal_today VARCHAR(20),
            rsnhbp_today BOOLEAN,
            last_bull_cross DATE,
            last_bear_cross DATE,
            PRIMARY KEY (symbol, date)
        )
    """))
    db.commit()

def calculate_and_store_rs_line_metrics(db: Session, target_date: date = None):
    """
    Calculate and store RS Line metrics (TraderLion) for all stocks on the target_date.
    If target_date is not provided, uses the latest date in the prices table.
    """
    print("=" * 60)
    print("📊 Starting RS Line Metrics Calculation (TraderLion scale)")
    print("=" * 60)
    
    if target_date is None:
        result = db.execute(text("SELECT MAX(date) FROM prices"))
        target_date = result.scalar()
        
    if not target_date:
        print("❌ No price data found.")
        return

    print(f"📅 Using date: {target_date}")
    create_table_if_not_exists(db)

    # Get all symbols available on the target date
    symbols_query = text("""
        SELECT DISTINCT symbol 
        FROM prices 
        WHERE date = :target_date
    """)
    symbols_result = db.execute(symbols_query, {"target_date": target_date}).fetchall()
    symbols = [row[0] for row in symbols_result]
    
    total_stocks = len(symbols)
    print(f"📈 Found {total_stocks} stocks to process")
    
    # We need to go back at least 250 days to get reliable MA50 and Lookback=50
    # The rs_line.calculate_rs_line function takes a start_date.
    start_date_str = "2020-01-01" # To ensure enough data
    end_date_str = target_date.strftime("%Y-%m-%d")

    processed = 0
    errors = 0
    successful = 0
    
    for symbol in symbols:
        try:
            # We calculate RS Line using scale_factor 3000 (TraderLion style) 
            df = calculate_rs_line(
                db=db,
                symbol=symbol,
                benchmark="^TASI.SR",
                start_date=start_date_str,
                end_date=end_date_str,
                scale_factor=3000
            )
            
            resp = df_to_response(df, symbol, "^TASI.SR")
            summary = resp["summary"]
            
            if summary["last_date"] != end_date_str:
                # Sometimes the stock doesn't trade on this day
                pass 
                
            # Insert into database
            insert_stmt = text("""
                INSERT INTO stock_rs_line_metrics (
                    symbol, date, rs_line, rs_ma1, rs_ma2, rs_direction, rs_position, 
                    rs_signal_today, rsnhbp_today, last_bull_cross, last_bear_cross
                ) VALUES (
                    :symbol, :date, :rs_line, :rs_ma1, :rs_ma2, :rs_direction, :rs_position,
                    :rs_signal_today, :rsnhbp_today, :last_bull_cross, :last_bear_cross
                )
                ON CONFLICT (symbol, date) DO UPDATE SET
                    rs_line = EXCLUDED.rs_line,
                    rs_ma1 = EXCLUDED.rs_ma1,
                    rs_ma2 = EXCLUDED.rs_ma2,
                    rs_direction = EXCLUDED.rs_direction,
                    rs_position = EXCLUDED.rs_position,
                    rs_signal_today = EXCLUDED.rs_signal_today,
                    rsnhbp_today = EXCLUDED.rsnhbp_today,
                    last_bull_cross = EXCLUDED.last_bull_cross,
                    last_bear_cross = EXCLUDED.last_bear_cross
            """)
            
            def parse_date(d_str):
                if not d_str:
                    return None
                return datetime.strptime(d_str, "%Y-%m-%d").date()
                
            db.execute(insert_stmt, {
                "symbol": symbol,
                "date": target_date,
                "rs_line": summary["rs_line"],
                "rs_ma1": summary["ma1"],
                "rs_ma2": summary["ma2"],
                "rs_direction": summary["direction"],
                "rs_position": summary["position"],
                "rs_signal_today": summary["signal_today"],
                "rsnhbp_today": summary["rsnhbp_today"],
                "last_bull_cross": parse_date(summary["last_bull_cross"]),
                "last_bear_cross": parse_date(summary["last_bear_cross"])
            })
            
            successful += 1
            processed += 1
            
            if processed % 50 == 0:
                db.commit()
                print(f"✅ Processed {processed}/{total_stocks} stocks...")
                
        except Exception as e:
            errors += 1
            processed += 1
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("Error processing %s: %s", symbol, type(e).__name__)
            
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    print("=" * 60)
    print("📊 RS Line Metrics Calculation Summary:")
    print(f"   ✅ Success: {successful}")
    print(f"   ❌ Errors: {errors}")
    print("=" * 60)
    
if __name__ == "__main__":
    db = SessionLocal()
    try:
        calculate_and_store_rs_line_metrics(db)
    finally:
        db.close()
