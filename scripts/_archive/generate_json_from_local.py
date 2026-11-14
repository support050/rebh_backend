import os
import sys
import json
import re

# Categories Mapping based on existing file patterns
# Pattern: {Year}_{Section}_{Period}.{ext}
# Sections seen: "Financial_Statements", "XBRL", "Board_Report", "ESG_Report"

SECTION_MAP = {
    "Financial_Statements": "Financial Statements",
    "XBRL": "XBRL",
    "Board_Report": "Board Report",
    "ESG_Report": "ESG Report"
}

AR_PERIOD_MAP = {
    "سنوي": "Annual",
    "الربع_الأول": "Q1",
    "الربع_الثاني": "Q2",
    "الربع_الثالث": "Q3",
    "الربع_الرابع": "Q4",
    "تقرير_مجلس_الإدارة": "Annual", # Board reports are typically annual
    "Q1": "Q1", "Q2": "Q2", "Q3": "Q3", "Q4": "Q4", "Annual": "Annual"
}

def generate_json_from_local(symbol, folder_suffix=""):
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    downloads_dir = os.path.join(base_dir, "downloads", f"{symbol}{folder_suffix}")
    output_path = os.path.join(base_dir, "scrape_financial_reports.json")

    if not os.path.exists(downloads_dir):
        print(f"❌ Error: Directory not found: {downloads_dir}")
        return

    json_data = {
        "Financial Statements": [],
        "XBRL": [],
        "Board Report": [],
        "ESG Report": []
    }

    files = os.listdir(downloads_dir)
    print(f"📂 Found {len(files)} files in {downloads_dir}")

    for filename in files:
        # Regex to parse filenames like: 2024_Financial_Statements_Annual.pdf
        # or 2024_XBRL_Q1.xls
        # Note: Board reports might be 2021_Board_Report_Board_Report.pdf (Period=Board_Report?)
        
        # We try to split by first underscore for year, and last underscore for period
        try:
            name_no_ext, ext = os.path.splitext(filename)
            ext = ext.replace(".", "").lower()
            if ext == 'xlsx': ext = 'excel' # Map to file_type 'excel'
            if ext == 'xls': ext = 'excel'
            
            parts = name_no_ext.split('_')
            
            if len(parts) < 3:
                print(f"⚠️ Skipping likely invalid file: {filename}")
                continue

            year = parts[0]
            
            # Reconstruct Section and Period
            # This is tricky because Section can have underscores (e.g. Board_Report)
            # Known suffixes (Period): Annual, Q1, Q2, Q3, Q4, Board_Report (sometimes period is same as section)
            
            # Simple heuristic:
            # Check for known sections
            section_key = None
            period = None
            
            # Try to match known sections in the filename
            for key in SECTION_MAP.keys():
                if f"_{key}_" in name_no_ext:
                    section_key = key
                    break
            
            if not section_key:
                # Fallback or specific handling
                if "Financial_Statements" in name_no_ext: section_key = "Financial_Statements"
                elif "XBRL" in name_no_ext: section_key = "XBRL"
                elif "Board_Report" in name_no_ext: section_key = "Board_Report"
                elif "ESG_Report" in name_no_ext: section_key = "ESG_Report"

            if not section_key:
                 print(f"⚠️ Could not identify section for: {filename}")
                 continue

            # Extract period: It is usually the part AFTER the section
            # e.g. 2024_XBRL_Q1 -> Q1
            # e.g. 2024_Financial_Statements_Annual -> Annual
            # e.g. 2024_Board_Report_Board_Report -> Board_Report (We normalize this to Annual usually for API, or keep as is if API handles it)
            
            prefix = f"{year}_{section_key}_"
            if name_no_ext.startswith(prefix):
                period = name_no_ext[len(prefix):]
            else:
                 # Fallback split
                 period = parts[-1]

            # Normalize Period for API (Handle Arabic or English)
            api_period = AR_PERIOD_MAP.get(period, period)
            
            # Special case for Board Report which might repeat the section name as period
            if period == 'Board_Report' or api_period == 'Board_Report':
                api_period = 'Annual'
            
            # Additional fallback: Check if period contains known English keys
            if api_period not in ['Annual', 'Q1', 'Q2', 'Q3', 'Q4']:
                 for k, v in AR_PERIOD_MAP.items():
                     if k in period:
                         api_period = v
                         break
            
            category_name = SECTION_MAP.get(section_key, "Other")
            
            # Construct Entry
            entry = {
                "url": f"http://internal/{filename}", # Dummy URL to satisfy validity checks
                "local_path": os.path.join(downloads_dir, filename),
                "file_type": "excel" if ext == "excel" else "pdf",
                "period": api_period,
                "year": year,
                "published_date": ""
            }
            
            if category_name in json_data:
                json_data[category_name].append(entry)
                
        except Exception as e:
            print(f"❌ Error parsing {filename}: {e}")

    # Write to JSON
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=4)
    
    print(f"✅ Generated scrape_financial_reports.json for {symbol} with {sum(len(v) for v in json_data.values())} entries.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Generate JSON from local download folder')
    parser.add_argument('symbol', type=str, help='Company Symbol', nargs='?') # Positional for backward compatibility
    parser.add_argument('--symbol', dest='flag_symbol', type=str, help='Company Symbol')
    parser.add_argument('suffix', type=str, help='Folder suffix (e.g. _ar)', nargs='?') # Positional for backward compatibility
    parser.add_argument('--folder-suffix', type=str, help='Folder suffix (e.g. _ar)')
    
    args = parser.parse_args()
    
    final_symbol = args.flag_symbol or args.symbol
    final_suffix = args.folder_suffix or args.suffix or ""
    
    if not final_symbol:
        print("⚠️  Usage: python scripts/generate_json_from_local.py <SYMBOL> [--folder-suffix _ar]")
        sys.exit(1)
        
    generate_json_from_local(final_symbol, final_suffix)
