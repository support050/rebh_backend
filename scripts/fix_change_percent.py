"""
إسكربت إصلاح شامل لحقلي change و change_percent في جدول prices
────────────────────────────────────────────────────────────────
المعادلة الصحيحة المعتمدة:
  change         = close_today - close_prev_session
  change_percent = ((close_today / close_prev_session) - 1) * 100

المنطق:
  - يُحدّث فقط الصفوف التي بها فارق > 0.01% (لتفادي UPDATE غير ضروري)
  - يترك أول جلسة تداول لكل سهم كما هي (لا يوجد إغلاق سابق لها)
  - يستخدم UPDATE واحد ضخم عبر CTE بدلاً من loop بـ Python (أسرع بكثير)
  - يطبع تقرير إجمالي قبل وبعد التصحيح

التشغيل:
  python scripts/fix_change_percent.py
  python scripts/fix_change_percent.py --dry-run   ← للمعاينة فقط بدون تعديل
"""

import sys
import time
import argparse
from pathlib import Path
from sqlalchemy import text

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine


def count_errors(conn, threshold=0.01):
    """إحصاء عدد الصفوف التي تحتاج تصحيح (مستثنياً close=0 وprev_close=0)."""
    q = text("""
    WITH lag_data AS (
        SELECT 
            id,
            close,
            change_percent,
            LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
        FROM prices
    )
    SELECT COUNT(*) AS cnt
    FROM lag_data
    WHERE 
        prev_close IS NOT NULL
        AND prev_close > 0
        AND close > 0
        AND ABS(change_percent - ROUND(((close / prev_close) - 1.0) * 100.0, 4)) > :threshold
    """)
    return conn.execute(q, {"threshold": threshold}).scalar()


