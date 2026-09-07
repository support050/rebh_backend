"""
Sukuk & Bonds Scraper & DB Ingestion — Saudi Exchange (TASI)
============================================================
Extracts listed Sukuk and Government Bonds data from Tadawul debt page
and saves/updates them into the DB with strict Upsert logic.

Canonical Filename: backend/scripts/Sukuk&Bonds.py
Run Schedule      : Weekly or on-demand (not daily — sukuk list rarely changes)
Idempotent        : Yes — safe to re-run repeatedly without duplication

Phase 4 Compliance:
  - Numeric types for coupon_rate, yield_to_maturity, outstanding_amount
  - Date types for issue_date, maturity_date
  - Composite uniqueness: symbol + parent_company_symbol
  - Strict separation: coupon_rate != YTM unless source says so
  - Govt (G) / Corporate (C) bond type labeling
  - Source URL + retrieval timestamp stored per record
  - Issuer-to-equity symbol mapping via parent_company_symbol
  - Retry logic with logged failure details
  - Build-Up R integration: company sukuk yield outranks generic grade curve
"""
import logging
import os
import sys
import time
import json
import re
import requests
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

# Setup path for backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.database import SessionLocal, engine
from app.models.sukuk_bonds import SukukMarketData


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
BASE_PAGE = "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/sukuk-market-watch?locale=en"

STATIC_AJAX_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/sukuk-market-watch"
    "/!ut/p/z1/04_Sj9CPykssy0xPLMnMz0vMAfIjo8ziTR3NDIw8LAz8LVxcnA0C3bwtPLwM_I0MXMz0w9EU-LqbGQT6OQb6G5mbGhgEG-lHkaTfIDjAFKggwNfYxyDIwN3AjDj9BjiAowFh_VFoSjB9gKoAixPBCvC4ITg1T78gNzQ0wiAzIN1RUREAdewi3A!!"
    "/p0/IZ7_5A602H80OOMQC0604RU6VD1091=CZ6_5A602H80O8DDC0QFK8HJ0O20D6=NJgetSukukMarketDetails=/"
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers — Type-safe parsers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_decimal(value, *, label: str = "") -> Optional[Decimal]:
    """Convert various string/number representations to Decimal, or None."""
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("%", "").replace("٪", "")
    if raw in ("", "-", "N/A", "n/a", "--"):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        logger.debug(f"[PARSE] Cannot convert {label!r}={value!r} to Decimal")
        return None


