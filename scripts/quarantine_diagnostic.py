"""
Diagnostic script: Inspect quarantine flags and find true data issues across all companies.
Checks:
1. Companies flagged for empty-statement or missing income statement / balance sheet.
2. Companies flagged for corruption / balance sheet identity (Assets != Liabilities + Equity).
3. Companies with scale mismatches (e.g. thousands vs millions).
4. Companies flagged for low f-score.
"""
import sys
import os
from pathlib import Path

# Add backend to path
backend_path = Path(r"d:\Work\LUMIVST\backend")
sys.path.insert(0, str(backend_path))

from app.services.market_universe_service import get_khurafshi_universe_data
from app.services.xbrl_data_service import get_company

def run_diagnostic():
    print("=" * 80)
    print("REBH Forensic & Data Quarantine Diagnostic")
    print("=" * 80)

    universe = get_khurafshi_universe_data()
    print(f"Total Universe Companies: {len(universe)}")

    no_filings = []
    stale_or_incomplete = []
    empty_statement = []
    corruption_identity = []
    low_f_score = []
    other_issues = []

    for item in universe:
        sym = item.get("sym")
        name = item.get("n")
        sec = item.get("sec")
        fresh = item.get("fresh")
        flags = item.get("flags", [])
        bs_ok = item.get("bs_ok")

        if not name and not sec:
            no_filings.append((sym, "No filings/name/sec"))
        elif not fresh:
            stale_or_incomplete.append((sym, name, flags))
        
        if bs_ok is False:
            corruption_identity.append((sym, name))
        
        if "⚑incomplete-source" in flags:
            empty_statement.append((sym, name))

        if "⚑low-f-score" in flags:
            low_f_score.append((sym, name, item.get("f_score")))

    print("\n--- Summary of Categories ---")
    print(f"1. No Filings: {len(no_filings)}")
    print(f"2. Stale or Incomplete (fresh=False): {len(stale_or_incomplete)}")
    print(f"3. Incomplete Source (missing IS or BS): {len(empty_statement)}")
    print(f"4. Corruption / Balance Identity Failure (bs_ok=False): {len(corruption_identity)}")
    print(f"5. Low F-Score (f_score <= 2): {len(low_f_score)}")

    # Deep dive into incomplete source / empty statement
    print("\n" + "=" * 80)
    print("Deep Dive: Incomplete Source / Empty Statement Companies")
    print("=" * 80)
    for sym, name in empty_statement[:15]:
        comp = get_company(sym)
        sections = comp.sections if comp else {}
        std_is = sections.get("standardized_income_statement")
        std_bs = sections.get("standardized_balance_sheet")
        has_is = bool(std_is and std_is.items and std_is.periods)
        has_bs = bool(std_bs and std_bs.items and std_bs.periods)
        is_items_count = len(std_is.items) if std_is else 0
        bs_items_count = len(std_bs.items) if std_bs else 0
        print(f"[{sym}] {name}: has_bs={has_bs} ({bs_items_count} items), has_is={has_is} ({is_items_count} items)")

    # Deep dive into corruption / balance identity
    print("\n" + "=" * 80)
    print("Deep Dive: Balance Sheet Identity / Corruption (A != L + E)")
    print("=" * 80)
    if not corruption_identity:
        print("No companies currently flagged with bs_ok is False.")
    for sym, name in corruption_identity[:10]:
        print(f"[{sym}] {name}")

    # Inspect Low F-Score sample
    print("\n" + "=" * 80)
    print(f"Sample of Low F-Score Companies (Total: {len(low_f_score)})")
    print("=" * 80)
    for sym, name, f_sc in low_f_score[:10]:
        print(f"[{sym}] {name}: F-Score = {f_sc}")

if __name__ == "__main__":
    run_diagnostic()
