import sys
import os
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, date
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

# Ensure stdout handles emoji/unicode safely when invoked as subprocess
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.models.stock_indicators import StockIndicator
from scripts.indicators_data_service import IndicatorsDataService

import sys
import os
import time
import argparse
from io import StringIO
import numpy as np
import pandas as pd
from datetime import datetime, date
import psycopg2
from sqlalchemy import text
from sqlalchemy.orm import Session

# Ensure stdout handles emoji/unicode safely when invoked as subprocess
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.database import SessionLocal
from scripts.indicators_data_service import IndicatorsDataService
from scripts.calculate_technicals import TechnicalCalculator


def _get_pg_connection():
    """Direct psycopg2 connection for fast COPY protocol."""
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


def recalculate_full_history_for_symbol(db: Session, symbol: str):
    """
    Unified, ultra-fast historical indicators calculation for a symbol.
    Calculates:
      1. Chart & Strategy indicators (RSI, The Number, CFG, STAMP, Trend)
      2. Market statistics & Screener metrics (SMAs 10-200, 30W/40W, 52W High/Low, % change 15-126d, Beta)
    Saves everything via PostgreSQL COPY protocol in a few seconds.
    """
    t0 = time.time()
    print(f"\n{'=' * 65}")
    print(f"🚀 [Unified Recalculation] Processing Full History for: {symbol}")
    print(f"{'=' * 65}")

    # ─── 1. Fetch Price Data ──────────────────────────────────────────
    print(f"📥 Loading price history for {symbol}...")
    query = text("""
        SELECT id, symbol, date, open, high, low, close, volume_traded
        FROM prices
        WHERE symbol = :symbol
        ORDER BY date ASC
    """)
    result = db.execute(query, {"symbol": symbol})
    rows = result.fetchall()
    rows_dicts = [dict(r._mapping) for r in rows]

    if not rows_dicts or len(rows_dicts) < 100:
        print(f"⚠️  {symbol}: Not enough data (< 100 days). Skipping.")
        return

    # ─── 2. Part A: Chart & Strategy Indicators ──────────────────────
    print(f"🧮 [1/2] Computing Chart & Strategy Indicators ({len(rows_dicts)} days)...")
    df_chart = IndicatorsDataService.prepare_price_dataframe(rows_dicts)
    if df_chart is None:
        return
    df_weekly = IndicatorsDataService.prepare_weekly_dataframe(df_chart)
    if df_weekly is None:
        return

    precomputed = IndicatorsDataService.prepare_precomputed_components(df_chart, df_weekly, symbol)
    weekly_dates = np.array([d.date() for d in df_weekly.index])

    chart_results = []
    # Start from index 100 to ensure moving averages have warmup data
    for idx in range(100, len(df_chart)):
        target_date = df_chart.index[idx].date()
        w_pos = np.searchsorted(weekly_dates, target_date, side='right') - 1
        w_idx = int(w_pos) if w_pos >= 0 else None

        data = IndicatorsDataService.calculate_all_indicators(
            df=df_chart,
            df_weekly=df_weekly,
            symbol=symbol,
            target_date=target_date,
            idx=idx,
            w_idx=w_idx,
            precomputed=precomputed
        )

        indicator_data = {
            'symbol': symbol,
            'date': target_date,
            **data
        }

        # Clean non-DB types
        for k, v in list(indicator_data.items()):
            if k in ['price_history', 'weekly_history']:
                del indicator_data[k]
                continue
            if isinstance(v, (np.float64, np.float32, np.integer)):
                indicator_data[k] = round(float(v), 4) if not pd.isna(v) else None
            elif isinstance(v, np.bool_):
                indicator_data[k] = bool(v)
            elif isinstance(v, float):
                indicator_data[k] = round(v, 4) if not pd.isna(v) else None
            elif isinstance(v, (list, dict, np.ndarray, pd.Series)):
                indicator_data[k] = None

        chart_results.append(indicator_data)

    df_chart_res = pd.DataFrame(chart_results)
    df_chart_res['date'] = pd.to_datetime(df_chart_res['date'])
    print(f"   ↳ Chart indicators ready: {len(df_chart_res)} dates.")

    # ─── 3. Part B: Market Stats & Screener Indicators ────────────────
    print(f"📊 [2/2] Computing Screener & Technicals (SMAs, % change 126d, Beta)...")
    tech_calc = TechnicalCalculator(str(settings.DATABASE_URL))
    df_prices_raw = pd.DataFrame(rows_dicts)
    df_prices_raw['date'] = pd.to_datetime(df_prices_raw['date'])
    df_prices_raw['close'] = df_prices_raw['close'].astype(float)
    df_prices_raw['open'] = df_prices_raw['open'].astype(float)
    df_prices_raw['high'] = df_prices_raw['high'].astype(float)
    df_prices_raw['low'] = df_prices_raw['low'].astype(float)
    df_prices_raw['volume_traded'] = df_prices_raw['volume_traded'].fillna(0).astype(float)

    df_tech_res = tech_calc.calculate(df_prices_raw)

    # ─── 4. Merge Both Parts Together ─────────────────────────────────
    tech_cols = [
        'sma_10', 'sma_21', 'sma_50', 'sma_150', 'sma_200',
        'sma_200_1m_ago', 'sma_200_2m_ago', 'sma_200_3m_ago', 'sma_200_4m_ago', 'sma_200_5m_ago',
        'sma_30w', 'sma_40w', 'fifty_two_week_high', 'fifty_two_week_low', 'average_volume_50',
        'price_minus_sma_10', 'price_minus_sma_21', 'price_minus_sma_50', 'price_minus_sma_150', 'price_minus_sma_200',
        'price_vs_sma_10_percent', 'price_vs_sma_21_percent', 'price_vs_sma_50_percent',
        'price_vs_sma_150_percent', 'price_vs_sma_200_percent',
        'percent_off_52w_high', 'percent_off_52w_low', 'vol_diff_50_percent',
        'percent_change_15d', 'percent_change_20d', 'percent_change_63d', 'percent_change_126d', 'beta'
    ]

    # Keep only available tech columns
    available_tech_cols = ['symbol', 'date'] + [c for c in tech_cols if c in df_tech_res.columns]
    merged_df = pd.merge(
        df_chart_res,
        df_tech_res[available_tech_cols],
        on=['symbol', 'date'],
        how='left'
    )
    merged_df['date_str'] = merged_df['date'].dt.strftime('%Y-%m-%d')
    cols_to_upload = [c for c in merged_df.columns if c not in ('date', 'date_str')]
    upload_cols = ['symbol', 'date'] + cols_to_upload

    # Prepare for COPY
    final_df = merged_df[['symbol', 'date_str'] + cols_to_upload].copy()
    final_df.rename(columns={'date_str': 'date'}, inplace=True)
    final_df = final_df.fillna('\\N')

    # ─── 5. Fast COPY into DB ─────────────────────────────────────────
    print(f"💾 Saving all {len(final_df)} days into stock_indicators via COPY protocol...")
    pg_conn = _get_pg_connection()
    try:
        cur = pg_conn.cursor()
        # Build temp table matching the dataframe columns
        col_defs = ["symbol VARCHAR(20)", "date DATE"]
        for c in cols_to_upload:
            col_defs.append(f"{c} TEXT")  # TEXT handles bool, numeric, varchar cleanly in temp table

        cur.execute(f"CREATE TEMP TABLE tmp_full_recalc ({', '.join(col_defs)});")

        buf = StringIO()
        final_df.to_csv(buf, header=False, index=False, sep='\t', na_rep='\\N')
        buf.seek(0)

        cur.copy_from(buf, 'tmp_full_recalc', sep='\t', null='\\N', columns=upload_cols)

        # Bulk upsert from temp table with cast
        set_clauses = ", ".join(f"{c} = t.{c}::VARCHAR::\"any\"" if False else f"{c} = CAST(NULLIF(t.{c}, '') AS {c})"
                                for c in cols_to_upload)
        # Using EXCLUDED style:
        # In Postgres, CAST from TEXT to actual target column type in INSERT ... SELECT:
        target_cols = ", ".join(upload_cols)
        select_cols = ", ".join(f"t.{c}::TEXT" if c not in ('symbol', 'date') else f"t.{c}" for c in upload_cols)
        # Directly do UPDATE from tmp table
        update_set = ", ".join([
            f"{c} = CASE WHEN t.{c} IS NULL OR t.{c} = '' THEN NULL ELSE t.{c}::numeric END"
            if 'sma' in c or 'percent' in c or 'rsi' in c or 'close' in c or 'high' in c or 'low' in c or 'price' in c or 'beta' in c or 'vol' in c or 'number' in c or 'stamp' in c or 'cfg' in c or 'cci' in c or 'aroon' in c or 'ema' in c or 'wma' in c
            else f"{c} = CASE WHEN t.{c} IS NULL OR t.{c} = '' THEN NULL ELSE t.{c}::boolean END"
            if 'gt' in c or 'signal' in c or 'trend' in c or 'has' in c or 'is_' in c or c == 'stamp'
            else f"{c} = t.{c}"
            for c in cols_to_upload
        ])

        # Ensure target rows exist or insert missing
        cur.execute(f"""
            INSERT INTO stock_indicators (symbol, date)
            SELECT t.symbol, t.date FROM tmp_full_recalc t
            ON CONFLICT (symbol, date) DO NOTHING;
        """)

        cur.execute(f"""
            UPDATE stock_indicators s
            SET {update_set}
            FROM tmp_full_recalc t
            WHERE s.symbol = t.symbol AND s.date = t.date;
        """)

        pg_conn.commit()
        cur.close()
        elapsed = time.time() - t0
        print(f"✅ Full recalculation for {symbol} finished in {elapsed:.1f}s ({len(final_df)} rows saved)!")
    finally:
        pg_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Unified Full Historical Indicators Recalculator")
    parser.add_argument("--symbol", nargs="+", required=True, help="Symbol(s) to recalculate (e.g. --symbol 6040 or --symbol 6040 4328)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        for sym in args.symbol:
            recalculate_full_history_for_symbol(db, sym)
    finally:
        db.close()


if __name__ == "__main__":
    main()
