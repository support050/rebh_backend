"""
Treasury.gov CSV Scraper — Daily Yield Curve (All Maturities)
==============================================================
Fetches ALL columns from 1 Mo to 30 Yr from the US Treasury website CSV endpoint.
It UPDATEs existing DB rows or INSERTs new rows if missing.
This ensures we get the most recent data immediately (since FRED is often delayed 1-2 days).

URL pattern:
  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
  daily-treasury-rates.csv/all/{YYYYMM}?type=daily_treasury_yield_curve
  &field_tdr_date_value={YYYY}&page&_format=csv
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

import csv
import io
import requests
import time
import logging
from datetime import datetime, date
from app.core.database import SessionLocal
from app.models.economic_indicators import TreasuryYieldCurve

logger = logging.getLogger(__name__)

BASE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/all/{yyyymm}"
    "?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={yyyy}&page&_format=csv"
)

# ALL columns from Treasury.gov
TARGET_COLUMNS = {
    "1 Mo":      "month_1",
    "1.5 Month": "month_1_5",
    "2 Mo":      "month_2",
    "3 Mo":      "month_3",
    "4 Mo":      "month_4",
    "6 Mo":      "month_6",
    "1 Yr":      "year_1",
    "2 Yr":      "year_2",
    "3 Yr":      "year_3",
    "5 Yr":      "year_5",
    "7 Yr":      "year_7",
    "10 Yr":     "year_10",
    "20 Yr":     "year_20",
    "30 Yr":     "year_30",
}


def fetch_treasury_csv(year: int, month: int) -> list[dict]:
    """Fetch one month of daily yield curve data, extracting only target columns."""
    yyyymm = f"{year}{month:02d}"
    url = BASE_URL.format(yyyymm=yyyymm, yyyy=year)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
    }

    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()

            reader = csv.DictReader(io.StringIO(resp.text))
            rows = []
            for row in reader:
                date_str = row.get("Date", "").strip()
                if not date_str:
                    continue
                try:
                    report_date = datetime.strptime(date_str, "%m/%d/%Y").date()
                except ValueError:
                    continue

                record = {"report_date": report_date}
                has_any = False
                for csv_col, db_field in TARGET_COLUMNS.items():
                    val_str = row.get(csv_col, "").strip()
                    if val_str in ("", "N/A", "ND", "."):
                        record[db_field] = None
                    else:
                        try:
                            record[db_field] = float(val_str)
                            has_any = True
                        except ValueError:
                            record[db_field] = None

                if has_any:
                    rows.append(record)

            logger.info(f"  ✅ {year}-{month:02d}: {len(rows)} records with target data")
            return rows

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info(f"  ℹ️ No data for {year}-{month:02d} (404)")
                return []
            if attempt < 3:
                logger.warning(f"  ⚠️ Attempt {attempt} failed: {e}. Retrying...")
                time.sleep(attempt * 2)
            else:
                logger.error(f"  ❌ Failed {year}-{month:02d} after 3 attempts: {e}")
                return []

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 3:
                logger.warning(f"  ⚠️ Network error attempt {attempt}: {e}. Retrying...")
                time.sleep(attempt * 2)
            else:
                logger.error(f"  ❌ Network error for {year}-{month:02d}: {e}")
                return []

    return []


def scrape_treasury_gov(mode: str = "incremental"):
    """
    Fetch all maturity data from Treasury.gov
    and UPDATE existing rows or INSERT new rows in the DB.

    Modes:
      - "incremental": Current month + previous month.
      - "backfill_recent": Last 5 years.
      - "backfill": All months from 1990 to today.
    """
    logger.info(f"🏛️ Treasury.gov full scraper started (mode={mode})")

    db = SessionLocal()
    try:
        today = date.today()
        months_to_fetch = []

        if mode == "last5days" or mode == "incremental":
            # Fetch current month; if we're in the first 5 days, also fetch previous month
            months_to_fetch.append((today.year, today.month))
            if today.day <= 5:
                if today.month == 1:
                    months_to_fetch.append((today.year - 1, 12))
                else:
                    months_to_fetch.append((today.year, today.month - 1))

        elif mode == "full":
            # سحب كل البيانات التاريخية من 1990
            for year in range(1990, today.year + 1):
                end_month = today.month if year == today.year else 12
                for month in range(1, end_month + 1):
                    months_to_fetch.append((year, month))

        elif mode == "backfill":
            for year in range(2018, today.year + 1):
                end_month = today.month if year == today.year else 12
                for month in range(1, end_month + 1):
                    months_to_fetch.append((year, month))

        elif mode == "backfill_recent":
            for year in range(today.year - 5, today.year + 1):
                end_month = today.month if year == today.year else 12
                for month in range(1, end_month + 1):
                    months_to_fetch.append((year, month))

        logger.info(f"  📅 Will fetch {len(months_to_fetch)} month(s)")

        total_updated = 0
        total_inserted = 0
        total_skipped = 0

        for year, month in months_to_fetch:
            records = fetch_treasury_csv(year, month)
            if not records:
                continue

            from sqlalchemy.dialects.postgresql import insert as pg_insert

            for rec in records:
                rd = rec["report_date"]
                update_values = {}
                for db_field in TARGET_COLUMNS.values():
                    if rec.get(db_field) is not None:
                        update_values[db_field] = rec[db_field]

                if not update_values:
                    total_skipped += 1
                    continue

                # Atomic UPSERT using ON CONFLICT on report_date
                stmt = pg_insert(TreasuryYieldCurve).values(
                    report_date=rd,
                    **update_values
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=['report_date'],
                    set_=update_values
                )
                db.execute(stmt)
                total_updated += 1

            db.commit()
            time.sleep(0.5)  # Be polite to Treasury servers

        logger.info(f"✅ Treasury.gov scraper done: {total_inserted} inserted, {total_updated} updated, {total_skipped} skipped (no changes)")
        return True

    except Exception as e:
        db.rollback()
        logger.error(f"❌ Treasury.gov scraper error: {e}", exc_info=True)
        return False
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import argparse
    parser = argparse.ArgumentParser(description="Treasury.gov Yield Curve Scraper")
    parser.add_argument("--mode", default="incremental",
                        choices=["incremental", "last5days", "full", "backfill", "backfill_recent"],
                        help="incremental: الشهر الحالي | full: من 1990 | backfill: من 2018 | backfill_recent: آخر 5 سنوات")
    args = parser.parse_args()
    scrape_treasury_gov(mode=args.mode)
