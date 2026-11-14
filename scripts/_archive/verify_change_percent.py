"""
إسكربت تحقق: يقارن change/change_percent المخزنة مع الحساب الصحيح
لعينة من سهم واحد أو يوم واحد — للتأكد إن الأرقام سليمة بعد الإصلاح.

التشغيل:
  python scripts/verify_change_percent.py --date 2026-09-24
  python scripts/verify_change_percent.py --symbol 1010 --limit 20
  python scripts/verify_change_percent.py --date 2026-09-24 --symbol 1010
"""

import sys
import argparse
from pathlib import Path
from sqlalchemy import text

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine


def verify(date_str=None, symbol=None, limit=30):
    where_clauses = ["prev_close IS NOT NULL", "prev_close > 0", "close > 0"]
    params = {}

    if date_str:
        where_clauses.append("date = :target_date")
        params["target_date"] = date_str
    if symbol:
        where_clauses.append("symbol = :symbol")
        params["symbol"] = symbol

    where_sql = " AND ".join(where_clauses)

    q = text(f"""
    WITH lag_data AS (
        SELECT
            symbol, date, close,
            change, change_percent,
            LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
        FROM prices
    )
    SELECT
        symbol,
        date,
        prev_close,
        close,
        change                                       AS stored_change,
        ROUND(close - prev_close, 4)                 AS calc_change,
        change_percent                               AS stored_pct,
        ROUND(((close / prev_close) - 1.0) * 100.0, 4)  AS calc_pct,
        ROUND(ABS(change_percent - ROUND(((close / prev_close) - 1.0) * 100.0, 4)), 4) AS diff
    FROM lag_data
    WHERE {where_sql}
    ORDER BY diff DESC, date DESC
    LIMIT :lim
    """)
    params["lim"] = limit

    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().fetchall()

    if not rows:
        print("لا توجد بيانات للمعايير المحددة.")
        return

    # Summary
    max_diff = max(float(r['diff']) for r in rows)
    errors = sum(1 for r in rows if float(r['diff']) > 0.01)

    print("=" * 100)
    print(f"  🔍 تحقق من change/change_percent")
    if date_str:
        print(f"     التاريخ: {date_str}")
    if symbol:
        print(f"     الرمز: {symbol}")
    print(f"     أعلى فارق: {max_diff:.4f}%")
    print(f"     أخطاء (> 0.01%): {errors} من {len(rows)}")
    print("=" * 100)

    header = f"  {'الرمز':<6} | {'التاريخ':<10} | {'إغلاق أمس':<10} | {'إغلاق اليوم':<10} | {'مخزن %':<9} | {'محسوب %':<9} | {'فارق':<8} | {'حالة'}"
    print(header)
    print("  " + "-" * 95)

    for r in rows:
        diff = float(r['diff'])
        status = "✅" if diff <= 0.01 else "❌"
        print(
            f"  {r['symbol']:<6} | {str(r['date']):<10} | "
            f"{float(r['prev_close']):<10.2f} | {float(r['close']):<10.2f} | "
            f"{float(r['stored_pct']):<9.4f} | {float(r['calc_pct']):<9.4f} | "
            f"{diff:<8.4f} | {status}"
        )


def main():
    p = argparse.ArgumentParser(description="تحقق من صحة change/change_percent")
    p.add_argument("--date", type=str, help="تاريخ محدد بصيغة YYYY-MM-DD")
    p.add_argument("--symbol", type=str, help="رمز سهم محدد")
    p.add_argument("--limit", type=int, default=30, help="عدد النتائج (افتراضي 30)")
    args = p.parse_args()

    if not args.date and not args.symbol:
        print("يجب تحديد --date أو --symbol أو كلاهما.")
        return

    verify(date_str=args.date, symbol=args.symbol, limit=args.limit)


if __name__ == "__main__":
    main()
