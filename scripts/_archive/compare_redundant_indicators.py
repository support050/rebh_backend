import sys
import os
from sqlalchemy import text
# from tabulate import tabulate # تم إزالتها لتجنب مشاكل المكتبات المفقودة

# إضافة مسار المشروع
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.database import SessionLocal

def compare_data(symbol: str):
    db = SessionLocal()
    try:
        # 1. الحصول على أحدث تاريخ متاح لهذا السهم
        latest_date_query = text("SELECT MAX(date) FROM prices WHERE symbol = :symbol")
        target_date = db.execute(latest_date_query, {"symbol": symbol}).scalar()

        if not target_date:
            print(f"❌ لم يتم العثور على بيانات للسهم {symbol}")
            return

        print(f"📅 مقارنة البيانات للسهم {symbol} بتاريخ {target_date}")
        print("-" * 60)

        # 2. جلب البيانات من جدول prices
        p_query = text("""
            SELECT 
                ema_10, ema_21, sma_50, sma_150, sma_200,
                sma_4, sma_9, sma_18,
                sma_4w, sma_9w, sma_18w,
                aroon_up, aroon_down, cci_14, cci_ema_20
            FROM prices 
            WHERE symbol = :symbol AND date = :target_date
        """)
        p_data = db.execute(p_query, {"symbol": symbol, "target_date": target_date}).fetchone()

        # 3. جلب البيانات من جدول stock_indicators
        si_query = text("""
            SELECT 
                ema10, ema21,
                sma4, sma9, sma18,
                sma4_w, sma9_w, sma18_w,
                aroon_up, aroon_down, cci, cci_ema20
            FROM stock_indicators 
            WHERE symbol = :symbol AND date = :target_date
        """)
        si_data = db.execute(si_query, {"symbol": symbol, "target_date": target_date}).fetchone()

        if not p_data or not si_data:
            print("⚠️ بيانات ناقصة في أحد الجدولين لهذا التاريخ.")
            return

        # 4. تجهيز جدول المقارنة
        comparison = [
            ["Indicator", "Table: prices", "Table: indicators", "Difference"],
            ["EMA 10", p_data[0], si_data[0], round(abs((p_data[0] or 0) - (si_data[0] or 0)), 4)],
            ["EMA 21", p_data[1], si_data[1], round(abs((p_data[1] or 0) - (si_data[1] or 0)), 4)],
            # SMA 50/150/200 غير موجودة في جدول indicators كأرقام، لذا سنقارن البقية
            ["SMA 4 (Daily)", p_data[5], si_data[2], round(abs((p_data[5] or 0) - (si_data[2] or 0)), 4)],
            ["SMA 9 (Daily)", p_data[6], si_data[3], round(abs((p_data[6] or 0) - (si_data[3] or 0)), 4)],
            ["SMA 18 (Daily)", p_data[7], si_data[4], round(abs((p_data[7] or 0) - (si_data[4] or 0)), 4)],
            ["SMA 4 (Weekly)", p_data[8], si_data[5], round(abs((p_data[8] or 0) - (si_data[5] or 0)), 4)],
            ["SMA 9 (Weekly)", p_data[9], si_data[6], round(abs((p_data[9] or 0) - (si_data[6] or 0)), 4)],
            ["SMA 18 (Weekly)", p_data[10], si_data[7], round(abs((p_data[10] or 0) - (si_data[7] or 0)), 4)],
            ["Aroon Up", p_data[11], si_data[8], round(abs((p_data[11] or 0) - (si_data[8] or 0)), 4)],
            ["Aroon Down", p_data[12], si_data[9], round(abs((p_data[12] or 0) - (si_data[9] or 0)), 4)],
            ["CCI (14)", p_data[13], si_data[10], round(abs((p_data[13] or 0) - (si_data[10] or 0)), 4)],
            ["CCI EMA 20", p_data[14], si_data[11], round(abs((p_data[14] or 0) - (si_data[11] or 0)), 4)],
        ]

        # طباعة النتائج بشكل منظم
        for row in comparison:
            print(f"{row[0]:<20} | {str(row[1]):<15} | {str(row[2]):<18} | {str(row[3])}")

    except Exception as e:
        print(f"❌ خطأ: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    # يمكنك تغيير الرمز هنا لأي سهم تريد اختباره
    symbol_to_test = "1321" # الراجحي كمثال
    compare_data(symbol_to_test)
