import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple, Type

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func
from app.core.database import SessionLocal
from app.models.market_reports import (
    SubstantialShareholder,
    NetShortPosition,
    ForeignHeadroom,
    ShareBuyback,
    SBLPosition,
)


MODEL_MAP = [
    ("substantial_shareholders", SubstantialShareholder, ["report_date", "company_name", "shareholder_name"]),
    ("net_short_positions", NetShortPosition, ["report_date", "symbol"]),
    ("foreign_headrooms", ForeignHeadroom, ["report_date", "symbol"]),
    ("share_buybacks", ShareBuyback, ["report_date", "symbol"]),
    ("sbl_positions", SBLPosition, ["report_date", "symbol"]),
]


def find_missing_business_days(start: date, end: date, existing_dates: set) -> List[date]:
    missing = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in existing_dates:
            missing.append(current)
        current += timedelta(days=1)
    return missing


def inspect_model(model_name: str, model_cls: Type, key_fields: List[str]):
    db = SessionLocal()
    try:
        total_count = db.query(model_cls).count()
        min_date = db.query(func.min(model_cls.report_date)).scalar()
        max_date = db.query(func.max(model_cls.report_date)).scalar()

        distinct_dates = db.query(model_cls.report_date).distinct().all()
        date_set = {row[0] for row in distinct_dates if row[0] is not None}

        print(f"\n=== {model_name} ===")
        print(f"rows: {total_count}")
        print(f"min_date: {min_date}")
        print(f"max_date: {max_date}")
        print(f"distinct_dates: {len(date_set)}")

        if min_date and max_date:
            missing = find_missing_business_days(min_date, max_date, date_set)
            print(f"missing_business_days: {len(missing)}")
            if missing:
                print("sample_missing_dates:", missing[:20])
        else:
            print("missing_business_days: n/a")

        # show first/last few dates
        if date_set:
            sorted_dates = sorted(date_set)
            print("sample_dates:", sorted_dates[:5], "...", sorted_dates[-5:])

    finally:
        db.close()


def main():
    print("Checking market report history in database...")
    for model_name, model_cls, key_fields in MODEL_MAP:
        inspect_model(model_name, model_cls, key_fields)


if __name__ == "__main__":
    main()
