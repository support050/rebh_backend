import sys
from pathlib import Path
import pandas as pd
import numpy as np
import logging
from datetime import date, timedelta
from sqlalchemy import text, update
from sqlalchemy.orm import Session

# Setup Paths
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.rs_daily import RSDaily
from app.models.price import Price

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class IBDMetricsCalculator:
    def __init__(self, db: Session):
        self.db = db

    def get_letter_grade(self, percentile: float) -> str:
        """
        Convert percentile (0-100) to Letter Grade (A+ to E).
        Top 7% -> A+, Next 8% -> A, etc.
        """
        if percentile >= 93: return 'A+'
        if percentile >= 85: return 'A'
        if percentile >= 77: return 'A-'
        if percentile >= 70: return 'B+'
        if percentile >= 63: return 'B'
        if percentile >= 56: return 'B-'
        if percentile >= 49: return 'C+'
        if percentile >= 42: return 'C'
        if percentile >= 35: return 'C-'
        if percentile >= 28: return 'D+'
        if percentile >= 21: return 'D'
        if percentile >= 14: return 'D-'
        return 'E'

    def load_data(self, lookback_days=200):
        """Load Price data for calculation."""
        logger.info("📡 Loading Price Data...")
        query = text("""
            SELECT symbol, date, close, high, low, volume_traded, industry_group, sector, industry, sub_industry
            FROM prices
            WHERE date >= :start_date
            ORDER BY symbol, date ASC
        """)
        start_date = date.today() - timedelta(days=lookback_days)
        
        # Use connection explicitly for Pandas/SQLAlchemy 2.0 compatibility
        with self.db.bind.connect() as connection:
            df = pd.read_sql(query, connection, params={"start_date": start_date})
        
        # Ensure date is datetime
        df['date'] = pd.to_datetime(df['date'])
        
        logger.info(f"📊 Loaded {len(df)} price records.")
        return df

    def calculate_group_rs(self, df_prices, target_date=None):
        """
        Calculate RS for Sector, Industry Group, Industry, Sub-Industry.
        metric: 6-month price performance.
        """
        if target_date is None:
            target_date = date.today()
        
        target_date = pd.to_datetime(target_date)
        
        # 1. Calculate 6-Month % Change for each stock
        # We need price at target_date and price 6 months ago (approx 126 trading days or 180 calendar days)
        
        # Filter for data up to target_date
        df = df_prices[df_prices['date'] <= target_date].copy()
        
        # Get latest close for each symbol (Current Price)
        latest_prices = df.sort_values('date').groupby('symbol').tail(1)[['symbol', 'close', 'sector', 'industry_group', 'industry', 'sub_industry']]
        latest_prices = latest_prices.rename(columns={'close': 'current_close'})
        
        # Get price ~6 months ago (Look back 180 days)
        six_months_ago = target_date - timedelta(days=180)
        
        # Find the closest date to 6 months ago for each symbol
        # Strategy: Get all records >= 180 days ago, take the first one
        old_prices_df = df[df['date'] >= six_months_ago].sort_values('date').groupby('symbol').head(1)[['symbol', 'close']]
        old_prices_df = old_prices_df.rename(columns={'close': 'old_close'})
        
        # Merge
        merged = pd.merge(latest_prices, old_prices_df, on='symbol', how='inner')
        
        # Calculate Check: Ensure the "old" price is actually old enough (e.g. at least 5 months difference)
        # For simplicity, we assume the query filter worked.
        
        merged['pct_change_6m'] = (merged['current_close'] - merged['old_close']) / merged['old_close']
        
        logger.info(f"Computing Group RS for {len(merged)} stocks...")

        results = {} # Map symbol -> { 'sector_rs': 'A', ... }

        # Helper to process each hierarchy level
        hierarchy_cols = {
            'sector': 'sector_rs_rating', 
            'industry_group': 'industry_group_rs_rating',
            'industry': 'industry_rs_rating', 
            'sub_industry': 'sub_industry_rs_rating'
        }

        for col, result_col in hierarchy_cols.items():
            # Group by hierarchy column
            # Check if column exists and not null
            valid_groups = merged[merged[col].notna() & (merged[col] != '')]
            
            # Avg return per group
            group_perf = valid_groups.groupby(col)['pct_change_6m'].mean().reset_index()
            
            if group_perf.empty:
                continue

            # Rank Groups (Percentile)
            # rank(pct=True) returns 0.0 to 1.0. We want 1.0 to be best.
            # pct_change is higher = better.
            group_perf['percentile'] = group_perf['pct_change_6m'].rank(pct=True) * 100
            
            # Apply Grading
            group_perf['grade'] = group_perf['percentile'].apply(self.get_letter_grade)
            
            # Map grade back to each symbol — vectorized (no iterrows)
            grade_map = dict(zip(group_perf[col], group_perf['grade']))

            # Map back to symbols — fully vectorized with zip (أسرع من iterrows)
            sym_grade_df = valid_groups[['symbol', col]].copy()
            sym_grade_df['grade'] = sym_grade_df[col].map(grade_map)
            sym_grade_df = sym_grade_df.dropna(subset=['grade'])

            for sym, grade in zip(sym_grade_df['symbol'], sym_grade_df['grade']):
                if sym not in results:
                    results[sym] = {}
                results[sym][result_col] = grade

        return results

    def calculate_acc_dis(self, df_prices, target_date=None):
        """
        Calculate Accumulation/Distribution Rating (13-Weeks).
        """
        if target_date is None:
            target_date = date.today()
        
        target_date = pd.to_datetime(target_date)
        start_date = target_date - timedelta(weeks=13) # Approx 90 days
        
        # Filter data for last 13 weeks
        mask = (df_prices['date'] >= start_date) & (df_prices['date'] <= target_date)
        df = df_prices.loc[mask].copy()
        
        if df.empty:
            return {}

        # 1. Calculate CLV (Close Location Value)
        # CLV = ((C - L) - (H - C)) / (H - L)
        # Range -1 to +1
        denom = df['high'] - df['low']
        # Avoid division by zero
        df['clv'] = np.where(denom == 0, 0, ((df['close'] - df['low']) - (df['high'] - df['close'])) / denom)
        
        # 2. Money Flow Volume
        df['mfv'] = df['clv'] * df['volume_traded']
        
        # 3. Sum for each symbol
        acc_dis_scores = df.groupby('symbol')['mfv'].sum().reset_index()
        acc_dis_scores.rename(columns={'mfv': 'total_mfv'}, inplace=True)
        
        # 4. Rank and Grade
        acc_dis_scores['percentile'] = acc_dis_scores['total_mfv'].rank(pct=True) * 100
        acc_dis_scores['grade'] = acc_dis_scores['percentile'].apply(self.get_letter_grade)
        
        # Result Map
        return dict(zip(acc_dis_scores['symbol'], acc_dis_scores['grade']))

    def save_results(self, group_rs_results, acc_dis_results, target_date):
        """Update DB records."""
        logger.info(f"💾 Saving IBD Metrics to DB for {target_date}...")
        
        # We need to update existing rows in rs_daily_v2
        # Or check if they exist. Usually daily_market_update runs first, so rows exist.
        
        count = 0
        
        # Merge results by symbol
        all_symbols = set(group_rs_results.keys()) | set(acc_dis_results.keys())
        
        updates = []
        for sym in all_symbols:
            grp = group_rs_results.get(sym, {})
            ad = acc_dis_results.get(sym, None)
            
            update_data = {
                'symbol': sym,
                'date': target_date,
                'sector_rs_rating': grp.get('sector_rs_rating'),
                'industry_group_rs_rating': grp.get('industry_group_rs_rating'),
                'industry_rs_rating': grp.get('industry_rs_rating'),
                'sub_industry_rs_rating': grp.get('sub_industry_rs_rating'),
                'acc_dis_rating': ad
            }
            updates.append(update_data)
        
        if not updates:
            logger.warning("No updates to save.")
            return

        # Bulk Update
        # SQLAlchemy Core bulk update is tricky because we need to match WHERE symbol=X AND date=Y
        # The easiest way for a batch this size (hundreds/thousands) is individual updates or bindparams?
        # Let's try to map over the existing objects if possible, or use raw SQL for speed.
        
        # Using raw SQL for performance on update
        # Postgres supports update from values? Or just simple loop for now (safest).
        # We assume rows exist. IF they don't, we skip? 
        # Typically the RS Calculation script runs BEFORE this, creating the rows.
        
        total = len(updates)
        logger.info(f"Preparing to update {total} records...")
        
        # We will use sqlalchemy 2.0 style updates
        # But looping is easiest for stability right now.
        
        try:
            for batch in chunk(updates, 500):
                # We can't easily bulk update with composite key in standard ORM bulk_update_mappings without fetching.
                # Let's try raw SQL execute with case/binds? 
                # Actually, standard loop with commit every 500 is fine for 2000 stocks (approx 4 batches).
                
                for item in batch:
                    stmt = update(RSDaily).where(
                        RSDaily.symbol == item['symbol'],
                        RSDaily.date == item['date']
                    ).values(
                        sector_rs_rating=item['sector_rs_rating'],
                        industry_group_rs_rating=item['industry_group_rs_rating'],
                        industry_rs_rating=item['industry_rs_rating'],
                        sub_industry_rs_rating=item['sub_industry_rs_rating'],
                        acc_dis_rating=item['acc_dis_rating']
                    )
                    self.db.execute(stmt)
                
                self.db.commit()
                count += len(batch)
                logger.info(f"Updated {count}/{total}...")
                
        except Exception as e:
            logger.error(f"Error saving results: {e}")
            self.db.rollback()
            
        logger.info("✅ IBD Metrics Saved.")

