"""Backfill substantial_shareholders.symbol / normalized_symbol from company_name.

DRY RUN BY DEFAULT — prints exactly what it would write, and what it refuses to
write, and only touches the DB with --execute.

    python scripts/backfill_shareholder_symbols.py            # inspect
    python scripts/backfill_shareholder_symbols.py --execute  # write

Matching is the conservative one in app/services/company_symbol_service.py:
exact normalised name, unambiguous across all sources, no fuzzy guessing. Rows
whose company cannot be confirmed stay NULL and are listed at the end.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
from collections import defaultdict

from sqlalchemy import text

from app.core.database import SessionLocal
from app.services.company_symbol_service import (
    build_lookup, resolve_name, normalise_symbol, ambiguous_keys,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--symbols", action="store_true",
                    help="also fill normalized_symbol for tables that already have symbol")
    args = ap.parse_args()

    lookup, ambiguous = build_lookup()
    print(f"lookup: {len(lookup)} unambiguous name keys, {len(ambiguous)} ambiguous keys rejected")

    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT company_name, count(*) AS n
            FROM substantial_shareholders
            WHERE company_name IS NOT NULL
            GROUP BY company_name ORDER BY count(*) DESC, company_name
        """)).fetchall()

        total_rows = sum(r[1] for r in rows)
        print(f"substantial_shareholders: {len(rows)} distinct company_name / {total_rows} rows\n")

        plan, unmatched = [], []
        for name, n in rows:
            sym = resolve_name(name)
            (plan if sym else unmatched).append((name, n, sym))

        hit_rows = sum(n for _, n, _ in plan)
        print(f"RESOLVED   {len(plan):>4} names / {hit_rows:>4} rows")
        for name, n, sym in sorted(plan, key=lambda x: x[2])[:40]:
            print(f"    {sym:>6}  <- {name}  ({n} rows)")
        if len(plan) > 40:
            print(f"    ... {len(plan) - 40} more")

        print(f"\nUNRESOLVED {len(unmatched):>4} names / {total_rows - hit_rows:>4} rows  "
              f"(left NULL, never guessed)")
        for name, n, _ in unmatched[:60]:
            print(f"    {n:>3}  {name}")
        if len(unmatched) > 60:
            print(f"    ... {len(unmatched) - 60} more")

        if ambiguous:
            print("\nAMBIGUOUS name keys (claimed by >1 symbol) — refused:")
            for k in sorted(ambiguous)[:20]:
                print("    ", k)

        # Reverse check: one symbol claimed by several DIFFERENT shareholder names is
        # the classic false-positive signature (a fund collapsed onto its parent, etc.).
        by_sym = defaultdict(set)
        for name, n, sym in plan:
            by_sym[sym].add(name)
        shared = {s: v for s, v in by_sym.items() if len(v) > 1}
        print(f"\nREVERSE CHECK: {len(shared)} symbols claimed by more than one name")
        for s in sorted(shared):
            print(f"    {s}: {sorted(shared[s])}")

        if not args.execute:
            print("\nDRY RUN — nothing written. Re-run with --execute.")
            return

        written = 0
        for name, n, sym in plan:
            res = db.execute(text("""
                UPDATE substantial_shareholders
                   SET symbol = :sym, normalized_symbol = :nsym
                 WHERE company_name = :name
                   AND (symbol IS NULL OR symbol <> :sym)
            """), {"sym": sym, "nsym": normalise_symbol(sym), "name": name})
            written += res.rowcount
        db.commit()
        print(f"\nWROTE {written} rows.")
        left = db.execute(text(
            "SELECT count(*) FROM substantial_shareholders WHERE symbol IS NULL")).scalar()
        print(f"remaining NULL: {left}")

        if args.symbols:
            # These four tables already carry a correct symbol; normalise_symbol is an
            # identity for 4-digit TASI codes and only caps anything else. Nothing reads
            # the column today, so this is opt-in, not part of the shareholder fix.
            for tbl in ("net_short_positions", "foreign_headrooms",
                        "share_buybacks", "sbl_positions"):
                canonical = ("CASE WHEN symbol ~ '^[0-9]+$' THEN symbol ELSE upper(symbol) END")
                r = db.execute(text(f"""
                    UPDATE {tbl} SET normalized_symbol = {canonical}
                    WHERE symbol IS NOT NULL
                      AND (normalized_symbol IS NULL
                           OR normalized_symbol <> {canonical})
                """))
                print(f"normalized_symbol refreshed on {tbl}: {r.rowcount} rows")
            db.commit()


if __name__ == "__main__":
    main()
