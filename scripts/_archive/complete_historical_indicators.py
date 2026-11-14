"""
ULTRA-FAST Historical Indicators Calculator - COMPLETE VERSION
يحسب جميع المؤشرات في جدول stock_indicators تاريخياً بأقصى سرعة
"""

import sys
import os
import time
import argparse
import pandas as pd
import numpy as np
import logging
import io
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from app.models.stock_indicators import StockIndicator

from scripts.calculate_technicals import TechnicalCalculator
from scripts.indicators_data_service import IndicatorsDataService
from scripts.calculate_rsi_indicators import calculate_rsi_components, calculate_rsi_pinescript, calculate_sma, calculate_wma, calculate_ema
from scripts.calculate_the_number_indicators import calculate_the_number_full
from scripts.calculate_trend_screener_indicators import calculate_trend_components, calculate_trend_conditions

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def calculate_complete_indicators_for_symbol(symbol: str, df_prices: pd.DataFrame) -> List[Dict[str, Any]]:
    """Calculate ALL indicators for a single symbol - comprehensive version"""
    try:
        df_sym = df_prices[df_prices['symbol'] == symbol].sort_values('date').copy()
        df_sym.set_index('date', inplace=True)

        if len(df_sym) < 200:  # Need more data for all indicators
            logger.warning(f"⚠️ {symbol}: Insufficient data ({len(df_sym)} rows)")
            return []

        closes = df_sym['close'].tolist()
        highs = df_sym['high'].tolist()
        lows = df_sym['low'].tolist()
        opens = df_sym['open'].tolist()
        
        # --- PRECALCULATE DAILY MARKET STATISTICS ---
        df_sym['sma_10'] = df_sym['close'].rolling(10).mean()
        df_sym['sma_21'] = df_sym['close'].rolling(21).mean()
        df_sym['sma_50'] = df_sym['close'].rolling(50).mean()
        df_sym['sma_150'] = df_sym['close'].rolling(150).mean()
        df_sym['sma_200'] = df_sym['close'].rolling(200).mean()

        df_sym['sma_200_1m_ago'] = df_sym['sma_200'].shift(21)
        df_sym['sma_200_2m_ago'] = df_sym['sma_200'].shift(42)
        df_sym['sma_200_3m_ago'] = df_sym['sma_200'].shift(63)
        df_sym['sma_200_4m_ago'] = df_sym['sma_200'].shift(84)
        df_sym['sma_200_5m_ago'] = df_sym['sma_200'].shift(105)

        df_sym['fifty_two_week_high'] = df_sym['high'].rolling(252).max()
        df_sym['fifty_two_week_low'] = df_sym['low'].rolling(252).min()
        df_sym['average_volume_50'] = df_sym['volume_traded'].rolling(50).mean()

        df_sym['price_vs_sma_10_percent'] = ((df_sym['close'] / df_sym['sma_10']) - 1) * 100
        df_sym['price_vs_sma_21_percent'] = ((df_sym['close'] / df_sym['sma_21']) - 1) * 100
        df_sym['price_vs_sma_50_percent'] = ((df_sym['close'] / df_sym['sma_50']) - 1) * 100
        df_sym['price_vs_sma_150_percent'] = ((df_sym['close'] / df_sym['sma_150']) - 1) * 100
        df_sym['price_vs_sma_200_percent'] = ((df_sym['close'] / df_sym['sma_200']) - 1) * 100

        df_sym['percent_off_52w_high'] = ((df_sym['close'] / df_sym['fifty_two_week_high']) - 1) * 100
        df_sym['percent_off_52w_low'] = ((df_sym['close'] / df_sym['fifty_two_week_low']) - 1) * 100
        df_sym['vol_diff_50_percent'] = ((df_sym['volume_traded'] / df_sym['average_volume_50']) - 1) * 100
        
        # Replace infinities with NaN to prevent PostgreSQL Numeric field overflow errors
        df_sym.replace([np.inf, -np.inf], np.nan, inplace=True)

        # --- 1. RSI Components ---
        rsi_components_data = calculate_rsi_components(closes)

        # --- 2. The Number Components ---
        tn_components = calculate_the_number_full(highs, lows, closes)

        # --- 3. Trend Components ---
        trend_components_data = calculate_trend_components(highs, lows, closes)

        # --- 4. STAMP and CFG Calculations ---
        rsi_14 = rsi_components_data['rsi_14']
        rsi_3 = rsi_components_data['rsi_3']
        sma3_rsi3 = rsi_components_data['sma3_rsi3']

        stamp_a_value = []
        rsi_14_9days_ago = []

        for j in range(len(rsi_14)):
            if j >= 9 and rsi_14[j-9] is not None:
                rsi_14_9days_ago.append(rsi_14[j-9])
                if rsi_14[j] is not None and sma3_rsi3[j] is not None:
                    stamp_a_value.append(rsi_14[j] - rsi_14[j-9] + sma3_rsi3[j])
                else:
                    stamp_a_value.append(None)
            else:
                rsi_14_9days_ago.append(None)
                stamp_a_value.append(None)

        # CFG calculations
        cfg_daily = stamp_a_value
        cfg_sma4 = calculate_sma(stamp_a_value, 4) if len(stamp_a_value) > 4 else [None] * len(stamp_a_value)
        cfg_sma9 = calculate_sma(stamp_a_value, 9) if len(stamp_a_value) > 9 else [None] * len(stamp_a_value)
        cfg_sma20 = calculate_sma(stamp_a_value, 20) if len(stamp_a_value) > 20 else [None] * len(stamp_a_value)
        cfg_ema20 = calculate_ema(stamp_a_value, 20) if len(stamp_a_value) > 20 else [None] * len(stamp_a_value)
        cfg_ema45 = calculate_ema(stamp_a_value, 45) if len(stamp_a_value) > 45 else [None] * len(stamp_a_value)
        cfg_wma45 = calculate_wma(stamp_a_value, 45) if len(stamp_a_value) > 45 else [None] * len(stamp_a_value)

        # --- 5. Weekly Data ---
        df_weekly = IndicatorsDataService.prepare_weekly_dataframe(df_sym) if len(df_sym) > 50 else None
        if df_weekly is not None:
            df_merged = IndicatorsDataService.merge_weekly_with_daily(df_sym, df_weekly)
        else:
            df_merged = df_sym.copy()

        records = []

        for idx in range(len(df_sym)):
            row_date = df_sym.index[idx].date()
            row_data = df_sym.iloc[idx]

            if idx < 100:  # Skip first 100 days for indicator stability
                continue

            def sf(v): # safe float
                try:
                    return round(float(v), 4) if v is not None and not pd.isna(v) and not np.isinf(float(v)) else None
                except (ValueError, TypeError):
                    return None

            # Trend conditions
            tc = calculate_trend_conditions(trend_components_data, {}, idx, 0, symbol, df_sym)

            # ---------------- COMPLETE INDICATORS FOR STOCK_INDICATORS TABLE ----------------
            item = {
                'symbol': symbol,
                'date': row_date,
                'company_name': row_data.get('company_name', symbol),
                'close': sf(closes[idx]),

                # ============ 1. RSI COMPONENTS ============
                'rsi_14': sf(rsi_components_data['rsi_14'][idx]),
                'rsi_3': sf(rsi_components_data['rsi_3'][idx]),
                'sma9_rsi': sf(rsi_components_data['sma9_rsi'][idx]),
                'wma45_rsi': sf(rsi_components_data['wma45_rsi'][idx]),
                'ema45_rsi': sf(rsi_components_data['ema45_rsi'][idx]),
                'sma3_rsi3': sf(rsi_components_data['sma3_rsi3'][idx]),
                'ema20_sma3': sf(rsi_components_data['ema20_sma3'][idx]),

                # ============ 2. THE NUMBER COMPONENTS ============
                'sma9_close': sf(tn_components['sma9_close'][idx]),
                'high_sma13': sf(tn_components['high_sma13'][idx]),
                'low_sma13': sf(tn_components['low_sma13'][idx]),
                'high_sma65': sf(tn_components['high_sma65'][idx]),
                'low_sma65': sf(tn_components['low_sma65'][idx]),
                'the_number': sf(tn_components['the_number'][idx]),
                'the_number_hl': sf(tn_components['the_number_hl'][idx]),
                'the_number_ll': sf(tn_components['the_number_ll'][idx]),

                # ============ 3. STAMP INDICATOR COMPONENTS ============
                'rsi_14_9days_ago': sf(rsi_14_9days_ago[idx]),
                'stamp_a_value': sf(stamp_a_value[idx]),
                'stamp_s9rsi': sf(rsi_components_data['sma9_rsi'][idx]),
                'stamp_e45cfg': sf(cfg_ema45[idx]) if cfg_ema45 and idx < len(cfg_ema45) else None,
                'stamp_e45rsi': sf(rsi_components_data['ema45_rsi'][idx]),
                'stamp_e20sma3': sf(rsi_components_data['ema20_sma3'][idx]),

                # ============ 4. CFG ANALYSIS ============
                'cfg_daily': sf(cfg_daily[idx]),
                'cfg_sma4': sf(cfg_sma4[idx]) if cfg_sma4 and idx < len(cfg_sma4) else None,
                'cfg_sma9': sf(cfg_sma9[idx]) if cfg_sma9 and idx < len(cfg_sma9) else None,
                'cfg_sma20': sf(cfg_sma20[idx]) if cfg_sma20 and idx < len(cfg_sma20) else None,
                'cfg_ema20': sf(cfg_ema20[idx]) if cfg_ema20 and idx < len(cfg_ema20) else None,
                'cfg_ema45': sf(cfg_ema45[idx]) if cfg_ema45 and idx < len(cfg_ema45) else None,
                'cfg_wma45': sf(cfg_wma45[idx]) if cfg_wma45 and idx < len(cfg_wma45) else None,
                'rsi_14_9days_ago_cfg': sf(rsi_14_9days_ago[idx]),
                'rsi_14_minus_9': sf(rsi_components_data['rsi_14'][idx] - rsi_14_9days_ago[idx]) if rsi_components_data['rsi_14'][idx] is not None and rsi_14_9days_ago[idx] is not None else None,

                # ============ 5. TREND SCREENER COMPONENTS ============
                'ema10': sf(trend_components_data['ema10'][idx]),
                'ema21': sf(trend_components_data['ema21'][idx]),
                'sma4': sf(trend_components_data['sma4'][idx]),
                'sma9': sf(trend_components_data['sma9'][idx]),
                'sma18': sf(trend_components_data['sma18'][idx]),
                'wma45_close': sf(trend_components_data['wma45_close'][idx]),
                'cci': sf(trend_components_data['cci'][idx]),
                'cci_ema20': sf(trend_components_data['cci_ema20'][idx]),
                'aroon_up': sf(trend_components_data['aroon_up'][idx]),
                'aroon_down': sf(trend_components_data['aroon_down'][idx]),

                # ============ 6. MARKET STATISTICS ============
                'sma_10': sf(row_data.get('sma_10')),
                'sma_21': sf(row_data.get('sma_21')),
                'sma_50': sf(row_data.get('sma_50')),
                'sma_150': sf(row_data.get('sma_150')),
                'sma_200': sf(row_data.get('sma_200')),
                'sma_200_1m_ago': sf(row_data.get('sma_200_1m_ago')),
                'sma_200_2m_ago': sf(row_data.get('sma_200_2m_ago')),
                'sma_200_3m_ago': sf(row_data.get('sma_200_3m_ago')),
                'sma_200_4m_ago': sf(row_data.get('sma_200_4m_ago')),
                'sma_200_5m_ago': sf(row_data.get('sma_200_5m_ago')),
                'sma_30w': sf(df_merged.iloc[idx].get('sma_30w')) if 'sma_30w' in df_merged.columns else None,
                'sma_40w': sf(df_merged.iloc[idx].get('sma_40w')) if 'sma_40w' in df_merged.columns else None,
                'fifty_two_week_high': sf(row_data.get('fifty_two_week_high')),
                'fifty_two_week_low': sf(row_data.get('fifty_two_week_low')),
                'average_volume_50': sf(row_data.get('average_volume_50')),

                'price_minus_sma_10': sf(closes[idx] - row_data.get('sma_10')) if pd.notnull(row_data.get('sma_10')) else None,
                'price_minus_sma_21': sf(closes[idx] - row_data.get('sma_21')) if pd.notnull(row_data.get('sma_21')) else None,
                'price_minus_sma_50': sf(closes[idx] - row_data.get('sma_50')) if pd.notnull(row_data.get('sma_50')) else None,
                'price_minus_sma_150': sf(closes[idx] - row_data.get('sma_150')) if pd.notnull(row_data.get('sma_150')) else None,
                'price_minus_sma_200': sf(closes[idx] - row_data.get('sma_200')) if pd.notnull(row_data.get('sma_200')) else None,

                'price_vs_sma_10_percent': sf(row_data.get('price_vs_sma_10_percent')),
                'price_vs_sma_21_percent': sf(row_data.get('price_vs_sma_21_percent')),
                'price_vs_sma_50_percent': sf(row_data.get('price_vs_sma_50_percent')),
                'price_vs_sma_150_percent': sf(row_data.get('price_vs_sma_150_percent')),
                'price_vs_sma_200_percent': sf(row_data.get('price_vs_sma_200_percent')),
                'percent_off_52w_high': sf(row_data.get('percent_off_52w_high')),
                'percent_off_52w_low': sf(row_data.get('percent_off_52w_low')),
                'vol_diff_50_percent': sf(row_data.get('vol_diff_50_percent')),

                # ============ 7. WEEKLY VALUES ============
                'close_w': sf(df_merged.iloc[idx].get('close_w')) if 'close_w' in df_merged.columns else None,
                'sma4_w': sf(df_merged.iloc[idx].get('sma4_w')) if 'sma4_w' in df_merged.columns else None,
                'sma9_w': sf(df_merged.iloc[idx].get('sma9_w')) if 'sma9_w' in df_merged.columns else None,
                'sma18_w': sf(df_merged.iloc[idx].get('sma18_w')) if 'sma18_w' in df_merged.columns else None,
                'wma45_close_w': sf(df_merged.iloc[idx].get('wma45_close_w')) if 'wma45_close_w' in df_merged.columns else None,
                'cci_w': sf(df_merged.iloc[idx].get('cci_w')) if 'cci_w' in df_merged.columns else None,
                'cci_ema20_w': sf(df_merged.iloc[idx].get('cci_ema20_w')) if 'cci_ema20_w' in df_merged.columns else None,
                'aroon_up_w': sf(df_merged.iloc[idx].get('aroon_up_w')) if 'aroon_up_w' in df_merged.columns else None,
                'aroon_down_w': sf(df_merged.iloc[idx].get('aroon_down_w')) if 'aroon_down_w' in df_merged.columns else None,

                'rsi_w': sf(df_merged.iloc[idx].get('rsi_w')) if 'rsi_w' in df_merged.columns else None,
                'rsi_3_w': sf(df_merged.iloc[idx].get('rsi_3_w')) if 'rsi_3_w' in df_merged.columns else None,
                'sma3_rsi3_w': sf(df_merged.iloc[idx].get('sma3_rsi3_w')) if 'sma3_rsi3_w' in df_merged.columns else None,
                'sma9_rsi_w': sf(df_merged.iloc[idx].get('sma9_rsi_w')) if 'sma9_rsi_w' in df_merged.columns else None,
                'wma45_rsi_w': sf(df_merged.iloc[idx].get('wma45_rsi_w')) if 'wma45_rsi_w' in df_merged.columns else None,
                'ema45_rsi_w': sf(df_merged.iloc[idx].get('ema45_rsi_w')) if 'ema45_rsi_w' in df_merged.columns else None,
                'ema20_sma3_w': sf(df_merged.iloc[idx].get('ema20_sma3_w')) if 'ema20_sma3_w' in df_merged.columns else None,

                'sma9_close_w': sf(df_merged.iloc[idx].get('sma9_close_w')) if 'sma9_close_w' in df_merged.columns else None,
                'the_number_w': sf(df_merged.iloc[idx].get('the_number_w')) if 'the_number_w' in df_merged.columns else None,
                'the_number_hl_w': sf(df_merged.iloc[idx].get('the_number_hl_w')) if 'the_number_hl_w' in df_merged.columns else None,
                'the_number_ll_w': sf(df_merged.iloc[idx].get('the_number_ll_w')) if 'the_number_ll_w' in df_merged.columns else None,

                'cfg_w': sf(df_merged.iloc[idx].get('cfg_w')) if 'cfg_w' in df_merged.columns else None,
                'cfg_sma4_w': sf(df_merged.iloc[idx].get('cfg_sma4_w')) if 'cfg_sma4_w' in df_merged.columns else None,
                'cfg_sma9_w': sf(df_merged.iloc[idx].get('cfg_sma9_w')) if 'cfg_sma9_w' in df_merged.columns else None,
                'cfg_sma20_w': sf(df_merged.iloc[idx].get('cfg_sma20_w')) if 'cfg_sma20_w' in df_merged.columns else None,
                'cfg_ema20_w': sf(df_merged.iloc[idx].get('cfg_ema20_w')) if 'cfg_ema20_w' in df_merged.columns else None,
                'cfg_ema45_w': sf(df_merged.iloc[idx].get('cfg_ema45_w')) if 'cfg_ema45_w' in df_merged.columns else None,
                'cfg_wma45_w': sf(df_merged.iloc[idx].get('cfg_wma45_w')) if 'cfg_wma45_w' in df_merged.columns else None,

                'rsi_14_9days_ago_w': sf(df_merged.iloc[idx].get('rsi_14_9days_ago_w')) if 'rsi_14_9days_ago_w' in df_merged.columns else None,
                'rsi_14_minus_9_w': sf(df_merged.iloc[idx].get('rsi_14_minus_9_w')) if 'rsi_14_minus_9_w' in df_merged.columns else None,
                'rsi_14_w_shifted': sf(df_merged.iloc[idx].get('rsi_14_w_shifted')) if 'rsi_14_w_shifted' in df_merged.columns else None,
                'stamp_a_value_w': sf(df_merged.iloc[idx].get('stamp_a_value_w')) if 'stamp_a_value_w' in df_merged.columns else None,
                'stamp_s9rsi_w': sf(df_merged.iloc[idx].get('stamp_s9rsi_w')) if 'stamp_s9rsi_w' in df_merged.columns else None,
                'stamp_e45cfg_w': sf(df_merged.iloc[idx].get('stamp_e45cfg_w')) if 'stamp_e45cfg_w' in df_merged.columns else None,
                'stamp_e45rsi_w': sf(df_merged.iloc[idx].get('stamp_e45rsi_w')) if 'stamp_e45rsi_w' in df_merged.columns else None,
                'stamp_e20sma3_w': sf(df_merged.iloc[idx].get('stamp_e20sma3_w')) if 'stamp_e20sma3_w' in df_merged.columns else None,

                # ============ 8. BOOLEAN CONDITIONS ============
                'is_etf_or_index': bool(tc.get('is_etf_or_index')),
                'has_gap': bool(tc.get('has_gap')),
                'trend_signal': bool(tc.get('trend_signal')),
                'price_gt_sma18': bool(tc.get('price_gt_sma18')),
                'price_gt_sma9_weekly': bool(tc.get('price_gt_sma9_weekly')),
                'sma_trend_daily': bool(tc.get('sma_trend_daily')),
                'sma_trend_weekly': bool(tc.get('sma_trend_weekly')),
                'cci_gt_100': bool(tc.get('cci_gt_100')),
                'cci_ema20_gt_0_daily': bool(tc.get('cci_ema20_gt_0_daily')),
                'cci_ema20_gt_0_weekly': bool(tc.get('cci_ema20_gt_0_weekly')),
                'aroon_up_gt_70': bool(tc.get('aroon_up_gt_70')),
                'aroon_down_lt_30': bool(tc.get('aroon_down_lt_30')),

                'ema10_gt_sma50': bool(tc.get('ema10_gt_sma50')),
                'ema10_gt_sma200': bool(tc.get('ema10_gt_sma200')),
                'ema21_gt_sma50': bool(tc.get('ema21_gt_sma50')),
                'ema21_gt_sma200': bool(tc.get('ema21_gt_sma200')),
                'sma50_gt_sma150': bool(tc.get('sma50_gt_sma150')),
                'sma50_gt_sma200': bool(tc.get('sma50_gt_sma200')),
                'sma150_gt_sma200': bool(tc.get('sma150_gt_sma200')),

                'sma200_gt_sma200_1m_ago': bool(tc.get('sma200_gt_sma200_1m_ago')),
                'sma200_gt_sma200_2m_ago': bool(tc.get('sma200_gt_sma200_2m_ago')),
                'sma200_gt_sma200_3m_ago': bool(tc.get('sma200_gt_sma200_3m_ago')),
                'sma200_gt_sma200_4m_ago': bool(tc.get('sma200_gt_sma200_4m_ago')),
                'sma200_gt_sma200_5m_ago': bool(tc.get('sma200_gt_sma200_5m_ago')),

                # RSI Screener conditions (simplified)
                'rsi_lt_80_d': sf(rsi_components_data['rsi_14'][idx]) < 80 if rsi_components_data['rsi_14'][idx] else False,
                'rsi_lt_80_w': sf(df_merged.iloc[idx].get('rsi_w')) < 80 if df_merged.iloc[idx].get('rsi_w') else False,

                # CFG conditions
                'cfg_gt_50_daily': sf(cfg_daily[idx]) > 50 if cfg_daily[idx] else False,
                'cfg_ema45_gt_50': sf(cfg_ema45[idx]) > 50 if cfg_ema45 and idx < len(cfg_ema45) and cfg_ema45[idx] else False,
                'cfg_ema20_gt_50': sf(cfg_ema20[idx]) > 50 if cfg_ema20 and idx < len(cfg_ema20) and cfg_ema20[idx] else False,
            }

            records.append(item)

        return records

    except Exception as e:
        logger.error(f"❌ Error calculating indicators for {symbol}: {e}")
        return []

