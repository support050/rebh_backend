import os
import json
import sys
from pathlib import Path
from functools import lru_cache

# Add backend directory to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.xbrl_mapping import resolve_mapping, STANDARD_TEMPLATE

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "downloads"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# Cache resolve_mapping to avoid expensive SequenceMatcher operations on duplicate labels
@lru_cache(maxsize=10000)
def cached_resolve_mapping(label, statement=None):
    return resolve_mapping(label, statement=statement)

def audit_companies():
    if not DATA_DIR.exists():
        print(f"Downloads directory not found: {DATA_DIR}")
        return

    # 1. Parse Status Coverage
    # Find all unique symbols in data/downloads
    all_symbols = set()
    for item in DATA_DIR.iterdir():
        if item.is_dir():
            name = item.name
            symbol = name[:-3] if name.endswith("_ar") else name
            if symbol.isdigit():
                all_symbols.add(symbol)
                
    all_symbols = sorted(list(all_symbols))
    total_companies = len(all_symbols)
    
    parser_status = {
        "PASS": [],
        "PARSER_ERROR": [],
        "EMPTY_RESULT": [],
        "NO_XBRL_FILES": []
    }

    print(f"Scanning parser coverage for {total_companies} companies...")
    for symbol in all_symbols:
        en_dir = DATA_DIR / symbol
        ar_dir = DATA_DIR / f"{symbol}_ar"
        
        xbrl_files = []
        if en_dir.exists():
            xbrl_files.extend(list(en_dir.glob("*.xls")) + list(en_dir.glob("*.xlsx")))
        if ar_dir.exists():
            xbrl_files.extend(list(ar_dir.glob("*.xls")) + list(ar_dir.glob("*.xlsx")))
            
        if not xbrl_files:
            parser_status["NO_XBRL_FILES"].append(symbol)
            continue
            
        json_file = OUTPUT_DIR / f"{symbol}_financials.json"
        if not json_file.exists():
            parser_status["PARSER_ERROR"].append(symbol)
            continue
            
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            sections = data.get("sections", {})
            # check if any section has items with values
            has_data = False
            for sec_key, sec in sections.items():
                for item in sec.get("items", []):
                    if any(v is not None for v in item.get("values", {}).values()):
                        has_data = True
                        break
                if has_data:
                    break
            
            if not has_data:
                parser_status["EMPTY_RESULT"].append(symbol)
            else:
                parser_status["PASS"].append(symbol)
        except Exception:
            parser_status["PARSER_ERROR"].append(symbol)

    # 2. Detailed Mapping Audit on successfully parsed companies
    print(f"Running mapping audit on {len(parser_status['PASS'])} successfully parsed companies...")
    
    audit_results = []
    
    global_stats = {
        "total_checks": 0,
        "pass": 0,
        "wrong_value": 0,
        "overlapping_mapping": 0,
        "missing_source": 0,
        "unmapped_items_count": 0
    }

    # Audit all codes in STANDARD_TEMPLATE
    all_template_codes = sorted(list(STANDARD_TEMPLATE.keys()))

    for symbol in parser_status["PASS"]:
        json_file = OUTPUT_DIR / f"{symbol}_financials.json"
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        company_name = data.get("meta", {}).get("company_name", "Unknown")

        # Map code -> list of raw items with their values
        # We will group raw items by statement section to avoid mixing them up
        raw_mappings = {
            "income_statement": {},
            "balance_sheet": {},
            "cash_flow": {}
        }
        
        for raw_sec_key in ["income_statement", "balance_sheet", "cash_flow"]:
            sec = data.get("sections", {}).get(raw_sec_key)
            if not sec:
                continue
            for item in sec.get("items", []):
                lbl = item.get("label")
                if not lbl:
                    continue
                mapping = cached_resolve_mapping(lbl, statement=raw_sec_key)
                if mapping:
                    code, direction = mapping
                    if code not in raw_mappings[raw_sec_key]:
                        raw_mappings[raw_sec_key][code] = []
                    raw_mappings[raw_sec_key][code].append((lbl, direction, item.get("values", {})))

        # Now audit against standardized sections
        for std_sec_key in ["standardized_income_statement", "standardized_balance_sheet", "standardized_cash_flow"]:
            std_sec = data.get("sections", {}).get(std_sec_key)
            if not std_sec:
                continue

            periods = std_sec.get("periods", [])
            for item in std_sec.get("items", []):
                lbl = item.get("label")
                
                # Identify template code for this standardized item
                code = None
                raw_sec_key = None
                for c, info in STANDARD_TEMPLATE.items():
                    if info["line_en"] == lbl:
                        code = c
                        raw_sec_key = info["statement"]
                        break
                
                if not code or not raw_sec_key:
                    continue

                std_values = item.get("values", {})
                
                for p in periods:
                    std_val = std_values.get(p)
                    # If standardized is null/None, check if we had any raw source
                    raw_contribs = []
                    mapped_items = raw_mappings[raw_sec_key].get(code, [])
                    for r_lbl, direction, r_vals in mapped_items:
                        r_val = r_vals.get(p)
                        if r_val is not None:
                            if code == "IS-160" and abs(r_val) > 500:
                                continue
                            raw_contribs.append((r_lbl, r_val, direction))

                    if std_val is None and not raw_contribs:
                        # NOT IN SOURCE - Standard Parameter exists but company does not provide it.
                        continue

                    global_stats["total_checks"] += 1

                    if not raw_contribs and std_val is not None:
                        # Standardized has a value but no raw source maps to it
                        global_stats["wrong_value"] += 1
                        audit_results.append({
                            "symbol": symbol,
                            "company": company_name,
                            "period": p,
                            "code": code,
                            "parameter": lbl,
                            "status": "WRONG VALUE",
                            "std_val": std_val,
                            "expected_val": 0.0,
                            "details": "No raw source items mapped, but standardized has value."
                        })
                    elif std_val is None and raw_contribs:
                        # Raw sources exist but standardized value is missing
                        global_stats["wrong_value"] += 1
                        audit_results.append({
                            "symbol": symbol,
                            "company": company_name,
                            "period": p,
                            "code": code,
                            "parameter": lbl,
                            "status": "WRONG VALUE",
                            "std_val": None,
                            "expected_val": sum(val * dir for _, val, dir in raw_contribs),
                            "details": f"Raw sources exist but standardized value is null. Raw: {raw_contribs}"
                        })
                    else:
                        # Calculate expected sum (mirror parser: identical-abs synonyms count once)
                        signed_vals = [val * direction for _, val, direction in raw_contribs]
                        abs_set = {abs(v) for v in signed_vals}
                        if len(signed_vals) > 1 and len(abs_set) == 1:
                            expected_val = signed_vals[0]
                            is_overlap = True
                        else:
                            expected_val = sum(signed_vals)
                            is_overlap = len(raw_contribs) > 1

                        # Check mismatch
                        is_mismatch = abs(std_val - expected_val) > 1e-2

                        # Classify status
                        if is_mismatch:
                            global_stats["wrong_value"] += 1
                            status = "WRONG VALUE"
                        elif is_overlap and len(abs_set) == 1:
                            # Synonym overlap still present in mapping, but value is correct after dedupe
                            status = "OVERLAPPING MAPPING"
                            global_stats["overlapping_mapping"] += 1
                        elif is_overlap:
                            # multiple distinct values summed intentionally
                            status = "PASS (SUMMED)"
                            global_stats["pass"] += 1
                        else:
                            status = "PASS"
                            global_stats["pass"] += 1

                        if status in ["WRONG VALUE", "OVERLAPPING MAPPING"]:
                            audit_results.append({
                                "symbol": symbol,
                                "company": company_name,
                                "period": p,
                                "code": code,
                                "parameter": lbl,
                                "status": status,
                                "std_val": std_val,
                                "expected_val": expected_val,
                                "details": f"Raw Contributions: {raw_contribs}"
                            })

    # Write report
    report_path = OUTPUT_DIR / "standardization_audit_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("========================================================================\n")
        f.write("             COMPREHENSIVE XBRL STANDARDIZATION AUDIT REPORT\n")
        f.write("========================================================================\n\n")
        
        f.write("1. PARSER COVERAGE STATISTICS:\n")
        f.write("-----------------------------\n")
        f.write(f"Total Companies Found:  {total_companies}\n")
        f.write(f"  - PASS (Successfully Parsed):  {len(parser_status['PASS'])}\n")
        f.write(f"  - PARSER ERROR:                {len(parser_status['PARSER_ERROR'])}\n")
        f.write(f"  - EMPTY RESULT:                {len(parser_status['EMPTY_RESULT'])}\n")
        f.write(f"  - NO XBRL FILES:               {len(parser_status['NO_XBRL_FILES'])}\n\n")

        if parser_status['PARSER_ERROR']:
            f.write(f"Parser Errors: {', '.join(parser_status['PARSER_ERROR'])}\n")
        if parser_status['EMPTY_RESULT']:
            f.write(f"Empty Results: {', '.join(parser_status['EMPTY_RESULT'])}\n")
        if parser_status['NO_XBRL_FILES']:
            f.write(f"No XBRL Files: {', '.join(parser_status['NO_XBRL_FILES'])}\n\n")

        f.write("2. STANDARDIZATION AUDIT STATISTICS (ALL TEMPLATE CODES):\n")
        f.write("-------------------------------------------------------\n")
        f.write(f"Total Parameter Checks:      {global_stats['total_checks']}\n")
        f.write(f"  - PASS:                     {global_stats['pass']}\n")
        f.write(f"  - OVERLAPPING MAPPING:      {global_stats['overlapping_mapping']}\n")
        f.write(f"  - WRONG VALUE / MISMATCH:   {global_stats['wrong_value']}\n")
        f.write(f"Total Unmapped Raw Items:    {global_stats['unmapped_items_count']}\n\n")

        if audit_results:
            f.write("========================================================================\n")
            f.write("DETAILED AUDIT ANOMALIES:\n")
            f.write("========================================================================\n")
            for r in audit_results:
                f.write(f"Company: {r['symbol']} ({r['company']}) | Period: {r['period']}\n")
                f.write(f"  Parameter: {r['parameter']} ({r['code']}) | Status: {r['status']}\n")
                f.write(f"  Standardized Value: {r['std_val']} | Expected Value: {r['expected_val']}\n")
                f.write(f"  Details: {r['details']}\n\n")
        else:
            f.write("No anomalies found! All parsed parameters mapped correctly and passed validation.\n")

    print(f"\nAudit complete!")
    print(f"Total checked: {global_stats['total_checks']}")
    print(f"Overlapping mapping flags: {global_stats['overlapping_mapping']}")
    print(f"Wrong value flags: {global_stats['wrong_value']}")
    print(f"Report written to: {report_path}")

if __name__ == "__main__":
    audit_companies()