def _parse_date(value, *, label: str = "") -> Optional[date]:
    """
    Parse date from various formats used by Tadawul:
    YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY, YYYY, 'DDMMYYYY', unix ms timestamps.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw in ("-", "N/A", "--"):
        return None

    # Unix-ms timestamp
    if raw.isdigit() and len(raw) == 13:
        try:
            return datetime.utcfromtimestamp(int(raw) / 1000).date()
        except Exception:
            pass

    # Try standard formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    # 4-digit year only
    if re.fullmatch(r"\d{4}", raw):
        try:
            return date(int(raw), 12, 31)
        except ValueError:
            pass

    logger.debug(f"[PARSE] Cannot parse {label!r}={value!r} as date")
    return None


def _classify_bond_type(raw: Optional[str]) -> str:
    """
    Normalize bond_type to 'G' (Government) or 'C' (Corporate).
    Never leave ambiguous: default to 'C' if unknown.
    """
    if not raw:
        return "C"
    r = str(raw).strip().upper()
    if r in ("G", "GOV", "GOVERNMENT", "SOVEREIGN", "SOVEREIGN SUKUK"):
        return "G"
    if r in ("C", "CORP", "CORPORATE", "COMPANY"):
        return "C"
    return "G" if "GOV" in r else "C"


# ──────────────────────────────────────────────────────────────────────────────
# DB Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def ensure_table_exists():
    """Ensure the sukuk_market_data table exists."""
    try:
        SukukMarketData.__table__.create(engine, checkfirst=True)
        logger.info("[DB] Table 'sukuk_market_data' checked/ready.")
    except Exception as e:
        logger.error(f"[DB] Error checking table: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Requests (fast path)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_sukuk_via_requests(max_retries: int = 3) -> Optional[list]:
    """
    Attempt fast direct requests to the static AJAX URL.
    Retries up to max_retries times on transient failures.
    Returns raw item list or None.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_PAGE,
    }
    session = requests.Session()
    session.headers.update(headers)

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[NET] Attempt {attempt}/{max_retries} — Requesting base page for cookies...")
            session.get(BASE_PAGE, timeout=15)
            params = {
                "sectorParameter": "all",
                "iswatchListSelected": "NO",
                "requestLocale": "en",
                "_": int(time.time() * 1000),
            }
            logger.info("[NET] Fetching Sukuk data via static AJAX URL...")
            r1 = session.get(STATIC_AJAX_URL, params=params, timeout=20)

            if r1.status_code == 200:
                data = r1.json()
                items = data.get("sukukList") or data.get("data") or data.get("aaData") or []
                if items:
                    logger.info(f"[NET] Successfully fetched {len(items)} items via requests.")
                    return items
                else:
                    logger.warning(f"[NET] Response OK but empty item list — attempt {attempt}.")
            else:
                logger.warning(f"[NET] HTTP {r1.status_code} on attempt {attempt}.")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[NET] Connection error on attempt {attempt}: {e}")
        except Exception as e:
            logger.warning(f"[NET] Requests failed on attempt {attempt}: {e}")

        if attempt < max_retries:
            time.sleep(2 ** attempt)  # exponential backoff

    logger.warning("[NET] All requests attempts failed. Trying Browser Fetch...")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Selenium (fallback)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_sukuk_via_browser() -> Optional[list]:
    """Dynamic Selenium discovery with in-browser fetch fallback."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        logger.error("[BROWSER] selenium or webdriver_manager not installed. Skipping browser fetch.")
        return None

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    try:
        logger.info("[BROWSER] Opening Sukuk page in headless Chrome...")
        driver.get(BASE_PAGE)
        time.sleep(4)

        ajax_url = None
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                log = json.loads(entry["message"])["message"]
                if log["method"] == "Network.requestWillBeSent":
                    req_url = log["params"]["request"]["url"]
                    if "getSukukMarketDetails" in req_url or (
                        "sukuk-market-watch/!ut/p/" in req_url and "http" in req_url
                    ):
                        ajax_url = req_url
                        break
            except Exception:
                continue

        if not ajax_url:
            match = re.search(r'[\'"]([^\'"]+getSukukMarketDetails[^\'"]*)[\'"]', driver.page_source)
            if match:
                ajax_url = match.group(1)
                if ajax_url.startswith("/"):
                    ajax_url = "https://www.saudiexchange.sa" + ajax_url

        if not ajax_url:
            ajax_url = STATIC_AJAX_URL

        js_code = f"""
        const done = arguments[0];
        fetch('{ajax_url}?sectorParameter=all&iswatchListSelected=NO&requestLocale=en&_=' + Date.now(), {{
            headers: {{
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest'
            }}
        }})
        .then(res => res.json())
        .then(data => done({{ success: true, data: data }}))
        .catch(err => done({{ success: false, error: err.toString() }}));
        """
        result = driver.execute_async_script(js_code)
        if result.get("success"):
            data = result.get("data", {})
            items = data.get("sukukList") or data.get("data") or data.get("aaData") or []
            if items:
                logger.info(f"[BROWSER] Fetched {len(items)} items via browser fetch.")
            return items

    except Exception as e:
        logger.error(f"[BROWSER] Browser fetch failed: {e}")
    finally:
        driver.quit()

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Normalize raw API item → typed dict
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_item(it: dict, source_url: str) -> Optional[dict]:
    """
    Normalize a raw item from the Tadawul JSON response.
    Returns a typed dict ready for DB insertion, or None if symbol is missing.

    IMPORTANT: coupon_rate is stored as coupon_rate only.
    yield_to_maturity is stored separately when the source provides it.
    Never treat coupon_rate as YTM unless the source explicitly says so.
    """
    sym = str(it.get("symbol", "")).strip()
    if not sym:
        return None

    # Coupon rate
    coupon = _parse_decimal(it.get("couponRate"), label="couponRate")

    # YTM — only from explicit ytm/yieldToMaturity field; NOT from couponRate
    ytm_raw = it.get("ytm") or it.get("yieldToMaturity") or it.get("yield_to_maturity")
    ytm = _parse_decimal(ytm_raw, label="ytm")

    # Dates
    issue_dt = _parse_date(
        it.get("issueDateStr") or it.get("issueDate"), label="issueDate"
    )
    maturity_dt = _parse_date(
        it.get("maturityDateStr") or it.get("maturityDate"), label="maturityDate"
    )

    # Outstanding amount
    amount = _parse_decimal(
        it.get("outstandingAmountModified") or it.get("outstandingAmount"),
        label="outstandingAmount"
    )

    # Bond type: G = Government, C = Corporate
    bond_type = _classify_bond_type(it.get("bondType") or it.get("bond_type"))

    # Parent company (equity symbol mapping)
    parent_sym = str(it.get("parentCompnaySymbol", "")).strip() or None

    return {
        "symbol":                sym,
        "issuer_name":           it.get("issuerName") or it.get("issuer_name"),
        "parent_company_symbol": parent_sym,
        "bond_type":             bond_type,
        "coupon_rate":           coupon,
        "yield_to_maturity":     ytm,
        "issue_date":            issue_dt,
        "maturity_date":         maturity_dt,
        "outstanding_amount":    amount,
        "currency":              str(it.get("currency", "SAR")).strip() or "SAR",
        "sector_name":           it.get("sectorName") or it.get("sector_name"),
        "source_url":            source_url,
        "is_active":             True,
        "as_of":                 datetime.utcnow().strftime("%Y-%m-%d"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# DB Upsert
# ──────────────────────────────────────────────────────────────────────────────

def save_sukuk_to_db(items: list, source_url: str = STATIC_AJAX_URL) -> int:
    """
    Saves or updates Sukuk records in PostgreSQL using strict upsert logic.
    Composite key: (symbol, parent_company_symbol).
    Records that fail validation are skipped and logged individually.
    """
    ensure_table_exists()
    db: Session = SessionLocal()
    saved_count = 0
    skipped_count = 0

    try:
        for raw_it in items:
            norm = _normalize_item(raw_it, source_url=source_url)
            if not norm:
                skipped_count += 1
                logger.warning(f"[DB] Skipped item with no symbol: {raw_it}")
                continue

            sym = norm["symbol"]
            parent_sym = norm["parent_company_symbol"]

            try:
                # Look up by composite key: symbol + parent_company_symbol
                existing = db.query(SukukMarketData).filter(
                    SukukMarketData.symbol == sym,
                    SukukMarketData.parent_company_symbol == parent_sym
                ).first()

                if existing:
                    # Update all mutable fields
                    existing.issuer_name           = norm["issuer_name"]
                    existing.bond_type             = norm["bond_type"]
                    existing.coupon_rate           = norm["coupon_rate"]
                    existing.yield_to_maturity     = norm["yield_to_maturity"]
                    existing.issue_date            = norm["issue_date"]
                    existing.maturity_date         = norm["maturity_date"]
                    existing.outstanding_amount    = norm["outstanding_amount"]
                    existing.currency              = norm["currency"]
                    existing.sector_name           = norm["sector_name"]
                    existing.source_url            = norm["source_url"]
                    existing.is_active             = norm["is_active"]
                    existing.as_of                 = norm["as_of"]
                else:
                    db.add(SukukMarketData(**norm))

                saved_count += 1

            except Exception as row_err:
                logger.error(f"[DB] Failed to upsert symbol={sym}: {row_err}")
                db.rollback()
                skipped_count += 1
                continue

        db.commit()
        logger.info(
            f"[DB] Upserted {saved_count} Sukuk & Bonds records. "
            f"Skipped {skipped_count} invalid items."
        )

    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Fatal error during bulk upsert: {e}")
    finally:
        db.close()

    return saved_count


# ──────────────────────────────────────────────────────────────────────────────
# Build-Up R Integration: get best yield for a company
# ──────────────────────────────────────────────────────────────────────────────

def get_buildup_sukuk_yield(equity_symbol: str) -> dict:
    """
    Returns the best available sukuk/debt yield for a company's Build-Up R calculation.
    Priority:
      1. Company-specific sukuk yield (from DB, coupon_rate or YTM)
      2. Government Sukuk benchmark (from DB, closest G-type sukuk)
      3. Hardcoded SAMA policy-rate fallback (5.5% repo)

    This function is imported by rebh_unified_engine.py to determine Rf in Build-Up.
    Company sukuk yield OUTRANKS the generic grade curve per Phase 4 requirement.
    """
    db = SessionLocal()
    try:
        # 1. Company-specific sukuk (best: YTM > coupon_rate)
        co_sukuk = db.query(SukukMarketData).filter(
            (SukukMarketData.parent_company_symbol == equity_symbol) |
            (SukukMarketData.symbol == equity_symbol),
            SukukMarketData.is_active == True
        ).order_by(SukukMarketData.maturity_date.desc()).first()

        if co_sukuk:
            # Prefer YTM if explicitly available; else use coupon_rate as proxy
            yield_val = co_sukuk.yield_to_maturity or co_sukuk.coupon_rate
            if yield_val is not None:
                return {
                    "yield_pct":    float(yield_val),
                    "source":       "company_sukuk_db",
                    "symbol":       co_sukuk.symbol,
                    "issuer":       co_sukuk.issuer_name,
                    "bond_type":    co_sukuk.bond_type,
                    "is_ytm":       co_sukuk.yield_to_maturity is not None,
                    "is_fallback":  False,
                    "as_of":        co_sukuk.as_of,
                }

        # 2. Government Sukuk benchmark (G-type, most recent maturity)
        govt = db.query(SukukMarketData).filter(
            SukukMarketData.bond_type == "G",
            SukukMarketData.is_active == True
        ).order_by(SukukMarketData.maturity_date.desc()).first()

        if govt:
            yield_val = govt.yield_to_maturity or govt.coupon_rate
            if yield_val is not None:
                return {
                    "yield_pct":    float(yield_val),
                    "source":       "govt_sukuk_benchmark_db",
                    "symbol":       govt.symbol,
                    "issuer":       govt.issuer_name,
                    "bond_type":    "G",
                    "is_ytm":       govt.yield_to_maturity is not None,
                    "is_fallback":  True,
                    "reason":       f"No company-specific sukuk for {equity_symbol}; using KSA Government Benchmark",
                    "as_of":        govt.as_of,
                }

    except Exception as e:
        logger.warning(f"[BUILD-UP] DB lookup failed for {equity_symbol}: {e}")
    finally:
        db.close()

    # 3. Hardcoded SAMA policy-rate fallback
    return {
        "yield_pct":    5.50,
        "source":       "sama_repo_rate_fallback",
        "symbol":       None,
        "issuer":       "SAMA",
        "bond_type":    "G",
        "is_ytm":       False,
        "is_fallback":  True,
        "reason":       "No active sukuk found in DB for this company or government benchmark.",
        "as_of":        None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def run_sukuk_sync() -> int:
    """Main execution entry point."""
    logger.info("=== Starting Sukuk & Bonds Data Sync (Phase 4) ===")
    items = fetch_sukuk_via_requests()
    if not items:
        logger.warning("[SYNC] Requests path returned no data — trying browser...")
        items = fetch_sukuk_via_browser()

    if items:
        count = save_sukuk_to_db(items, source_url=STATIC_AJAX_URL)
        logger.info(f"=== Sync Finished: {count} instruments updated ===")
        return count
    else:
        logger.error("=== Sync FAILED: No data fetched from any source ===")
        return 0


if __name__ == "__main__":
    result = run_sukuk_sync()
    print(f"\n[SUCCESS] Sukuk & Bonds sync complete: {result} records upserted.")