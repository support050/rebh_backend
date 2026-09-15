import re
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.core.database import SessionLocal
from app.models.static_stock_info import StaticStockInfo
from sqlalchemy.dialects.postgresql import insert

def parse_marginable_md(file_path: Path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    entries = []
    # Match markdown table row like: | 2030 | Saudi Arabia Refineries Co. | 50% |
    row_pattern = re.compile(r'\|\s*(\d{4})\s*\|.*?\|\s*(\d+(?:\.\d+)?)\s*%\s*\|')
    
    for line in lines:
        match = row_pattern.search(line)
        if match:
            symbol = match.group(1).strip()
            pct_str = match.group(2).strip()
            marginable_percent = float(pct_str)
            entries.append((symbol, marginable_percent))
            
    return entries

def update_marginable():
    md_path = BASE_DIR / "scripts" / "Marginable.md"
    if not md_path.exists():
        print(f"Error: {md_path} does not exist.")
        return
    
    entries = parse_marginable_md(md_path)
    print(f"Parsed {len(entries)} stocks from {md_path.name}")
    
    if not entries:
        print("No valid entries found!")
        return

    db = SessionLocal()
    updated_count = 0
    
    try:
        for symbol, marginable_percent in entries:
            stmt = insert(StaticStockInfo).values(
                symbol=symbol,
                marginable_percent=marginable_percent
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=['symbol'],
                set_={'marginable_percent': stmt.excluded.marginable_percent}
            )
            db.execute(stmt)
            updated_count += 1
            
        db.commit()
        print(f"Successfully updated {updated_count} stocks in StaticStockInfo table!")
    except Exception as e:
        db.rollback()
        print(f"Error during DB update: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    update_marginable()