def chunk(l, n):
    for i in range(0, len(l), n):
        yield l[i:i + n]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_pg_connection():
    """Direct psycopg2 connection (for COPY protocol in backfill)."""
    import psycopg2
    from app.core.config import settings
    db_url = str(settings.DATABASE_URL).replace("postgresql://", "").replace("postgresql+psycopg2://", "")
    user_pass, host_db = db_url.split("@")
    user, password = user_pass.split(":", 1)
    host_port, dbname = host_db.split("/", 1)
    if "?" in dbname:
        dbname = dbname.split("?")[0]
    host, port = (host_port.split(":") + ["5432"])[:2]
    return psycopg2.connect(
        dbname=dbname, user=user, password=password,
        host=host, port=port, sslmode="require"
    )


def _get_letter_grade_series(percentiles: "pd.Series") -> "pd.Series":
    """Vectorized grading (same thresholds as get_letter_grade())."""
    bins   = [-np.inf, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 85, 93, np.inf]
    labels = ['E', 'D-', 'D', 'D+', 'C-', 'C', 'C+', 'B-', 'B', 'B+', 'A-', 'A', 'A+']
    return pd.cut(percentiles, bins=bins, labels=labels, right=False).astype(str)


# ─────────────────────────────────────────────────────────────────────────────
# Daily mode (original logic, unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _run_daily(calc):
    """Original daily logic — called when --backfill is NOT passed."""
    df_prices = calc.load_data(lookback_days=230)

    target_date = date.today()
    if not df_prices.empty:
        max_date = df_prices['date'].max().date()
        if max_date < target_date:
            logger.warning(f"Using latest data date: {max_date} instead of {target_date}")
            target_date = max_date

    logger.info(f"🚀 Calculating Metrics for {target_date}...")
    group_rs_map = calc.calculate_group_rs(df_prices, target_date)
    acc_dis_map  = calc.calculate_acc_dis(df_prices, target_date)
    calc.save_results(group_rs_map, acc_dis_map, target_date)


