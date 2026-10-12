"""
Repair the industry_group gap for symbols new.csv never covered (P1 / sector gap).

Background
----------
`daily_market_update.load_full_hierarchy_mapping()` builds the hierarchy map
from backend/new.csv. Symbols absent from that file get industry_group = NULL on
every price row, so they are invisible to every Group-RS aggregation
(industry_group_history, rs_daily_v2.industry_group_rs_rating).

On 2026-10-05 exactly 6 of 272 symbols had no industry_group:
    1324 SALEH ALRASHED, 2288 NOFOTH, 4148 ALWASAIL INDUSTRIAL,
    4328 LADUN, 6022 ARMAH, 7205 DBS

Classification used here comes from two INDEPENDENT sources that agree:
  * Rebh_Market_Breadth_Package/api_samples/stocks.json (group per symbol)
  * the pipeline's own history in `prices` — 1324 / 2288 / 4148 already have
    rows where the scraper DID return a group (Materials / Food & Beverages /
    Capital Goods), identical to stocks.json.
Sector is derived from new.csv's own convention, which is strictly 1:1
(Industry Group -> Sector). Industry / Sub-Industry are NOT inventable from
these sources, so they stay empty rather than being fabricated.

What it does (idempotent):
  1. appends the missing symbols to backend/new.csv (deduplicating identical
     duplicate rows it finds along the way),
  2. backfills prices.industry_group / prices.sector for those symbols on every
     date where the value is NULL, empty or the scraper's 'Unknown' placeholder,
  3. never touches any other symbol and never changes a good existing group.

Usage
    python scripts/repair_industry_group_gap.py            # dry run
    python scripts/repair_industry_group_gap.py --execute
"""
import sys
import os
import argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text
from app.core.database import SessionLocal

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(BACKEND, "new.csv")

# symbol -> (company, industry_group, sector)
FIXES = {
    "1324": ("SALEH ALRASHED", "Materials", "Materials"),
    # 1325 appeared 2026-10-06: one price row, industry_group/sector NULL, absent from
    # new.csv AND from api_samples/stocks.json (listed after that snapshot). It is the
    # single symbol that bricks the breadth engine -- build_symbols_master leaves
    # industry_group NA -> is_reit becomes pd.NA -> np.where raises
    # "boolean value of NA is ambiguous". Classification is the peer convention, not a
    # guess: every steel/metal name in prices is Materials/Materials
    # (1304 ALYAMAMAH STEEL, 1320 Saudi Steel Pipe, 2220 National Metal Mfg).
    "1325": ("WATANI STEEL", "Materials", "Materials"),
    "2288": ("NOFOTH", "Food & Beverages", "Consumer Staples"),
    "4148": ("ALWASAIL INDUSTRIAL", "Capital Goods", "Industrials"),
    "4328": ("LADUN", "Capital Goods", "Industrials"),
    "6022": ("ARMAH", "Consumer Services", "Consumer Discretionary"),
    "7205": ("DBS", "Software & Services", "Information Technology"),
}

# the new.csv convention: Industry Group -> Sector is 1:1 in the file itself
GROUP_TO_SECTOR = {
    "Materials": "Materials",
    "Food & Beverages": "Consumer Staples",
    "Capital Goods": "Industrials",
    "Consumer Services": "Consumer Discretionary",
    "Software & Services": "Information Technology",
}

COLUMNS = ["Symbol", "Industry Group", "Sector", "Industry", "Sub-Industry"]


