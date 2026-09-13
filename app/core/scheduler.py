"""
Automated Scraper Scheduler — APScheduler + Render compatible.

Schedule (Egypt Time — Africa/Cairo):
  Daily  (Mon–Fri, 01:00):  SP500, Treasury.gov, Spreads, Eurodollar, FedWatch
  Weekly (Fri,      01:30):  IC4WSA (released Thursday)
  Monthly(10th,     02:00):  UNRATE, PAYEMS, SP500 PE, GuruFocus EY/PE

Why 01:00 AM Egypt?
  = 22:00 UTC = 18:00 ET → 2 hours after US market close.
  Most data sources update within 1–2 h after close.

Activation:
  Set env var  ENABLE_SCHEDULER=true  on Render (or locally).
"""

import logging
import os
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
EGYPT_TZ = "Africa/Cairo"
_scheduler: BackgroundScheduler | None = None


# ─── Job: Daily ────────────────────────────────────────────────
def job_daily_scrapers():
    logger.info("=" * 50)
    logger.info("📅 [Scheduler] DAILY scrapers starting…")

    _safe("SP500", lambda: __import__(
        "app.scrapers.sp500_scraper", fromlist=["scrape_sp500"]
    ).scrape_sp500(mode="incremental"))

    _safe("Treasury.gov", lambda: __import__(
        "app.scrapers.treasury_gov_scraper", fromlist=["scrape_treasury_gov"]
    ).scrape_treasury_gov(mode="incremental"))

    for code in ["BAMLC0A3CA", "BAMLC0A4CBBB", "BAMLC0A3CAEY"]:
        _safe(f"FRED:{code}", lambda c=code: __import__(
            "app.scrapers.fred_scraper", fromlist=["scrape_fred_indicator"]
        ).scrape_fred_indicator(c))

    # ── Eurodollar & CME FedWatch: run MANUALLY from Admin Dashboard ──
    # Eurodollar: Investing.com blocks Render's server IPs (403).
    # CME FedWatch: QuikStrike returns error page from server IPs.
    # Both work fine when run locally → use Admin "Run Now" buttons.

    _clear_cache()
    logger.info("📅 [Scheduler] DAILY scrapers finished.")


# ─── Job: Weekly ───────────────────────────────────────────────
def job_weekly_scrapers():
    logger.info("📆 [Scheduler] WEEKLY scrapers starting…")
    _safe("IC4WSA", lambda: __import__(
        "app.scrapers.fred_scraper", fromlist=["scrape_fred_indicator"]
    ).scrape_fred_indicator("IC4WSA"))
    _clear_cache()
    logger.info("📆 [Scheduler] WEEKLY scrapers finished.")


# ─── Job: Weekly NAAIM (Thursday) ─────────────────────────────
def job_naaim_scraper():
    """NAAIM Exposure Index is published every Thursday."""
    logger.info("📆 [Scheduler] NAAIM scraper starting…")
    _safe("NAAIM", lambda: __import__(
        "app.scrapers.naaim_scraper", fromlist=["scrape_naaim"]
    ).scrape_naaim(mode="incremental"))
    _clear_naaim_cache()
    _clear_cache()
    logger.info("📆 [Scheduler] NAAIM scraper finished.")


def _clear_naaim_cache():
    """Clear NAAIM-specific cache keys (sync)."""
    try:
        import redis as sync_redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = sync_redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        keys = r.keys("naaim:*")
        if keys:
            r.delete(*keys)
        logger.info(f"  🗑️ Cleared {len(keys)} NAAIM cache keys")
        r.close()
    except Exception as e:
        logger.warning(f"  ⚠️ NAAIM cache clear skipped: {e}")


