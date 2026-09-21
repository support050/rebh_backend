"""
╔══════════════════════════════════════════════════════════════════════════╗
║          TASI Stock Indicators - Full Historical CSV Export              ║
║                                                                          ║
║  يسحب كل البيانات الفنية التاريخية من جدول stock_indicators           ║
║  من أقدم تاريخ لكل سهم حتى آخر تاريخ متاح                             ║
║                                                                          ║
║  الاستخدام:                                                              ║
║    # كل الأسهم في ملف واحد (الافتراضي)                                  ║
║    python scripts/export_stock_indicators_csv.py                         ║
║                                                                          ║
║    # سهم واحد فقط                                                        ║
║    python scripts/export_stock_indicators_csv.py --symbol 2222           ║
║                                                                          ║
║    # ملف منفصل لكل سهم                                                  ║
║    python scripts/export_stock_indicators_csv.py --split-by-symbol       ║
║                                                                          ║
║    # تحديد فترة                                                          ║
║    python scripts/export_stock_indicators_csv.py --from 2020-01-01       ║
║                                                                          ║
║    # عرض كل الأعمدة المتاحة                                              ║
║    python scripts/export_stock_indicators_csv.py --list-columns          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sys
import os
import argparse
import pandas as pd
from datetime import datetime, date
from pathlib import Path

# Path setup
HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import SessionLocal
from app.models.stock_indicators import StockIndicator

# Output directory
OUTPUT_DIR = BACKEND_ROOT / "output" / "indicators_export"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── الأعمدة الكاملة مرتّبة بالمجموعات مع شرح ─────────────────────────────
COLUMNS_INFO = {
    # أساسيات
    "symbol":       "رمز السهم",
    "date":         "تاريخ الجلسة",
    "company_name": "اسم الشركة",
    "close":        "سعر الإغلاق",
    # SMA
    "sma_10":  "SMA10", "sma_20":  "SMA20", "sma_21":  "SMA21",
    "sma_50":  "SMA50", "sma_100": "SMA100","sma_150": "SMA150","sma_200": "SMA200",
    "sma_200_1m_ago": "SMA200 قبل شهر","sma_200_2m_ago": "SMA200 قبل شهرين",
    "sma_200_3m_ago": "SMA200 قبل 3 أشهر","sma_200_4m_ago": "SMA200 قبل 4 أشهر",
    "sma_200_5m_ago": "SMA200 قبل 5 أشهر",
    "sma_30w": "SMA30 أسبوعي","sma_40w": "SMA40 أسبوعي",
    # EMA/WMA
    "ema10": "EMA10","ema21": "EMA21","wma45_close": "WMA45 للسعر",
    "sma4": "SMA4","sma9": "SMA9","sma18": "SMA18","sma9_close": "SMA9 (The Number)",
    "close_w": "إغلاق أسبوعي","sma4_w": "SMA4 أسبوعي","sma9_w": "SMA9 أسبوعي",
    "sma18_w": "SMA18 أسبوعي","wma45_close_w": "WMA45 أسبوعي",
    # 52w & Volume
    "fifty_two_week_high": "أعلى 52 أسبوع","fifty_two_week_low": "أدنى 52 أسبوع",
    "average_volume_50": "متوسط الحجم 50 يوم",
    # Price vs SMA (absolute)
    "price_minus_sma_10": "السعر-SMA10","price_minus_sma_20": "السعر-SMA20",
    "price_minus_sma_50": "السعر-SMA50","price_minus_sma_100": "السعر-SMA100",
    "price_minus_sma_150": "السعر-SMA150","price_minus_sma_200": "السعر-SMA200",
    # Price vs SMA (%)
    "price_vs_sma_10_percent": "% vs SMA10","price_vs_sma_20_percent": "% vs SMA20",
    "price_vs_sma_50_percent": "% vs SMA50","price_vs_sma_100_percent": "% vs SMA100",
    "price_vs_sma_150_percent": "% vs SMA150","price_vs_sma_200_percent": "% vs SMA200",
    "percent_off_52w_high": "% بُعد عن أعلى 52 أسبوع",
    "percent_off_52w_low":  "% بُعد عن أدنى 52 أسبوع",
    "vol_diff_50_percent": "% الحجم مقابل متوسط 50 يوم",
    # % Changes
    "percent_change_15d": "% تغيّر 15 يوم","percent_change_20d": "% تغيّر 20 يوم",
    "percent_change_63d": "% تغيّر 63 يوم","percent_change_126d": "% تغيّر 126 يوم",
    # RSI Daily
    "rsi_14": "RSI14 يومي","rsi_3": "RSI3 يومي",
    "sma9_rsi": "SMA9(RSI14)","wma45_rsi": "WMA45(RSI14)","ema45_rsi": "EMA45(RSI14)",
    "sma3_rsi3": "SMA3(RSI3)","ema20_sma3": "EMA20(SMA3(RSI3))",
    # RSI Weekly
    "rsi_w": "RSI14 أسبوعي","rsi_3_w": "RSI3 أسبوعي",
    "sma9_rsi_w": "SMA9(RSI14) أسبوعي","wma45_rsi_w": "WMA45(RSI14) أسبوعي",
    "ema45_rsi_w": "EMA45(RSI14) أسبوعي","sma3_rsi3_w": "SMA3(RSI3) أسبوعي",
    "ema20_sma3_w": "EMA20(SMA3(RSI3)) أسبوعي",
    # CFG Daily
    "cfg_daily": "CFG يومي","cfg_sma4": "SMA4(CFG)","cfg_sma9": "SMA9(CFG)",
    "cfg_sma20": "SMA20(CFG)","cfg_ema20": "EMA20(CFG)","cfg_ema45": "EMA45(CFG)",
    "cfg_wma45": "WMA45(CFG)","rsi_14_minus_9": "RSI14-RSI14[9]",
    # CFG Weekly
    "cfg_w": "CFG أسبوعي","cfg_sma4_w": "SMA4(CFG) أسبوعي","cfg_sma9_w": "SMA9(CFG) أسبوعي",
    "cfg_ema20_w": "EMA20(CFG) أسبوعي","cfg_ema45_w": "EMA45(CFG) أسبوعي",
    "cfg_wma45_w": "WMA45(CFG) أسبوعي","rsi_14_minus_9_w": "RSI14-RSI14[9] أسبوعي",
    # STAMP Daily
    "stamp_a_value": "STAMP A يومي","stamp_s9rsi": "STAMP S9rsi","stamp_e45cfg": "STAMP E45cfg",
    "stamp_e45rsi": "STAMP E45rsi","stamp_e20sma3": "STAMP E20sma3",
    # STAMP Weekly
    "stamp_a_value_w": "STAMP A أسبوعي","stamp_s9rsi_w": "STAMP S9rsi أسبوعي",
    "stamp_e45cfg_w": "STAMP E45cfg أسبوعي","stamp_e45rsi_w": "STAMP E45rsi أسبوعي",
    "stamp_e20sma3_w": "STAMP E20sma3 أسبوعي",
    # The Number
    "the_number": "The Number","the_number_hl": "The Number Upper","the_number_ll": "The Number Lower",
    "high_sma13": "SMA13(High)","low_sma13": "SMA13(Low)",
    "high_sma65": "SMA65(High)","low_sma65": "SMA65(Low)",
    "the_number_w": "The Number أسبوعي","the_number_hl_w": "The Number Upper أسبوعي",
    "the_number_ll_w": "The Number Lower أسبوعي","high_sma13_w": "SMA13(High) أسبوعي",
    "low_sma13_w": "SMA13(Low) أسبوعي","high_sma65_w": "SMA65(High) أسبوعي",
    "low_sma65_w": "SMA65(Low) أسبوعي",
    # CCI
    "cci": "CCI يومي","cci_ema20": "EMA20(CCI) يومي",
    "cci_w": "CCI أسبوعي","cci_ema20_w": "EMA20(CCI) أسبوعي",
    # Aroon
    "aroon_up": "Aroon Up","aroon_down": "Aroon Down",
    "aroon_up_w": "Aroon Up أسبوعي","aroon_down_w": "Aroon Down أسبوعي",
    # Beta
    "beta": "Beta",
    # ─── Conditions (Boolean) ────────────────────────────────────────────
    "price_gt_sma18": "السعر > SMA18",
    "price_gt_sma9_weekly": "السعر > SMA9 أسبوعي",
    "sma_trend_daily": "ترتيب المتوسطات تصاعدي يومي",
    "sma_trend_weekly": "ترتيب المتوسطات تصاعدي أسبوعي",
    "cci_gt_100": "CCI > 100",
    "cci_ema20_gt_0_daily": "EMA20(CCI) > 0 يومي",
    "cci_ema20_gt_0_weekly": "EMA20(CCI) > 0 أسبوعي",
    "ema10_gt_sma50": "EMA10 > SMA50","ema10_gt_sma200": "EMA10 > SMA200",
    "ema21_gt_sma50": "EMA21 > SMA50","ema21_gt_sma200": "EMA21 > SMA200",
    "sma50_gt_sma150": "SMA50 > SMA150","sma50_gt_sma200": "SMA50 > SMA200",
    "sma150_gt_sma200": "SMA150 > SMA200",
    "sma200_gt_sma200_1m_ago": "SMA200↑ (قبل شهر)","sma200_gt_sma200_2m_ago": "SMA200↑ (شهرين)",
    "sma200_gt_sma200_3m_ago": "SMA200↑ (3 أشهر)","sma200_gt_sma200_4m_ago": "SMA200↑ (4 أشهر)",
    "sma200_gt_sma200_5m_ago": "SMA200↑ (5 أشهر)",
    "aroon_up_gt_70": "Aroon Up > 70","aroon_down_lt_30": "Aroon Down < 30",
    "cfg_gt_50_daily": "CFG > 50 يومي","cfg_ema45_gt_50": "EMA45(CFG) > 50 يومي",
    "cfg_ema20_gt_50": "EMA20(CFG) > 50 يومي","cfg_gt_50_w": "CFG > 50 أسبوعي",
    "cfg_ema45_gt_50_w": "EMA45(CFG) > 50 أسبوعي","cfg_ema20_gt_50_w": "EMA20(CFG) > 50 أسبوعي",
    "sma9_gt_tn_daily": "SMA9 > The Number يومي","sma9_gt_tn_weekly": "SMA9 > The Number أسبوعي",
    "rsi_lt_80_d": "RSI < 80 يومي","rsi_lt_80_w": "RSI < 80 أسبوعي",
    "sma9_rsi_lte_75_d": "SMA9(RSI) ≤ 75 يومي","sma9_rsi_lte_75_w": "SMA9(RSI) ≤ 75 أسبوعي",
    "ema45_rsi_lte_70_d": "EMA45(RSI) ≤ 70 يومي","ema45_rsi_lte_70_w": "EMA45(RSI) ≤ 70 أسبوعي",
    "rsi_55_70": "RSI بين 55-70",
    "rsi_gt_wma45_d": "RSI > WMA45(RSI) يومي","rsi_gt_wma45_w": "RSI > WMA45(RSI) أسبوعي",
    "sma9rsi_gt_wma45rsi_d": "SMA9(RSI)>WMA45(RSI) يومي",
    "sma9rsi_gt_wma45rsi_w": "SMA9(RSI)>WMA45(RSI) أسبوعي",
    "stamp_daily": "إشارة STAMP يومي","stamp_weekly": "إشارة STAMP أسبوعي",
    "stamp": "إشارة STAMP النهائية",
    "trend_signal": "إشارة Trend الكاملة",
    "final_signal": "الإشارة النهائية الجامعة",
    "score": "النقاط الإجمالية",
    "is_etf_or_index": "ETF أو مؤشر؟",
    "has_gap": "يوجد Gap؟",
}

EXPORT_COLUMNS = list(COLUMNS_INFO.keys())


def parse_args():
    p = argparse.ArgumentParser(description="Export TASI stock technical indicators to CSV")
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--from",   dest="from_date", type=str, default=None)
    p.add_argument("--to",     dest="to_date",   type=str, default=None)
    p.add_argument("--split-by-symbol", action="store_true")
    p.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR))
    p.add_argument("--columns", type=str, default=None)
    p.add_argument("--list-columns", action="store_true")
    return p.parse_args()


def list_all_columns():
    print("\n" + "="*68)
    print("  البيانات الفنية المتاحة في stock_indicators")
    print("="*68)
    for col, desc in COLUMNS_INFO.items():
        print(f"  {col:<38} -> {desc}")
    print("="*68)
    print(f"  المجموع: {len(COLUMNS_INFO)} عمود")
    print("="*68 + "\n")


def fetch_data(db, symbol=None, from_date=None, to_date=None):
    q = db.query(StockIndicator)
    if symbol:
        q = q.filter(StockIndicator.symbol == symbol)
    if from_date:
        q = q.filter(StockIndicator.date >= from_date)
    if to_date:
        q = q.filter(StockIndicator.date <= to_date)
    q = q.order_by(StockIndicator.symbol, StockIndicator.date)
    return q.all()


def rows_to_df(rows, columns):
    data = []
    for r in rows:
        row_dict = {}
        for col in columns:
            val = getattr(r, col, None)
            if val is not None and hasattr(val, "__float__"):
                val = float(val)
            row_dict[col] = val
        data.append(row_dict)
    df = pd.DataFrame(data, columns=columns)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def main():
    args = parse_args()

    if args.list_columns:
        list_all_columns()
        return

    columns = EXPORT_COLUMNS
    if args.columns:
        columns = [c.strip() for c in args.columns.split(",")]
        if "symbol" not in columns:
            columns.insert(0, "symbol")
        if "date" not in columns:
            columns.insert(1, "date")

    from_date = date.fromisoformat(args.from_date) if args.from_date else None
    to_date   = date.fromisoformat(args.to_date)   if args.to_date   else None

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nجاري سحب البيانات...")
    if args.symbol:
        print(f"  السهم: {args.symbol}")
    if from_date:
        print(f"  من: {from_date}")
    if to_date:
        print(f"  الى: {to_date}")

    db = SessionLocal()
    try:
        rows = fetch_data(db, symbol=args.symbol, from_date=from_date, to_date=to_date)
        if not rows:
            print("لا توجد بيانات.")
            return

        print(f"  تم جلب {len(rows):,} صف. جاري التحويل...")
        df = rows_to_df(rows, columns)

        if args.split_by_symbol:
            for sym, gdf in df.groupby("symbol"):
                p = out_dir / f"TASI_{sym}.csv"
                gdf.to_csv(p, index=False, encoding="utf-8-sig")
            print(f"\nتم: {df['symbol'].nunique()} ملف في {out_dir}")
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"TASI_indicators_{args.symbol or 'ALL'}_{ts}.csv"
            p = out_dir / fname
            df.to_csv(p, index=False, encoding="utf-8-sig")
            print(f"\nتم: {len(df):,} صف | {df['symbol'].nunique()} سهم")
            print(f"الملف: {p}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
