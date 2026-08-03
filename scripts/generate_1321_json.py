"""
Generate JSON for company 1321 from local XBRL files.
Saves to output/1321_financials.json — the file that the API reads from.
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.xbrl_parser import parse_and_merge_xbrl_files
from app.services.xbrl_data_service import save_company

SYMBOL = "1321"
BASE_DIR_EN = Path(__file__).resolve().parent.parent / "data" / "downloads" / SYMBOL
BASE_DIR_AR = Path(__file__).resolve().parent.parent / "data" / "downloads" / f"{SYMBOL}_ar"

xbrl_files = []
for directory in (BASE_DIR_EN, BASE_DIR_AR):
    if directory.exists():
        xbrl_files.extend(list(directory.glob("*.xls")) + list(directory.glob("*.xlsx")))

xbrl_files = sorted(list(set(xbrl_files)), key=lambda p: p.name)

print(f"Found {len(xbrl_files)} XBRL files for {SYMBOL} (English + Arabic)")
merged = parse_and_merge_xbrl_files(xbrl_files)

sections = merged.get("sections", {})
for sec_name, sec_data in sections.items():
    print(f"  {sec_name}: {len(sec_data['items'])} items, {len(sec_data['periods'])} periods")

output_path = save_company(SYMBOL, merged)
print(f"\nSaved to: {output_path}")
