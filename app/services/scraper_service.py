"""
ScraperService
==============
Orchestrates all scrapers in the correct order and tracks their run status.
Designed to be called from a scheduler (APScheduler / Celery / cron).
"""

import logging
import traceback
from datetime import datetime, date
from typing import Optional

from app.core.database import SessionLocal
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)


# ── Internal status tracker (in-memory, reset on restart) ────────────────────
_scraper_status: dict[str, dict] = {}


def _record(name: str, success: bool, detail: str = ""):
    _scraper_status[name] = {
        "name":       name,
        "success":    success,
        "detail":     detail,
        "ran_at":     datetime.utcnow().isoformat(),
    }


def _run(name: str, fn, *args, **kwargs) -> bool:
    logger.info(f"▶ Running scraper: {name}")
    try:
        result = fn(*args, **kwargs)
        ok = result if isinstance(result, bool) else bool(result)
        _record(name, ok)
        logger.info(f"  {'✅' if ok else '⚠️'} {name}: {'ok' if ok else 'returned falsy'}")
        return ok
    except Exception as e:
        _record(name, False, traceback.format_exc(limit=3))
        logger.error(f"  ❌ {name} failed: {e}")
        return False


# ── Individual runner wrappers ────────────────────────────────────────────────

def run_fred(indicators: Optional[list[str]] = None) -> dict:
    from app.scrapers.fred_scraper import scrape_fred_indicator, FRED_SERIES_CONFIG
    codes = indicators or list(FRED_SERIES_CONFIG.keys())
    results = {}
    for code in codes:
        results[code] = _run(f"fred:{code}", scrape_fred_indicator, code)
    return results


def run_sp500_price(mode: str = "incremental") -> bool:
    from app.scrapers.sp500_scraper import scrape_sp500
    return _run("sp500_price", scrape_sp500, mode=mode)


def run_sp500_pe() -> bool:
    from app.scrapers.sp500_pe_scraper import scrape_sp500_pe
    return _run("sp500_pe", scrape_sp500_pe)


def run_sp500_ey(force: bool = False) -> bool:
    from app.scrapers.sp500_ey_scraper import scrape_sp500_earnings_yield
    return _run("sp500_ey", scrape_sp500_earnings_yield, force=force)


def run_treasury_gov(mode: str = "incremental") -> bool:
    from app.scrapers.treasury_gov_scraper import scrape_treasury_gov
    return _run("treasury_gov", scrape_treasury_gov, mode=mode)


def run_tasi_components(symbols: Optional[list[str]] = None) -> bool:
    from app.scrapers.tasi_components_scraper import scrape_tasi_components
    result = scrape_tasi_components(symbols=symbols)
    ok = result.get("failed", 0) == 0
    _record("tasi_components", ok, str(result))
    return ok


# ── Daily routine (called by scheduler every weekday morning) ─────────────────

def run_daily_scrapers() -> dict:
    """
    Full daily scrape sequence. Order matters:
      1. Treasury data  — needed by Bond dashboard and TYC (from Treasury.gov)
      2. FRED indicators — needed by Bond dashboard and Economy assessment
      3. S&P 500 price  — needed by all valuation scenarios
      4. S&P 500 P/E    — needed by historical PE and scenarios
      5. S&P 500 EY     — needed by Bond dashboard and Economy assessment
      6. TASI market data — needed by Market Weight and Report tabs
    """
    logger.info("=" * 60)
    logger.info(f"🚀 Daily scraper run started — {datetime.utcnow().isoformat()}")
    logger.info("=" * 60)

    results = {
        "treasury_gov":      run_treasury_gov(mode="incremental"),
        "fred_indicators":   run_fred(),
        "sp500_price":       run_sp500_price(mode="incremental"),
        "sp500_pe":          run_sp500_pe(),
        "sp500_ey":          run_sp500_ey(),
        "tasi_components":   run_tasi_components(),
    }

    ok_count = sum(1 for v in results.values() if v is True or (isinstance(v, dict) and all(v.values())))
    logger.info(f"\n📊 Daily run complete — {ok_count}/{len(results)} tasks succeeded")
    return results


# ── Full historical backfill (run once on first setup) ────────────────────────

def run_full_backfill() -> dict:
    """
    One-time backfill. Pull all historical data from each source.
    This can take 10-30 minutes depending on network speed.
    Only run this once during initial setup.
    """
    logger.info("🔄 Starting full historical backfill — this may take a while...")

    return {
        "treasury_gov":    run_treasury_gov(mode="full"),
        "fred_indicators": run_fred(),
        "sp500_price":     run_sp500_price(mode="full"),
        "sp500_pe":        run_sp500_pe(),
        "sp500_ey":        run_sp500_ey(force=True),
    }


# ── Status report ─────────────────────────────────────────────────────────────

def get_scraper_status() -> dict:
    """Return the last run status of every scraper."""
    return {
        "scrapers":    list(_scraper_status.values()),
        "as_of":       datetime.utcnow().isoformat(),
        "total":       len(_scraper_status),
        "failed":      sum(1 for v in _scraper_status.values() if not v["success"]),
    }
