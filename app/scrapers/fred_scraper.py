"""
FRED API Scraper
Fetches economic indicators and macroeconomic data from the official FRED API.
(Treasury yield curves are handled separately by treasury_gov_scraper.py)
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

import os
import requests
import time
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.economic_indicators import EconomicIndicator

logger = logging.getLogger(__name__)

# Official FRED API settings — key must be set in .env as FRED_API_KEY
FRED_API_KEY: str = (settings.FRED_API_KEY or os.getenv("FRED_API_KEY") or "").strip()
if not FRED_API_KEY:
    raise RuntimeError(
        "FRED_API_KEY is not set. Add it to your .env file.\n"
        "Get a free key at: https://fredaccount.stlouisfed.org/apikeys"
    )
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"

# All FRED series configurations (Indicator Code -> series_id and optional params)
FRED_SERIES_CONFIG: Dict[str, Dict[str, Any]] = {
    # ── Labor market ─────────────────────────────────────────────────────────
    "UNRATE": {
        "series_id": "UNRATE",
        "description": "Unemployment Rate",
    },
    "PAYEMS": {
        "series_id": "PAYEMS",
        "description": "All Employees, Total Nonfarm Payrolls",
    },
    "IC4WSA": {
        "series_id": "IC4WSA",
        "description": "4-Week Moving Average of Initial Claims",
    },

    # ── Corporate bond spreads (OAS) & Yields ────────────────────────────────
    "BAMLC0A3CA": {
        "series_id": "BAMLC0A3CA",
        "description": "ICE BofA Single-A US Corporate Option-Adjusted Spread",
    },
    "BAMLC0A4CBBB": {
        "series_id": "BAMLC0A4CBBB",
        "description": "ICE BofA BBB US Corporate Option-Adjusted Spread",
    },
    "BAMLC0A3CAEY": {
        "series_id": "BAMLC0A3CAEY",
        "description": "ICE BofA Single-A US Corporate Effective Yield",
    },
    "BAMLC0A4CBBBEY": {
        "series_id": "BAMLC0A4CBBBEY",
        "description": "ICE BofA BBB US Corporate Effective Yield",
    },

    # ── High-yield bond spreads & yields ─────────────────────────────────────
    "BAMLH0A1HYBBEY": {
        "series_id": "BAMLH0A1HYBBEY",
        "description": "ICE BofA BB US High Yield Effective Yield",
    },
    "BAMLH0A2HYBEY": {
        "series_id": "BAMLH0A2HYBEY",
        "description": "ICE BofA Single-B US High Yield Effective Yield",
    },

    # ── Monetary Policy / Fed Rates ──────────────────────────────────────────
    "FEDFUNDS": {
        "series_id": "FEDFUNDS",
        "description": "Federal Funds Effective Rate",
    },

    # ── Macro / Monetary / Balance Sheet ──────────────────────────────────────
    "TLAACBW027SBOG": {
        "series_id": "TLAACBW027SBOG",
        "description": "Total Assets, All Commercial Banks",
    },
    "WALCL": {
        "series_id": "WALCL",
        "description": "Federal Reserve Total Assets (Fed Balance Sheet)",
    },
    "TREAST": {
        "series_id": "TREAST",
        "description": "Total Reserves (U.S. Reserve Assets)",
    },
    "CPIAUCSL_PC1": {
        "series_id": "CPIAUCSL",
        "params": {"units": "pc1"},
        "description": "Consumer Price Index: All Items (Percent Change from Year Ago)",
    },
    "TOTLL": {
        "series_id": "TOTLL",
        "description": "Loans and Leases in Bank Credit, All Commercial Banks",
    },
    "BOGMBASE": {
        "series_id": "BOGMBASE",
        "description": "Monetary Base; Total (Money Supply M0)",
    },
    "M1SL": {
        "series_id": "M1SL",
        "description": "M1 Money Supply",
    },
    "M2SL": {
        "series_id": "M2SL",
        "description": "M2 Money Supply",
    },
}


def fetch_fred_observations(
    series_id: str,
    extra_params: Optional[dict] = None,
    observation_start: Optional[str] = None,
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    """
    Fetch all historical observations for a series from the official FRED API,
    handling pagination if necessary (limit=100000).
    Skips missing / invalid values ('.', '', 'ND', 'N/A').
    """
    all_observations = []
    offset = 0
    limit = 100000  # FRED API supports up to 100,000 per request

    while True:
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "asc",
            "limit": limit,
            "offset": offset,
        }
        if observation_start:
            params["observation_start"] = observation_start
        if extra_params:
            params.update(extra_params)

        success = False
        data = None

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(FRED_API_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                success = True
                break
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.warning(f"FRED series '{series_id}' not found (404).")
                    return []
                if attempt < max_retries:
                    wait_time = attempt * 3
                    logger.warning(f"FRED API HTTP error on {series_id} (attempt {attempt}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"FRED API HTTP error on {series_id} failed after {max_retries} attempts: {e}")
                    return []
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries:
                    wait_time = attempt * 3
                    logger.warning(f"FRED API network error on {series_id} (attempt {attempt}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"FRED API network error on {series_id} failed after {max_retries} attempts: {e}")
                    return []
            except Exception as e:
                logger.error(f"Unexpected error fetching {series_id}: {e}")
                return []

        if not success or not data:
            break

        obs_list = data.get("observations", [])
        if not obs_list:
            break

        for obs in obs_list:
            val_str = obs.get("value", "").strip()
            if val_str in (".", "", "N/A", "ND", "null"):
                continue
            try:
                dt = datetime.strptime(obs["date"], "%Y-%m-%d").date()
                val = float(val_str)
                all_observations.append({"date": dt, "value": val})
            except (ValueError, KeyError):
                continue

        count = data.get("count", len(all_observations))
        offset += len(obs_list)
        if offset >= count or len(obs_list) < limit:
            break

        time.sleep(0.5)

    return all_observations


def scrape_fred_indicator(indicator_code: str) -> bool:
    """
    Fetch and store historical data from the official FRED API for one indicator code.
    Uses bulk insert with existing-date check to avoid duplicating rows.
    """
    indicator_code = indicator_code.upper()
    if indicator_code not in FRED_SERIES_CONFIG:
        logger.error(f"Unknown indicator code: {indicator_code}")
        return False

    config = FRED_SERIES_CONFIG[indicator_code]
    series_id = config["series_id"]
    extra_params = config.get("params")

    logger.info(f"Fetching {indicator_code} (series_id: {series_id}) from official FRED API...")

    parsed_data = fetch_fred_observations(series_id, extra_params=extra_params)
    logger.info(f"Parsed {len(parsed_data)} valid records for {indicator_code}")

    if not parsed_data:
        logger.warning(f"No observations received for {indicator_code}")
        return False

    db = SessionLocal()
    try:
        # Load existing dates in a single query to prevent duplicate records
        existing_dates = {
            row[0]
            for row in db.query(EconomicIndicator.report_date)
                          .filter(EconomicIndicator.indicator_code == indicator_code)
                          .all()
        }

        new_objects = [
            EconomicIndicator(
                report_date=item["date"],
                indicator_code=indicator_code,
                value=item["value"],
            )
            for item in parsed_data
            if item["date"] not in existing_dates
        ]

        if new_objects:
            db.bulk_save_objects(new_objects)
            db.commit()
            logger.info(f"✅ {indicator_code}: inserted {len(new_objects)} new records (total fetched: {len(parsed_data)})")
        else:
            logger.info(f"ℹ️ {indicator_code}: already up-to-date (0 new records)")

        return True

    except Exception as e:
        db.rollback()
        logger.error(f"DB error saving {indicator_code}: {e}", exc_info=True)
        return False
    finally:
        db.close()


def scrape_all_fred() -> Dict[str, bool]:
    """Run all configured FRED indicators and return a status map."""
    logger.info(f"Starting FRED API scrape for all {len(FRED_SERIES_CONFIG)} indicators...")
    results = {}
    for code in FRED_SERIES_CONFIG:
        results[code] = scrape_fred_indicator(code)
        time.sleep(0.5)  # Respect FRED API rate limit (120 requests/minute)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = scrape_all_fred()
    print("\nSummary of FRED scraping:")
    for code, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {code}")
