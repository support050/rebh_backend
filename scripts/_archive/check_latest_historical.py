import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.database import SessionLocal
from app.models.market_reports import HistoricalReport

def main():
    db = SessionLocal()
    try:
        latest = db.query(HistoricalReport.report_date).order_by(HistoricalReport.report_date.desc()).first()
        if latest:
            print(f"🗓️ Latest date in historical_reports table: {latest[0]}")
        else:
            print("No data in historical_reports table.")
    finally:
        db.close()

if __name__ == "__main__":
    main()