# ─── Job: Monthly ─────────────────────────────────────────────
def job_monthly_scrapers():
    logger.info("🗓️ [Scheduler] MONTHLY scrapers starting…")

    for code in ["UNRATE", "PAYEMS"]:
        _safe(f"FRED:{code}", lambda c=code: __import__(
            "app.scrapers.fred_scraper", fromlist=["scrape_fred_indicator"]
        ).scrape_fred_indicator(c))

    _safe("SP500 PE (Multpl)", lambda: __import__(
        "app.scrapers.sp500_pe_scraper", fromlist=["scrape_sp500_pe"]
    ).scrape_sp500_pe())

    _safe("GuruFocus EY", lambda: __import__(
        "app.scrapers.gurufocus_scraper", fromlist=["scrape_gurufocus_indicator"]
    ).scrape_gurufocus_indicator(
        url="https://www.gurufocus.com/economic_indicators/151/sp-500-earnings-yield",
        indicator_code="SP500_EY",
        mode="incremental",
    ))

    _safe("GuruFocus PE", lambda: __import__(
        "app.scrapers.gurufocus_scraper", fromlist=["scrape_gurufocus_indicator"]
    ).scrape_gurufocus_indicator(
        url="https://www.gurufocus.com/economic_indicators/57/sp-500-pe-ratio",
        indicator_code="SP500_PE",
        mode="incremental",
    ))

    _clear_cache()
    logger.info("🗓️ [Scheduler] MONTHLY scrapers finished.")


# ─── Job: Market Reports ──────────────────────────────────────
def job_market_reports_scrapers():
    logger.info("=" * 50)
    logger.info("📊 [Scheduler] MARKET REPORTS scrapers starting…")
    _safe("Market Reports (Tadawul)", lambda: __import__(
        "scripts.update_market_reports", fromlist=["main"]
    ).main())
    logger.info("📊 [Scheduler] MARKET REPORTS scrapers finished.")


# ─── Job: Daily Market Update (Prices & Indicators) ────────────
def job_daily_market_update():
    logger.info("=" * 50)
    logger.info("📈 [Scheduler] DAILY MARKET UPDATE starting…")
    _safe("Daily Market Update", lambda: __import__(
        "scripts.daily_market_update", fromlist=["main"]
    ).main())
    logger.info("📈 [Scheduler] DAILY MARKET UPDATE finished.")


# ─── Job: Sukuk Importer (Phase 4 / Phase 12) ──────────────────
def job_sukuk_importer():
    logger.info("📜 [Scheduler] SUKUK & BONDS importer starting…")
    _safe("Sukuk Importer", lambda: __import__(
        "app.services.rebh_importers_service", fromlist=["run_sukuk_importer"]
    ).run_sukuk_importer())
    logger.info("📜 [Scheduler] SUKUK & BONDS importer finished.")


# ─── Job: SAMA & GaStat Macro Importer (Phase 5 / Phase 12) ────
def job_macro_importer():
    logger.info("🏛️ [Scheduler] SAMA & GASTAT Macro importer starting…")
    _safe("Macro Importer", lambda: __import__(
        "app.services.rebh_importers_service", fromlist=["run_macro_importer"]
    ).run_macro_importer())
    logger.info("🏛️ [Scheduler] SAMA & GASTAT Macro importer finished.")


# ─── Job: Bank Financial Lines Importer (Phase 3 / Phase 12) ───
def job_bank_lines_importer():
    logger.info("🏦 [Scheduler] Bank Lines Verification starting…")
    _safe("Bank Lines Importer", lambda: __import__(
        "app.services.rebh_importers_service", fromlist=["run_bank_lines_importer"]
    ).run_bank_lines_importer())
    logger.info("🏦 [Scheduler] Bank Lines Verification finished.")


# ─── Job: Filings Importer (Phase 12) ──────────────────────────
def job_filings_importer():
    logger.info("📑 [Scheduler] Official Filings check starting…")
    _safe("Filings Importer", lambda: __import__(
        "app.services.rebh_importers_service", fromlist=["run_filings_importer"]
    ).run_filings_importer())
    logger.info("📑 [Scheduler] Official Filings check finished.")


# ─── Job: REBH Engine Vintage Snapshot (Phase 12) ─────────────
def job_engine_vintages():
    logger.info("⚙️ [Scheduler] REBH Engine Vintage Snapshot job starting…")
    _safe("Engine Vintage Snapshot", lambda: __import__(
        "app.services.rebh_importers_service", fromlist=["run_engine_vintages_job"]
    ).run_engine_vintages_job())
    logger.info("⚙️ [Scheduler] REBH Engine Vintage Snapshot job finished.")


