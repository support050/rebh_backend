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

The staleness test above JOINs industry_group_history, so it can only ever see dates
that ALREADY have a row. It is blind to the other kind of hole: this table was written
once a WEEK (the weekday drifted Thursday -> Wednesday -> Thursday) from 2025-02-06
until 2026-02-04, and became daily from 2026-02-05. Those ~200 weekly-skipped trading
days have no row at all, so --execute alone will never fill them.

    --fill-missing  derives the Saudi trading master (prices UNION market_pulse UNION
                    historical_reports), keeps the dates that have NO row, and computes
                    them. Bounded by the table's own min/max date so no history is
                    invented before the current 22-group taxonomy starts.
    --from / --to   override that window. With --fill-missing they narrow/widen the
                    search; alone they recompute EVERY master date in the window
                    (existing rows included) -- use that to refresh rank_1_week_ago /
                    rank_3_months_ago / rank_6_months_ago, which were resolved against
                    a weekly-only world and stay stale otherwise.

Dates without group-tagged prices are dropped up front instead of failing one by one.
Everything re-derives its own work list at start, so an interrupted run resumes simply
by being launched again.

Usage:
    python scripts/backfill_industry_group_history.py            # dry run (default)
    python scripts/backfill_industry_group_history.py --execute
    python scripts/backfill_industry_group_history.py --execute --limit 3
    python scripts/backfill_industry_group_history.py --fill-missing            # list only
    python scripts/backfill_industry_group_history.py --fill-missing --list     # dates, 1/line
    python scripts/backfill_industry_group_history.py --fill-missing --execute
    python scripts/backfill_industry_group_history.py --from 2026-02-05 --to 2026-10-25 --execute
"""
import sys, argparse
from datetime import date
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


# Data-derived trading calendar -- there is no holiday table in the schema, and these
# three tables are the ones the daily pipeline writes for every real session.
MASTER_SQL = text("""
    SELECT dt FROM (
        SELECT DISTINCT CAST(date AS DATE) AS dt FROM prices
        UNION
        SELECT DISTINCT CAST(date AS DATE) FROM market_pulse
        UNION
        SELECT DISTINCT CAST(report_date AS DATE) FROM historical_reports
    ) m WHERE m.dt BETWEEN :lo AND :hi ORDER BY dt
""")

HAVE_SQL = text("SELECT DISTINCT CAST(date AS DATE) FROM industry_group_history")
BOUNDS_SQL = text("SELECT min(CAST(date AS DATE)), max(CAST(date AS DATE)) "
                  "FROM industry_group_history")

# Mirrors STALE_SQL's membership predicate: a date with no group-tagged prices cannot
# produce a single group row, so it is not a candidate at all.
GROUPED_DAYS_SQL = text("""
    SELECT DISTINCT date FROM prices
    WHERE industry_group IS NOT NULL AND TRIM(industry_group) <> '' AND close > 0
      AND date BETWEEN :lo AND :hi
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


SECS_PER_DATE = 16      # measured 2026-10-25 on the live Render DB (2026-10-06: 17:08:36 -> :52)


def parse_iso(flag, value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"{flag} expects YYYY-MM-DD, got {value!r}")


def probe_window(engine, lo, hi):
    """(master trading days, dates already stored, dates prices can group) for [lo, hi]."""
    with engine.connect() as probe:
        master = [r[0] for r in probe.execute(MASTER_SQL, {"lo": lo, "hi": hi}).fetchall()]
        have = {r[0] for r in probe.execute(HAVE_SQL).fetchall()}
        grouped = {r[0] for r in probe.execute(GROUPED_DAYS_SQL,
                                              {"lo": lo, "hi": hi}).fetchall()}
    return master, have, grouped


def month_blocks(dates):
    """Compact per-month view — makes a weekly-vs-daily cadence obvious at a glance."""
    by_month = {}
    for d in dates:
        by_month.setdefault(str(d)[:7], []).append(str(d)[8:])
    for mk in sorted(by_month):
        print(f"     {mk} ({len(by_month[mk]):>3})  " + " ".join(by_month[mk]))


