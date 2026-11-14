"""
Extract table data from YRI Earnings Outlook images (Figure 1 & 2)
using OCR and save as JSON for the frontend interactive tables.

Usage:
    python extract_yri_tables.py

Requirements:
    pip install pytesseract Pillow pandas
    Also needs Tesseract-OCR installed on the system.

If OCR is not available, edit the JSON files manually:
    frontend/public/yri-earnings/figure1_data.json
    frontend/public/yri-earnings/figure2_data.json
"""

import json
import os
import sys

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Go up from backend/scripts to backend, then to workspace root
WORKSPACE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
IMAGES_DIR = os.path.join(WORKSPACE_DIR, 'frontend', 'public', 'yri-earnings')

def try_ocr_extraction():
    """Attempt OCR extraction using pytesseract."""
    try:
        import pytesseract
        from PIL import Image
        print("[OK] pytesseract and Pillow available")
    except ImportError:
        print("[ERROR] pytesseract or Pillow not installed.")
        print("   Install with: pip install pytesseract Pillow")
        print("   Also install Tesseract-OCR: https://github.com/tesseract-ocr/tesseract")
        return False

    for fig_id in [1, 2]:
        img_path = os.path.join(IMAGES_DIR, f'figure{fig_id}.png')
        if not os.path.exists(img_path):
            print(f"[WARN] Image not found: {img_path}")
            continue

        print(f"\n[INFO] Processing figure{fig_id}.png...")
        img = Image.open(img_path)

        # Use pytesseract with TSV output for structured data
        try:
            raw_text = pytesseract.image_to_string(img, config='--psm 6')
            print(f"   Raw OCR output ({len(raw_text)} chars):")
            lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
            for line in lines[:5]:
                print(f"   | {line}")
            print(f"   ... ({len(lines)} total lines)")
            print(f"\n[WARN] OCR output needs manual verification.")
            print(f"   Please check and edit: figure{fig_id}_data.json")
        except Exception as e:
            print(f"   [ERROR] OCR failed: {e}")

    return True


def validate_json_files():
    """Validate existing JSON files."""
    for fig_id in [1, 2]:
        json_path = os.path.join(IMAGES_DIR, f'figure{fig_id}_data.json')
        if not os.path.exists(json_path):
            print(f"[ERROR] Missing: figure{fig_id}_data.json")
            continue

        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        print(f"\n[OK] figure{fig_id}_data.json: {len(data)} rows")

        if fig_id == 1:
            cols = ['period', 'ae', 'yri_level', 'yri_yoy', 'consensus_level', 'consensus_yoy']
            print(f"   Columns: {', '.join(cols)}")
            years = [r['period'] for r in data if 'Q' not in r['period']]
            quarters = [r['period'] for r in data if 'Q' in r['period']]
            print(f"   Years: {len(years)} ({years[0]} to {years[-1]})")
            print(f"   Quarters: {len(quarters)}")
            estimates = [r for r in data if r['ae'] == 'e']
            print(f"   Estimates (e): {len(estimates)}")
            actuals = [r for r in data if r['ae'] == 'a']
            print(f"   Actuals (a): {len(actuals)}")

        if fig_id == 2:
            cols = ['period', 'ae', 'revenue_growth', 'revenue', 'earnings', 'profit_margin']
            print(f"   Columns: {', '.join(cols)}")
            years = [r['period'] for r in data if 'Q' not in r['period']]
            quarters = [r['period'] for r in data if 'Q' in r['period']]
            print(f"   Years: {len(years)} ({years[0]} to {years[-1]})")
            print(f"   Quarters: {len(quarters)}")
            nulls = sum(1 for r in data if r.get('revenue_growth') is None)
            if nulls:
                print(f"   [WARN] Null revenue_growth: {nulls}")

        # Sample rows
        print(f"   First row: {json.dumps(data[0])}")
        print(f"   Last row:  {json.dumps(data[-1])}")


def main():
    print("=" * 60)
    print("YRI Earnings Outlook — Table Data Extractor")
    print("=" * 60)

    # 1. Check for existing JSON files
    print("\n[INFO] Checking existing JSON data files...")
    validate_json_files()

    # 2. Attempt OCR if requested
    if '--ocr' in sys.argv:
        print("\n" + "=" * 60)
        print("[INFO] Attempting OCR extraction...")
        print("=" * 60)
        try_ocr_extraction()

    print("\n" + "=" * 60)
    print("[INFO] Instructions for updating data:")
    print("=" * 60)
    print("""
When images are updated:
  1. Replace the PNG files in: frontend/public/yri-earnings/
  2. Edit the JSON data files to match the new images:
     - frontend/public/yri-earnings/figure1_data.json
     - frontend/public/yri-earnings/figure2_data.json
  3. Or run: python extract_yri_tables.py --ocr
     (requires pytesseract + Tesseract-OCR installed)
  4. Verify: python extract_yri_tables.py
""")


if __name__ == '__main__':
    main()
