import sys
from pathlib import Path
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
import logging

# Append the project root to sys.path so the settings module is importable
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.config import settings

# Logging setup — used to follow the progress of the run
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _bulk_update_prices_change(conn, updates, chunk_size=500):
    """
    Update prices.change for many rows using ONE statement per chunk.

    Why this exists: psycopg2's executemany still sends one round-trip per row,
    so the 294-symbol batch cost 294 WAN trips (~82s in the stage-10 timing).
    `UPDATE ... FROM (VALUES ...)` ships the whole batch in a single statement
    and Postgres resolves it by primary key in one pass -> 1 round-trip.
    Returns the number of rows touched.
    """
    if not updates:
        return 0

    affected = 0
    for start in range(0, len(updates), chunk_size):
        batch = updates[start:start + chunk_size]
        # NOTE: CAST(:p AS type) is deliberate — SQLAlchemy's text() does NOT
        # treat ":name::type" as a bind parameter (the Postgres cast right after
        # the name makes the lexer skip it), so the raw ":" would reach psycopg2
        # and the statement would fail with a syntax error.
        rows_sql = ", ".join(
            f"(CAST(:id_{i} AS bigint), CAST(:ch_{i} AS numeric))" for i in range(len(batch))
        )
        params = {}
        for i, row in enumerate(batch):
            params[f"id_{i}"] = row["id"]
            params[f"ch_{i}"] = row["change"]
        res = conn.execute(
            text(
                f"UPDATE prices AS p SET change = v.ch "
                f"FROM (VALUES {rows_sql}) AS v(id, ch) WHERE p.id = v.id"
            ),
            params,
        )
        affected += res.rowcount or 0
    return affected


