"""
Historical Beta Backfill Script
================================
Calculates Beta (β) for all stocks across ALL historical dates using:
  - Stock prices from the `prices` table
  - TASI benchmark from the `historical_reports` table (scraped from Tadawul)

Formula: β = Cov(Ri, Rm) / Var(Rm)
  - Rolling window: 260 trading days (~1 year)
  - Minimum periods: 130 trading days (~6 months)

Safety:
  - Processes stocks in batches (default 50) to limit memory
  - Writes to DB in chunks (default 500 rows) to avoid long transactions
  - Uses row-level UPDATEs only — no table locks
  - Single DB connection via SQLAlchemy engine

Usage:
  python -m scripts.backfill_beta_historical
  python -m scripts.backfill_beta_historical --batch-size 30 --write-chunk 300
"""

import sys
import gc
import time
import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# Add parent directory to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_tasi_benchmark(engine) -> pd.DataFrame:
    """
    Load TASI close prices from historical_reports table.
    Returns DataFrame with columns: [date, market_close, market_return]
    """
    logger.info("📊 Loading TASI benchmark from historical_reports...")
    query = """
        SELECT report_date AS date, close_price AS market_close
        FROM historical_reports
        WHERE close_price IS NOT NULL
        ORDER BY report_date
    """
    with engine.connect() as conn:
        market_df = pd.read_sql(text(query), conn)

    if market_df.empty:
        raise RuntimeError("❌ historical_reports table is empty — cannot compute Beta")

    market_df['date'] = pd.to_datetime(market_df['date'])
    # close_price is stored as String in the table
    market_df['market_close'] = pd.to_numeric(
        market_df['market_close'].astype(str).str.replace(',', ''), errors='coerce'
    )
    market_df = market_df.dropna(subset=['market_close']).reset_index(drop=True)

    # Pre-compute market returns and rolling variance (done once, shared across all stocks)
    market_df['market_return'] = market_df['market_close'].pct_change()
    market_df['market_var'] = market_df['market_return'].rolling(window=260, min_periods=130).var()

    logger.info(
        f"   ✅ Loaded {len(market_df)} TASI records "
        f"({market_df['date'].min().date()} → {market_df['date'].max().date()})"
    )
    return market_df


def load_all_symbols(engine) -> list[str]:
    """Get all unique symbols from prices table."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT DISTINCT symbol FROM prices ORDER BY symbol"))
        symbols = [row[0] for row in result]
    logger.info(f"📋 Found {len(symbols)} unique symbols in prices table")
    return symbols


def load_stock_prices(engine, symbols: list[str]) -> pd.DataFrame:
    """Load price data for a batch of symbols."""
    placeholders = ', '.join([f':s{i}' for i in range(len(symbols))])
    params = {f's{i}': sym for i, sym in enumerate(symbols)}

    query = f"""
        SELECT symbol, date, close
        FROM prices
        WHERE symbol IN ({placeholders})
        ORDER BY symbol, date
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn, params=params)

    df['date'] = pd.to_datetime(df['date'])
    return df