# ─── Helpers ───────────────────────────────────────────────────
def _safe(name: str, fn):
    """Run a scraper function with error handling."""
    try:
        logger.info(f"  🔄 {name}…")
        fn()
        logger.info(f"  ✅ {name} done")
    except Exception as e:
        logger.error(f"  ❌ {name} failed: {e}")


def _clear_cache():
    """
    Clear economic indicator cache keys using a SYNC Redis connection.
    APScheduler runs jobs in a background thread, so we can't reliably
    access the async event loop. A direct sync redis.Redis call avoids
    the 'Event loop is closed' / 'Future attached to a different loop' errors.
    """
    try:
        import redis as sync_redis

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = sync_redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        keys = r.keys("economic:*")
        if keys:
            r.delete(*keys)
        logger.info(f"  🗑️ Cleared {len(keys)} cache keys")
        r.close()
    except Exception as e:
        logger.warning(f"  ⚠️ Cache clear skipped: {e}")


# ─── Start / Stop ─────────────────────────────────────────────
def start_scheduler():
    global _scheduler
    if os.getenv("ENABLE_SCHEDULER", "false").lower() not in ("true", "1", "yes"):
        logger.info("⏸️ [Scheduler] Disabled. Set ENABLE_SCHEDULER=true to enable.")
        return

    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone=EGYPT_TZ)

    _scheduler.add_job(
        job_daily_scrapers,
        CronTrigger(day_of_week="mon-fri", hour=1, minute=0, timezone=EGYPT_TZ),
        id="daily", name="Daily Scrapers", replace_existing=True,
    )
    _scheduler.add_job(
        job_weekly_scrapers,
        CronTrigger(day_of_week="fri", hour=1, minute=30, timezone=EGYPT_TZ),
        id="weekly", name="Weekly Scrapers (IC4WSA)", replace_existing=True,
    )
    _scheduler.add_job(
        job_monthly_scrapers,
        CronTrigger(day=10, hour=2, minute=0, timezone=EGYPT_TZ),
        id="monthly", name="Monthly Scrapers", replace_existing=True,
    )
    _scheduler.add_job(
        job_naaim_scraper,
        CronTrigger(day_of_week="thu", hour=1, minute=45, timezone=EGYPT_TZ),
        id="naaim_weekly", name="Weekly NAAIM Exposure Index", replace_existing=True,
    )
    _scheduler.add_job(
        job_market_reports_scrapers,
        CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=18, minute=0, timezone=EGYPT_TZ),
        id="market_reports", name="Saudi Market Reports Scrapers", replace_existing=True,
    )
    _scheduler.add_job(
        job_daily_market_update,
        CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=18, minute=30, timezone=EGYPT_TZ),
        id="daily_market_update", name="Daily Market Update Pipeline", replace_existing=True,
    )
    _scheduler.add_job(
        job_sukuk_importer,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=EGYPT_TZ),
        id="sukuk_importer", name="Sukuk & Debt Instruments Importer", replace_existing=True,
    )
    _scheduler.add_job(
        job_macro_importer,
        CronTrigger(day=1, hour=3, minute=0, timezone=EGYPT_TZ),
        id="macro_importer", name="SAMA & GaStat Macro Importer", replace_existing=True,
    )
    _scheduler.add_job(
        job_bank_lines_importer,
        CronTrigger(day_of_week="sun", hour=19, minute=30, timezone=EGYPT_TZ),
        id="bank_lines_importer", name="Bank Financial Lines Importer", replace_existing=True,
    )
    _scheduler.add_job(
        job_filings_importer,
        CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=19, minute=45, timezone=EGYPT_TZ),
        id="filings_importer", name="Official Filings Ingestion Check", replace_existing=True,
    )
    _scheduler.add_job(
        job_engine_vintages,
        CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=20, minute=0, timezone=EGYPT_TZ),
        id="engine_vintages", name="REBH Unified Engine Vintage Snapshot", replace_existing=True,
    )

    _scheduler.start()
    logger.info("✅ [Scheduler] Started — timezone: Africa/Cairo")
    for job in _scheduler.get_jobs():
        logger.info(f"   📌 {job.name} → next: {job.next_run_time}")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("🛑 [Scheduler] Stopped.")
        _scheduler = None