class TechnicalCalculator:
    # Every distinct symbol, walked key-by-key off idx_prices_symbol_date. The
    # recursive "skip scan" idiom costs ~6ms; a plain SELECT DISTINCT on the same
    # 993K-row table has to sort and takes ~2.3s.
    _SYMBOLS_SQL = """
        WITH RECURSIVE syms AS (
            (SELECT symbol FROM prices ORDER BY symbol LIMIT 1)
            UNION ALL
            SELECT (SELECT symbol FROM prices WHERE symbol > syms.symbol ORDER BY symbol LIMIT 1)
            FROM syms WHERE syms.symbol IS NOT NULL
        )
        SELECT symbol FROM syms WHERE symbol IS NOT NULL
    """

    LOOKBACK_ROWS = 500

    def __init__(self, db_url):
        self.engine = create_engine(db_url)
        # [Perf] Removed _ensure_columns_exist(): it issued one
        # `ALTER TABLE stock_indicators ADD COLUMN IF NOT EXISTS ...` per column
        # (35 statements, each in its own transaction) on EVERY instantiation —
        # 35+ seconds of WAN round-trips for a schema that is already stable,
        # plus live DDL performed from a data script. Table shape is owned by the
        # ORM models and Alembic migrations, not by this script.

    def load_data(self):
        """
        Load the last LOOKBACK_ROWS (500) trading rows per symbol, not all history.

        Longest indicator = SMA200 + shift(126) = 326 rows, so 500 is safe.

        [Perf] Rewritten on both sides of the wire, measured on production data
        (993,729 price rows -> 140,640 loaded):
          1. Server side: the old ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY
             date DESC) window forced a seq scan + sort of ALL rows just to keep
             the last 500 each (28.8s). A LATERAL probe per symbol with
             "ORDER BY date DESC LIMIT 500" walks the existing
             idx_prices_symbol_date backwards instead (6.4s), returning the exact
             same rows.
          2. Client side: NUMERIC columns are cast to float8 in SQL. psycopg2
             builds a Decimal object for every numeric cell (140K rows x 5 cols),
             which dominated the fetch.
        Combined wall time: 50.5s -> 25.9s, value-identical (max diff 0.0 and the
        same NULL counts on all 140,640 rows).

        No ORDER BY in the SQL: calculate() starts with sort_values(['symbol','date']).
        """
        query = f"""
            SELECT p.id, p.symbol, p.date, p.open, p.close, p.high, p.low, p.volume_traded
            FROM ({self._SYMBOLS_SQL}) s
            CROSS JOIN LATERAL (
                SELECT id, symbol, date,
                       open::float8, close::float8, high::float8,
                       low::float8, volume_traded::float8
                FROM prices pr
                WHERE pr.symbol = s.symbol
                ORDER BY pr.date DESC
                LIMIT {self.LOOKBACK_ROWS}
            ) p
        """
        logger.info("⏳ Loading price history (last %d rows per symbol)...", self.LOOKBACK_ROWS)
        with self.engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
        df['date'] = pd.to_datetime(df['date'])
        logger.info(f"✅ Loaded {len(df)} price rows.")
        return df

    def calculate(self, df):
        logger.info("📈 Calculating technical indicators...")

        df = df.sort_values(['symbol', 'date'])

        # Drop holiday rows (rows whose prices are unchanged from the previous session)
        columns_to_check = ['open', 'high', 'low', 'close']
        mask = (df[columns_to_check] != df.groupby('symbol')[columns_to_check].shift(1)).any(axis=1) | \
               (df.groupby('symbol')['date'].cumcount() == 0)
        df = df[mask].copy()

        grouped = df.groupby('symbol')

        # 1. Daily SMAs
        for window in [10, 20, 21, 50, 100, 150, 200]:
            df[f'sma_{window}'] = grouped['close'].transform(
                lambda x: x.rolling(window=window).mean()
            )

        # 2. 52 Week High / Low
        df['fifty_two_week_high'] = grouped['high'].transform(
            lambda x: x.rolling(window=260).max()
        )
        df['fifty_two_week_low'] = grouped['low'].transform(
            lambda x: x.rolling(window=260).min()
        )

        # 3. Average Volume (50 days)
        df['average_volume_50'] = grouped['volume_traded'].transform(
            lambda x: x.rolling(window=50).mean()
        )

        # 4. Change
        logger.info("   ... computing change")
        df['change'] = grouped['close'].transform(lambda x: x.diff())

        # 4b. Power Play: percent changes over fixed horizons
        logger.info("   ... computing percent_change_15d / 20d / 63d / 126d")
        df['percent_change_15d'] = grouped['close'].transform(
            lambda x: ((x - x.shift(15)) / x.shift(15).replace(0, np.nan)) * 100
        )
        df['percent_change_20d'] = grouped['close'].transform(
            lambda x: ((x - x.shift(20)) / x.shift(20).replace(0, np.nan)) * 100
        )
        df['percent_change_63d'] = grouped['close'].transform(
            lambda x: ((x - x.shift(63)) / x.shift(63).replace(0, np.nan)) * 100
        )
        df['percent_change_126d'] = grouped['close'].transform(
            lambda x: ((x - x.shift(126)) / x.shift(126).replace(0, np.nan)) * 100
        )

        # 5. Historical 200MA
        logger.info("   ... computing historical 200MA series")
        for months_ago, days in [(1, 21), (2, 42), (3, 63), (4, 84), (5, 105)]:
            col_name = f'sma_200_{months_ago}m_ago'
            df[col_name] = grouped['close'].transform(
                lambda x, d=days: x.rolling(window=200).mean().shift(d)
            )

        # 6. 30W and 40W SMAs
        logger.info("   ... computing 30W and 40W SMAs")
        df['week_ending'] = df['date'] + pd.to_timedelta((4 - df['date'].dt.dayofweek) % 7, unit='D')
        weekly_closes = df.groupby(['symbol', 'week_ending'])['close'].last().reset_index()
        weekly_closes = weekly_closes.sort_values(['symbol', 'week_ending'])
        weekly_closes['sma_30w_calc'] = weekly_closes.groupby('symbol')['close'].transform(
            lambda x: x.rolling(window=30).mean()
        )
        weekly_closes['sma_40w_calc'] = weekly_closes.groupby('symbol')['close'].transform(
            lambda x: x.rolling(window=40).mean()   
        )
        df = df.merge(
            weekly_closes[['symbol', 'week_ending', 'sma_30w_calc', 'sma_40w_calc']],
            on=['symbol', 'week_ending'],
            how='left'
        )
        df['sma_30w'] = df['sma_30w_calc']
        df['sma_40w'] = df['sma_40w_calc']
        df = df.drop(['sma_30w_calc', 'sma_40w_calc', 'week_ending'], axis=1)

        # 7. Ratios (Price vs SMA)
        for window in [10, 20, 21, 50, 100, 150, 200]:
            col_sma = f'sma_{window}'
            df[f'price_vs_sma_{window}_percent'] = (
                (df['close'] - df[col_sma]) / df[col_sma].replace(0, np.nan)
            ) * 100

        df['percent_off_52w_high'] = (
            (df['close'] - df['fifty_two_week_high'].replace(0, np.nan)) /
            df['fifty_two_week_high'].replace(0, np.nan)
        ) * 100
        df['percent_off_52w_low'] = (
            (df['close'] - df['fifty_two_week_low'].replace(0, np.nan)) /
            df['fifty_two_week_low'].replace(0, np.nan)
        ) * 100
        df['vol_diff_50_percent'] = (
            (df['volume_traded'] - df['average_volume_50']) /
            df['average_volume_50'].replace(0, np.nan)
        ) * 100

        # 8. Beta Calculation vs TASI Benchmark
        logger.info("   ... computing beta (volatility) vs TASI benchmark")

        tasi_df = df[df['symbol'].isin(['TASI', 'TASI.SR', '^TASI', 'TASI.CM'])]
        if not tasi_df.empty:
            market_df = tasi_df.groupby('date')['close'].last().reset_index()
            market_df.rename(columns={'close': 'market_close'}, inplace=True)
            logger.info("   ✅ TASI index present in loaded data — using it as benchmark")
        else:
            try:
                logger.info("   📊 Loading TASI series from historical_reports...")
                hr_query = """
                    SELECT report_date AS date, close_price AS market_close
                    FROM historical_reports
                    WHERE close_price IS NOT NULL
                    ORDER BY report_date
                """
                with self.engine.connect() as conn:
                    market_df = pd.read_sql(text(hr_query), conn)

                if market_df.empty:
                    raise Exception("historical_reports is empty — no TASI benchmark data available")

                market_df['date'] = pd.to_datetime(market_df['date'])
                # close_price is stored as String in the table — coerce it to a number
                market_df['market_close'] = pd.to_numeric(
                    market_df['market_close'].astype(str).str.replace(',', ''), errors='coerce'
                )
                market_df = market_df.dropna(subset=['market_close'])
                logger.info(f"   ✅ Loaded {len(market_df)} rows from historical_reports (latest: {market_df['date'].max().date()}).")

            except Exception as e:
                logger.error(f"   ❌ Failed to load TASI from historical_reports: {e}")
                market_df = df.groupby('date')['close'].mean().reset_index()
                market_df.rename(columns={'close': 'market_close'}, inplace=True)
                logger.info("   ⚠️ Fallback: synthetic benchmark built from the mean close of all stocks")

        df = df.sort_values(['symbol', 'date']).reset_index(drop=True)
        market_df = market_df.sort_values('date').reset_index(drop=True)

        df['stock_return'] = df.groupby('symbol')['close'].pct_change()
        market_df['market_return'] = market_df['market_close'].pct_change()
        market_df['market_var'] = market_df['market_return'].rolling(window=260, min_periods=130).var()

        df = df.merge(market_df[['date', 'market_return', 'market_var']], on='date', how='left')

        df['cov_stock_market'] = np.nan
        for sym, grp in df.groupby('symbol'):
            cov_vals = grp['stock_return'].rolling(window=260, min_periods=130).cov(grp['market_return'])
            df.loc[grp.index, 'cov_stock_market'] = cov_vals

        df['beta'] = df['cov_stock_market'] / df['market_var']
        df['beta'] = df['beta'].replace([np.inf, -np.inf], np.nan)
        df.drop(['stock_return', 'market_return', 'market_var', 'cov_stock_market'], axis=1, inplace=True)

        df.replace([np.inf, -np.inf], np.nan, inplace=True)
        return df

    def save_latest(self, df):
        """
        Persists:
          - prices.change only, via a single bulk UPDATE ... FROM (VALUES ...) statement
          - all market statistics in stock_indicators via UPSERT
        """
        logger.info("💾 Preparing data for save...")

        latest_dates = df.groupby('symbol')['date'].max().reset_index()
        latest_data = pd.merge(df, latest_dates, on=['symbol', 'date'])

        logger.info(f"🚀 Updating {len(latest_data)} symbols...")

        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                # ─── 1. Bulk UPDATE prices.change (one statement) ────────
                change_updates = [
                    {
                        'change': round(float(row['change']), 2) if pd.notnull(row['change']) else None,
                        'id': int(row['id'])
                    }
                    for _, row in latest_data.iterrows()
                ]
                _n = _bulk_update_prices_change(conn, change_updates)
                logger.info("✅ prices.change updated in one bulk statement: %d rows", _n)

                # ─── 2. UPSERT into stock_indicators ────────────────────────
                for _, row in latest_data.iterrows():
                    symbol   = row['symbol']
                    rec_date = row['date'].date() if hasattr(row['date'], 'date') else row['date']

                    def fv(key, r=row):
                        """safe float value"""
                        val = r.get(key)
                        return float(val) if val is not None and pd.notnull(val) else None

                    def pm(a, b, r=row):
                        """price minus SMA"""
                        va, vb = r.get(a), r.get(b)
                        return float(va - vb) if va is not None and vb is not None \
                               and pd.notnull(va) and pd.notnull(vb) else None

                    si_params = {
                        'symbol':   symbol,
                        'date':     rec_date,
                        'sma_10':   fv('sma_10'),
                        'sma_21':   fv('sma_21'),
                        'sma_50':   fv('sma_50'),
                        'sma_150':  fv('sma_150'),
                        'sma_200':  fv('sma_200'),
                        'sma_200_1m': fv('sma_200_1m_ago'),
                        'sma_200_2m': fv('sma_200_2m_ago'),
                        'sma_200_3m': fv('sma_200_3m_ago'),
                        'sma_200_4m': fv('sma_200_4m_ago'),
                        'sma_200_5m': fv('sma_200_5m_ago'),
                        'sma_30w':  fv('sma_30w'),
                        'sma_40w':  fv('sma_40w'),
                        'h52':      fv('fifty_two_week_high'),
                        'l52':      fv('fifty_two_week_low'),
                        'avg_vol':  fv('average_volume_50'),
                        'pm10':  pm('close', 'sma_10'),
                        'pm21':  pm('close', 'sma_21'),
                        'pm50':  pm('close', 'sma_50'),
                        'pm150': pm('close', 'sma_150'),
                        'pm200': pm('close', 'sma_200'),
                        'p10_pct':   fv('price_vs_sma_10_percent'),
                        'p21_pct':   fv('price_vs_sma_21_percent'),
                        'p50_pct':   fv('price_vs_sma_50_percent'),
                        'p150_pct':  fv('price_vs_sma_150_percent'),
                        'p200_pct':  fv('price_vs_sma_200_percent'),
                        'pct_off_high': fv('percent_off_52w_high'),
                        'pct_off_low':  fv('percent_off_52w_low'),
                        'vol_diff':     fv('vol_diff_50_percent'),
                        'pct_chg_15d':  fv('percent_change_15d'),
                        'pct_chg_20d':  fv('percent_change_20d'),
                        'pct_chg_63d':  fv('percent_change_63d'),
                        'pct_chg_126d': fv('percent_change_126d'),
                        'beta':         fv('beta'),
                    }

                    conn.execute(text("""
                        INSERT INTO stock_indicators (
                            symbol, date,
                            sma_10, sma_21, sma_50, sma_150, sma_200,
                            sma_200_1m_ago, sma_200_2m_ago, sma_200_3m_ago, sma_200_4m_ago, sma_200_5m_ago,
                            sma_30w, sma_40w,
                            fifty_two_week_high, fifty_two_week_low, average_volume_50,
                            price_minus_sma_10, price_minus_sma_21, price_minus_sma_50,
                            price_minus_sma_150, price_minus_sma_200,
                            price_vs_sma_10_percent, price_vs_sma_21_percent, price_vs_sma_50_percent,
                            price_vs_sma_150_percent, price_vs_sma_200_percent,
                            percent_off_52w_high, percent_off_52w_low, vol_diff_50_percent,
                            percent_change_15d, percent_change_20d, percent_change_63d, percent_change_126d, beta
                        ) VALUES (
                            :symbol, :date,
                            :sma_10, :sma_21, :sma_50, :sma_150, :sma_200,
                            :sma_200_1m, :sma_200_2m, :sma_200_3m, :sma_200_4m, :sma_200_5m,
                            :sma_30w, :sma_40w,
                            :h52, :l52, :avg_vol,
                            :pm10, :pm21, :pm50, :pm150, :pm200,
                            :p10_pct, :p21_pct, :p50_pct, :p150_pct, :p200_pct,
                            :pct_off_high, :pct_off_low, :vol_diff,
                            :pct_chg_15d, :pct_chg_20d, :pct_chg_63d, :pct_chg_126d, :beta
                        )
                        ON CONFLICT (symbol, date) DO UPDATE SET
                            sma_10  = EXCLUDED.sma_10,
                            sma_21  = EXCLUDED.sma_21,
                            sma_50  = EXCLUDED.sma_50,
                            sma_150 = EXCLUDED.sma_150,
                            sma_200 = EXCLUDED.sma_200,
                            sma_200_1m_ago = EXCLUDED.sma_200_1m_ago,
                            sma_200_2m_ago = EXCLUDED.sma_200_2m_ago,
                            sma_200_3m_ago = EXCLUDED.sma_200_3m_ago,
                            sma_200_4m_ago = EXCLUDED.sma_200_4m_ago,
                            sma_200_5m_ago = EXCLUDED.sma_200_5m_ago,
                            sma_30w = EXCLUDED.sma_30w,
                            sma_40w = EXCLUDED.sma_40w,
                            fifty_two_week_high = EXCLUDED.fifty_two_week_high,
                            fifty_two_week_low  = EXCLUDED.fifty_two_week_low,
                            average_volume_50   = EXCLUDED.average_volume_50,
                            price_minus_sma_10  = EXCLUDED.price_minus_sma_10,
                            price_minus_sma_21  = EXCLUDED.price_minus_sma_21,
                            price_minus_sma_50  = EXCLUDED.price_minus_sma_50,
                            price_minus_sma_150 = EXCLUDED.price_minus_sma_150,
                            price_minus_sma_200 = EXCLUDED.price_minus_sma_200,
                            price_vs_sma_10_percent  = EXCLUDED.price_vs_sma_10_percent,
                            price_vs_sma_21_percent  = EXCLUDED.price_vs_sma_21_percent,
                            price_vs_sma_50_percent  = EXCLUDED.price_vs_sma_50_percent,
                            price_vs_sma_150_percent = EXCLUDED.price_vs_sma_150_percent,
                            price_vs_sma_200_percent = EXCLUDED.price_vs_sma_200_percent,
                            percent_off_52w_high = EXCLUDED.percent_off_52w_high,
                            percent_off_52w_low  = EXCLUDED.percent_off_52w_low,
                            vol_diff_50_percent  = EXCLUDED.vol_diff_50_percent,
                            percent_change_15d   = EXCLUDED.percent_change_15d,
                            percent_change_20d   = EXCLUDED.percent_change_20d,
                            percent_change_63d   = EXCLUDED.percent_change_63d,
                            percent_change_126d  = EXCLUDED.percent_change_126d,
                            beta                 = EXCLUDED.beta
                    """), si_params)

                trans.commit()
                logger.info("✅ Saved indicator statistics to stock_indicators.")
            except Exception as e:
                trans.rollback()
                logger.error(f"❌ Update failed: {e}")
                raise

    def save_change_only_and_return_tech_map(self, df):
        """
        Atomic Pipeline Mode:
          1. Updates ONLY the 'change' column in prices with a single bulk statement
          2. Returns a dict of {symbol: {sma_10: x, sma_50: y, ...}} for later merging
          3. Does NOT write to stock_indicators (that happens later in one atomic shot)
        """
        logger.info("💾 Preparing data (atomic mode — prices.change only)...")

        latest_dates = df.groupby('symbol')['date'].max().reset_index()
        latest_data = pd.merge(df, latest_dates, on=['symbol', 'date'])

        logger.info(f"🚀 Updating change for {len(latest_data)} symbols...")

        # ─── 1. Bulk UPDATE prices.change (single statement, one round-trip) ───
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                change_updates = [
                    {
                        'change': round(float(row['change']), 2) if pd.notnull(row['change']) else None,
                        'id': int(row['id'])
                    }
                    for _, row in latest_data.iterrows()
                ]
                _n = _bulk_update_prices_change(conn, change_updates)
                trans.commit()
                logger.info("✅ prices.change updated in one bulk statement: %d rows", _n)
            except Exception as e:
                trans.rollback()
                logger.error(f"❌ prices.change update failed: {e}")
                raise

        # ─── 2. Build tech_map: {symbol: {col: value, ...}} ──────────────
        # Note: fv and pm take r=row as a default argument to avoid the closure bug
        tech_map = {}
        for _, row in latest_data.iterrows():
            symbol   = row['symbol']
            rec_date = row['date'].date() if hasattr(row['date'], 'date') else row['date']

            def fv(key, r=row):
                val = r.get(key)
                return float(val) if val is not None and pd.notnull(val) else None

            def pm(a, b, r=row):
                va, vb = r.get(a), r.get(b)
                return float(va - vb) if va is not None and vb is not None \
                       and pd.notnull(va) and pd.notnull(vb) else None

            tech_map[symbol] = {
                'date': rec_date,
                'sma_10':   fv('sma_10'),
                'sma_20':   fv('sma_20'),
                'sma_21':   fv('sma_21'),
                'sma_50':   fv('sma_50'),
                'sma_100':  fv('sma_100'),
                'sma_150':  fv('sma_150'),
                'sma_200':  fv('sma_200'),
                'sma_200_1m_ago': fv('sma_200_1m_ago'),
                'sma_200_2m_ago': fv('sma_200_2m_ago'),
                'sma_200_3m_ago': fv('sma_200_3m_ago'),
                'sma_200_4m_ago': fv('sma_200_4m_ago'),
                'sma_200_5m_ago': fv('sma_200_5m_ago'),
                'sma_30w':  fv('sma_30w'),
                'sma_40w':  fv('sma_40w'),
                'fifty_two_week_high': fv('fifty_two_week_high'),
                'fifty_two_week_low':  fv('fifty_two_week_low'),
                'average_volume_50':   fv('average_volume_50'),
                'price_minus_sma_10':  pm('close', 'sma_10'),
                'price_minus_sma_20':  pm('close', 'sma_20'),
                'price_minus_sma_21':  pm('close', 'sma_21'),
                'price_minus_sma_50':  pm('close', 'sma_50'),
                'price_minus_sma_100': pm('close', 'sma_100'),
                'price_minus_sma_150': pm('close', 'sma_150'),
                'price_minus_sma_200': pm('close', 'sma_200'),
                'price_vs_sma_10_percent':  fv('price_vs_sma_10_percent'),
                'price_vs_sma_20_percent':  fv('price_vs_sma_20_percent'),
                'price_vs_sma_21_percent':  fv('price_vs_sma_21_percent'),
                'price_vs_sma_50_percent':  fv('price_vs_sma_50_percent'),
                'price_vs_sma_100_percent': fv('price_vs_sma_100_percent'),
                'price_vs_sma_150_percent': fv('price_vs_sma_150_percent'),
                'price_vs_sma_200_percent': fv('price_vs_sma_200_percent'),
                'percent_off_52w_high': fv('percent_off_52w_high'),
                'percent_off_52w_low':  fv('percent_off_52w_low'),
                'vol_diff_50_percent':  fv('vol_diff_50_percent'),
                'percent_change_15d':   fv('percent_change_15d'),
                'percent_change_20d':   fv('percent_change_20d'),
                'percent_change_63d':   fv('percent_change_63d'),
                'percent_change_126d':  fv('percent_change_126d'),
                'beta': fv('beta'),
            }

        logger.info(f"✅ Technical indicators ready for {len(tech_map)} symbols (held in memory).")
        return tech_map


if __name__ == "__main__":
    calc = TechnicalCalculator(str(settings.DATABASE_URL))
    df = calc.load_data()
    df_calc = calc.calculate(df)
    calc.save_latest(df_calc)