def bulk_save_complete_records(records: List[Dict[str, Any]]):
    """Save complete records in bulk using ultra-fast COPY with temporary table upsert"""
    db_session = SessionLocal()
    if not records:
        return 0

    try:
        if not records:
            return 0

        # Create a CSV in memory
        csv_buffer = io.StringIO()
        
        # Get all keys from the first record (assuming homogeneous dictionaries)
        # Filter out keys with None values in the first pass just to get base structure
        # But for SQL COPY, we need all columns consistent. We use the keys of the first record.
        # Ensure 'id' is not in keys as it's auto-generated
        keys = [k for k in records[0].keys() if k != 'id']
        keys.append('created_at') # Always supply this explicitly or let DB handle it. We will let DB handle it by not including it or explicitly setting None
        if 'created_at' in keys: keys.remove('created_at')

        writer = csv.DictWriter(csv_buffer, fieldnames=keys, extrasaction='ignore')
        writer.writerows(records)
        csv_buffer.seek(0)
        
        # We need the raw psycopg2 connection to use copy_expert
        raw_conn = db_session.connection().connection
        cursor = raw_conn.cursor()

        # 1. Create a temporary table with the exact same structure
        temp_table_name = "temp_stock_indicators"
        cursor.execute(f"CREATE TEMP TABLE {temp_table_name} (LIKE stock_indicators INCLUDING ALL)")

        # 2. Use COPY to load data directly into the temp table
        columns_str = ", ".join(keys)
        copy_sql = f"COPY {temp_table_name} ({columns_str}) FROM STDIN WITH CSV"
        cursor.copy_expert(sql=copy_sql, file=csv_buffer)

        # 3. UPSERT data from the temp table to the main table
        # We create the SET clause dynamically for all columns except the primary keys
        update_cols = [col for col in keys if col not in ('id', 'symbol', 'date', 'created_at')]
        set_statements = ", ".join([f"{col} = EXCLUDED.{col}" for col in update_cols])

        upsert_sql = f"""
            INSERT INTO stock_indicators ({columns_str})
            SELECT {columns_str} FROM {temp_table_name}
            ON CONFLICT (symbol, date) DO UPDATE SET
            {set_statements};
        """
        cursor.execute(upsert_sql)

        # 4. Drop the temporary table (optional as it drops on disconnect, but good practice)
        cursor.execute(f"DROP TABLE {temp_table_name}")

        db_session.commit()
        db_session.close()
        return len(records)

    except Exception as e:
        logger.error(f"❌ Error saving complete records via COPY method: {e}")
        db_session.rollback()
        # Fallback to standard line-by-line upsert if COPY fails
        try:
            logger.info("⚠️ Falling back to slow standard insert method...")
            chunk_size = 200
            total_saved = 0
            for i in range(0, len(records), chunk_size):
                chunk = records[i:i + chunk_size]
                stmt = insert(StockIndicator).values(chunk)
                update_dict = {
                    c.name: c for c in stmt.excluded
                    if c.name not in ('id', 'symbol', 'date', 'created_at')
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=['symbol', 'date'],
                    set_=update_dict
                )
                db_session.execute(stmt)
                total_saved += len(chunk)
            db_session.commit()
            db_session.close()
            return total_saved
        except Exception as e2:
            logger.error(f"❌ Total failure in saving records: {e2}")
            db_session.rollback()
            db_session.close()
            return 0

