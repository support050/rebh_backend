"""
SP500 Earnings Yield Scraper
Fetches the S&P 500 Earnings Yield (EY) and stores it as indicator_code='SP500_EY'.

Primary source : GuruFocus HTML page (same pattern as existing gurufocus_scraper.py)
Fallback source: Yahoo Finance PE → EY = 1 / PE
"""

import logging
import time
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.economic_indicators import EconomicIndicator

logger = logging.getLogger(__name__)

INDICATOR_CODE = "SP500_EY"

GURUFOCUS_URL = (
    "https://www.gurufocus.com/economic_indicators/1031/us-sp-500-earnings-yield"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.gurufocus.com/",
}


# ── Source 1: GuruFocus ───────────────────────────────────────────────────────

def _fetch_from_gurufocus() -> Optional[float]:
    """
    Scrape the latest S&P 500 Earnings Yield from GuruFocus.
    Returns the value as a decimal fraction (e.g. 0.039 for 3.9%).
    """
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(GURUFOCUS_URL, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        # Look for specific structural elements that hold the value
        main_value_text = None
        
        # 1. Look for a span with a class related to value or current indicator
        current_span = soup.select_one("span.currentValue, h1 + span, .indicator-value")
        if current_span:
            main_value_text = current_span.get_text(strip=True)
        else:
            # 2. Fallback to a table row labeled 'Current Value'
            for tr in soup.find_all("tr"):
                if "Current Value" in tr.get_text():
                    tds = tr.find_all("td")
                    if len(tds) >= 2:
                        main_value_text = tds[1].get_text(strip=True)
                        break

        if main_value_text and "%" in main_value_text:
            clean = main_value_text.replace("%", "").replace(",", "").strip()
            try:
                val = float(clean)
                if 0.5 < val < 15:
                    logger.info(f"GuruFocus SP500_EY: {val}%")
                    return val   # Standardized: store as percentage e.g. 4.107% matching DB history
            except ValueError:
                pass

        logger.warning("GuruFocus: could not parse earnings yield from page")
        return None

    except Exception as e:
        logger.warning(f"GuruFocus fetch failed: {e}")
        return None


# ── Source 2: Yahoo Finance (fallback via P/E) ────────────────────────────────

def _fetch_from_yahoo() -> Optional[float]:
    """
    Fetch S&P 500 trailing P/E from Yahoo Finance and derive EY = 1 / PE.
    Uses the ^GSPC summary endpoint.
    """
    url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/%5EGSPC"
    params = {"modules": "summaryDetail,defaultKeyStatistics"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        result = data["quoteSummary"]["result"][0]
        trailing_pe = (
            result.get("summaryDetail", {})
                  .get("trailingPE", {})
                  .get("raw")
        )

        if trailing_pe and trailing_pe > 0:
            ey_pct = (1.0 / trailing_pe) * 100.0
            logger.info(f"Yahoo Finance PE={trailing_pe:.2f} → EY={ey_pct:.2f}%")
            return ey_pct

    except Exception as e:
        logger.warning(f"Yahoo Finance EY fallback failed: {e}")

    return None


# ── Storage ───────────────────────────────────────────────────────────────────

def _save_sp500_ey(value: float, report_date: date) -> bool:
    db = SessionLocal()
    try:
        existing = (
            db.query(EconomicIndicator)
              .filter(
                  EconomicIndicator.indicator_code == INDICATOR_CODE,
                  EconomicIndicator.report_date == report_date,
              )
              .first()
        )

        if existing:
            existing.value = value
            logger.info(f"Updated SP500_EY for {report_date}: {value:.4f}")
        else:
            db.add(
                EconomicIndicator(
                    report_date=report_date,
                    indicator_code=INDICATOR_CODE,
                    value=value,
                )
            )
            logger.info(f"Inserted SP500_EY for {report_date}: {value:.4f}")

        db.commit()
        return True

    except Exception as e:
        db.rollback()
        logger.error(f"DB error saving SP500_EY: {e}")
        return False
    finally:
        db.close()


# ── Main entry point ──────────────────────────────────────────────────────────

def scrape_sp500_earnings_yield(force: bool = False) -> bool:
    """
    Fetch and store today's S&P 500 Earnings Yield.
    Skips if a value already exists for today unless force=True.
    """
    today = date.today()

    if not force:
        db = SessionLocal()
        try:
            exists = (
                db.query(EconomicIndicator)
                  .filter(
                      EconomicIndicator.indicator_code == INDICATOR_CODE,
                      EconomicIndicator.report_date == today,
                  )
                  .first()
            )
            if exists:
                logger.info(f"SP500_EY already scraped today ({today}). Use force=True to re-fetch.")
                return True
        finally:
            db.close()

    # Try GuruFocus first, then Yahoo fallback
    ey_value = _fetch_from_gurufocus()
    if ey_value is None:
        logger.info("Falling back to Yahoo Finance for SP500_EY...")
        time.sleep(1)
        ey_value = _fetch_from_yahoo()

    if ey_value is None:
        logger.error("❌ Failed to fetch SP500_EY from all sources")
        return False

    return _save_sp500_ey(ey_value, today)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    parser = argparse.ArgumentParser(description="SP500 Earnings Yield Scraper")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if today already has data")
    args = parser.parse_args()
    ok = scrape_sp500_earnings_yield(force=args.force)
    sys.exit(0 if ok else 1)
