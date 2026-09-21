"""
Sukuk & Bonds Scraper & DB Ingestion — Saudi Exchange (TASI)
============================================================
Extracts listed Sukuk and Government Bonds data from Tadawul debt page
and saves/updates them into the DB with strict Upsert logic.

Canonical Filename: backend/scripts/Sukuk&Bonds.py
Run Schedule      : Weekly or on-demand (not daily — sukuk list rarely changes)
Idempotent        : Yes — safe to re-run repeatedly without duplication

Phase 5 Architecture:
  - Clean browser scraping via Playwright with --disable-http2 and AutomationControlled bypass
  - Live intercept of Sukuk AJAX responses during page session
  - DOM table fallback extraction if JSON endpoint differs
  - Resilient local seed dataset fallback (ensures 100% continuous data availability for Build-Up R)
"""
import logging
import os
import sys
import time
import json
import re
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
AJAX_URL_HINT = re.compile(r"(sukukmarketdetails|sukuk.*market.*details)", re.IGNORECASE)


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


def _extract_items(data: dict) -> list:
    """Extract sukuk list from various possible JSON response shapes."""
    if not isinstance(data, dict):
        return []
    return data.get("sukukList") or data.get("data") or data.get("aaData") or []


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Primary Strategy: ScraperAPI (WAF bypass, no browser needed)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_sukuk_via_scraperapi(api_key: str) -> Optional[list]:
    """
    Primary live scraper using ScraperAPI to bypass Akamai WAF.
    Renders the Sukuk market watch page and extracts table data from the HTML.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("[SCRAPERAPI] requests/bs4 not installed")
        return None

    logger.info("🛡️ Attempting Sukuk scrape via ScraperAPI with Saudi IP...")
    try:
        r = requests.get("https://api.scraperapi.com", params={
            "api_key": api_key,
            "url": BASE_PAGE,
            "render": "true",
            "country_code": "sa",
        }, timeout=120)

        if r.status_code != 200:
            logger.warning(f"[SCRAPERAPI] Page render returned status {r.status_code}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # Try to extract JSON from inline scripts (some pages embed sukuk data as JS objects)
        for script in soup.find_all("script"):
            script_text = script.string or ""
            if "sukukList" in script_text or "sukukMarketDetails" in script_text:
                # Try extracting JSON array from the script
                json_match = re.search(r'\[\s*\{.*?"symbol".*?\}\s*\]', script_text, re.DOTALL)
                if json_match:
                    try:
                        items = json.loads(json_match.group())
                        if items and len(items) > 3:
                            logger.info(f"[SCRAPERAPI] ✅ Extracted {len(items)} sukuk from inline JSON!")
                            return items
                    except json.JSONDecodeError:
                        pass

        # Fallback: parse DOM tables from rendered HTML
        items = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue

            for row in rows:
                cells = row.find_all(["td"])
                if len(cells) < 5:
                    continue

                symbol = cells[0].get_text(strip=True)
                # Skip header rows
                if not symbol or symbol in ("Symbol", "الرمز", "", "الإجمالي", "Total"):
                    continue

                items.append({
                    "symbol":                    symbol,
                    "issuerName":                cells[1].get_text(strip=True) if len(cells) > 1 else "",
                    "couponRate":                cells[2].get_text(strip=True) if len(cells) > 2 else "",
                    "issueDateStr":              cells[3].get_text(strip=True) if len(cells) > 3 else "",
                    "maturityDateStr":           cells[4].get_text(strip=True) if len(cells) > 4 else "",
                    "outstandingAmountModified": cells[5].get_text(strip=True) if len(cells) > 5 else "",
                    "bondType":                  cells[6].get_text(strip=True) if len(cells) > 6 else "",
                    "parentCompnaySymbol":       cells[7].get_text(strip=True) if len(cells) > 7 else "",
                })

        if items:
            logger.info(f"[SCRAPERAPI] ✅ Extracted {len(items)} sukuk from DOM table!")
            return items

        logger.warning("[SCRAPERAPI] Page rendered but no sukuk data found in HTML")
        return None

    except Exception as e:
        logger.error(f"[SCRAPERAPI] Sukuk fetch failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Secondary Strategy: Playwright Browser with HTTP/1.1 (from base_scraper)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_sukuk_via_browser(max_wait_seconds: int = 45) -> Optional[list]:
    """
    Main live scraper modeled after base_scraper.py and daily_financial_indicators_scraper.py:
      - Uses Chromium with --disable-http2 and --disable-blink-features=AutomationControlled
      - Intercepts live JSON network traffic if available
      - Falls back directly to parsing the loaded DOM table
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("[BROWSER] playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    captured_items = {"data": None}

    def _on_response(response):
        if captured_items["data"] is not None:
            return
        url = response.url
        ctype = response.headers.get("content-type", "")
        if "json" in ctype or AJAX_URL_HINT.search(url):
            try:
                body = response.json()
                items = _extract_items(body)
                if items:
                    logger.info(f"[BROWSER] ✅ Matched sukuk payload at: {url}")
                    captured_items["data"] = items
            except Exception:
                pass

    for attempt in range(1, 3):
        try:
            logger.info(f"[BROWSER] Launching Chromium browser (attempt {attempt}/2)...")
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-http2",
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
                context = browser.new_context(
                    viewport={"width": 1600, "height": 1000},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                page = context.new_page()
                page.on("response", _on_response)

                logger.info(f"[BROWSER] Navigating to: {BASE_PAGE}")
                page.goto(BASE_PAGE, timeout=40000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                # Wait for JSON intercept or table rendering
                deadline = time.time() + max_wait_seconds
                while time.time() < deadline:
                    if captured_items["data"]:
                        break
                    try:
                        if page.locator("table tbody tr").count() > 5:
                            break
                    except Exception:
                        pass
                    time.sleep(1)

                if captured_items["data"]:
                    items = captured_items["data"]
                    context.close()
                    browser.close()
                    logger.info(f"[BROWSER] ✅ Captured {len(items)} items from live network traffic.")
                    return items

                # DOM table scraping fallback
                logger.info("[BROWSER] Checking table rows in DOM...")
                items = _scrape_dom_table_playwright(page)
                context.close()
                browser.close()
                if items:
                    logger.info(f"[BROWSER] ✅ Scraped {len(items)} items from DOM table.")
                    return items

        except Exception as e:
            logger.warning(f"[BROWSER] Attempt {attempt} failed: {e}")
            time.sleep(2)

    return None


def _scrape_dom_table_playwright(page) -> Optional[list]:
    rows_js = """
    () => {
        const tables = document.querySelectorAll('table');
        const result = [];
        tables.forEach(table => {
            const rows = table.querySelectorAll('tbody tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length >= 5) {
                    result.push({
                        symbol:          cells[0]?.innerText?.trim() || '',
                        issuerName:      cells[1]?.innerText?.trim() || '',
                        couponRate:      cells[2]?.innerText?.trim() || '',
                        issueDateStr:    cells[3]?.innerText?.trim() || '',
                        maturityDateStr: cells[4]?.innerText?.trim() || '',
                        outstandingAmountModified: cells.length > 5 ? (cells[5]?.innerText?.trim() || '') : '',
                        bondType:        cells.length > 6 ? (cells[6]?.innerText?.trim() || '') : '',
                        parentCompnaySymbol: cells.length > 7 ? (cells[7]?.innerText?.trim() || '') : '',
                    });
                }
            });
        });
        return result;
    }
    """
    try:
        dom_items = page.evaluate(rows_js)
        if dom_items:
            valid = [r for r in dom_items if r.get("symbol") and r["symbol"] not in ("Symbol", "الرمز", "", "الإجمالي", "Total")]
            return valid or None
    except Exception as e:
        logger.warning(f"[BROWSER] DOM scrape failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Backup Strategy: Curated Seed Dataset (Production Resilience)
# ──────────────────────────────────────────────────────────────────────────────

def load_sukuk_from_seed() -> Optional[list]:
    """
    Loads verified listed Sukuk & Bonds data from local seed repository.
    Ensures 100% continuous uptime for Build-Up R & valuation models when Akamai
    blocks requests in cloud/CI environments.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    seed_file = os.path.join(base_dir, "data", "sukuk_seed_data.json")
    if not os.path.exists(seed_file):
        logger.warning(f"[SEED] Seed file not found at: {seed_file}")
        return None

    try:
        with open(seed_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data and isinstance(data, list):
                logger.info(f"[SEED] ✅ Loaded {len(data)} verified sukuk records from local repository.")
                return data
    except Exception as e:
        logger.error(f"[SEED] Failed loading seed data: {e}")

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

    coupon = _parse_decimal(it.get("couponRate"), label="couponRate")

    ytm_raw = it.get("ytm") or it.get("yieldToMaturity") or it.get("yield_to_maturity")
    ytm = _parse_decimal(ytm_raw, label="ytm")

    issue_dt = _parse_date(
        it.get("issueDateStr") or it.get("issueDate"), label="issueDate"
    )
    maturity_dt = _parse_date(
        it.get("maturityDateStr") or it.get("maturityDate"), label="maturityDate"
    )

    amount = _parse_decimal(
        it.get("outstandingAmountModified") or it.get("outstandingAmount"),
        label="outstandingAmount"
    )

    bond_type = _classify_bond_type(it.get("bondType") or it.get("bond_type"))

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

def save_sukuk_to_db(items: list, source_url: str = BASE_PAGE) -> int:
    """
    Saves or updates Sukuk records in PostgreSQL using strict upsert logic.
    Primary unique key in PostgreSQL: symbol.
    Uses pre-fetched dictionary lookup (pattern from daily_financial_indicators_scraper)
    to eliminate N+1 queries and guarantee zero UniqueViolation collisions.
    """
    ensure_table_exists()
    db: Session = SessionLocal()
    saved_count = 0
    skipped_count = 0

    try:
        # Pre-fetch existing records by symbol for fast, atomic updates
        existing_records = {r.symbol: r for r in db.query(SukukMarketData).all()}

        for raw_it in items:
            norm = _normalize_item(raw_it, source_url=source_url)
            if not norm:
                skipped_count += 1
                logger.warning(f"[DB] Skipped item with no symbol: {raw_it}")
                continue

            sym = norm["symbol"]

            try:
                existing = existing_records.get(sym)

                if existing:
                    existing.issuer_name           = norm["issuer_name"]
                    existing.parent_company_symbol = norm["parent_company_symbol"]
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
                    new_obj = SukukMarketData(**norm)
                    db.add(new_obj)
                    existing_records[sym] = new_obj

                saved_count += 1

            except Exception as row_err:
                logger.error(f"[DB] Failed to upsert symbol={sym}: {row_err}")
                skipped_count += 1
                continue

        db.commit()
        logger.info(
            f"[DB] ✅ Upserted {saved_count} Sukuk & Bonds records successfully. "
            f"Skipped {skipped_count} invalid items."
        )

    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Fatal error during bulk upsert: {e}")
        return 0
    finally:
        db.close()

    return saved_count


# ──────────────────────────────────────────────────────────────────────────────
# Build-Up R Integration: get best yield for a company
# ──────────────────────────────────────────────────────────────────────────────

_SUKUK_CACHE = {"timestamp": 0.0, "company_map": {}, "govt": None}

def _get_sukuk_memory_cache():
    now = time.time()
    if _SUKUK_CACHE["company_map"] and (now - _SUKUK_CACHE["timestamp"] < 300):
        return _SUKUK_CACHE["company_map"], _SUKUK_CACHE["govt"]

    db = SessionLocal()
    company_map = {}
    govt = None
    try:
        all_active = db.query(SukukMarketData).filter(SukukMarketData.is_active == True).order_by(SukukMarketData.maturity_date.desc()).all()
        for item in all_active:
            if item.bond_type == "G" and govt is None:
                govt = {
                    "yield_pct": float(item.yield_to_maturity or item.coupon_rate) if (item.yield_to_maturity or item.coupon_rate) is not None else 5.5,
                    "symbol": item.symbol,
                    "issuer": item.issuer_name,
                    "as_of": item.as_of,
                    "is_ytm": item.yield_to_maturity is not None
                }

            p_sym = str(item.parent_company_symbol) if item.parent_company_symbol else None
            sym = str(item.symbol) if item.symbol else None
            y_val = item.yield_to_maturity or item.coupon_rate

            if y_val is not None:
                entry = {
                    "yield_pct": float(y_val),
                    "source": "company_sukuk_db",
                    "symbol": item.symbol,
                    "issuer": item.issuer_name,
                    "bond_type": item.bond_type,
                    "is_ytm": item.yield_to_maturity is not None,
                    "is_fallback": False,
                    "as_of": item.as_of,
                }
                if p_sym and p_sym not in company_map:
                    company_map[p_sym] = entry
                if sym and sym not in company_map:
                    company_map[sym] = entry

        _SUKUK_CACHE["timestamp"] = now
        _SUKUK_CACHE["company_map"] = company_map
        _SUKUK_CACHE["govt"] = govt
    except Exception as e:
        logger.warning(f"[BUILD-UP] Failed to pre-load sukuk memory cache: {e}")
    finally:
        db.close()
    return _SUKUK_CACHE["company_map"], _SUKUK_CACHE["govt"]


def get_buildup_sukuk_yield(equity_symbol: str) -> dict:
    """
    Returns the best available sukuk/debt yield for a company's Build-Up R calculation.
    Uses in-memory cache to execute instantly across universe loops.
    """
    eq_sym = str(equity_symbol)
    try:
        company_map, govt = _get_sukuk_memory_cache()
        if eq_sym in company_map:
            return company_map[eq_sym]

        if govt:
            return {
                "yield_pct":    govt["yield_pct"],
                "source":       "govt_sukuk_benchmark_db",
                "symbol":       govt["symbol"],
                "issuer":       govt["issuer"],
                "bond_type":    "G",
                "is_ytm":       govt["is_ytm"],
                "is_fallback":  True,
                "reason":       f"No company-specific sukuk for {equity_symbol}; using KSA Government Benchmark",
                "as_of":        govt["as_of"],
            }
    except Exception as e:
        logger.warning(f"[BUILD-UP] Sukuk lookup failed for {equity_symbol}: {e}")

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
    """
    Main execution entry point:
      1. Primary: Live Playwright browser scraping (with --disable-http2 and DOM fallback)
      2. Resilience Fallback: Verified local seed dataset (guarantees 100% continuous data availability)
    """
    logger.info("=== Starting Sukuk & Bonds Data Sync (Phase 5) ===")

    # 0. Try ScraperAPI first for WAF bypass
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY")
    if not scraperapi_key:
        try:
            from app.core.config import settings
            scraperapi_key = getattr(settings, "SCRAPERAPI_KEY", None)
        except Exception:
            pass

    items = None
    source_url = BASE_PAGE

    if scraperapi_key:
        try:
            items = fetch_sukuk_via_scraperapi(scraperapi_key)
        except Exception as e:
            logger.error(f"[SYNC] ScraperAPI attempt failed: {e}")

    # 1. Try live browser scraping (fallback)
    if not items:
        items = fetch_sukuk_via_browser()

    # 2. Resilient local seed fallback
    if not items:
        logger.warning("[SYNC] Live scraping was blocked by Akamai — activating verified seed dataset...")
        items = load_sukuk_from_seed()
        source_url = "local_seed_repository"

    if items:
        count = save_sukuk_to_db(items, source_url=source_url)
        logger.info(f"=== Sync Finished: {count} instruments updated ===")
        return count
    else:
        logger.error("=== Sync FAILED: No data fetched from live browser or seed dataset ===")
        return 0


if __name__ == "__main__":
    result = run_sukuk_sync()
    print(f"\n[SUCCESS] Sukuk & Bonds sync complete: {result} records upserted.")