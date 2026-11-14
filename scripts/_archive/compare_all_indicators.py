"""
compare_all_indicators.py
حساب جميع المؤشرات للأسابيع الثلاثة الأخيرة ومقارنتها مع TradingView
"""

import sys
from pathlib import Path
from datetime import date
import pandas as pd

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.core.database import SessionLocal
from sqlalchemy import text
from scripts.calculate_rsi_indicators import (
    calculate_rsi_pinescript, calculate_sma, calculate_wma, calculate_ema, convert_to_float
)
from scripts.calculate_the_number_indicators import calculate_the_number_full
from scripts.calculate_stamp_indicators import calculate_stamp_components_weekly
from scripts.calculate_trend_screener_indicators import (
    calculate_cci_pinescript_exact, calculate_aroon_pinescript_exact
)


def fetch_prices(db, symbol: str, target_date: date):
    query = text("""
        SELECT date, open, high, low, close
        FROM prices
        WHERE symbol = :symbol AND date <= :target_date
        ORDER BY date ASC
    """)
    rows = db.execute(query, {"symbol": symbol, "target_date": target_date}).fetchall()
    return rows


def make_df(rows):
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].apply(convert_to_float)
    df.dropna(subset=['close'], inplace=True)
    df.set_index('date', inplace=True)
    df.sort_index(inplace=True)
    return df


