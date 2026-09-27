"""
إسكربت احترافي لإعادة حساب change و change_percent لكل الأسهم وكل الأيام
═══════════════════════════════════════════════════════════════════════════

المعادلة المعتمدة:
  change         = close[t] - close[t-1]
  change_percent = ((close[t] / close[t-1]) - 1) × 100

المميزات:
  ✓ يعالج بالدُفعات (batches) لتجنب ضغط الذاكرة على DB كبيرة
  ✓ تقرير تفصيلي قبل وبعد التصحيح
  ✓ تصدير التقرير كـ CSV (اختياري)
  ✓ وضع معاينة (dry-run)
  ✓ فلترة بالرمز أو نطاق تواريخ
  ✓ حماية من التكرار و close=0
  ✓ Rollback تلقائي عند الفشل
  ✓ ملخص إحصائي شامل

التشغيل:
  python scripts/recalc_change.py                         # إعادة حساب الكل
  python scripts/recalc_change.py --dry-run               # معاينة فقط
  python scripts/recalc_change.py --symbol 1010           # سهم محدد
  python scripts/recalc_change.py --from 2025-01-01       # من تاريخ
  python scripts/recalc_change.py --export report.csv     # تصدير تقرير
"""

import sys
import time
import argparse
import csv as csv_module
from pathlib import Path
from datetime import datetime
from sqlalchemy import text, bindparam

# ضمان دعم الترميز utf-8 على Windows terminal
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine

# ─── ثوابت ────────────────────────────────────────────────────────────────
BATCH_SIZE = 50  # عدد الرموز في الدفعة الواحدة
# ✅ تم رفعه من 0.005 إلى 0.01 بعد التحقق العملي (verify_change_percent.py):
# فروقات التقريب الطبيعية بتوصل لحد 0.0049%، فـ threshold أقل من 0.01
# كان هيصنّف آلاف الصفوف الصحيحة كـ "أخطاء" ويحدّثها من غير داعي.
DIFF_THRESHOLD = 0.01


def get_all_symbols(conn, symbol_filter=None):
    """جلب كل الرموز الفريدة من الجدول."""
    if symbol_filter:
        return [symbol_filter]
    rows = conn.execute(text("SELECT DISTINCT symbol FROM prices ORDER BY symbol")).fetchall()
    return [str(r[0]) for r in rows]