# ─────────────────────────────────────────────────────────────────────────────
# Backfill mode — fast, COPY-based, all 5 columns, all historical dates
# ─────────────────────────────────────────────────────────────────────────────

def _run_backfill():
    """
    Calculate all 5 IBD metric columns for EVERY date in rs_daily_v2.
    Uses COPY protocol via psycopg2 — does NOT touch rs_rating column.

    Strategy
    --------
    acc_dis_rating     → fully vectorised rolling-window (like backfill_ad_rating.py)
    *_rs_rating (x4)   → per-date 6-month slice, vectorised ranking per date group
    """
    import time
    from io import StringIO
    import psycopg2

    PRICE_LOAD_FROM = "2001-09-01"   # warm-up for 63-day rolling window
    OUTPUT_FROM     = "2002-01-01"   # don't output rows before this date

    print("=" * 65)
    print("🚀  IBD METRICS FULL HISTORICAL BACKFILL")
    print("=" * 65)
    print(f"   Will update: sector_rs_rating, industry_group_rs_rating,")
    print(f"                industry_rs_rating, sub_industry_rs_rating,")
    print(f"                acc_dis_rating")
    print(f"   Source: ALL dates in rs_daily_v2")
    print(f"   Method: COPY to temp table → bulk UPDATE (safe, no TRUNCATE)")
    print("=" * 65)
    t0 = time.time()

    pg = _get_pg_connection()

    # ── [1/6] Load ALL price data ─────────────────────────────────────────
    print(f"\n[1/6] Loading all prices since {PRICE_LOAD_FROM} ...")
    df = pd.read_sql(
        f"""
        SELECT symbol, date, close, high, low, volume_traded,
               industry_group, sector, industry, sub_industry
        FROM prices
        WHERE date >= '{PRICE_LOAD_FROM}'
        ORDER BY symbol, date ASC
        """,
        pg
    )
    df['date'] = pd.to_datetime(df['date'])
    print(f"      Loaded {len(df):,} price records for {df['symbol'].nunique()} symbols.")

    # ── [2/6] Get all dates from rs_daily_v2 ─────────────────────────────
    print("\n[2/6] Fetching all distinct dates from rs_daily_v2 ...")
    dates_df = pd.read_sql(
        f"SELECT DISTINCT date FROM rs_daily_v2 WHERE date >= '{OUTPUT_FROM}' ORDER BY date ASC",
        pg
    )
    all_dates = pd.to_datetime(dates_df['date']).tolist()
    print(f"      Found {len(all_dates):,} dates  ({all_dates[0].date()} → {all_dates[-1].date()})")

    # ── [3/6] Vectorised A/D Rating (rolling 63-day MFV per symbol) ───────
    print("\n[3/6] Computing A/D Rating (vectorised rolling 63-day MFV) ...")
    df_sorted = df.sort_values(['symbol', 'date'])
    denom = df_sorted['high'] - df_sorted['low']
    df_sorted['clv'] = np.where(
        denom == 0, 0,
        ((df_sorted['close'] - df_sorted['low']) - (df_sorted['high'] - df_sorted['close'])) / denom
    )
    df_sorted['mfv'] = df_sorted['clv'] * df_sorted['volume_traded']

    df_indexed = df_sorted.set_index('date')
    rolling_mfv = (
        df_indexed.groupby('symbol')['mfv']
        .rolling(window=63, min_periods=10)
        .sum()
        .reset_index()
    )
    df_sorted = df_sorted.reset_index(drop=True)
    df_ad = df_sorted.merge(rolling_mfv, on=['symbol', 'date'], suffixes=('', '_13w'))

    valid_ad = df_ad.dropna(subset=['mfv_13w']).copy()
    valid_ad = valid_ad[valid_ad['date'] >= OUTPUT_FROM]
    valid_ad['percentile'] = valid_ad.groupby('date')['mfv_13w'].rank(pct=True) * 100
    valid_ad['acc_dis_rating'] = _get_letter_grade_series(valid_ad['percentile'])
    ad_result = valid_ad[['symbol', 'date', 'acc_dis_rating']].copy()
    print(f"      Computed {len(ad_result):,} A/D rating rows.")

    # ── [4/6] Vectorised Group RS for all dates ───────────────────────────
    print(f"\n[4/6] Computing Group RS ratings for all {len(all_dates):,} dates ...")

    hierarchy = {
        'sector':         'sector_rs_rating',
        'industry_group': 'industry_group_rs_rating',
        'industry':       'industry_rs_rating',
        'sub_industry':   'sub_industry_rs_rating',
    }

    # Pre-build a date-indexed lookup for fast 6-month-ago price queries.
    # For each symbol, we have a sorted (date, close) series.
    # We'll use a "latest price at or before target_date" and
    # "first price at or after (target_date - 180 days)" approach,
    # but do it in a fully vectorised way across all dates at once.

    # Pivot: index=date, columns=symbol, values=close
    price_pivot = df_sorted.pivot_table(index='date', columns='symbol', values='close', aggfunc='last')
    price_pivot = price_pivot.sort_index()
    price_dates = price_pivot.index  # DatetimeIndex

    # Also keep the latest metadata (sector etc.) per symbol — use the last known row
    meta_cols = ['symbol', 'sector', 'industry_group', 'industry', 'sub_industry']
    sym_meta = (
        df_sorted[meta_cols]
        .drop_duplicates(subset=['symbol'], keep='last')
        .set_index('symbol')
    )

    all_rs_rows = []
    LOG_EVERY = 500

    for i, td in enumerate(all_dates):
        six_months_ago = td - pd.Timedelta(days=180)

        # Slice price_pivot to get current and old price
        # current = latest price at or before td
        idx_cur = price_dates.searchsorted(td, side='right') - 1
        if idx_cur < 0:
            continue
        cur_prices = price_pivot.iloc[idx_cur]  # Series: symbol → close

        # old = first price at or after six_months_ago
        idx_old = price_dates.searchsorted(six_months_ago, side='left')
        if idx_old >= len(price_dates):
            continue
        old_prices = price_pivot.iloc[idx_old]  # Series: symbol → close

        # Build merged frame for this date
        frame = pd.DataFrame({
            'current_close': cur_prices,
            'old_close':     old_prices,
        }).dropna()

        if frame.empty:
            continue

        frame['pct_change_6m'] = (frame['current_close'] - frame['old_close']) / frame['old_close']
        frame = frame.join(sym_meta, how='left')

        date_str = td.strftime('%Y-%m-%d')

        for grp_col, rating_col in hierarchy.items():
            valid = frame[frame[grp_col].notna() & (frame[grp_col] != '')]
            if valid.empty:
                continue
            grp_perf = valid.groupby(grp_col)['pct_change_6m'].mean()
            grp_pct  = grp_perf.rank(pct=True) * 100
            grp_grade = _get_letter_grade_series(grp_pct)
            # Map back to symbols
            sym_grade = valid[grp_col].map(dict(zip(grp_grade.index, grp_grade)))
            for sym, grade in sym_grade.items():
                all_rs_rows.append((sym, date_str, rating_col, grade))

        if (i + 1) % LOG_EVERY == 0 or (i + 1) == len(all_dates):
            elapsed = time.time() - t0
            print(f"      [{i+1:>5}/{len(all_dates)}] done  ({elapsed:.0f}s elapsed)")

    # Reshape rs rows into a DataFrame
    print(f"\n      Reshaping {len(all_rs_rows):,} RS rating rows ...")
    rs_df = pd.DataFrame(all_rs_rows, columns=['symbol', 'date', 'metric', 'grade'])
    rs_pivot = rs_df.pivot_table(index=['symbol', 'date'], columns='metric', values='grade', aggfunc='first')
    rs_pivot = rs_pivot.reset_index()
    # Ensure all 4 columns exist
    for col in hierarchy.values():
        if col not in rs_pivot.columns:
            rs_pivot[col] = None
    print(f"      RS ratings ready: {len(rs_pivot):,} symbol-date rows.")

    # ── [5/6] Merge everything & upload via COPY ──────────────────────────
    print("\n[5/6] Merging A/D + Group RS and uploading via COPY ...")

    # Merge on symbol + date
    ad_result['date_str'] = ad_result['date'].dt.strftime('%Y-%m-%d')
    rs_pivot['date_str'] = rs_pivot['date']  # already string from above

    merged = pd.merge(
        ad_result[['symbol', 'date_str', 'acc_dis_rating']],
        rs_pivot[['symbol', 'date_str',
                  'sector_rs_rating', 'industry_group_rs_rating',
                  'industry_rs_rating', 'sub_industry_rs_rating']],
        on=['symbol', 'date_str'], how='outer'
    )

    upload_cols = [
        'symbol', 'date_str',
        'acc_dis_rating',
        'sector_rs_rating', 'industry_group_rs_rating',
        'industry_rs_rating', 'sub_industry_rs_rating',
    ]
    upload_df = merged[upload_cols].copy()
    upload_df = upload_df.fillna('\\N')   # psycopg2 COPY NULL representation

    buf = StringIO()
    upload_df.to_csv(buf, header=False, index=False, sep='\t', na_rep='\\N')
    buf.seek(0)

    cur = pg.cursor()
    cur.execute("""
        CREATE TEMP TABLE tmp_ibd_backfill (
            symbol                   VARCHAR(20),
            date                     DATE,
            acc_dis_rating           VARCHAR(5),
            sector_rs_rating         VARCHAR(5),
            industry_group_rs_rating VARCHAR(5),
            industry_rs_rating       VARCHAR(5),
            sub_industry_rs_rating   VARCHAR(5)
        )
    """)
    cur.copy_from(
        buf, 'tmp_ibd_backfill', sep='\t', null='\\N',
        columns=[
            'symbol', 'date',
            'acc_dis_rating', 'sector_rs_rating',
            'industry_group_rs_rating', 'industry_rs_rating',
            'sub_industry_rs_rating',
        ]
    )
    pg.commit()
    print(f"      Uploaded {len(upload_df):,} rows to temp table.")

    cur.execute("CREATE INDEX tmp_ibd_idx ON tmp_ibd_backfill(symbol, date)")
    pg.commit()

    # ── [6/6] Bulk UPDATE rs_daily_v2 ────────────────────────────────────
    print("\n[6/6] Updating rs_daily_v2 (safe UPDATE — rs_rating NOT touched) ...")
    cur.execute("""
        UPDATE rs_daily_v2
        SET
            acc_dis_rating           = CASE WHEN t.acc_dis_rating           = '\\N' THEN NULL ELSE t.acc_dis_rating           END,
            sector_rs_rating         = CASE WHEN t.sector_rs_rating         = '\\N' THEN NULL ELSE t.sector_rs_rating         END,
            industry_group_rs_rating = CASE WHEN t.industry_group_rs_rating = '\\N' THEN NULL ELSE t.industry_group_rs_rating END,
            industry_rs_rating       = CASE WHEN t.industry_rs_rating       = '\\N' THEN NULL ELSE t.industry_rs_rating       END,
            sub_industry_rs_rating   = CASE WHEN t.sub_industry_rs_rating   = '\\N' THEN NULL ELSE t.sub_industry_rs_rating   END
        FROM tmp_ibd_backfill t
        WHERE rs_daily_v2.symbol = t.symbol
          AND rs_daily_v2.date   = t.date
    """)
    updated = cur.rowcount
    pg.commit()
    cur.close()
    pg.close()

    elapsed = time.time() - t0
    print(f"\n{'=' * 65}")
    print(f"   BACKFILL COMPLETE in {elapsed/60:.1f} min")
    print(f"   Updated {updated:,} rows in rs_daily_v2")
    print(f"   rs_rating column was NOT touched.")
    print(f"{'=' * 65}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="IBD Metrics Calculator")
    parser.add_argument(
        '--backfill', action='store_true',
        help='Calculate all 5 IBD metric columns for ALL historical dates in rs_daily_v2 '
             '(uses COPY protocol, safe — does NOT touch rs_rating).'
    )
    args = parser.parse_args()

    if args.backfill:
        _run_backfill()
    else:
        db = SessionLocal()
        try:
            calc = IBDMetricsCalculator(db)
            _run_daily(calc)
        except Exception as e:
            logger.error(f"Failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            db.close()


if __name__ == "__main__":
    main()