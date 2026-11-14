import sys
import asyncio
from pathlib import Path
import json

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.api.routes.screeners import fetch_historical

def test_api():
    db = SessionLocal()
    # Mocking the dependency to call fetch_historical directly
    try:
        # Since fetch_historical is an inner function in get_historical_trend,
        # we can't call it directly. Let's just run the exact query logic instead
        # to see what it produces.
        from app.models.stock_indicators import StockIndicator
        from sqlalchemy import and_, func
        
        wide_rows = (
            db.query(
                StockIndicator.date,
                func.count(StockIndicator.symbol).label("count"),
            )
            .filter(
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
            )
            .group_by(StockIndicator.date)
            .order_by(StockIndicator.date)
            .all()
        )
        
        wide_map = {str(row.date): int(row.count) for row in wide_rows}
        
        print("Dates around 2021-06-01 in wide_map:")
        dates = sorted(wide_map.keys())
        for d in dates:
            if '2021-05-25' <= d <= '2021-06-05':
                print(f"{d}: {wide_map[d]}")
                
        print(f"\nTotal dates with data: {len(dates)}")
        if dates:
            print(f"Max stocks on a single day: {max(wide_map.values())}")
            print(f"Latest date: {dates[-1]} = {wide_map[dates[-1]]}")
            print(f"First date: {dates[0]} = {wide_map[dates[0]]}")
            
    finally:
        db.close()

if __name__ == "__main__":
    test_api()
