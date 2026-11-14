"""
export_weekly_indicators.py
Export weekly indicators for a symbol up to a target date into CSV.
Includes requested fields: CFG_EMA20, WMA45_CLOSE, CFG_WMA45, EMA45(RSI)
Usage: python scripts/export_weekly_indicators.py SYMBOL YYYY-MM-DD
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


def build_weekly_indicators(df_w: pd.DataFrame) -> pd.DataFrame:
    closes_w = df_w['close'].tolist()
    highs_w = df_w['high'].tolist()
    lows_w = df_w['low'].tolist()

    rsi_w = calculate_rsi_pinescript(closes_w, 14)
    rsi3_w = calculate_rsi_pinescript(closes_w, 3)
    sma9_rsi_w = calculate_sma(rsi_w, 9)
    wma45_rsi_w = calculate_wma(rsi_w, 45)
    ema45_rsi_w = calculate_ema(rsi_w, 45)

    sma3_rsi3_w = calculate_sma(rsi3_w, 3)
    ema20_sma3_w = calculate_ema(sma3_rsi3_w, 20)

    # CFG (A) and derivatives
    a_values = []
    for i in range(len(rsi_w)):
        if i < 9 or rsi_w[i] is None or rsi_w[i-9] is None or sma3_rsi3_w[i] is None:
            a_values.append(None)
        else:
            a_values.append(rsi_w[i] - rsi_w[i-9] + sma3_rsi3_w[i])

    cfg_sma4_w = calculate_sma(a_values, 4)
    cfg_sma9_w = calculate_sma(a_values, 9)
    cfg_ema20_w = calculate_ema(a_values, 20)
    cfg_ema45_w = calculate_ema(a_values, 45)
    cfg_wma45_w = calculate_wma(a_values, 45)

    # THE NUMBER
    tn = calculate_the_number_full(highs_w, lows_w, closes_w)

    # Trend
    sma4_w = calculate_sma(closes_w, 4)
    sma9_w = calculate_sma(closes_w, 9)
    sma18_w = calculate_sma(closes_w, 18)
    # Use weekly closes rounded to 1 decimal (TradingView display) when computing WMA45 on closes
    closes_w_rounded = [round(x, 1) if x is not None else None for x in closes_w]
    wma45_close_w = calculate_wma(closes_w_rounded, 45)
    cci_w = calculate_cci_pinescript_exact(highs_w, lows_w, closes_w, 14)
    cci_ema20_w = calculate_ema(cci_w, 20)
    aroon_up_w, aroon_down_w = calculate_aroon_pinescript_exact(highs_w, lows_w, 25)

    # Assemble DataFrame
    data = {
        'open_w': df_w['open'].tolist(),
        'high_w': df_w['high'].tolist(),
        'low_w': df_w['low'].tolist(),
        'close_w': df_w['close'].tolist(),
        'rsi14_w': rsi_w,
        'rsi3_w': rsi3_w,
        'sma9_rsi_w': sma9_rsi_w,
        'wma45_rsi_w': wma45_rsi_w,
        'ema45_rsi_w': ema45_rsi_w,
        'sma3_rsi3_w': sma3_rsi3_w,
        'ema20_sma3_w': ema20_sma3_w,
        'cfg_w': a_values,
        'cfg_sma4_w': cfg_sma4_w,
        'cfg_sma9_w': cfg_sma9_w,
        'cfg_ema20_w': cfg_ema20_w,
        'cfg_ema45_w': cfg_ema45_w,
        'cfg_wma45_w': cfg_wma45_w,
        'the_number_w': tn['the_number'],
        'the_number_hl_w': tn['the_number_hl'],
        'the_number_ll_w': tn['the_number_ll'],
        'sma9_close_w': tn['sma9_close'],
        'sma4_w': sma4_w,
        'sma9_w': sma9_w,
        'sma18_w': sma18_w,
        'wma45_close_w': wma45_close_w,
        'cci_w': cci_w,
        'cci_ema20_w': cci_ema20_w,
        'aroon_up_w': aroon_up_w,
        'aroon_down_w': aroon_down_w,
    }

    df_out = pd.DataFrame(data, index=df_w.index)
    return df_out


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/export_weekly_indicators.py SYMBOL YYYY-MM-DD")
        sys.exit(1)

    symbol = sys.argv[1]
    try:
        target_date = date.fromisoformat(sys.argv[2])
    except Exception:
        print("Invalid date format. Use YYYY-MM-DD")
        sys.exit(1)

    db = SessionLocal()
    try:
        rows = fetch_prices(db, symbol, target_date)
        if not rows:
            print(f"No price rows found for {symbol} up to {target_date}")
            return
        df = make_df(rows)
        if df is None or df.empty:
            print("Not enough data")
            return

        df_w = df.resample('W-THU').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last'
        }).dropna()

        df_ind = build_weekly_indicators(df_w)

        # restrict to weeks <= target_date
        df_ind = df_ind[df_ind.index.to_pydatetime() <= pd.to_datetime(target_date)]

        out_dir = Path('output')
        out_dir.mkdir(exist_ok=True)
        out_file = out_dir / f"weekly_indicators_{symbol}_{target_date}.csv"
        df_ind.to_csv(out_file, float_format='%.6f')

        print(f"Wrote weekly indicators CSV: {out_file}")
    finally:
        db.close()


if __name__ == '__main__':
    main()
