"""Apply 2026_10_26_macro_period_date without touching the rest of the chain.

WHY THIS SCRIPT EXISTS
----------------------
``alembic upgrade head`` must NOT be run against production. Measured live on
2026-10-26: the database sits at ``2026_10_12_weekly`` while the file chain head
is ``2026_10_25_drop_unwritten``, whose parent ``b7f1drop20261018`` is destructive
and unapplied (company_financial_metrics 809,019 rows,
financial_metric_categories 5,653, company_metric_display_settings 8). Walking
the chain to reach our revision would drop those tables as a side effect.

So the DDL lives in the Alembic revision (for history and for a future chain
clean-up) and this runner executes the very same ``apply_to()`` against the
engine directly. The revision is idempotent and guarded — once ``period`` is a
DATE column, both this script and any later ``alembic upgrade`` are a no-op.

USAGE
-----
    python scripts/migrate_macro_period_date.py            # read-only plan
    python scripts/migrate_macro_period_date.py --execute  # the write (operator)

Default mode opens a READ ONLY transaction and changes nothing: it prints the
column type before/after, which rows the migration deletes, and how each
surviving ``period`` string maps to a date + basis. Never run ``--execute``
without reading the plan first.

The alembic_version table is intentionally NOT stamped: the live row still
describes the real schema state (2026_10_12_weekly) and stamping past the
pending destructive revision would hide it.
"""
import argparse
import os
import sys

# backend/ on sys.path when invoked as scripts/migrate_macro_period_date.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util  # noqa: E402
import sqlalchemy as sa  # noqa: E402

from app.core.database import engine  # noqa: E402

_REVISION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "alembic", "versions", "2026_10_26_macro_period_date.py")

_spec = importlib.util.spec_from_file_location("macro_period_date_rev", _REVISION_PATH)
REV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REV)

TABLE = REV.TABLE
REWRITTEN_KEYS = REV.REWRITTEN_KEYS


def _period_type(conn) -> str:
    return conn.execute(sa.text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = 'period'"), {"t": TABLE}).scalar() or "<no table>"


def _print_plan(conn) -> None:
    print(f"Table : {TABLE}")
    applied = REV._already_applied(conn)
    print(f"period: {_period_type(conn)}   "
          f"({'ALREADY APPLIED — nothing to do' if applied else 'needs conversion'})")
    has_basis = bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t "
        "AND column_name = 'basis'"), {"t": TABLE}).scalar())
    print(f"basis : {'present' if has_basis else 'will be added'}")

    total = conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar()
    print(f"Rows  : {total}")

    keys = ", ".join(f"'{k}'" for k in REWRITTEN_KEYS)
    doomed = conn.execute(sa.text(
        f"SELECT indicator_key, period, source, value FROM {TABLE} "
        f"WHERE indicator_key IN ({keys}) ORDER BY indicator_key, period")).fetchall()
    print(f"\n-- DELETE {len(doomed)} rows (re-derivable: the rewritten scraper "
          f"writes the full monthly series) --")
    for r in doomed:
        print(f"   {r.indicator_key:<26} {str(r.period):<18} {r.source:<26} {r.value}")

    keep = conn.execute(sa.text(
        f"SELECT indicator_key, period, source FROM {TABLE} "
        f"WHERE indicator_key NOT IN ({keys}) ORDER BY indicator_key, period")).fetchall()
    print(f"\n-- KEEP {len(keep)} rows; period strings must map to a real date --")
    unmapped = []
    for r in keep:
        mapped = _simulate_map(r.period)
        if mapped is None:
            unmapped.append(r)
            print(f"   {r.indicator_key:<26} {str(r.period):<18} {r.source:<26} *** UNMAPPED -> row deleted ***")
        else:
            print(f"   {r.indicator_key:<26} {str(r.period):<18} {r.source:<26} -> {mapped[0]}  basis={mapped[1]}")

    src = conn.execute(sa.text(
        f"SELECT source, count(*) FROM {TABLE} WHERE indicator_key IN "
        f"('repo_rate','reverse_repo_rate','saibor_3m','saibor_12m') "
        f"GROUP BY source ORDER BY count(*) DESC")).fetchall()
    print("\n-- SAMA source spellings (all collapse to "
          f"'{REV.SOURCE_SAMA}' before the delete) --")
    for s in src:
        print(f"   {s.source!r:<32} {s[1]}")

    print("\nNext: re-run the scraper so the series is repopulated with real dates:")
    print('    python "scripts/SAMA&GaStat.py" --execute')


def _simulate_map(period: str):
    """Mirror the SQL CASE in the revision, so the plan shows the same result."""
    import re
    from datetime import date
    from calendar import monthrange
    p = str(period or "")
    if re.match(r"^\d{4}-\d{2}$", p):
        return date(int(p[:4]), int(p[5:7]), 1), "Monthly"
    m = re.search(r"Q([1-4])", p, re.I)
    if m:
        y = int(p[:4])
        q = int(m.group(1))
        start_month = (q - 1) * 3 + 1
        end_month = start_month + 2
        return date(y, end_month, monthrange(y, end_month)[1]), (
            "TTM" if "TTM" in p.upper() else "Quarterly")
    if re.match(r"^\d{4}", p):
        label = p.upper()
        return date(int(p[:4]), 12, 31), (
            "Annualized" if "ANNUALIZED" in label else "Annual")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true",
                    help="actually run the DDL (default: read-only plan)")
    args = ap.parse_args()

    if not args.execute:
        with engine.connect() as conn:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            _print_plan(conn)
        print("\nDRY RUN — nothing written. Add --execute to apply.")
        return 0

    with engine.begin() as conn:  # single transaction: all or nothing
        if REV._already_applied(conn):
            print(f"{TABLE}.period is already DATE — migration already applied.")
            return 0
        ran = REV.apply_to(conn)
    print(f"Applied {len(ran)} statements.")
    for sql in ran:
        print("   " + sql[:160])
    with engine.connect() as conn:
        print(f"\n{TABLE}.period is now: {_period_type(conn)}")
        print(f"rows remaining: "
              f"{conn.execute(sa.text(f'SELECT count(*) FROM {TABLE}')).scalar()}")
    print("\nNow repopulate: python \"scripts/SAMA&GaStat.py\" --execute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