def calculate_complete_historical_ultra_fast(symbols_list: List[str] = None, max_workers: int = 2):
    """Ultra-fast complete historical calculation using parallel processing"""
    db = SessionLocal()
    try:
        engine = db.get_bind()
        from app.core.config import settings
        tech_calc = TechnicalCalculator(settings.DATABASE_URL)
        logger.info("⏳ Loading ALL price data for Complete Historical Indicators...")

        # Build query
        query = "SELECT * FROM prices"
        conditions = []
        if symbols_list:
            symbols_str = ','.join(f"'{s}'" for s in symbols_list)
            conditions.append(f"symbol IN ({symbols_str})")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        with engine.connect() as conn:
            df_prices = pd.read_sql(text(query), conn)

        if df_prices.empty:
            logger.warning("No data found!")
            return

        df_prices['date'] = pd.to_datetime(df_prices['date'])

        # Get unique symbols
        if symbols_list:
            symbols = symbols_list
        else:
            symbols = df_prices['symbol'].unique().tolist()

        logger.info(f"✅ Loaded {len(df_prices)} price records for {len(symbols)} symbols.")
        logger.info("📈 Calculating historical market stats (ALL SMAs, 52W, Vol)...")

        df_tech = tech_calc.calculate(df_prices)
        df_tech.replace([np.inf, -np.inf], None, inplace=True)
        df_tech = df_tech.where(pd.notnull(df_tech), None)

        logger.info(f"🚀 Processing {len(symbols)} stocks for ALL PineScript Indicators using {max_workers} workers...")

        total_saved = 0
        start_time = time.time()

        # Process symbols in parallel (use fewer workers for memory-intensive calculations)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_symbol = {
                executor.submit(calculate_complete_indicators_for_symbol, symbol, df_tech): symbol
                for symbol in symbols
            }

            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    records = future.result()
                    if records:
                        saved = bulk_save_complete_records(records)
                        total_saved += saved
                        logger.info(f"✅ {symbol}: saved {saved} complete historical days")
                    else:
                        logger.warning(f"⚠️ {symbol}: no records generated")

                except Exception as e:
                    logger.error(f"❌ {symbol}: failed with error {e}")

                # Memory cleanup
                import gc
                gc.collect()

        total_time = time.time() - start_time
        logger.info(f"🎉 COMPLETED! Total saved: {total_saved:,} complete records in {total_time:.1f} seconds")
        logger.info(f"🚀 Average speed: {total_saved/total_time:.0f} records/second")

    except Exception as e:
        logger.error(f"Error during complete calculation: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ULTRA-FAST Calculate ALL indicators Historically - COMPLETE VERSION")
    parser.add_argument("--symbols", help="Comma-separated list of symbols (e.g. 1321,1010,2222)")
    parser.add_argument("--workers", type=int, default=2, help="Number of parallel workers (default: 2, use fewer for complete calculations)")
    parser.add_argument("--symbol", help="Single symbol (alternative to --symbols)")

    args = parser.parse_args()

    # Parse symbols
    symbols_list = None
    if args.symbol:
        symbols_list = [args.symbol]
    elif args.symbols:
        symbols_str = args.symbols
        symbols_list = [s.strip() for s in symbols_str.split(',')]

    print("="*100)
    print("🚀 ULTRA-FAST COMPLETE HISTORICAL INDICATORS CALCULATION")
    print("   Calculates ALL indicators in stock_indicators table")
    print("="*100)
    print(f"📊 Target Symbols: {symbols_list if symbols_list else 'ALL'}")
    print(f"⚡ Workers: {args.workers}")
    print("="*100)

    calculate_complete_historical_ultra_fast(symbols_list, args.workers)