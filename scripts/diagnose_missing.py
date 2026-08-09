import sys
from pathlib import Path

# Add backend dir to python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.xbrl_parser import parse_and_merge_xbrl_files

DATA_DIR = Path("data/downloads")

def analyze_missing():
    if not DATA_DIR.exists():
        print(f"Directory not found: {DATA_DIR}")
        return

    symbols = set()
    for item in DATA_DIR.iterdir():
        if item.is_dir():
            name = item.name
            symbol = name[:-3] if name.endswith("_ar") else name
            if symbol.isdigit():
                symbols.add(symbol)
                
    symbols = sorted(list(symbols))
    
    no_xbrl = []
    parse_errors = []
    output_dir = Path("output")
    
    for symbol in symbols:
        # If output JSON already exists, it is successful. Skip it!
        if (output_dir / f"{symbol}_financials.json").exists():
            continue
            
        en_dir = DATA_DIR / symbol
        ar_dir = DATA_DIR / f"{symbol}_ar"
        
        xbrl_files = []
        if en_dir.exists():
            xbrl_files.extend(list(en_dir.glob("*.xls")) + list(en_dir.glob("*.xlsx")))
        if ar_dir.exists():
            xbrl_files.extend(list(ar_dir.glob("*.xls")) + list(ar_dir.glob("*.xlsx")))
            
        xbrl_files = sorted(list(set(xbrl_files)))
        
        if not xbrl_files:
            no_xbrl.append(symbol)
            continue
            
        try:
            merged = parse_and_merge_xbrl_files(xbrl_files)
            if not merged or not merged.get("sections"):
                parse_errors.append((symbol, "Empty sections result"))
        except Exception as e:
            parse_errors.append((symbol, str(e)))

    print(f"\n--- Total companies: {len(symbols)} ---")
    print(f"❌ Companies with NO XBRL files at all (only PDFs) ({len(no_xbrl)}):")
    print(", ".join(no_xbrl))
    print(f"\n💥 Companies that failed parsing due to errors ({len(parse_errors)}):")
    for sym, err in parse_errors:
        print(f"  - Symbol {sym}: {err}")

if __name__ == "__main__":
    analyze_missing()
