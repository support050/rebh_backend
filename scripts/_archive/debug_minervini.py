import sys
import asyncio
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.stock_indicators import StockIndicator
from sqlalchemy import and_, func
from datetime import date

def debug_query():
    db = SessionLocal()
    
    # Check for a specific date, e.g., 2021-06-01
    test_date = date(2021, 6, 1)
    
    base_query = db.query(StockIndicator).filter(StockIndicator.date == test_date)
    print(f"Total stocks on {test_date}: {base_query.count()}")
    
    c1 = base_query.filter(StockIndicator.sma_50 > StockIndicator.sma_200).count()
    print(f"sma_50 > sma_200: {c1}")
    
    c2 = base_query.filter(StockIndicator.sma_200 > StockIndicator.sma_200_5m_ago).count()
    print(f"sma_200 > sma_200_5m_ago: {c2}")
    
    c3 = base_query.filter(StockIndicator.price_vs_sma_50_percent > 0.0).count()
    print(f"price_vs_sma_50_percent > 0.0: {c3}")
    
    c4 = base_query.filter(StockIndicator.price_vs_sma_150_percent > 0.0).count()
    print(f"price_vs_sma_150_percent > 0.0: {c4}")
    
    c5 = base_query.filter(StockIndicator.price_vs_sma_200_percent > 0.0).count()
    print(f"price_vs_sma_200_percent > 0.0: {c5}")
    
    c6 = base_query.filter(StockIndicator.sma_30w.isnot(None), StockIndicator.close > StockIndicator.sma_30w).count()
    print(f"close > sma_30w: {c6}")
    
    c7 = base_query.filter(StockIndicator.sma_40w.isnot(None), StockIndicator.close > StockIndicator.sma_40w).count()
    print(f"close > sma_40w: {c7}")
    
    # Combined:
    combined = base_query.filter(
        and_(
            StockIndicator.sma_50 > StockIndicator.sma_200,
            StockIndicator.sma_200 > StockIndicator.sma_200_5m_ago,
            StockIndicator.price_vs_sma_50_percent > 0.0,
            StockIndicator.price_vs_sma_150_percent > 0.0,
            StockIndicator.price_vs_sma_200_percent > 0.0,
            StockIndicator.sma_30w.isnot(None),
            StockIndicator.close > StockIndicator.sma_30w,
            StockIndicator.sma_40w.isnot(None),
            StockIndicator.close > StockIndicator.sma_40w,
        )
    ).count()
    print(f"COMBINED 5 Months Wide: {combined}")

if __name__ == "__main__":
    debug_query()