def get_total_rows(conn, date_from=None, date_to=None, symbol=None):
    """إحصاء عدد الصفوف الإجمالي."""
    where = ["1=1"]
    params = {}
    if date_from:
        where.append("date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("date <= :date_to")
        params["date_to"] = date_to
    if symbol:
        where.append("symbol = :symbol")
        params["symbol"] = symbol

    q = text(f"SELECT COUNT(*) FROM prices WHERE {' AND '.join(where)}")
    return conn.execute(q, params).scalar()


def analyze_batch(conn, symbols, date_from=None, date_to=None):
    """
    تحليل دفعة من الرموز: حساب الفارق بين المخزن والصحيح.
    يُرجع قائمة بالصفوف التي تحتاج تصحيح.
    """
    date_filter = ""
    params = {"symbols": tuple(symbols)}
    if date_from:
        date_filter += " AND date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        date_filter += " AND date <= :date_to"
        params["date_to"] = date_to

    # ✅ إصلاح: استخدام bindparam صريح مع expanding=True
    # عشان IN :symbols يتوسّع صح لقائمة الرموز بدل ما يتعامل معاها
    # كـ قيمة واحدة (tuple) وده كان ممكن يفشل أو يرجع نتائج غلط
    # حسب إصدار SQLAlchemy المستخدم.
    q = text(f"""
    WITH lag_data AS (
        SELECT
            id, symbol, date, close,
            change, change_percent,
            LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close,
            LAG(date)  OVER (PARTITION BY symbol ORDER BY date) AS prev_date
        FROM prices
        WHERE symbol IN :symbols
    )
    SELECT
        id, symbol, date, close, prev_close, prev_date,
        change         AS old_change,
        change_percent AS old_pct,
        ROUND(close - prev_close, 4)                         AS new_change,
        ROUND(((close / prev_close) - 1.0) * 100.0, 4)      AS new_pct,
        ROUND(ABS(
            COALESCE(change_percent, 0) -
            ROUND(((close / prev_close) - 1.0) * 100.0, 4)
        ), 6)                                                AS diff
    FROM lag_data
    WHERE
        prev_close IS NOT NULL
        AND prev_close > 0
        AND close > 0
        {date_filter}
    ORDER BY symbol, date
    """).bindparams(bindparam("symbols", expanding=True))

    return conn.execute(q, params).mappings().fetchall()


def update_batch(conn, rows_to_fix):
    """تحديث دفعة من الصفوف."""
    if not rows_to_fix:
        return 0

    update_data = [
        {"id": int(r["id"]), "change": float(r["new_change"]), "pct": float(r["new_pct"])}
        for r in rows_to_fix
    ]

    conn.execute(
        text("UPDATE prices SET change = :change, change_percent = :pct WHERE id = :id"),
        update_data
    )
    return len(update_data)


def format_duration(seconds):
    """تحويل الثواني لصيغة مقروءة."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def run_recalc(dry_run=False, symbol=None, date_from=None, date_to=None,
               export_path=None, threshold=DIFF_THRESHOLD):
    """المنطق الرئيسي."""

    print()
    print("╔" + "═" * 73 + "╗")
    print("║  🔧  إعادة حساب change و change_percent لجدول prices" + " " * 20 + "║")
    print("╠" + "═" * 73 + "╣")
    mode = "🟡 معاينة فقط (DRY RUN)" if dry_run else "🟢 تصحيح فعلي"
    print(f"║  الوضع: {mode:<62}║")
    if symbol:
        print(f"║  الرمز: {symbol:<64}║")
    if date_from:
        print(f"║  من تاريخ: {date_from:<61}║")
    if date_to:
        print(f"║  إلى تاريخ: {date_to:<60}║")
    print(f"║  فارق الخطأ المعتبر: > {threshold}%{'':<48}║")
    print("╚" + "═" * 73 + "╝")

    start_time = time.time()

    # إحصائيات عامة
    stats = {
        "total_rows_scanned": 0,
        "rows_with_errors": 0,
        "rows_updated": 0,
        "rows_sign_flipped": 0,
        "rows_zero_stored": 0,
        "max_diff": 0.0,
        "max_diff_row": None,
        "symbols_with_errors": set(),
        "error_details": [],  # للتصدير
    }

    with engine.connect() as conn:
        # ── 0. فحص التكرار ───────────────────────────────────────────────
        print("\n🔍 فحص التكرار في (symbol, date)...")
        dup_count = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT symbol, date FROM prices
                GROUP BY symbol, date HAVING COUNT(*) > 1
            ) sub
        """)).scalar()
        if dup_count > 0:
            print(f"  ⚠️  يوجد {dup_count} مجموعة مكررة! يُنصح بتنظيفها أولاً.")
            if not dry_run:
                answer = input("  هل تريد المتابعة؟ (y/n): ").strip().lower()
                if answer != "y":
                    print("  تم الإلغاء.")
                    return
        else:
            print("  ✅ لا يوجد تكرار.")

        # ── 1. جلب الرموز ────────────────────────────────────────────────
        symbols = get_all_symbols(conn, symbol)
        total_symbols = len(symbols)
        total_rows = get_total_rows(conn, date_from, date_to, symbol)
        print(f"\n📊 الرموز: {total_symbols:,} | الصفوف الإجمالية: {total_rows:,}")

        # ── 2. المعالجة بالدفعات ──────────────────────────────────────────
        print(f"\n⚡ المعالجة بالدفعات ({BATCH_SIZE} رمز/دفعة)...\n")

        batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
        processed_symbols = 0

        for batch_idx, batch_symbols in enumerate(batches, 1):
            batch_start = time.time()
            rows = analyze_batch(conn, batch_symbols, date_from, date_to)
            stats["total_rows_scanned"] += len(rows)

            # فرز الأخطاء
            errors_in_batch = []
            for r in rows:
                diff = float(r["diff"])
                old_pct = float(r["old_pct"]) if r["old_pct"] is not None else 0.0
                new_pct = float(r["new_pct"])

                if diff > threshold:
                    errors_in_batch.append(r)
                    stats["rows_with_errors"] += 1
                    stats["symbols_with_errors"].add(r["symbol"])

                    # تصنيف
                    if old_pct != 0 and new_pct != 0 and (old_pct > 0) != (new_pct > 0):
                        stats["rows_sign_flipped"] += 1
                    if old_pct == 0 and new_pct != 0:
                        stats["rows_zero_stored"] += 1

                    if diff > stats["max_diff"]:
                        stats["max_diff"] = diff
                        stats["max_diff_row"] = r

                    # للتصدير
                    stats["error_details"].append({
                        "symbol": r["symbol"],
                        "date": str(r["date"]),
                        "prev_close": float(r["prev_close"]),
                        "close": float(r["close"]),
                        "old_change": float(r["old_change"]) if r["old_change"] else 0,
                        "new_change": float(r["new_change"]),
                        "old_pct": old_pct,
                        "new_pct": new_pct,
                        "diff": diff,
                    })

            # تحديث الدفعة
            if errors_in_batch and not dry_run:
                try:
                    update_batch(conn, errors_in_batch)
                    conn.commit()
                    stats["rows_updated"] += len(errors_in_batch)
                except Exception as e:
                    conn.rollback()
                    print(f"\n  ❌ فشل تحديث الدفعة {batch_idx}: {e}")
                    raise

            processed_symbols += len(batch_symbols)
            batch_time = time.time() - batch_start
            pct_done = (processed_symbols / total_symbols) * 100

            # Progress bar
            bar_width = 30
            filled = int(bar_width * processed_symbols / total_symbols)
            bar = "█" * filled + "░" * (bar_width - filled)
            errors_str = f" | أخطاء: {len(errors_in_batch)}" if errors_in_batch else ""
            print(
                f"  [{bar}] {pct_done:5.1f}%  "
                f"دفعة {batch_idx}/{len(batches)} "
                f"({len(batch_symbols)} رمز, {len(rows)} صف, {batch_time:.1f}s)"
                f"{errors_str}"
            )

        elapsed = time.time() - start_time

        # ── 3. التحقق بعد التصحيح ────────────────────────────────────────
        remaining_errors = 0
        if not dry_run and stats["rows_updated"] > 0:
            print("\n🔍 التحقق بعد التصحيح...")
            for batch_symbols in batches:
                rows = analyze_batch(conn, batch_symbols, date_from, date_to)
                remaining_errors += sum(1 for r in rows if float(r["diff"]) > threshold)

        # ── 4. التقرير الشامل ─────────────────────────────────────────────
        print()
        print("╔" + "═" * 73 + "╗")
        print("║  📋  التقرير الشامل" + " " * 53 + "║")
        print("╠" + "═" * 73 + "╣")
        print(f"║  الصفوف المفحوصة:           {stats['total_rows_scanned']:>10,}" + " " * 27 + "║")
        print(f"║  الصفوف الخاطئة (> {threshold}%):   {stats['rows_with_errors']:>10,}" + " " * 27 + "║")
        print(f"║  إشارة معكوسة (+/-):        {stats['rows_sign_flipped']:>10,}" + " " * 27 + "║")
        print(f"║  مخزن صفر مع وجود تغير:    {stats['rows_zero_stored']:>10,}" + " " * 27 + "║")
        print(f"║  أقصى فارق:                  {stats['max_diff']:>10.4f}%" + " " * 26 + "║")
        print(f"║  الرموز المتأثرة:           {len(stats['symbols_with_errors']):>10,}" + " " * 27 + "║")
        print("╠" + "═" * 73 + "╣")

        if dry_run:
            print(f"║  🟡 الصفوف التي ستُصحح:     {stats['rows_with_errors']:>10,}" + " " * 27 + "║")
        else:
            print(f"║  ✅ الصفوف المُحدّثة:        {stats['rows_updated']:>10,}" + " " * 27 + "║")
            status = "✅ صفر" if remaining_errors == 0 else f"⚠️ {remaining_errors:,}"
            print(f"║  أخطاء متبقية:              {status:>10}" + " " * 27 + "║")

        print(f"║  الوقت المستغرق:            {format_duration(elapsed):>10}" + " " * 27 + "║")
        print("╚" + "═" * 73 + "╝")

        # ── 5. عينة من أكبر الأخطاء ──────────────────────────────────────
        if stats["error_details"]:
            top_errors = sorted(stats["error_details"], key=lambda x: x["diff"], reverse=True)[:15]
            print(f"\n📋 عينة من أكبر {len(top_errors)} خطأ:")
            print(f"  {'الرمز':<6} | {'التاريخ':<10} | {'إغلاق أمس':<10} | {'إغلاق':<8} | {'قديم %':<9} | {'جديد %':<9} | {'فارق':<8}")
            print("  " + "-" * 78)
            for e in top_errors:
                print(
                    f"  {e['symbol']:<6} | {e['date']:<10} | "
                    f"{e['prev_close']:<10.2f} | {e['close']:<8.2f} | "
                    f"{e['old_pct']:<9.4f} | {e['new_pct']:<9.4f} | {e['diff']:<8.4f}"
                )

        # ── 6. تصدير CSV ─────────────────────────────────────────────────
        if export_path and stats["error_details"]:
            export_file = Path(export_path)
            with open(export_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv_module.DictWriter(f, fieldnames=[
                    "symbol", "date", "prev_close", "close",
                    "old_change", "new_change", "old_pct", "new_pct", "diff"
                ])
                writer.writeheader()
                writer.writerows(sorted(stats["error_details"], key=lambda x: (x["symbol"], x["date"])))
            print(f"\n📁 تم تصدير التقرير: {export_file.resolve()}")

    print()


def main():
    p = argparse.ArgumentParser(
        description="إعادة حساب change و change_percent لكل بيانات جدول prices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
أمثلة:
  python scripts/recalc_change.py                         # الكل
  python scripts/recalc_change.py --dry-run               # معاينة
  python scripts/recalc_change.py --symbol 1010           # سهم محدد
  python scripts/recalc_change.py --from 2025-01-01       # من تاريخ
  python scripts/recalc_change.py --export report.csv     # تصدير
        """
    )
    p.add_argument("--dry-run", action="store_true", help="معاينة بدون تعديل")
    p.add_argument("--symbol", type=str, help="رمز سهم محدد")
    p.add_argument("--from", dest="date_from", type=str, help="بداية النطاق (YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", type=str, help="نهاية النطاق (YYYY-MM-DD)")
    p.add_argument("--export", type=str, help="تصدير تقرير الأخطاء كـ CSV")
    p.add_argument("--threshold", type=float, default=DIFF_THRESHOLD,
                    help=f"فارق الخطأ المعتبر (افتراضي {DIFF_THRESHOLD}%%)")

    args = p.parse_args()
    run_recalc(
        dry_run=args.dry_run,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        export_path=args.export,
        threshold=args.threshold,
    )


if __name__ == "__main__":
    main()