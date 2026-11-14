import sys
import asyncio
from pathlib import Path

# Add parent directory to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.stock_indicators import StockIndicator

def check_db():
    db = SessionLocal()
    
    total = db.query(StockIndicator).count()
    print(f"Total rows in stock_indicators: {total}")
    
    for col in ['sma_50', 'sma_200', 'sma_30w', 'sma_40w', 'percent_off_52w_low']:
        count_not_null = db.query(StockIndicator).filter(getattr(StockIndicator, col) != None).count()
        print(f"Rows where {col} is NOT NULL: {count_not_null}")
    
    # Let's check symbol 2040 (which had the crash previously)
    sym_total = db.query(StockIndicator).filter(StockIndicator.symbol == '2040').count()
    print(f"\nTotal rows for symbol 2040: {sym_total}")
    
    for col in ['sma_50', 'sma_200', 'sma_30w', 'sma_40w']:
        count_not_null = db.query(StockIndicator).filter(StockIndicator.symbol == '2040', getattr(StockIndicator, col) != None).count()
        print(f"Rows for 2040 where {col} is NOT NULL: {count_not_null}")

if __name__ == "__main__":
    check_db()