def resolve_candidates(engine, args, quiet=False):
    """Build the work list for --fill-missing / --from / --to. Returns (dates, lo, hi).

    quiet=True is the --list contract: stdout must carry the dates and nothing else,
    so it stays redirectable straight into a file or a foreach loop.
    """
    with engine.connect() as probe:
        lo, hi = probe.execute(BOUNDS_SQL).one()
    if lo is None:
        raise SystemExit("industry_group_history is empty — no window to derive a range from")
    if args.date_from:
        lo = parse_iso("--from", args.date_from)
    if args.date_to:
        hi = parse_iso("--to", args.date_to)

    master, have, grouped = probe_window(engine, lo, hi)
    picked = [d for d in master if d not in have] if args.fill_missing else list(master)
    computable = [d for d in picked if d in grouped]
    uncomputable = [d for d in picked if d not in grouped]

    if quiet:
        return computable, lo, hi

    print(f"\n== {'fill-missing' if args.fill_missing else 'recompute window'} ==")
    print(f"   window                        : {lo} .. {hi}")
    print(f"   master trading days           : {len(master)}")
    print(f"   dates already stored          : {len(have)}")
    print(f"   candidates                    : {len(picked)}")
    print(f"   computable from prices        : {len(computable)}")
    if uncomputable:
        print(f"   dropped (no group-tagged prices): {len(uncomputable)} "
              f"{[str(d) for d in uncomputable][:10]}")
    if computable:
        print(f"   ~duration at {SECS_PER_DATE}s/date   : "
              f"{round(len(computable) * SECS_PER_DATE / 60)} min")
        month_blocks(computable)
    return computable, lo, hi


def process_dates(db, calc, todo):
    """The calculator's four steps, oldest first, over any work list.

    Ranks resolve with 'date <= target ... ORDER BY date DESC LIMIT 1' (nearest prior
    row), so ascending order is what lets a freshly inserted date be seen by every
    date after it.
    """
    ok, failed, conn_errors = 0, [], 0
    # A dead WAN connection makes every later date fail the same way; bail out early
    # instead of burning an hour on doomed attempts.
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
                          f"re-derives the remaining dates by itself.")
                    break
    return ok, failed


def main():
    ap = argparse.ArgumentParser(
        description="Recompute stale dates of industry_group_history, or fill the "
                    "trading days its old weekly cadence never wrote.")
    ap.add_argument("--execute", action="store_true", help="actually write (default is dry run)")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N dates (smoke test)")
    ap.add_argument("--fill-missing", action="store_true",
                    help="compute trading days that have NO row (the weekly gap)")
    ap.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                    help="window start; without --fill-missing every master date in the "
                         "window is recomputed, existing rows included (refreshes ranks)")
    ap.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="window end")
    ap.add_argument("--list", action="store_true",
                    help="print the work list, one date per line, and exit")
    args = ap.parse_args()

    engine = create_engine(str(settings.DATABASE_URL), pool_pre_ping=True,
                           pool_recycle=300)
    windowed = bool(args.fill_missing or args.date_from or args.date_to)
    lo = hi = None

    if windowed:
        dates, lo, hi = resolve_candidates(engine, args, quiet=args.list)
        detail = "missing days to insert" if args.fill_missing else "dates to recompute"
    else:
        with engine.connect() as probe:
            rows = probe.execute(STALE_SQL).fetchall()
        dates = summarize(rows)
        detail = (f"{len(rows)} stale rows, plus the other 21 groups of each date "
                  f"since rank is relative")

    if args.list:
        for d in dates:
            print(d)
        return

    if not dates:
        print("\nNothing to do — " + ("that window already has a row for every trading day."
              if windowed else "every history row already matches prices membership."))
        return
    if not args.execute:
        print(f"\nDRY RUN — {len(dates)} dates would be computed ({detail}). "
              f"Add --execute to write.")
        return

    db = SessionLocal()
    calc = IndustryGroupCalculator(db)
    todo = dates[:args.limit] if args.limit else dates
    print(f"\n== executing {len(todo)} dates, oldest first ==")

    ok, failed = process_dates(db, calc, todo)

    db.close()
    print(f"\n   written ok : {ok}")
    print(f"   failed     : {len(failed)}")
    for f in failed[:10]:
        print("     ", f)

    try:
        if windowed:
            master, have, _ = probe_window(engine, lo, hi)
            left = [d for d in master if d not in have]
            print(f"\n== re-check ==\n   master dates still without a row: {len(left)}")
            for d in left[:15]:
                print(f"     {d}")
        else:
            with engine.connect() as probe:
                after = probe.execute(STALE_SQL).fetchall()
            print(f"\n== re-check ==\n   stale rows remaining: {len(after)}")
            for r in after[:15]:
                print(f"     {r[0]}  {str(r[1])[:34]:<34} recorded={r[2]} actual={r[3]}")
    except Exception as e:
        print(f"\n== re-check ==\n   could not re-measure ({type(e).__name__}) "
              f"— re-run the dry run to check")


if __name__ == "__main__":
    main()