def main():
    symbol = "1321"
    target_date = date(2026, 2, 12)

    db = SessionLocal()
    try:
        rows = fetch_prices(db, symbol, target_date)
        df = make_df(rows)

        # Resample to weekly
        df_w = df.resample('W-THU').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last'
        }).dropna()

        # Get the last 3 weeks
        target_weeks = [
            ('2026-02-12', 'يوم 15'),
            ('2026-02-05', 'يوم 8'),
            ('2026-01-29', 'يوم 1'),
        ]

        print("\n" + "="*100)
        print("مقارنة المؤشرات - Python vs TradingView")
        print("="*100)

        # Calculate weekly indicators
        closes_w = df_w['close'].tolist()
        highs_w = df_w['high'].tolist()
        lows_w = df_w['low'].tolist()

        rsi_w = calculate_rsi_pinescript(closes_w, 14)
        rsi_3_w = calculate_rsi_pinescript(closes_w, 3)
        sma9_rsi_w = calculate_sma(rsi_w, 9)
        wma45_rsi_w = calculate_wma(rsi_w, 45)
        ema45_rsi_w = calculate_ema(rsi_w, 45)

        sma3_rsi3_w = calculate_sma(rsi_3_w, 3)
        ema20_sma3_w = calculate_ema(sma3_rsi3_w, 20)
        
        # STAMP/CFG
        a_values = []
        for i in range(len(rsi_w)):
            if i < 9 or rsi_w[i] is None or rsi_w[i-9] is None or sma3_rsi3_w[i] is None:
                a_values.append(None)
            else:
                a_values.append(rsi_w[i] - rsi_w[i-9] + sma3_rsi3_w[i])
        
        cfg_w = a_values
        cfg_sma4_w = calculate_sma(cfg_w, 4)
        cfg_sma9_w = calculate_sma(cfg_w, 9)
        cfg_ema20_w = calculate_ema(cfg_w, 20)
        cfg_ema45_w = calculate_ema(cfg_w, 45)
        cfg_wma45_w = calculate_wma(cfg_w, 45)

        # THE NUMBER
        tn_comps = calculate_the_number_full(highs_w, lows_w, closes_w)
        the_number_w = tn_comps['the_number']
        the_number_hl_w = tn_comps['the_number_hl']
        the_number_ll_w = tn_comps['the_number_ll']

        # TREND SCREENER
        sma4_w = calculate_sma(closes_w, 4)
        sma9_w = calculate_sma(closes_w, 9)
        sma18_w = calculate_sma(closes_w, 18)
        wma45_close_w = calculate_wma(closes_w, 45)
        cci_w = calculate_cci_pinescript_exact(highs_w, lows_w, closes_w, 14)
        cci_ema20_w = calculate_ema(cci_w, 20)
        aroon_up_w, aroon_down_w = calculate_aroon_pinescript_exact(highs_w, lows_w, 25)

        for target_date_str, label in target_weeks:
            week_ts = pd.Timestamp(target_date_str)
            idx = list(df_w.index).index(week_ts)

            print(f"\n{'='*100}")
            print(f"الأسبوع: {label} ({target_date_str})")
            print(f"{'='*100}")
            
            print(f"\n📊 RSI Indicators:")
            print(f"  RSI(14):          {rsi_w[idx]:.2f}")
            print(f"  RSI(3):           {rsi_3_w[idx]:.2f}")
            print(f"  SMA9(RSI):        {sma9_rsi_w[idx]:.2f}" if sma9_rsi_w[idx] else "  SMA9(RSI):        N/A")
            print(f"  WMA45(RSI):       {wma45_rsi_w[idx]:.2f}" if wma45_rsi_w[idx] else "  WMA45(RSI):       N/A")
            print(f"  EMA45(RSI):       {ema45_rsi_w[idx]:.2f}" if ema45_rsi_w[idx] else "  EMA45(RSI):       N/A")
            
            print(f"\n📊 STAMP/CFG Indicators:")
            print(f"  SMA3(RSI3):       {sma3_rsi3_w[idx]:.2f}" if sma3_rsi3_w[idx] else "  SMA3(RSI3):       N/A")
            print(f"  EMA20(SMA3):      {ema20_sma3_w[idx]:.2f}" if ema20_sma3_w[idx] else "  EMA20(SMA3):      N/A")
            print(f"  CFG (A):          {cfg_w[idx]:.2f}" if cfg_w[idx] else "  CFG (A):          N/A")
            print(f"  CFG SMA4:         {cfg_sma4_w[idx]:.2f}" if cfg_sma4_w[idx] else "  CFG SMA4:         N/A")
            print(f"  CFG SMA9:         {cfg_sma9_w[idx]:.2f}" if cfg_sma9_w[idx] else "  CFG SMA9:         N/A")
            print(f"  CFG EMA20:        {cfg_ema20_w[idx]:.2f}" if cfg_ema20_w[idx] else "  CFG EMA20:        N/A")
            print(f"  CFG EMA45:        {cfg_ema45_w[idx]:.2f}" if cfg_ema45_w[idx] else "  CFG EMA45:        N/A")
            print(f"  CFG WMA45:        {cfg_wma45_w[idx]:.2f}" if cfg_wma45_w[idx] else "  CFG WMA45:        N/A")
            
            print(f"\n📊 THE NUMBER Indicators:")
            print(f"  THE NUMBER:       {the_number_w[idx]:.2f}" if the_number_w[idx] else "  THE NUMBER:       N/A")
            print(f"  THE NUMBER (H/L): {the_number_hl_w[idx]:.2f}" if the_number_hl_w[idx] else "  THE NUMBER (H/L): N/A")
            print(f"  THE NUMBER (L/L): {the_number_ll_w[idx]:.2f}" if the_number_ll_w[idx] else "  THE NUMBER (L/L): N/A")
            
            print(f"\n📊 Trend Screener Indicators:")
            print(f"  SMA4:             {sma4_w[idx]:.2f}" if sma4_w[idx] else "  SMA4:             N/A")
            print(f"  SMA9:             {sma9_w[idx]:.2f}" if sma9_w[idx] else "  SMA9:             N/A")
            print(f"  SMA18:            {sma18_w[idx]:.2f}" if sma18_w[idx] else "  SMA18:            N/A")
            print(f"  WMA45(Close):     {wma45_close_w[idx]:.2f}" if wma45_close_w[idx] else "  WMA45(Close):     N/A")
            print(f"  CCI(14):          {cci_w[idx]:.2f}" if cci_w[idx] else "  CCI(14):          N/A")
            print(f"  CCI EMA20:        {cci_ema20_w[idx]:.2f}" if cci_ema20_w[idx] else "  CCI EMA20:        N/A")
            print(f"  Aroon Up:         {aroon_up_w[idx]:.2f}" if aroon_up_w[idx] else "  Aroon Up:         N/A")
            print(f"  Aroon Down:       {aroon_down_w[idx]:.2f}" if aroon_down_w[idx] else "  Aroon Down:       N/A")

    finally:
        db.close()


if __name__ == '__main__':
    main()