def repair_csv(execute):
    df = pd.read_csv(CSV_PATH, dtype=str)
    before = len(df)

    # 1) collapse exact duplicate rows (e.g. 4322 appeared twice; the last one
    #    silently won when the pipeline built its dict)
    dupe_full = df[df.duplicated(keep="first")]
    if not dupe_full.empty:
        print(f"  duplicate rows in new.csv: {sorted(set(dupe_full['Symbol']))}")

    # 2) append what is missing
    missing = [s for s in FIXES if s not in set(df["Symbol"])]
    rows = []
    for sym in missing:
        company, group, sector = FIXES[sym]
        assert GROUP_TO_SECTOR[group] == sector, f"sector mismatch for {group}"
        rows.append({"Symbol": sym, "Industry Group": group, "Sector": sector,
                     "Industry": None, "Sub-Industry": None})
        print(f"  + {sym} {company:22s} -> {group} / {sector}")

    if not execute:
        print(f"  DRY RUN: {len(missing)} symbol(s) would be appended, "
              f"{len(dupe_full)} duplicate row(s) would collapse "
              f"({before} -> {before + len(missing)} rows)")
        return len(missing)

    out = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    before_n = len(out)
    out = out.drop_duplicates(subset=COLUMNS, keep="first").reset_index(drop=True)
    print(f"  new.csv: {before} -> {len(out)} rows "
          f"(added {len(missing)}, collapsed {before_n - len(out)} duplicate)")
    out.to_csv(CSV_PATH, index=False)
    return len(missing)


def repair_db(execute):
    db = SessionLocal()
    try:
        syms = list(FIXES)
        for sym, (_c, group, sector) in FIXES.items():
            row = db.execute(text("""
                SELECT count(*) FILTER (WHERE industry_group IS NULL
                                          OR btrim(industry_group) = ''
                                          OR industry_group = 'Unknown'),
                       count(*), min(date), max(date)
                FROM prices WHERE symbol = :s
            """), {"s": sym}).fetchone()
            print(f"  {sym} {group:20s} rows needing group: {row[0]:>4d} / {row[1]}"
                  f"   ({row[2]} .. {row[3]})")

        total = db.execute(text("""
            SELECT count(*) FROM prices WHERE symbol = ANY(:s)
              AND (industry_group IS NULL OR btrim(industry_group) = ''
                   OR industry_group = 'Unknown')
        """), {"s": syms}).scalar()
        print(f"\n  price rows to repair in total: {total}")
        if not execute:
            print("  DRY RUN — nothing was written.")
            return total

        # one statement per symbol so a bad value can never hit the others
        written = 0
        for sym, (_c, group, sector) in FIXES.items():
            res = db.execute(text("""
                UPDATE prices
                   SET industry_group = :g,
                       sector = COALESCE(NULLIF(sector, ''), :sec, sector)
                 WHERE symbol = :s
                   AND (industry_group IS NULL OR btrim(industry_group) = ''
                        OR industry_group = 'Unknown')
            """), {"g": group, "sec": sector, "s": sym})
            written += res.rowcount
        db.commit()
        print(f"  ✅ updated {written} price row(s)")

        left = db.execute(text("""
            SELECT count(*) FROM prices WHERE symbol = ANY(:s)
              AND (industry_group IS NULL OR btrim(industry_group) = ''
                   OR industry_group = 'Unknown')
        """), {"s": syms}).scalar()
        print(f"  rows still without a group: {left}")
        return written
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description="Repair industry_group coverage gaps")
    ap.add_argument("--execute", action="store_true",
                    help="write changes (default is a dry run)")
    args = ap.parse_args()

    print(f"mode: {'EXECUTE' if args.execute else 'DRY RUN'}\n")
    print("── [1] new.csv mapping file ──")
    repair_csv(args.execute)
    print("\n── [2] prices backfill ──")
    repair_db(args.execute)
    print("\n── [3] post-check: universe coverage ──")
    df = pd.read_csv(CSV_PATH, dtype=str)
    groups = Counter(df["Industry Group"].dropna())
    print(f"  new.csv rows={len(df)}, symbols={df['Symbol'].nunique()}, "
          f"groups={len(groups)}")
    blank = df[df["Industry Group"].isna() | (df["Industry Group"].str.strip() == "")]
    print(f"  rows still without an Industry Group: {len(blank)}")


if __name__ == "__main__":
    main()
