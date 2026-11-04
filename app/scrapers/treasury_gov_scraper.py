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
from datetime import datetime, date, timedelta
from typing import Optional
from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.economic_indicators import TreasuryYieldCurve
from app.scrapers.scrapling_fetch import http_get as scrapling_http_get

logger = logging.getLogger(__name__)

# ── Smart Incremental Backfill knobs ──────────────────────────────────────────
# Safety margin re-covered on every run: covers holidays, a late publication,
# and Treasury's own next-day revisions around the newest stored date.
TREASURY_LOOKBACK_BUFFER_DAYS = int(os.getenv("TREASURY_LOOKBACK_BUFFER_DAYS", "7"))
# Where an empty table starts from (Treasury's daily CSV runs back to 1990, but
# this system's consumers only need the modern series).
TREASURY_DEFAULT_START = date(2010, 1, 1)
# Guard rail: a two-year outage must not silently turn the routine job into a
# multi-thousand-request full backfill. Beyond this the run fetches the window
# and logs that an explicit backfill mode is required.
TREASURY_MAX_INCREMENTAL_MONTHS = int(os.getenv("TREASURY_MAX_INCREMENTAL_MONTHS", "24"))

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


# Sanity window for a stored yield, in percent. Treasury par curve rates have
# always lived inside this band (max ~19% in 1981, min ~0% post-2008); a value
# outside it means the parse latched onto the wrong cell — drop the cell, never
# the whole row, and log so the Runbook shows a warning.
YIELD_MIN, YIELD_MAX = -2.0, 25.0


def _to_yield(val_str: str) -> Optional[float]:
    """Parse one CSV cell into a guarded float yield (None = absent/invalid)."""
    if val_str in ("", "N/A", "ND", "."):
        return None
    try:
        v = float(val_str)
    except ValueError:
        return None
    if not (YIELD_MIN <= v <= YIELD_MAX):
        logger.warning(f"  ⚠️ yield {v} outside sane window — cell dropped")
        return None
    return v


def _fetch_text(url: str, headers: dict) -> tuple[Optional[str], int]:
    """Fetch a monthly CSV body.

    Measured 2026-11-03: home.treasury.gov answers plain `requests` with 200 +
    the full CSV, so requests stays the PRIMARY path (cheap, no browser stack).
    If the site ever starts 403-ing this server — as Akamai does for CME — the
    shared Scrapling layer rotates real TLS fingerprints (chrome120, chrome,
    safari180, …) and a bare curl_cffi pass covers images where `scrapling`
    itself is missing. Returns (text, status); text None means unusable.
    """
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200 and resp.text.lstrip().startswith("Date"):
            return resp.text, 200
        if resp.status_code == 404:
            return None, 404
        logger.warning(f"  ⚠️ requests got status={resp.status_code} "
                       f"len={len(resp.text):,} — falling through to Scrapling")
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning(f"  ⚠️ requests network error: {type(e).__name__} — "
                       f"falling through to Scrapling")

    # Fallback 1: Scrapling Fetcher (curl_cffi TLS impersonation, rotating).
    res = scrapling_http_get(url, timeout=30, extra_headers=headers)
    if res is not None and res.ok:
        return res.text, res.status

    # Fallback 2: bare curl_cffi — survives images where scrapling is absent
    # (curl_cffi is its dependency, measured 2026-11-01 on gurufocus).
    try:
        from curl_cffi import requests as cc_requests
        resp = cc_requests.get(url, headers=headers, impersonate="chrome",
                               timeout=30)
        if resp.status_code == 200 and resp.text.lstrip().startswith("Date"):
            return resp.text, 200
        if resp.status_code == 404:
            return None, 404
    except Exception as e:
        logger.warning(f"  ⚠️ curl_cffi fallback failed: {type(e).__name__}")
    return None, 0


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
        text, status = _fetch_text(url, headers)
        if status == 404:
            logger.info(f"  ℹ️ No data for {year}-{month:02d} (404)")
            return []
        if text is not None:
            reader = csv.DictReader(io.StringIO(text))
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
                    val = _to_yield(row.get(csv_col, "").strip())
                    record[db_field] = val
                    if val is not None:
                        has_any = True

                if has_any:
                    rows.append(record)

            logger.info(f"  ✅ {year}-{month:02d}: {len(rows)} records with target data")
            return rows

        if attempt < 3:
            logger.warning(f"  ⚠️ Attempt {attempt} failed for {year}-{month:02d}. Retrying...")
            time.sleep(attempt * 2)
        else:
            logger.error(f"  ❌ Failed {year}-{month:02d} after 3 attempts (all transports)")
            return []

    return []


