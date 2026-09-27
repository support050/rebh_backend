"""
فحص دقة حساب change_percent في جدول prices
يقارن القيمة المخزنة مع: ((close_today / close_prev) - 1) * 100
"""

import sys
import os
from pathlib import Path
import pandas as pd
from sqlalchemy import text

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine

def run_audit(threshold_pct=0.05, limit_samples=30):
    print("=" * 80)
    print("🔍 بدء فحص جدول prices لاكتشاف الأخطاء في change_percent...")
    print(f"المعادلة المعتمدة: ((close_today / close_prev_session) - 1) * 100")
    print(f"فارق الخطأ المعتبر: أكثر من {threshold_pct}%")
    print("=" * 80)

    # استعلام SQL يفحص الفروقات مستثنياً إغلاق 0 أو عدم التداول
    query = f"""
    WITH ranked_prices AS (
        SELECT 
            id,
            symbol,
            company_name,
            date,
            close,
            change,
            change_percent,
            LAG(close) OVER (PARTITION BY symbol ORDER BY date ASC) AS prev_close,
            LAG(date) OVER (PARTITION BY symbol ORDER BY date ASC) AS prev_date
        FROM prices
    ),
    diff_analysis AS (
        SELECT 
            id,
            symbol,
            company_name,
            date,
            prev_date,
            close,
            prev_close,
            change,
            ROUND(close - prev_close, 4) AS expected_change,
            change_percent,
            ROUND(((close / prev_close) - 1.0) * 100.0, 4) AS expected_change_percent,
            ROUND(ABS(change_percent - (((close / prev_close) - 1.0) * 100.0)), 4) AS abs_diff
        FROM ranked_prices
        WHERE prev_close IS NOT NULL AND prev_close > 0 AND close > 0
    )
    SELECT 
        id,
        symbol,
        company_name,
        date,
        prev_date,
        close,
        prev_close,
        change,
        expected_change,
        change_percent,
        expected_change_percent,
        abs_diff
    FROM diff_analysis
    WHERE abs_diff > :threshold
    ORDER BY date DESC, abs_diff DESC
    """

    # استعلام عام للإحصاء
    stats_query = f"""
    WITH ranked_prices AS (
        SELECT 
            symbol,
            date,
            close,
            change_percent,
            LAG(close) OVER (PARTITION BY symbol ORDER BY date ASC) AS prev_close
        FROM prices
    ),
    diff_analysis AS (
        SELECT 
            ABS(change_percent - (((close / prev_close) - 1.0) * 100.0)) AS abs_diff
        FROM ranked_prices
        WHERE prev_close IS NOT NULL AND prev_close > 0
    )
    SELECT 
        COUNT(*) AS total_checked,
        COUNT(CASE WHEN abs_diff > {threshold_pct} THEN 1 END) AS count_errors,
        COUNT(CASE WHEN abs_diff > 1.0 THEN 1 END) AS count_major_errors,
        MAX(abs_diff) AS max_error
    FROM diff_analysis
    """

    with engine.connect() as conn:
        print("📊 جاري حساب الإحصائيات الشاملة على كل الصفوف...")
        stats = conn.execute(text(stats_query)).mappings().first()
        total_checked = stats["total_checked"]
        count_errors = stats["count_errors"]
        count_major_errors = stats["count_major_errors"]
        max_error = stats["max_error"]

        print(f"\n📌 نتائج الإحصاء العام:")
        print(f" • إجمالي الجلسات المفحوصة (بعد استثناء أول جلسة لكل سهم): {total_checked:,}")
        print(f" • عدد الصفوف التي بها فارق > {threshold_pct}%: {count_errors:,} ({(count_errors/total_checked)*100:.2f}%)")
        print(f" • عدد الصفوف التي بها فارق كبير (> 1.0%): {count_major_errors:,}")
        print(f" • أقصى فارق مسجل: {max_error}")

        print(f"\n📋 عينة من أكبر الحالات الخاطئة (أول {limit_samples} سجل):")
        print("-" * 115)
        print(f"{'الرمز':<6} | {'التاريخ':<10} | {'إغلاق أمس':<10} | {'إغلاق اليوم':<11} | {'المخزن %':<9} | {'المفترض %':<10} | {'الفارق':<8} | {'ملاحظات'}")
        print("-" * 115)

        samples = conn.execute(text(query), {"threshold": threshold_pct}).mappings().fetchmany(limit_samples)
        for s in samples:
            sym = s["symbol"]
            dt = str(s["date"])
            prev_c = float(s["prev_close"])
            curr_c = float(s["close"])
            stored_pct = float(s["change_percent"]) if s["change_percent"] is not None else 0.0
            expected_pct = float(s["expected_change_percent"])
            diff = float(s["abs_diff"])

            note = ""
            if abs(expected_pct) > 50 and abs(stored_pct) < 15:
                note = "احتمال تجزئة أسهم أو تعديل تاريخي"
            elif (stored_pct > 0 and expected_pct < 0) or (stored_pct < 0 and expected_pct > 0):
                note = "عكس الإشارة (+ / -)"
            elif abs(stored_pct) == 0:
                note = "المخزن صفر مع وجود تغير"

            print(f"{sym:<6} | {dt:<10} | {prev_c:<10.2f} | {curr_c:<11.2f} | {stored_pct:<9.2f} | {expected_pct:<10.2f} | {diff:<8.2f} | {note}")

    print("=" * 115)

if __name__ == "__main__":
    run_audit()
