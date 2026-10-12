"""Recompute the stale dates of industry_group_history (directive 4).

Six symbols were repaired into prices with an industry_group:
    2288 Food & Beverages      first dated membership 2026-01-21
    4148 Capital Goods         first dated membership 2026-01-21
    1324 Materials             first dated membership 2026-03-11
    7205 Software & Services   first dated membership 2026-05-20
    6022 Consumer Services     first dated membership 2026-08-30
    4328 Capital Goods         first dated membership 2026-08-31

industry_group_history rows for the dates they already covered were computed BEFORE
that repair, so their number_of_stocks (and therefore the equal-weighted group index,
rs_score and rank of all 22 groups on those dates) is stale.

This driver does NOT reimplement anything: it calls the same four steps
calculate_industry_groups.main() runs for --date, on the same DB session:
    calculate_group_index_prices -> calculate_ibd_group_score
    -> prepare_summary_data -> save
Membership inside the calculator is date-scoped (WHERE p.date = :period_date), so a
recomputed historical date still reflects only the stocks that existed on that date —
the 6 symbols are added to a date only from the day they first traded.

Only dates that are demonstrably stale are touched; unaffected dates are left alone.

Usage:
    python scripts/backfill_industry_group_history.py            # dry run (default)
    python scripts/backfill_industry_group_history.py --execute
    python scripts/backfill_industry_group_history.py --execute --limit 3
"""
import sys, argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, text
from app.core.database import SessionLocal
from app.core.config import settings
from calculate_industry_groups import IndustryGroupCalculator

# Exactly the membership filter the calculator uses, so the staleness test measures
# what the calculator itself would see.
STALE_SQL = text("""
    WITH actual AS (
        SELECT date, industry_group, count(DISTINCT symbol) AS n
        FROM prices
        WHERE industry_group IS NOT NULL AND TRIM(industry_group) <> '' AND close > 0
        GROUP BY 1, 2
    )
    SELECT h.date, h.industry_group, h.number_of_stocks AS recorded, a.n AS actual
    FROM industry_group_history h
    JOIN actual a ON a.date = h.date AND a.industry_group = h.industry_group
    WHERE a.n IS DISTINCT FROM h.number_of_stocks
    ORDER BY h.date, h.industry_group
""")


def summarize(rows):
    dates = sorted({r[0] for r in rows})
    groups = {}
    for r in rows:
        groups.setdefault(r[1], []).append(r[0])
    print(f"\n== staleness ==\n   stale (date,group) rows : {len(rows)}")
    print(f"   distinct stale dates      : {len(dates)}")
    if dates:
        print(f"   range                     : {dates[0]} .. {dates[-1]}")
    print("   per group (rows / first .. last date):")
    for g in sorted(groups, key=lambda k: -len(groups[k])):
        ds = sorted(groups[g])
        print(f"     {g:<38} {len(ds):>4}  {ds[0]} .. {ds[-1]}")
    return dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually write (default is dry run)")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N dates (smoke test)")
    args = ap.parse_args()

    engine = create_engine(str(settings.DATABASE_URL), pool_pre_ping=True,
                           pool_recycle=300)
    with engine.connect() as probe:
        rows = probe.execute(STALE_SQL).fetchall()
    dates = summarize(rows)

    if not rows:
        print("\nNothing to do — every history row already matches prices membership.")
        return
    if not args.execute:
        print(f"\nDRY RUN — {len(dates)} dates would be recomputed "
              f"({len(rows)} stale rows, plus the other 21 groups of each date since "
              f"rank is relative). Add --execute to write.")
        return

    db = SessionLocal()
    calc = IndustryGroupCalculator(db)
    todo = dates[:args.limit] if args.limit else dates
    print(f"\n== executing {len(todo)} dates, oldest first ==")

    ok, failed = 0, []
    # A dead WAN connection makes every later date fail the same way; bail out early
    # instead of burning an hour on 150 doomed attempts.
    conn_errors = 0
    for i, d in enumerate(todo, 1):
        try:
            idx = calc.calculate_group_index_prices(d)
            if not idx:
                failed.append((d, "no group data")); continue
            gdf = calc.calculate_ibd_group_score(idx)
            if gdf.empty:
                failed.append((d, "no scores")); continue
            sdf = calc.prepare_summary_data(gdf, d)
            if sdf.empty:
                failed.append((d, "no summary")); continue
            calc.save(sdf, d)
            ok += 1
            conn_errors = 0
            if i % 10 == 1 or i == len(todo):
                print(f"   [{i}/{len(todo)}] {d} -> {len(sdf)} groups")
        except Exception as e:
            db.rollback()
            failed.append((d, f"{type(e).__name__}: {e}"))
            print(f"   !! {d}: {type(e).__name__}: {e}")
            if type(e).__name__ in ("OperationalError", "DisconnectionError", "InterfaceError") \
                    or "psycopg2.OperationalError" in str(e):
                conn_errors += 1
                if conn_errors >= 3:
                    print(f"\n   ABORT after {conn_errors} consecutive connection errors "
                          f"at date {d} — network/DB unreachable. Re-run this script; it "
                          f"re-derives the remaining stale dates by itself.")
                    break

    db.close()
    print(f"\n   written ok : {ok}")
    print(f"   failed     : {len(failed)}")
    for f in failed[:10]:
        print("     ", f)

    try:
        with engine.connect() as probe:
            after = probe.execute(STALE_SQL).fetchall()
    except Exception as e:
        print(f"\n== re-check ==\n   could not re-measure ({type(e).__name__}) "
              f"— re-run the dry run to check")
        return
    print(f"\n== re-check ==\n   stale rows remaining: {len(after)}")
    for r in after[:15]:
        print(f"     {r[0]}  {str(r[1])[:34]:<34} recorded={r[2]} actual={r[3]}")


if __name__ == "__main__":
    main()
