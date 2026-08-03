"""
generate_all_companies_json.py
==============================
Loops through all company folders in data/downloads/, parses both English
and Arabic XBRL files for all years, merges them, and saves the output
to backend/output/[SYMBOL]_financials.json so they can be consumed by the frontend.
"""

import sys
import os
from pathlib import Path

# Add backend dir to python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.xbrl_parser import parse_and_merge_xbrl_files
from app.services.xbrl_data_service import save_company

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "downloads"

def main():
    if not DATA_DIR.exists():
        print(f"Directory not found: {DATA_DIR}")
        return

    # Find all company symbols (numeric folder names)
    symbols = set()
    for item in DATA_DIR.iterdir():
        if item.is_dir():
            name = item.name
            if name.endswith("_ar"):
                symbol = name[:-3]
            else:
                symbol = name
            if symbol.isdigit():
                symbols.add(symbol)
                
    symbols = sorted(list(symbols))
    print(f"Found {len(symbols)} companies to process.")

    success_count = 0
    for symbol in symbols:
        print(f"\nProcessing symbol: {symbol}...")
        
        # Paths to English and Arabic folders
        en_dir = DATA_DIR / symbol
        ar_dir = DATA_DIR / f"{symbol}_ar"
        
        xbrl_files = []
        if en_dir.exists():
            xbrl_files.extend(list(en_dir.glob("*.xls")) + list(en_dir.glob("*.xlsx")))
        if ar_dir.exists():
            xbrl_files.extend(list(ar_dir.glob("*.xls")) + list(ar_dir.glob("*.xlsx")))
            
        xbrl_files = sorted(list(set(xbrl_files)), key=lambda p: p.name)
        
        if not xbrl_files:
            print(f"  No XBRL files found for {symbol}. Skipping.")
            continue
            
        print(f"  Found {len(xbrl_files)} files (English + Arabic). Parsing...")
        try:
            merged = parse_and_merge_xbrl_files(xbrl_files)
            if not merged or not merged.get("sections"):
                print(f"  Parsing returned empty result for {symbol}.")
                continue
                
            output_path = save_company(symbol, merged)
            print(f"  Saved to: {output_path}")
            success_count += 1
        except Exception as e:
            print(f"  Error processing {symbol}: {e}")
            
    print(f"\nCompleted! Successfully processed {success_count}/{len(symbols)} companies.")

if __name__ == "__main__":
    main()
