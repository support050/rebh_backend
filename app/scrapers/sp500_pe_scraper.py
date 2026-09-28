import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

import re
import logging
from datetime import datetime, date, timedelta
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import SessionLocal
from app.models.economic_indicators import SP500History

logger = logging.getLogger(__name__)

MULTPL_URL = "https://www.multpl.com/s-p-500-pe-ratio/table/by-month"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html",
}
# Incremental: fetch last N months from Multpl (covers revisions + new data)
INCREMENTAL_LOOKBACK_MONTHS = 6


def _fetch_multpl_pe() -> list:
    """Scrape all monthly TTM P/E rows from Multpl."""
    logger.info(f"Fetching S&P 500 TTM P/E from {MULTPL_URL} ...")
    resp = requests.get(MULTPL_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"id": "datatable"})
    if not table:
        logger.error("datatable not found on multpl.com")
        return []

    records = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 2:
            continue
        date_str = cols[0].text.strip()
        match = re.search(r"[\d.]+", cols[1].text)
        if not match:
            continue
        try:
            dt = datetime.strptime(date_str, "%b %d, %Y").date()
            records.append({"date": dt, "pe": float(match.group())})
        except ValueError:
            continue

    logger.info(f"   Fetched {len(records)} monthly PE rows from Multpl")
    return records


def scrape_sp500_pe(mode: str = "incremental") -> bool:
    """
    Scrape S&P 500 TTM P/E from Multpl and upsert into sp500_history.pe_ratio.

    mode:
      incremental  - fetch only the last INCREMENTAL_LOOKBACK_MONTHS months (default)
      full         - fetch and upsert all available history
    """
    logger.info(f"[sp500_pe] Starting (mode={mode}) ...")
    db = SessionLocal()
    try:
        # Make sure pe_ratio column exists and close is nullable
        for ddl in [
            "ALTER TABLE sp500_history ADD COLUMN IF NOT EXISTS pe_ratio FLOAT",
            "ALTER TABLE sp500_history ALTER COLUMN close DROP NOT NULL",
        ]:
            try:
                db.execute(text(ddl))
                db.commit()
            except Exception:
                db.rollback()

        all_records = _fetch_multpl_pe()
        if not all_records:
            return False

        # Incremental: keep only recent months
        if mode == "incremental":
            cutoff = date.today() - timedelta(days=INCREMENTAL_LOOKBACK_MONTHS * 31)
            all_records = [r for r in all_records if r["date"] >= cutoff]
            logger.info(f"   Incremental: {len(all_records)} records since {cutoff}")

        if not all_records:
            logger.info("   Nothing to upsert.")
            return True

        # Build month -> first-of-month mapping
        # Multpl returns exact dates (e.g. Sep 25); normalise to 1st of month
        # because sp500_history already has daily OHLCV rows keyed by exact date.
        # Strategy: match by (year, month), update pe_ratio on the FIRST existing
        # OHLCV row for that month, or insert a stub row on the 1st if none exists.
        from sqlalchemy import extract
        inserted = 0
        updated = 0

        for rec in all_records:
            yr, mo = rec["date"].year, rec["date"].month
            pe_val = rec["pe"]

            # Find ANY existing row in that month
            existing = (
                db.query(SP500History)
                .filter(
                    extract("year", SP500History.trade_date) == yr,
                    extract("month", SP500History.trade_date) == mo,
                )
                .order_by(SP500History.trade_date)
                .first()
            )

            if existing:
                if existing.pe_ratio is None or abs(float(existing.pe_ratio) - pe_val) > 0.001:
                    existing.pe_ratio = pe_val
                    updated += 1
            else:
                # Insert a stub row for the 1st of the month
                stub_date = date(yr, mo, 1)
                db.add(SP500History(trade_date=stub_date, pe_ratio=pe_val))
                inserted += 1

        db.commit()
        logger.info(f"[sp500_pe] Done -- updated: {updated}, inserted stubs: {inserted}")
        return True

    except Exception as e:
        db.rollback()
        logger.error(f"[sp500_pe] Failed: {e}")
        return False
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="S&P 500 TTM P/E scraper (Multpl)")
    parser.add_argument("--mode", default="incremental", choices=["incremental", "full"])
    args = parser.parse_args()
    scrape_sp500_pe(mode=args.mode)
