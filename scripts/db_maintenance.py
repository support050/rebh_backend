"""
Database maintenance for the Daily Market Update pipeline (P1-1).

VACUUM ANALYZE the heavy pipeline tables so the planner gets fresh statistics
and dead space is reusable.

Why this exists: the production DB currently has *no usable statistics at all* —
pg_stat reports n_live_tup=0 for dozens of tables that hold hundreds of
thousands of rows, and last_vacuum / last_analyze are NULL everywhere. Every
pipeline query is therefore planned on defaults.

VACUUM cannot run inside a transaction, so this script uses an AUTOCOMMIT
connection. It never drops or rewrites data.

Usage
    python scripts/db_maintenance.py                     # default tables
    python scripts/db_maintenance.py --analyze-all       # + ANALYZE every table
    python scripts/db_maintenance.py --tables prices rs_daily_v2
    python scripts/db_maintenance.py --analyze-only      # no space cleanup
    python scripts/db_maintenance.py --verbose
"""
import sys
import os
import argparse
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, text
from app.core.config import settings

DEFAULT_TABLES = ["prices", "stock_indicators", "rs_daily_v2", "industry_group_history"]

# The pipeline's hottest query shape — used to show the planner's row estimate
# before and after ANALYZE. Literal values only; EXPLAIN never executes it.
PROBE_QUERY = (
    "SELECT symbol, date, close FROM prices "
    "WHERE date <= '2026-10-05' AND symbol IN ('1010','1020','1030') "
    "ORDER BY symbol, date DESC LIMIT 100"
)


def _fmt_stats(conn, tables):
    rows = conn.execute(text("""
        SELECT relname, n_live_tup, n_dead_tup, last_vacuum, last_analyze,
               last_autovacuum, seq_scan, idx_scan
        FROM pg_stat_user_tables
        WHERE relname = ANY(:t)
        ORDER BY relname
    """), {"t": tables}).fetchall()
    return {r[0]: r for r in rows}


def _planner_estimate(conn, sql):
    """Return the top-level row estimate the planner puts on a query."""
    try:
        plan = conn.execute(text("EXPLAIN " + sql)).scalars().all()
    except Exception as e:
        return "?? (%s)" % e
    for line in plan:
        if "rows=" in line:
            return line.strip()
    return (plan[0].strip() if plan else "?")


def main():
    ap = argparse.ArgumentParser(description="VACUUM ANALYZE pipeline tables")
    ap.add_argument("--tables", nargs="*", default=DEFAULT_TABLES)
    ap.add_argument("--analyze-all", action="store_true",
                    help="also ANALYZE every other user table (cheap: sampled)")
    ap.add_argument("--analyze-only", action="store_true",
                    help="skip the VACUUM part (statistics only)")
    ap.add_argument("--vacuum-only", action="store_true",
                    help="skip ANALYZE")
    ap.add_argument("--maintenance-mem", default="128MB",
                    help="maintenance_work_mem for this connection")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # AUTOCOMMIT: required — VACUUM/ANALYZE cannot run inside a transaction block
    engine = create_engine(str(settings.DATABASE_URL), isolation_level="AUTOCOMMIT")
    print(f"🔌 target host: {engine.url.host}  db: {engine.url.database}")

    with engine.connect() as conn:
        try:
            conn.execute(text("SET maintenance_work_mem = :v"), {"v": args.maintenance_mem})
        except Exception:
            conn.exec_driver_sql("SET maintenance_work_mem = '%s'" % args.maintenance_mem)

        tables = list(dict.fromkeys(args.tables))
        before = _fmt_stats(conn, tables)

        print("\n── BEFORE ──")
        print(f"  {'table':26s} {'rows(stat)':>11s} {'dead':>8s} {'last_vacuum':>19s} "
              f"{'last_analyze':>19s}")
        for t in tables:
            r = before.get(t)
            if r:
                print(f"  {t:26s} {r[1]:>11d} {r[2]:>8d} "
                      f"{str(r[3])[:19]:>19s} {str(r[4])[:19]:>19s}")
            else:
                print(f"  {t:26s}  (not in pg_stat_user_tables)")

        print("\n  planner estimate BEFORE: %s" % _planner_estimate(conn, PROBE_QUERY))

        total0 = time.time()
        for t in tables:
            stmt = f'ANALYZE "{t}"' if args.analyze_only else f'VACUUM (ANALYZE) "{t}"'
            if args.vacuum_only:
                stmt = f'VACUUM "{t}"'
            t0 = time.time()
            print(f"\n▶ {stmt}", flush=True)
            try:
                conn.exec_driver_sql(stmt)
                print(f"   ✅ done in {time.time() - t0:.1f}s", flush=True)
            except Exception as e:
                print(f"   ❌ failed: {type(e).__name__}: {e}", flush=True)

        if args.verbose:
            conn.exec_driver_sql("VACUUM (ANALYZE, VERBOSE)")

        if args.analyze_all:
            others = [r[0] for r in conn.execute(text("""
                SELECT relname FROM pg_stat_user_tables
                WHERE relname <> ANY(:t) ORDER BY n_live_tup DESC
            """), {"t": tables}).fetchall()]
            print(f"\n▶ ANALYZE {len(others)} remaining tables", flush=True)
            t0 = time.time()
            failed = 0
            for t in others:
                try:
                    conn.exec_driver_sql(f'ANALYZE "{t}"')
                except Exception as e:
                    failed += 1
                    print(f"   ⚠️ {t}: {type(e).__name__}", flush=True)
            print(f"   ✅ batch ANALYZE done in {time.time() - t0:.1f}s (failures={failed})",
                  flush=True)

        after = _fmt_stats(conn, tables)
        print("\n── AFTER ──")
        print(f"  {'table':26s} {'rows(stat)':>11s} {'exact_rows':>11s} {'dead':>8s} "
              f"{'last_vacuum':>19s} {'last_analyze':>19s}")
        for t in tables:
            r = after.get(t)
            if not r:
                continue
            exact = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
            print(f"  {t:26s} {r[1]:>11d} {exact:>11d} {r[2]:>8d} "
                  f"{str(r[3])[:19]:>19s} {str(r[4])[:19]:>19s}")

        print("\n  planner estimate AFTER : %s" % _planner_estimate(conn, PROBE_QUERY))
        print(f"\n🏁 total {time.time() - total0:.1f}s")

    engine.dispose()


if __name__ == "__main__":
    main()