def compute_beta_for_batch(stock_df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute rolling Beta for all stocks in this batch.
    Returns DataFrame with columns: [symbol, date, beta]
    """
    # Compute stock daily returns
    stock_df = stock_df.sort_values(['symbol', 'date']).copy()
    stock_df['stock_return'] = stock_df.groupby('symbol')['close'].pct_change()

    # Merge market data onto stock data
    merged = stock_df.merge(
        market_df[['date', 'market_return', 'market_var']],
        on='date', how='left'
    )

    # Compute rolling covariance per symbol
    results = []
    for symbol, grp in merged.groupby('symbol'):
        grp = grp.sort_values('date').copy()

        if len(grp) < 130:
            # Not enough data for meaningful Beta
            continue

        cov_vals = grp['stock_return'].rolling(window=260, min_periods=130).cov(grp['market_return'])
        grp['beta'] = cov_vals / grp['market_var']
        grp['beta'] = grp['beta'].replace([np.inf, -np.inf], np.nan)

        # Only keep rows with valid Beta
        valid = grp.dropna(subset=['beta'])[['symbol', 'date', 'beta']]
        if not valid.empty:
            results.append(valid)

    if results:
        return pd.concat(results, ignore_index=True)
    return pd.DataFrame(columns=['symbol', 'date', 'beta'])


def save_beta_to_db(engine, beta_df: pd.DataFrame, write_chunk: int = 500):
    """
    Update Beta values in stock_indicators using ultra-fast COPY protocol via psycopg2.
    """
    if beta_df.empty:
        return 0

    import io

    # Ensure clean standard string types
    df_clean = beta_df[['symbol', 'date', 'beta']].copy()
    if pd.api.types.is_datetime64_any_dtype(df_clean['date']):
        df_clean['date'] = df_clean['date'].dt.strftime('%Y-%m-%d')
    else:
        df_clean['date'] = df_clean['date'].astype(str)
        
    df_clean['beta'] = df_clean['beta'].round(4)
    df_clean = df_clean.dropna(subset=['symbol', 'date', 'beta'])
    if df_clean.empty:
        return 0

    csv_buf = io.StringIO()
    df_clean.to_csv(csv_buf, index=False, header=False, sep='\t', na_rep='\\N')
    csv_buf.seek(0)

    raw_conn = engine.raw_connection()
    try:
        cur = raw_conn.cursor()
        cur.execute("""
            CREATE TEMP TABLE tmp_beta_update (
                symbol VARCHAR(20),
                date DATE,
                beta NUMERIC(14, 4)
            ) ON COMMIT DROP;
        """)

        cur.copy_expert(
            "COPY tmp_beta_update (symbol, date, beta) FROM STDIN WITH (FORMAT text, DELIMITER '\t', NULL '\\N')",
            csv_buf
        )

        cur.execute("""
            UPDATE stock_indicators s
            SET beta = t.beta
            FROM tmp_beta_update t
            WHERE s.symbol = t.symbol AND s.date = t.date;
        """)
        updated_count = cur.rowcount
        raw_conn.commit()
        return updated_count
    except Exception as e:
        raw_conn.rollback()
        logger.error(f"❌ Error in fast save_beta_to_db: {e}")
        raise e
    finally:
        raw_conn.close()


def main():
    parser = argparse.ArgumentParser(description='Backfill historical Beta values')
    parser.add_argument('--batch-size', type=int, default=100,
                        help='Number of stocks to process per batch (default: 100)')
    parser.add_argument('--write-chunk', type=int, default=500,
                        help='Number of rows per DB write chunk (default: 500)')
    args = parser.parse_args()

    t_start = time.time()
    engine = create_engine(str(settings.DATABASE_URL))

    # 1. Load TASI benchmark (done once — shared across all batches)
    market_df = load_tasi_benchmark(engine)

    # 2. Get all stock symbols
    all_symbols = load_all_symbols(engine)

    # 3. Process in batches
    total_computed = 0
    total_saved = 0
    num_batches = (len(all_symbols) + args.batch_size - 1) // args.batch_size

    for batch_num, batch_start in enumerate(range(0, len(all_symbols), args.batch_size), start=1):
        batch_symbols = all_symbols[batch_start: batch_start + args.batch_size]

        logger.info(
            f"⚙️  Batch {batch_num}/{num_batches}: "
            f"Processing {len(batch_symbols)} stocks "
            f"({batch_symbols[0]}...{batch_symbols[-1]})..."
        )

        # Load prices for this batch
        stock_df = load_stock_prices(engine, batch_symbols)
        if stock_df.empty:
            logger.warning(f"   ⚠️ No price data for batch {batch_num}, skipping")
            continue

        # Compute Beta
        beta_df = compute_beta_for_batch(stock_df, market_df)
        total_computed += len(beta_df)

        # Save to DB
        if not beta_df.empty:
            saved = save_beta_to_db(engine, beta_df, write_chunk=args.write_chunk)
            total_saved += saved
            logger.info(
                f"   ✅ Batch {batch_num}: "
                f"Computed {len(beta_df)} Beta values, "
                f"Updated {saved} rows in stock_indicators"
            )
        else:
            logger.info(f"   ℹ️ Batch {batch_num}: No valid Beta values computed")

        # Free memory
        del stock_df, beta_df
        gc.collect()

    elapsed = time.time() - t_start
    logger.info(
        f"\n🎉 Historical Beta Backfill Complete!\n"
        f"   Total Beta values computed: {total_computed:,}\n"
        f"   Total rows updated in DB:   {total_saved:,}\n"
        f"   Time elapsed:               {elapsed:.1f}s"
    )

    engine.dispose()


if __name__ == "__main__":
    main()