def _months_covering(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) buckets from `start` to `end`."""
    if start > end:
        return []
    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


def _last_report_date(db) -> Optional[date]:
    """Newest closing date already stored for the yield curve."""
    return db.query(func.max(TreasuryYieldCurve.report_date)).scalar()


def scrape_treasury_gov(mode: str = "incremental") -> dict:
    """
    Fetch all maturity data from Treasury.gov
    and UPDATE existing rows or INSERT new rows in the DB.

    Modes:
      - "incremental": everything from (last stored date − buffer) to today —
        i.e. every month that covers the gap, not just the current one.
      - "backfill_recent": Last 5 years.
      - "backfill": All months from 2018 to today.
      - "full": All months from 1990 to today.

    Returns {"ok": bool, "rows": n_written, "updated": .., "skipped": ..} —
    the shape job_tracking.with_result_handling turns into rows_written / a
    red Runbook entry (the old bool return left the Rows column empty).
    """
    logger.info(f"🏛️ Treasury.gov full scraper started (mode={mode})")

    db = SessionLocal()
    try:
        today = date.today()
        months_to_fetch = []
        incremental_from = None

        if mode == "last5days" or mode == "incremental":
            # Smart incremental: ask the DB how far behind we actually are, then
            # fetch EVERY month that spans the gap. A skipped week of scheduling
            # closes by itself on the next run instead of leaving holes.
            last = _last_report_date(db)
            start = (last - timedelta(days=TREASURY_LOOKBACK_BUFFER_DAYS)
                     if last else TREASURY_DEFAULT_START)
            incremental_from = max(start, TREASURY_DEFAULT_START)
            months_to_fetch = _months_covering(incremental_from, today)
            if len(months_to_fetch) > TREASURY_MAX_INCREMENTAL_MONTHS:
                logger.warning(
                    f"  ⚠️ Gap spans {len(months_to_fetch)} months (> "
                    f"{TREASURY_MAX_INCREMENTAL_MONTHS} cap). Fetching only the "
                    f"newest {TREASURY_MAX_INCREMENTAL_MONTHS}; run mode=backfill "
                    f"to close the older part.")
                months_to_fetch = months_to_fetch[-TREASURY_MAX_INCREMENTAL_MONTHS:]
                incremental_from = date(*months_to_fetch[0], 1)
            logger.info(f"  📅 Last stored date: {last} → fetching "
                        f"{len(months_to_fetch)} month(s) from {incremental_from}")

        elif mode == "full":
            # سحب كل البيانات التاريخية من 1990
            months_to_fetch = _months_covering(date(1990, 1, 1), today)

        elif mode == "backfill":
            months_to_fetch = _months_covering(date(2018, 1, 1), today)

        elif mode == "backfill_recent":
            months_to_fetch = _months_covering(
                date(today.year - 5, today.month, 1), today)

        logger.info(f"  📅 Will fetch {len(months_to_fetch)} month(s)")

        total_updated = 0
        total_inserted = 0
        total_skipped = 0
        months_with_data = 0

        for year, month in months_to_fetch:
            records = fetch_treasury_csv(year, month)
            if not records:
                continue
            months_with_data += 1

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

                # Atomic UPSERT using ON CONFLICT on report_date. The EXISTS
                # probe only picks the log counter — the statement itself
                # decides insert-vs-update, so a race still cannot duplicate.
                exists = (db.query(TreasuryYieldCurve.id)
                            .filter(TreasuryYieldCurve.report_date == rd)
                            .first()) is not None

                stmt = pg_insert(TreasuryYieldCurve).values(
                    report_date=rd,
                    updated_at=func.now(),
                    **update_values
                )
                # Core pg_insert bypasses the ORM onupdate hook, so stamp
                # freshness explicitly inside the DO UPDATE clause.
                stmt = stmt.on_conflict_do_update(
                    index_elements=['report_date'],
                    set_={**update_values,
                          "updated_at": func.now()}
                )
                db.execute(stmt)
                if exists:
                    total_updated += 1
                else:
                    total_inserted += 1

            db.commit()
            time.sleep(0.5)  # Be polite to Treasury servers

        logger.info(f"✅ Treasury.gov scraper done: {total_inserted} inserted, {total_updated} updated, {total_skipped} skipped (no changes)")
        if months_with_data == 0:
            # Every monthly file was missing/blocked (fetch_treasury_csv
            # returns [] on non-200) — that is a source failure, not "fresh".
            return {"ok": False, "rows": 0, "stage_failures": [{
                "stage": "treasury_gov",
                "error": f"Treasury.gov served no data for any of the "
                        f"{len(months_to_fetch)} requested month(s)"}]}
        return {"ok": True, "rows": total_updated + total_inserted,
                "updated": total_updated, "skipped": total_skipped}

    except Exception as e:
        db.rollback()
        logger.error(f"❌ Treasury.gov scraper error: {e}", exc_info=True)
        return {"ok": False, "rows": 0, "stage_failures": [
            {"stage": "treasury_gov", "error": str(e)[:400]}]}
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import argparse
    parser = argparse.ArgumentParser(description="Treasury.gov Yield Curve Scraper")
    parser.add_argument("--mode", default="incremental",
                        choices=["incremental", "last5days", "full", "backfill", "backfill_recent"],
                        help="incremental: آخر تاريخ في القاعدة ← اليوم | full: من 1990 | "
                             "backfill: من 2018 | backfill_recent: آخر 5 سنوات")
    args = parser.parse_args()
    scrape_treasury_gov(mode=args.mode)
