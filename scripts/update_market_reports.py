import gc
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.core.database import SessionLocal
from app.models.market_reports import (
    SubstantialShareholder,
    NetShortPosition,
    ForeignHeadroom,
    ShareBuyback,
    SBLPosition,
)
from app.services.daily_detailed_scraper import build_driver
from app.services.market_reports_scraper import (
    scrape_substantial_shareholders,
    scrape_net_short_positions,
    scrape_foreign_headroom,
    scrape_share_buybacks,
    scrape_sbl_positions,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _create_driver():
    """Create a memory-optimized ChromeDriver instance."""
    driver = build_driver(headless=True)
    # Set timeouts to prevent Chrome from hanging indefinitely
    driver.set_page_load_timeout(60)        # 60s max per page load
    driver.set_script_timeout(30)           # 30s max for scripts
    driver.implicitly_wait(10)              # 10s implicit wait
    return driver


def _run_scraper_with_own_driver(scrape_fn, save_fn, name):
    """
    Run a single scraper task with its own ChromeDriver.

    Each scraper gets a fresh driver that is quit immediately after,
    preventing Chrome from accumulating memory over 17+ minutes.

    Returns (rows_saved, error_or_None). A first pass that yields 0 records
    (e.g. the 2026-10-13 chromedriver read-timeout) is retried ONCE with a
    fresh driver — Render's slowness is bursty and most transient failures
    succeed on the second attempt.
    """
    max_attempts = 2
    data = []
    error = None
    for attempt in range(1, max_attempts + 1):
        driver = None
        try:
            logger.info(f"Initializing ChromeDriver for {name} (attempt {attempt}/{max_attempts})...")
            driver = _create_driver()
            logger.info(f"Scraping {name}...")

            data = scrape_fn(driver) or []
            logger.info(f"Scraped {len(data)} {name} records. Closing ChromeDriver...")

            # Quit driver BEFORE saving to DB (frees ~300MB)
            driver.quit()
            driver = None
            gc.collect()

            if data:
                error = None
                break
            error = f"attempt {attempt}: scraper returned 0 records"
            if attempt < max_attempts:
                logger.warning(f"{name}: 0 records — retrying once with a fresh driver...")

        except Exception as e:
            error = f"attempt {attempt}: {type(e).__name__}: {e}"
            logger.error(f"Error during {name} ({error})")
            data = []
            if attempt < max_attempts:
                logger.warning(f"{name}: failed — retrying once with a fresh driver...")

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None
                gc.collect()

    rows = 0
    if data:
        # Save to DB without Chrome in memory
        save_fn(data)
        rows = len(data)
    else:
        logger.error(f"{name}: still empty after {max_attempts} attempts — nothing saved.")

    # Free the data list
    del data
    gc.collect()
    return rows, (None if rows else error)

def save_substantial_shareholders(data):
    if not data:
        logger.warning("No Substantial Shareholders data to save.")
        return
    new_date = data[0].get('report_date')
    with SessionLocal() as db:
        # Purge older records so only the latest scraped day remains
        deleted = db.query(SubstantialShareholder).filter(SubstantialShareholder.report_date != new_date).delete()
        if deleted > 0:
            logger.info(f"Purged {deleted} older Substantial Shareholders records.")

        count = 0
        for item in data:
            # The source page carries the company NAME only. This report is stored
            # exactly as published — NO name->symbol resolution (removed 2026-10-11
            # on request), so symbol / normalized_symbol are always NULL and no
            # external lookup runs.
            item = {**item, "symbol": None, "normalized_symbol": None}

            existing = db.query(SubstantialShareholder).filter(
                SubstantialShareholder.report_date == item['report_date'],
                SubstantialShareholder.company_name == item['company_name'],
                SubstantialShareholder.shareholder_name == item['shareholder_name']
            ).first()

            if not existing:
                obj = SubstantialShareholder(**item)
                db.add(obj)
                count += 1
            else:
                for k, v in item.items():
                    setattr(existing, k, v)
        db.commit()
    logger.info(f"Saved {len(data)} Substantial Shareholders records for {new_date} (new: {count}).")


def save_net_short_positions(data):
    if not data:
        logger.warning("No Net Short Positions data to save.")
        return
    new_date = data[0].get('report_date')
    with SessionLocal() as db:
        deleted = db.query(NetShortPosition).filter(NetShortPosition.report_date != new_date).delete()
        if deleted > 0:
            logger.info(f"Purged {deleted} older Net Short Positions records.")

        count = 0
        for item in data:
            existing = db.query(NetShortPosition).filter(
                NetShortPosition.report_date == item['report_date'],
                NetShortPosition.symbol == item['symbol']
            ).first()

            if not existing:
                obj = NetShortPosition(**item)
                db.add(obj)
                count += 1
            else:
                for k, v in item.items():
                    setattr(existing, k, v)
        db.commit()
    logger.info(f"Saved {len(data)} Net Short Positions records for {new_date} (new: {count}).")


def save_foreign_headroom(data):
    if not data:
        logger.warning("No Foreign Headroom data to save.")
        return
    new_date = data[0].get('report_date')
    with SessionLocal() as db:
        deleted = db.query(ForeignHeadroom).filter(ForeignHeadroom.report_date != new_date).delete()
        if deleted > 0:
            logger.info(f"Purged {deleted} older Foreign Headroom records.")

        count = 0
        for item in data:
            existing = db.query(ForeignHeadroom).filter(
                ForeignHeadroom.report_date == item['report_date'],
                ForeignHeadroom.symbol == item['symbol']
            ).first()

            if not existing:
                obj = ForeignHeadroom(**item)
                db.add(obj)
                count += 1
            else:
                for k, v in item.items():
                    setattr(existing, k, v)
        db.commit()
    logger.info(f"Saved {len(data)} Foreign Headroom records for {new_date} (new: {count}).")


def save_share_buybacks(data):
    if not data:
        logger.warning("No Share Buybacks data to save.")
        return
    new_date = data[0].get('report_date')
    with SessionLocal() as db:
        deleted = db.query(ShareBuyback).filter(ShareBuyback.report_date != new_date).delete()
        if deleted > 0:
            logger.info(f"Purged {deleted} older Share Buybacks records.")

        count = 0
        for item in data:
            existing = db.query(ShareBuyback).filter(
                ShareBuyback.report_date == item['report_date'],
                ShareBuyback.symbol == item['symbol']
            ).first()

            if not existing:
                obj = ShareBuyback(**item)
                db.add(obj)
                count += 1
            else:
                for k, v in item.items():
                    setattr(existing, k, v)
        db.commit()
    logger.info(f"Saved {len(data)} Share Buybacks records for {new_date} (new: {count}).")


def save_sbl_positions(data):
    if not data:
        logger.warning("No SBL Positions data to save.")
        return
    new_date = data[0].get('report_date')
    with SessionLocal() as db:
        deleted = db.query(SBLPosition).filter(SBLPosition.report_date != new_date).delete()
        if deleted > 0:
            logger.info(f"Purged {deleted} older SBL Positions records.")

        count = 0
        for item in data:
            existing = db.query(SBLPosition).filter(
                SBLPosition.report_date == item['report_date'],
                SBLPosition.symbol == item['symbol']
            ).first()

            if not existing:
                obj = SBLPosition(**item)
                db.add(obj)
                count += 1
            else:
                for k, v in item.items():
                    setattr(existing, k, v)
        db.commit()
    logger.info(f"Saved {len(data)} SBL Positions records for {new_date} (new: {count}).")


def main():
    logger.info("=" * 50)
    logger.info("Starting Market Reports Update (memory-safe mode)")
    logger.info("Each scraper will use its own short-lived ChromeDriver.")
    logger.info("=" * 50)

    tasks = [
        (scrape_substantial_shareholders, save_substantial_shareholders, "Substantial Shareholders"),
        (scrape_net_short_positions,      save_net_short_positions,      "Net Short Positions"),
        (scrape_foreign_headroom,         save_foreign_headroom,         "Foreign Headroom"),
        (scrape_share_buybacks,           save_share_buybacks,           "Share Buybacks"),
        (scrape_sbl_positions,            save_sbl_positions,            "SBL Positions"),
    ]

    per_scraper = {}
    stage_failures = []
    for scrape_fn, save_fn, name in tasks:
        rows, err = _run_scraper_with_own_driver(scrape_fn, save_fn, name)
        per_scraper[name] = rows
        if err:
            stage_failures.append({"stage": name, "error": err})

    total = sum(per_scraper.values())
    logger.info("=" * 50)
    logger.info(f"Market Reports Update finished. Rows per scraper: {per_scraper} (total {total}).")
    logger.info("=" * 50)

    # Honesty for job_runs: a run where EVERY scraper produced nothing is a
    # failure, not a silent success (the 2026-10-13 Run Now showed green with
    # 0 rows saved). Partial zeros stay success — some reports legitimately
    # publish empty tables — but the reasons are logged per stage above.
    # This shape is read by job_tracking.with_result_handling (ok/stage_failures)
    # and the total feeds rows_written via the "rows" key.
    ok = total > 0
    if not ok:
        stage_failures.append({
            "stage": "all_scrapers",
            "error": "every scraper returned 0 rows — nothing was saved",
        })
    return {"ok": ok, "stage_failures": stage_failures, "rows": total,
            "per_scraper": per_scraper}


if __name__ == "__main__":
    # Tracking lives here, not inside main(), so the same job is never recorded
    # twice when APScheduler or the admin endpoint already wrapped its own call.
    from app.services.job_tracking import track_job, with_result_handling
    with track_job("market_reports", "manual") as run:
        with_result_handling(run, main())
