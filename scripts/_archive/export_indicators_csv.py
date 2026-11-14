"""
╔══════════════════════════════════════════════════════════════════════════╗
║          TASI Technical Indicators Exporter — Script 2 (v2 - fast)       ║
║          نسخة محسّنة: تصدير لكل سهم في اتصال منفصل + COPY                 ║
║                                                                          ║
║  ليه أسرع وأضمن من النسخة القديمة؟                                        ║
║   1) بدل ما نفتح كيرسر Streaming واحد على 170 عمود لكل الجدول (بيفضل     ║
║      شغال لساعة، والسيرفر بيقفل الاتصال idle/network) → بنعمل استعلام    ║
║      صغير منفصل لكل سهم (WHERE symbol = X)، كل استعلام بياخد ثواني.      ║
║   2) بنستخدم COPY (SELECT ...) TO STDOUT مباشرة من postgres بدل ما نمرّ  ║
║      بـ pandas/SQLAlchemy ORM لكل صف → أسرع بمرات كتير.                  ║
║   3) لو سهم معيّن فشل (انقطاع نت/سيرفر)، بيعيد المحاولة عليه بس (3       ║
║      مرات) من غير ما يخسر باقي الأسهم اللي خلصت.                         ║
║   4) بنعمل resume تلقائي: أي سهم ملفه موجود بالفعل ومكتمل (فيه سطر       ║
║      نهاية) بيتخطّاه، فلو السكربت وقع تقدر تشغّله تاني من غير ما تعيد    ║
║      كل حاجة من الأول.                                                   ║
║   5) ORDER BY date لكل سهم لوحده (رخيص) بدل ORDER BY symbol,date على     ║
║      الجدول كله (sort ضخم قبل ما يرجع أي نتيجة).                         ║
║                                                                          ║
║  طريقة الاستخدام (نفس القديمة تقريباً):                                   ║
║    python scripts/export_indicators_csv_v2.py                 ← كل الأسهم║
║    python scripts/export_indicators_csv_v2.py --symbol 1120  ← سهم واحد ║
║    python scripts/export_indicators_csv_v2.py --from 2020-01-01         ║
║    python scripts/export_indicators_csv_v2.py --single-file  ← ملف واحد ║
║    python scripts/export_indicators_csv_v2.py --list-columns            ║
║    python scripts/export_indicators_csv_v2.py --workers 4    ← تصدير    ║
║                                                    متوازي (أسرع بكتير)   ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import inspect, text, create_engine

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import engine as _base_engine  # نفس مصدر الاتصال الأصلي (للـ inspect فقط)

DEFAULT_OUTPUT_DIR = BACKEND_ROOT / "output" / "indicators_export"

EXCLUDED_FROM_INDICATORS = {"id", "created_at", "updated_at"}
IDENTITY_COLS = ["symbol", "company_name", "date", "close"]

MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 5

# محرك DB خاص بسكربت التصدير — pool أوسع + pool_pre_ping عشان نكتشف
# أي اتصال "ميت" (السيرفر/الـ proxy قفله بسبب idle timeout) قبل ما نستخدمه،
# بدل ما نستنى TCP timeout طويل ونفشل بشكل عشوائي أثناء التوازي.
def _make_export_engine(workers):
    return create_engine(
        _base_engine.url,
        pool_size=max(workers, 2) + 2,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=180,   # يجدد أي اتصال أقدم من 3 دقائق قبل استخدامه
        pool_timeout=30,
    )


engine = _base_engine  # يُستبدل داخل export_data بمحرك مخصص حسب عدد الـ workers


def get_indicator_columns():
    insp = inspect(engine)
    all_cols = [c["name"] for c in insp.get_columns("stock_indicators")]
    identity = [c for c in IDENTITY_COLS if c in all_cols]
    indicator_cols = [
        c for c in all_cols
        if c not in EXCLUDED_FROM_INDICATORS and c not in IDENTITY_COLS
    ]
    return identity + indicator_cols


def list_columns():
    cols = get_indicator_columns()
    print("\n" + "=" * 60)
    print("  الأعمدة المتاحة في stock_indicators")
    print("=" * 60)
    for i, c in enumerate(cols, 1):
        tag = " ← هوية" if c in IDENTITY_COLS else ""
        print(f"  {i:>3}. {c}{tag}")
    print("=" * 60)
    print(f"  المجموع: {len(cols)} عمود")
    print("=" * 60 + "\n")


def get_symbols(symbol_filter=None):
    """يرجّع قائمة الأسهم المطلوب تصديرها."""
    if symbol_filter:
        return [str(symbol_filter)]
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT symbol FROM stock_indicators ORDER BY symbol")
        ).fetchall()
    return [r[0] for r in rows]


def build_copy_sql(columns, symbol, from_date=None, to_date=None):
    cols_sql = ", ".join(columns)
    where = ["symbol = %(symbol)s"]
    params = {"symbol": str(symbol)}
    if from_date:
        where.append("date >= %(from_date)s")
        params["from_date"] = str(from_date)
    if to_date:
        where.append("date <= %(to_date)s")
        params["to_date"] = str(to_date)
    where_sql = " AND ".join(where)
    # ORDER BY date فقط (لسهم واحد) — رخيص جداً مقارنة بـ ORDER BY على الجدول كله
    inner_query = f"SELECT {cols_sql} FROM stock_indicators WHERE {where_sql} ORDER BY date ASC"
    return inner_query, params


def export_symbol(symbol, columns, out_path, from_date=None, to_date=None,
                   include_header=True, append=False):
    """
    يصدّر سهم واحد باستخدام COPY مباشرة (سريع جداً)، مع retry عند انقطاع الاتصال.
    يرجّع عدد الصفوف المُصدَّرة.
    """
    inner_query, params = build_copy_sql(columns, symbol, from_date, to_date)

    # psycopg2 COPY لا يدعم %(name)s placeholders مباشرة داخل copy_expert بسهولة
    # لذلك نبني الاستعلام بأمان يدوياً (القيم أرقام/تواريخ فقط، مفيش خطر SQL injection)
    safe_symbol = str(symbol).replace("'", "''")
    where = [f"symbol = '{safe_symbol}'"]
    if from_date:
        where.append(f"date >= '{str(from_date)}'")
    if to_date:
        where.append(f"date <= '{str(to_date)}'")
    where_sql = " AND ".join(where)
    cols_sql = ", ".join(columns)

    copy_sql = (
        f"COPY (SELECT {cols_sql} FROM stock_indicators "
        f"WHERE {where_sql} ORDER BY date ASC) "
        f"TO STDOUT WITH (FORMAT csv, HEADER {'true' if include_header else 'false'})"
    )

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        raw_conn = None
        try:
            raw_conn = engine.raw_connection()
            cur = raw_conn.cursor()
            mode = "a" if append else "w"
            with open(out_path, mode, encoding="utf-8", newline="") as f:
                cur.copy_expert(copy_sql, f)
            row_count = max(cur.rowcount, 0)
            cur.close()
            raw_conn.close()
            return row_count
        except Exception as e:  # noqa: BLE001
            last_err = e
            # الاتصال ده احتمال يكون بايظ (السيرفر قفله فجأة) — نتخلص منه فوراً
            # ونجبر الـ pool يفتح واحد جديد نضيف في المحاولة الجاية، بدل ما نسيبه
            # يتلخبط جوه الـ pool ويعطّل باقي الـ threads.
            try:
                if raw_conn is not None:
                    raw_conn.close()
            except Exception:  # noqa: BLE001
                pass
            print(f"   ⚠️  فشل السهم {symbol} (محاولة {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"فشل تصدير السهم {symbol} بعد {MAX_RETRIES} محاولات: {last_err}")


def export_data(symbol=None, from_date=None, to_date=None,
                 single_file=False, output_dir=DEFAULT_OUTPUT_DIR,
                 workers=1, resume=True):

    global engine
    engine = _make_export_engine(workers)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(" 📈 تصدير المؤشرات الفنية المحسوبة — Script 2 (v2 fast)")
    print("=" * 70)

    columns = get_indicator_columns()
    print(f" ✔ إجمالي الأعمدة: {len(columns)} عمود")

    symbols = get_symbols(symbol)
    print(f" ✔ عدد الأسهم المطلوب تصديرها: {len(symbols)}")
    print(f" • المجلد: {out_dir}")
    print(f" • عدد العمليات المتوازية (workers): {workers}")

    start_time = time.time()
    total_rows = 0
    done = 0
    failed = []

    if single_file:
        # نصدّر كل سهم لملف مؤقت خاص به، وفي الآخر ندمجهم في ملف واحد.
        # ده أضمن بكتير من append متوازي على نفس الملف (race conditions).
        tmp_dir = out_dir / "_tmp_parts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        symbol_paths = {s: tmp_dir / f"part_{s}.csv" for s in symbols}
    else:
        symbol_paths = {s: out_dir / f"TASI_indicators_{s}.csv" for s in symbols}

    def is_complete(path):
        return resume and path.exists() and path.stat().st_size > 0

    def work(sym):
        path = symbol_paths[sym]
        if is_complete(path):
            return sym, -1  # -1 = تم تخطيه (موجود مسبقاً)
        rows = export_symbol(
            sym, columns, path,
            from_date=from_date, to_date=to_date,
            include_header=True, append=False,
        )
        return sym, rows

    print("\n⚡ جاري التصدير...\n")

    if workers <= 1:
        for sym in symbols:
            sym_result, rows = work(sym)
            done += 1
            if rows == -1:
                print(f" [{done}/{len(symbols)}] ⏭️  {sym} — موجود مسبقاً، تم التخطي")
                continue
            total_rows += rows
            elapsed = round(time.time() - start_time, 1)
            print(f" [{done}/{len(symbols)}] ✔ {sym}: {rows:,} صف — إجمالي {total_rows:,} ({elapsed}s)")
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(work, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                done += 1
                try:
                    sym_result, rows = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed.append(sym)
                    print(f" [{done}/{len(symbols)}] ❌ {sym}: {e}")
                    continue
                if rows == -1:
                    print(f" [{done}/{len(symbols)}] ⏭️  {sym} — موجود مسبقاً، تم التخطي")
                    continue
                total_rows += rows
                elapsed = round(time.time() - start_time, 1)
                print(f" [{done}/{len(symbols)}] ✔ {sym}: {rows:,} صف — إجمالي {total_rows:,} ({elapsed}s)")

    if single_file:
        print("\n📎 جاري دمج كل الأسهم في ملف واحد...")
        ts = time.strftime("%Y%m%d_%H%M%S")
        final_path = out_dir / f"TASI_indicators_{symbol or 'ALL'}_{ts}.csv"
        header_written = False
        with open(final_path, "w", encoding="utf-8-sig", newline="") as out_f:
            for sym in symbols:
                part_path = symbol_paths[sym]
                if not part_path.exists():
                    continue
                with open(part_path, "r", encoding="utf-8") as in_f:
                    lines = in_f.readlines()
                if not lines:
                    continue
                if not header_written:
                    out_f.writelines(lines)
                    header_written = True
                else:
                    out_f.writelines(lines[1:])  # نتخطى الـ header
        print(f" ✔ الملف النهائي: {final_path}")

        # تنظيف: مسح كل الملفات المؤقتة لكل سهم (مش محتاجينها بعد الدمج)
        print(" 🧹 جاري مسح الملفات المؤقتة...")
        for sym in symbols:
            part_path = symbol_paths[sym]
            try:
                if part_path.exists():
                    part_path.unlink()
            except Exception as e:  # noqa: BLE001
                print(f"   ⚠️  تعذر مسح {part_path.name}: {e}")
        try:
            tmp_dir.rmdir()  # يشتغل بس لو فاضي تماماً
        except Exception:
            pass

    duration = round(time.time() - start_time, 2)
    print("\n" + "=" * 70)
    print(" ✅ تم!")
    print(f" • إجمالي الصفوف: {total_rows:,}")
    print(f" • عدد الأسهم: {len(symbols)}")
    if failed:
        print(f" • ⚠️  أسهم فشلت ({len(failed)}): {', '.join(failed)}")
        print("   شغّل السكربت تاني بنفس الأمر — resume هيكمل عليهم بس.")
    print(f" • الوقت: {duration} ثانية")
    print(f" • المجلد: {out_dir}")
    print("=" * 70)


def main():
    p = argparse.ArgumentParser(
        description="تصدير المؤشرات الفنية المحسوبة من جدول stock_indicators (نسخة سريعة)"
    )
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--from", dest="from_date", type=str, default=None)
    p.add_argument("--to", dest="to_date", type=str, default=None)
    p.add_argument("--single-file", action="store_true",
                   help="دمج كل الأسهم في ملف واحد بالنهاية (زي السلوك الافتراضي القديم)")
    p.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--workers", type=int, default=4,
                   help="عدد الاتصالات المتوازية (افتراضي 4). جرب تزوّدها لو النت والسيرفر تحمّلوا")
    p.add_argument("--no-resume", action="store_true",
                   help="تجاهل الملفات الموجودة وأعد تصدير كل شيء من الصفر")
    p.add_argument("--list-columns", action="store_true")
    args = p.parse_args()

    if args.list_columns:
        list_columns()
        return

    export_data(
        symbol=args.symbol,
        from_date=args.from_date,
        to_date=args.to_date,
        single_file=args.single_file,
        output_dir=args.output_dir,
        workers=args.workers,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()