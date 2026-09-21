"""
╔══════════════════════════════════════════════════════════════════════════╗
║        TASI Full Historical Data Exporter (Prices + Indicators)          ║
║                                                                          ║
║  يسحب كامل البيانات التاريخية (OHLCV + المؤشرات الفنية المحسوبة)         ║
║  من أول تاريخ متاح لكل سهم وحتى اليوم بأسرع أداء ممكن عبر دمج SQL مباشر  ║
║                                                                          ║
║  طريقة الاستخدام:                                                         ║
║    # 1. سحب سهم محدد لتجربة سريعة                                       ║
║    python scripts/export_full_history_csv.py --symbol 2222              ║
║                                                                          ║
║    # 2. سحب كل الأسهم في ملفات منفصلة لكل سهم (الأنسب والأنظف)          ║
║    python scripts/export_full_history_csv.py --split-by-symbol          ║
║                                                                          ║
║    # 3. سحب كل الأسهم في ملف CSV واحد ضخم                               ║
║    python scripts/export_full_history_csv.py                            ║
║                                                                          ║
║    # 4. تحديد فترة زمنية معينة                                           ║
║    python scripts/export_full_history_csv.py --from 2020-01-01          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sys
import os
import time
import argparse
from pathlib import Path
from datetime import datetime
import pandas as pd
from sqlalchemy import inspect, text

# Setup paths
HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine

DEFAULT_OUTPUT_DIR = BACKEND_ROOT / "output" / "full_history_export"


def get_columns_to_select():
    """
    استخراج أسماء الأعمدة من الجدولين بدقة لتفادي تكرار (symbol, date, close, company_name, id).
    """
    insp = inspect(engine)
    price_cols_meta = insp.get_columns("prices")
    ind_cols_meta = insp.get_columns("stock_indicators")

    price_cols = [c["name"] for c in price_cols_meta]
    ind_cols = [c["name"] for c in ind_cols_meta]

    # أعمدة أسعار التداول الأساسية المراد استخراجها
    core_price_cols = [
        "symbol", "company_name", "date",
        "open", "high", "low", "close",
        "change", "change_percent",
        "volume_traded", "value_traded_sar", "no_of_trades", "market_cap",
        "sector", "industry", "sub_industry", "industry_group"
    ]
    selected_price_cols = [f"p.{c}" for c in core_price_cols if c in price_cols]

    # استبعاد الأعمدة المتكررة أو أعمدة النظام من جدول المؤشرات
    excluded_from_ind = {
        "id", "symbol", "date", "company_name", "close",
        "created_at", "updated_at"
    }
    selected_ind_cols = [f"si.{c}" for c in ind_cols if c not in excluded_from_ind]

    return selected_price_cols, selected_ind_cols


def build_sql_query(price_cols, ind_cols, symbol=None, from_date=None, to_date=None):
    select_fields = ",\n    ".join(price_cols + ind_cols)

    where_clauses = ["1=1"]
    params = {}

    if symbol:
        where_clauses.append("p.symbol = :symbol")
        params["symbol"] = str(symbol)

    if from_date:
        where_clauses.append("p.date >= :from_date")
        params["from_date"] = str(from_date)

    if to_date:
        where_clauses.append("p.date <= :to_date")
        params["to_date"] = str(to_date)

    where_sql = " AND ".join(where_clauses)

    query = f"""
    SELECT 
        {select_fields}
    FROM prices p
    LEFT JOIN stock_indicators si 
        ON p.symbol = si.symbol AND p.date = si.date
    WHERE {where_sql}
    ORDER BY p.symbol ASC, p.date ASC
    """
    return query, params


def export_data(symbol=None, from_date=None, to_date=None, split_by_symbol=False, output_dir=DEFAULT_OUTPUT_DIR):
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(" 🚀 بدء تجهيز وتصدير البيانات الشاملة (الأسعار + المؤشرات الفنية)")
    print("=" * 70)
    if symbol:
        print(f" • السهم: {symbol}")
    if from_date:
        print(f" • من تاريخ: {from_date}")
    if to_date:
        print(f" • إلى تاريخ: {to_date}")
    print(f" • المجلد: {out_dir}")

    start_time = time.time()

    print("\n🔍 فحص هيكل الجداول في قاعدة البيانات...")
    price_cols, ind_cols = get_columns_to_select()
    total_cols = len(price_cols) + len(ind_cols)
    print(f" ✔ أعمدة الأسعار الأساسية: {len(price_cols)}")
    print(f" ✔ أعمدة المؤشرات الفنية: {len(ind_cols)}")
    print(f" ✔ إجمالي الأعمدة المدمجة: {total_cols} عمود")

    raw_query, params = build_sql_query(price_cols, ind_cols, symbol, from_date, to_date)

    print("\n⚡ جاري سحب البيانات عبر استعلام SQL عالي السرعة (Chunked Stream)...")

    # استخدام Server-side stream / chunksize لتفادي استهلاك الذاكرة وتسريع التصدير
    chunk_size = 50000

    if split_by_symbol:
        # إذا طلب تقسيم حسب السهم: نقوم بتجميع أو كتابة لكل سهم
        print(" 📁 النمط المحدد: ملف CSV منفصل لكل سهم")
        first_chunk = True
        total_rows = 0
        symbols_seen = set()

        # تشغيل الاستعلام وسحب البيانات
        with engine.connect().execution_options(stream_results=True) as conn:
            chunks = pd.read_sql(text(raw_query), conn, params=params, chunksize=chunk_size)
            
            # حفظ المقاطع بحسب الرمز
            for i, chunk in enumerate(chunks, start=1):
                rows_in_chunk = len(chunk)
                total_rows += rows_in_chunk
                symbols_in_chunk = chunk["symbol"].unique()
                symbols_seen.update(symbols_in_chunk)

                for sym, group in chunk.groupby("symbol"):
                    sym_file = out_dir / f"TASI_{sym}_full.csv"
                    # إذا كان أول مرة نكتب في ملف هذا السهم نكتب الـ header
                    write_header = not sym_file.exists()
                    group.to_csv(sym_file, mode="a", index=False, header=write_header, encoding="utf-8-sig")

                elapsed = round(time.time() - start_time, 1)
                print(f"   ↳ تم معالجة {total_rows:,} صف... ({elapsed} ثانية)")

        duration = round(time.time() - start_time, 2)
        print("\n" + "=" * 70)
        print(f" ✅ تم الانتهاء بنجاح!")
        print(f" • عدد الصفوف الكلي: {total_rows:,}")
        print(f" • عدد الشركات/الأسهم: {len(symbols_seen)}")
        print(f" • استغرق الوقت: {duration} ثانية")
        print(f" • المجلد المحفوظ: {out_dir}")
        print("=" * 70)

    else:
        # ملف واحد مدمج
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_filename = f"TASI_full_history_{symbol or 'ALL'}_{ts}.csv"
        out_file = out_dir / out_filename
        print(f" 📄 النمط المحدد: ملف CSV واحد -> {out_filename}")

        first_chunk = True
        total_rows = 0

        with engine.connect().execution_options(stream_results=True) as conn:
            chunks = pd.read_sql(text(raw_query), conn, params=params, chunksize=chunk_size)

            for i, chunk in enumerate(chunks, start=1):
                rows_in_chunk = len(chunk)
                total_rows += rows_in_chunk

                chunk.to_csv(
                    out_file,
                    mode="w" if first_chunk else "a",
                    index=False,
                    header=first_chunk,
                    encoding="utf-8-sig"
                )
                first_chunk = False
                elapsed = round(time.time() - start_time, 1)
                print(f"   ↳ تم تنزيل وكتابة {total_rows:,} صف... ({elapsed} ثانية)")

        duration = round(time.time() - start_time, 2)
        print("\n" + "=" * 70)
        print(f" ✅ تم الانتهاء بنجاح!")
        print(f" • إجمالي الصفوف المصدرة: {total_rows:,}")
        print(f" • استغرق الوقت: {duration} ثانية")
        print(f" • مسار الملف الكامل: {out_file}")
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="تصدير بيانات الأسهم الشاملة (الأسعار + المؤشرات الفنية) إلى ملفات CSV بأقصى سرعة"
    )
    parser.add_argument("--symbol", type=str, default=None, help="رمز السهم (مثال: 2222 لأرامكو)")
    parser.add_argument("--from", dest="from_date", type=str, default=None, help="تاريخ البداية (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, default=None, help="تاريخ النهاية (YYYY-MM-DD)")
    parser.add_argument("--split-by-symbol", action="store_true", help="إنشاء ملف منفصل لكل سهم بدلاً من ملف واحد ضخم")
    parser.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR), help="مجلد حفظ الملفات")

    args = parser.parse_args()

    export_data(
        symbol=args.symbol,
        from_date=args.from_date,
        to_date=args.to_date,
        split_by_symbol=args.split_by_symbol,
        output_dir=args.output_dir
    )


if __name__ == "__main__":
    main()
