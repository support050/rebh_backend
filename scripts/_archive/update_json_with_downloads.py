import json
import os
import sys

def update_json_with_downloads(symbol):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.normpath(os.path.join(script_dir, "..", "data", "scrape_financial_reports.json"))
    downloads_dir = os.path.normpath(os.path.join(script_dir, "..", "data", "downloads", symbol))
    
    if not os.path.exists(json_path):
        print(f"❌ JSON file not found: {json_path}")
        return
        
    if not os.path.exists(downloads_dir):
        print(f"❌ Downloads dir not found: {downloads_dir}")
        return

    print(f"Listing Dir content for: {downloads_dir}")
    try:
        print(os.listdir(downloads_dir))
    except Exception as e:
        print(f"Error listing dir: {e}")

    print(f"📂 Reading JSON from {json_path}...")
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    updated_count = 0
    
    # Iterate over sections and items
    for section, items in data.items():
        print(f"Processing section: {section}")
        for item in items:
            url = item.get('url')
            if not url: continue
            
            # Reconstruct filename logic used in scraper
            period = item.get('period', '')
            year = item.get('year', '')
            f_type = item.get('file_type', 'pdf')
            
            # Determine base extension logic
            base_ext = 'pdf'
            is_excel = False
            if f_type == 'excel' or '.xls' in (url or '').lower(): 
                base_ext = 'xlsx'
                is_excel = True
            elif '.pdf' in (url or '').lower(): 
                base_ext = 'pdf'
            
            safe_period = period.replace(" ", "_").replace("/", "-")
            safe_section = section.replace(" ", "_").replace("/", "-")
            
            # Try primary extension
            filename = f"{year}_{safe_section}_{safe_period}.{base_ext}"
            file_path = os.path.join(downloads_dir, filename)
            
            # If Excel, check for alternative extension .xls if .xlsx missing
            final_path = None
            if os.path.exists(file_path):
                final_path = file_path
            elif is_excel:
                # Try .xls
                alt_filename = f"{year}_{safe_section}_{safe_period}.xls"
                alt_path = os.path.join(downloads_dir, alt_filename)
                if os.path.exists(alt_path):
                    final_path = alt_path
                    filename = alt_filename # Update filename for log

            # DEBUG
            print(f"Checking: {file_path}")
            if final_path and final_path != file_path:
                print(f"  ↪️ Found alternative: {final_path}")
            
            if final_path:
                before = item.get('local_path')
                item['local_path'] = final_path
                if before != final_path:
                    print(f"  ✅ Linked {filename}")
                    updated_count += 1
            else:
               pass
               
    if updated_count > 0:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print(f"💾 Updated JSON with {updated_count} local paths.")
    else:
        print("⚠️ No matching files found to update.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', type=str, required=True, help="Company symbol (e.g., 4322)")
    args = parser.parse_args()
    
    update_json_with_downloads(args.symbol)
