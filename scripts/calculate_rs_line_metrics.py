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

SYMBOL_CHUNK_SIZE = 40      # symbols per price query (272 → ~7 queries)
COMMIT_EVERY      = 50      # rows per executemany round-trip / commit


def _load_benchmark_series(db: Session, start_date_str: str, end_date_str: str) -> pd.Series:
    """TASI closes loaded ONCE for the whole run (market_pulse.date is unique)."""
    rows = db.execute(text("""
        SELECT date, close
        FROM market_pulse
        WHERE date >= :start_date AND date <= :end_date
        ORDER BY date ASC
    """), {"start_date": start_date_str, "end_date": end_date_str}).fetchall()
    if not rows:
        raise ValueError("No TASI data in market_pulse table")
    return pd.Series([float(r[1]) for r in rows],
                     index=pd.DatetimeIndex([r[0] for r in rows]),
                     name="TASI")


def _load_all_stock_closes(db: Session, symbols, start_date_str: str, end_date_str: str) -> dict:
    """
    One query per chunk of symbols instead of one query per symbol.
    Returns {symbol: ascending close Series} — identical rows/values to
    rs_line._fetch_stock_close (same table, same date bounds, same ORDER BY).
    """
    out = {}
    for i in range(0, len(symbols), SYMBOL_CHUNK_SIZE):
        chunk = symbols[i:i + SYMBOL_CHUNK_SIZE]
        placeholders = ", ".join(f":sym_{j}" for j in range(len(chunk)))
        params = {f"sym_{j}": s for j, s in enumerate(chunk)}
        params["start_date"] = start_date_str
        params["end_date"] = end_date_str
        rows = db.execute(text(f"""
            SELECT symbol, date, close
            FROM prices
            WHERE date >= :start_date AND date <= :end_date
              AND symbol IN ({placeholders})
            ORDER BY symbol, date ASC
        """), params).fetchall()
        for sym, date_, close_ in rows:
            bucket = out.setdefault(sym, ([], []))
            bucket[0].append(date_)
            bucket[1].append(float(close_))
    for sym, (dates, closes) in out.items():
        out[sym] = pd.Series(closes, index=pd.DatetimeIndex(dates), name=sym)
    return out


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

    # ── Batched loading: kills the N+1 ───────────────────────────
    # benchmark  : 1 query  (was total_stocks identical queries)
    # stock close: ceil(total_stocks / SYMBOL_CHUNK_SIZE) queries (was total_stocks)
    t_load = time.time()
    bench_close = _load_benchmark_series(db, start_date_str, end_date_str)
    stock_closes = _load_all_stock_closes(db, symbols, start_date_str, end_date_str)
    n_batches = -(-total_stocks // SYMBOL_CHUNK_SIZE)
    print(f"🗂️  Loaded TASI ({len(bench_close)} bars) + {len(stock_closes)} symbols "
          f"in {n_batches} batch queries ({time.time() - t_load:.1f}s)")

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

    processed = 0
    errors = 0
    successful = 0
    pending = []

    def flush_pending():
        """Write accumulated rows in a single executemany round-trip."""
        if not pending:
            return
        try:
            db.execute(insert_stmt, list(pending))
        except Exception as bulk_err:
            # Transaction is aborted — roll back, then retry row by row so one
            # bad row cannot cost the whole chunk.
            logger.warning("bulk upsert failed (%s) — retrying row by row",
                           type(bulk_err).__name__)
            try:
                db.rollback()
            except Exception:
                pass
            for p in pending:
                try:
                    db.execute(insert_stmt, p)
                    db.commit()
                except Exception as one_err:
                    db.rollback()
                    logger.warning("insert failed for %s: %s",
                                   p["symbol"], type(one_err).__name__)
        pending.clear()

    for symbol in symbols:
        try:
            sc = stock_closes.get(symbol)
            if sc is None or sc.empty:
                raise ValueError(f"No price data for symbol {symbol} in database")

            # We calculate RS Line using scale_factor 3000 (TraderLion style) 
            df = calculate_rs_line(
                db=db,
                symbol=symbol,
                benchmark="^TASI.SR",
                start_date=start_date_str,
                end_date=end_date_str,
                scale_factor=3000,
                stock_close=sc,
                bench_close=bench_close
            )
            
            # include_series=False → we only need summary["..."]; building the
            # full ~1400-bar list here was pure wasted work (and it was dropped).
            resp = df_to_response(df, symbol, "^TASI.SR", include_series=False)
            summary = resp["summary"]
            
            if summary["last_date"] != end_date_str:
                # Sometimes the stock doesn't trade on this day
                pass 
                
            pending.append({
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
            
            if len(pending) >= COMMIT_EVERY:
                flush_pending()
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
            
    flush_pending()
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
