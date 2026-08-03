"""
Measure PARAM_MAPPING coverage across companies.

Reports mapped vs unmapped for income_statement / balance_sheet / cash_flow.
Uses resolve_mapping (normalize + token + fuzzy) so results match the parser.

Usage:
  ..\\venv\\Scripts\\python.exe scripts/test_mapping_coverage.py --from-json
  ..\\venv\\Scripts\\python.exe scripts/test_mapping_coverage.py 1010 1301 --missed
  ..\\venv\\Scripts\\python.exe scripts/test_mapping_coverage.py 1301 --reparse
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.xbrl_mapping import resolve_mapping

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "downloads"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
SECTIONS = ("income_statement", "balance_sheet", "cash_flow")


def _has_numeric(item: dict) -> bool:
    return any(isinstance(v, (int, float)) for v in item.get("values", {}).values())


def _count_section(items: list) -> tuple[int, int, list[str]]:
    mapped = unmapped = 0
    missed: list[str] = []
    for item in items:
        if item.get("is_header") and not _has_numeric(item):
            continue
        if not _has_numeric(item) and not item.get("values"):
            continue
        if resolve_mapping(item.get("label", "")):
            mapped += 1
        else:
            unmapped += 1
            missed.append(item.get("label", ""))
    return mapped, unmapped, missed


def analyze_sections(symbol: str, sections: dict) -> dict:
    result = {"symbol": symbol, "sections": {}}
    for sec in SECTIONS:
        raw = sections.get(sec)
        if not raw:
            continue
        mapped, unmapped, missed = _count_section(raw.get("items", []))
        std = sections.get(f"standardized_{sec}", {})
        other_shown = sum(
            1
            for i in std.get("items", [])
            if i.get("is_unmapped") and not i.get("is_header")
        )
        total = mapped + unmapped
        result["sections"][sec] = {
            "mapped": mapped,
            "unmapped": unmapped,
            "total": total,
            "pct": round(100.0 * mapped / total, 1) if total else 0.0,
            "other_shown": other_shown,
            "display_complete": (mapped + other_shown) >= total if total else True,
            "missed_sample": missed[:8],
        }
    return result


def analyze_from_json(symbol: str) -> dict | None:
    path = OUTPUT_DIR / f"{symbol}_financials.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return analyze_sections(symbol, data.get("sections", {}))


def analyze_from_parse(symbol: str) -> dict | None:
    from app.services.xbrl_parser import parse_and_merge_xbrl_files

    en_dir = DATA_DIR / symbol
    ar_dir = DATA_DIR / f"{symbol}_ar"
    files = []
    if en_dir.exists():
        files.extend(list(en_dir.glob("*.xls")) + list(en_dir.glob("*.xlsx")))
    if ar_dir.exists():
        files.extend(list(ar_dir.glob("*.xls")) + list(ar_dir.glob("*.xlsx")))
    if not files:
        return None
    merged = parse_and_merge_xbrl_files(files)
    return analyze_sections(symbol, merged.get("sections", {}))


def list_symbols_from_output() -> list[str]:
    if not OUTPUT_DIR.exists():
        return []
    out = []
    for p in OUTPUT_DIR.glob("*_financials.json"):
        sym = p.name.replace("_financials.json", "")
        if sym.isdigit():
            out.append(sym)
    return sorted(out)


def print_report(results: list[dict], show_missed: bool = False) -> None:
    if not results:
        print("No companies analyzed.")
        return

    agg = {sec: {"mapped": 0, "unmapped": 0, "total": 0} for sec in SECTIONS}
    complete = 0

    print(
        f"{'Symbol':<8} {'Section':<18} {'Mapped':>7} {'Unmapped':>9} "
        f"{'Pct':>7} {'Other':>7} {'Complete':>9}"
    )
    print("-" * 78)
    for r in results:
        all_complete = True
        for sec, stats in r["sections"].items():
            agg[sec]["mapped"] += stats["mapped"]
            agg[sec]["unmapped"] += stats["unmapped"]
            agg[sec]["total"] += stats["total"]
            flag = "YES" if stats["display_complete"] else "NO"
            if not stats["display_complete"]:
                all_complete = False
            print(
                f"{r['symbol']:<8} {sec:<18} {stats['mapped']:>7} {stats['unmapped']:>9} "
                f"{stats['pct']:>6.1f}% {stats['other_shown']:>7} {flag:>9}"
            )
            if show_missed and stats["missed_sample"]:
                for label in stats["missed_sample"]:
                    print(f"           - {label}")
        if all_complete and r["sections"]:
            complete += 1

    print("-" * 78)
    print("AGGREGATE")
    for sec, a in agg.items():
        if not a["total"]:
            continue
        pct = 100.0 * a["mapped"] / a["total"]
        print(f"  {sec:<18} mapped={a['mapped']} unmapped={a['unmapped']} ({pct:.1f}%)")
    print(f"Companies with full display (mapped+Other): {complete}/{len(results)}")
    print("Rule: display completeness = mapped + Other/Unmapped covers all value-bearing raw rows.")


def main():
    parser = argparse.ArgumentParser(description="XBRL mapping coverage report")
    parser.add_argument("symbols", nargs="*", help="Optional company symbols")
    parser.add_argument(
        "--from-json",
        action="store_true",
        help="Analyze existing output/*_financials.json (fast)",
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="Re-parse Excel files (uses latest mapping + Other bucket)",
    )
    parser.add_argument("--missed", action="store_true", help="Print sample unmapped labels")
    args = parser.parse_args()

    symbols = args.symbols or list_symbols_from_output() or ["1010", "1301", "1321"]
    use_parse = args.reparse or (bool(args.symbols) and not args.from_json)

    results = []
    for sym in symbols:
        r = analyze_from_parse(sym) if use_parse else analyze_from_json(sym)
        if r is None and use_parse:
            r = analyze_from_json(sym)
        if r and r["sections"]:
            results.append(r)
        else:
            print(f"[skip] {sym}: no data")

    print_report(results, show_missed=args.missed)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