def run_fix(dry_run=False, threshold=0.01):
    print("=" * 75)
    print("  🔧 إصلاح حقلي change و change_percent في جدول prices")
    print("=" * 75)
    print(f"  الوضع: {'🟡 معاينة فقط (DRY RUN) — لن يتم أي تعديل' if dry_run else '🟢 تصحيح فعلي'}")
    print(f"  فارق الخطأ المعتبر للتصحيح: > {threshold}%")
    print("=" * 75)

    start = time.time()

    with engine.connect() as conn:
        # ── 0. فحص وجود تكرار (symbol, date) قبل أي شيء ───────────────────
        print("\n🔍 فحص وجود تكرار في (symbol, date)...")
        dup_q = text("""
            SELECT symbol, date, COUNT(*) AS cnt
            FROM prices
            GROUP BY symbol, date
            HAVING COUNT(*) > 1
            LIMIT 10
        """)
        dups = conn.execute(dup_q).mappings().fetchall()
        if dups:
            print(f"  ⚠️ تحذير: يوجد {len(dups)} مجموعة مكررة! عينة:")
            for d in dups:
                print(f"     symbol={d['symbol']} date={d['date']} count={d['cnt']}")
            print("  ← LAG() قد يُعطي نتائج غير دقيقة. يُنصح بإزالة التكرار أولاً.")
            if not dry_run:
                answer = input("\n  هل تريد المتابعة رغم ذلك؟ (y/n): ").strip().lower()
                if answer != 'y':
                    print("  تم الإلغاء.")
                    return
        else:
            print("  ✅ لا يوجد تكرار — (symbol, date) فريد في الجدول.")
        print("\n📊 جاري حساب عدد الصفوف التي تحتاج تصحيح...")
        before_count = count_errors(conn, threshold)
        print(f"  ← يوجد {before_count:,} صف يحتاج تصحيح من إجمالي ~985,000 جلسة")

        if before_count == 0:
            print("\n✅ لا توجد أخطاء! قاعدة البيانات سليمة.")
            return

        if dry_run:
            print("\n🟡 DRY RUN: تم الفحص بنجاح. لتطبيق التصحيح أعد التشغيل بدون --dry-run")
            print(f"\n  ← الصفوف التي ستُصحح: {before_count:,}")

            # عرض أمثلة على ما سيتغير
            sample_q = text("""
            WITH lag_data AS (
                SELECT 
                    id,
                    symbol,
                    date,
                    close,
                    change,
                    change_percent,
                    LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
                FROM prices
            )
            SELECT 
                symbol,
                date,
                close,
                prev_close,
                change AS stored_change,
                ROUND(close - prev_close, 4) AS correct_change,
                change_percent AS stored_pct,
                ROUND(((close / prev_close) - 1.0) * 100.0, 4) AS correct_pct,
                ROUND(ABS(change_percent - ROUND(((close / prev_close) - 1.0) * 100.0, 4)), 4) AS diff
            FROM lag_data
            WHERE 
                prev_close IS NOT NULL AND prev_close > 0 AND close > 0
                AND ABS(change_percent - ROUND(((close / prev_close) - 1.0) * 100.0, 4)) > :threshold
            ORDER BY date DESC, diff DESC
            LIMIT 20
            """)
            samples = conn.execute(sample_q, {"threshold": threshold}).mappings().fetchall()

            print(f"\n  عينة من أول 20 سجل ستُصحح:")
            print(f"  {'الرمز':<6} | {'التاريخ':<10} | {'مخزن %':<9} | {'صحيح %':<9} | {'فارق':<8}")
            print("  " + "-" * 55)
            for s in samples:
                print(f"  {s['symbol']:<6} | {str(s['date']):<10} | {float(s['stored_pct']):<9.2f} | {float(s['correct_pct']):<9.2f} | {float(s['diff']):<8.4f}")
            return

        # ── 2. تنفيذ UPDATE الشامل بـ CTE واحدة ──────────────────────────
        print("\n⚡ جاري تنفيذ UPDATE الشامل في قاعدة البيانات...")
        print("   (عملية واحدة ضخمة — أكثر كفاءة من loop سطر بسطر)")

        fix_query = text("""
        WITH lag_data AS (
            SELECT 
                id,
                close,
                LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
            FROM prices
        ),
        corrections AS (
            SELECT 
                id,
                ROUND(close - prev_close, 4) AS correct_change,
                ROUND(((close / prev_close) - 1.0) * 100.0, 4) AS correct_pct
            FROM lag_data
            WHERE 
                prev_close IS NOT NULL AND prev_close > 0 AND close > 0
        )
        UPDATE prices p
        SET 
            change         = c.correct_change,
            change_percent = c.correct_pct
        FROM corrections c
        WHERE 
            p.id = c.id
            AND ABS(p.change_percent - c.correct_pct) > :threshold
        """)

        try:
            result = conn.execute(fix_query, {"threshold": threshold})
            updated_rows = result.rowcount
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"\n❌ فشل التحديث! تم Rollback كامل — لم تتأثر أي بيانات.")
            print(f"   السبب: {e}")
            raise

        elapsed = round(time.time() - start, 2)

        # ── 3. التحقق بعد التصحيح ─────────────────────────────────────────
        print(f"\n🔍 التحقق من النتائج بعد التصحيح...")
        after_count = count_errors(conn, threshold)

        print("\n" + "=" * 75)
        print("  ✅ تم الإصلاح بنجاح!")
        print("=" * 75)
        print(f"  • الصفوف التي تم تحديثها:   {updated_rows:,}")
        print(f"  • أخطاء متبقية بعد الإصلاح: {after_count:,}  ({'✅ صفر' if after_count == 0 else '⚠️ تحتاج مراجعة'})")
        print(f"  • الوقت المستغرق:            {elapsed} ثانية")
        print("=" * 75)

        if after_count > 0:
            print(f"\n⚠️  يوجد {after_count:,} صف لم يُصحح.")
            print("   الأسباب المحتملة:")
            print("   - صفوف بها close=NULL أو prev_close=NULL")
            print("   - أخطاء في البيانات الأصلية تستلزم مراجعة يدوية")


def main():
    p = argparse.ArgumentParser(
        description="إصلاح حقلي change و change_percent في جدول prices"
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="معاينة الأخطاء فقط بدون تعديل قاعدة البيانات"
    )
    p.add_argument(
        "--threshold", type=float, default=0.01,
        help="الحد الأدنى للفارق الذي يستوجب التصحيح (افتراضي 0.01%%)"
    )
    args = p.parse_args()

    run_fix(dry_run=args.dry_run, threshold=args.threshold)


if __name__ == "__main__":
    main()
