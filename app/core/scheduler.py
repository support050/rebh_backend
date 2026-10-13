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
import sys                                       # BREADTH_ENGINE_PY_DEFAULT = sys.executable
from typing import Any, Callable, Dict, List, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
EGYPT_TZ = "Africa/Cairo"
_scheduler: BackgroundScheduler | None = None


# ─── Job: Daily ────────────────────────────────────────────────
# Every step is one JOB_CALLABLES key (defined further down, next to the run-now
# dispatcher) so the cron path and the admin "Run Now" path run the SAME
# callable and write the SAME job_name into job_runs.
DAILY_SCRAPER_KEYS: Tuple[str, ...] = (
    "sp500", "treasury_gov",
    "fred:BAMLC0A3CA", "fred:BAMLC0A4CBBB", "fred:BAMLC0A3CAEY",
)
WEEKLY_SCRAPER_KEYS: Tuple[str, ...] = ("fred:IC4WSA",)
MONTHLY_SCRAPER_KEYS: Tuple[str, ...] = (
    "fred:UNRATE", "fred:PAYEMS",
    # Saudi CPI growth (YoY %) — IMF via FRED. Feeds market-macro inflation_pct.
    "fred:SAUCPALTT01GPM",
    "sp500_pe",
)


def job_daily_scrapers():
    logger.info("=" * 50)
    logger.info("📅 [Scheduler] DAILY scrapers starting…")
    for key in DAILY_SCRAPER_KEYS:
        _safe_job(key)

    # ── Eurodollar & CME FedWatch: run MANUALLY from Admin Dashboard ──
    # Eurodollar: Investing.com blocks Render's server IPs (403).
    # CME FedWatch: QuikStrike returns error page from server IPs.
    # Both work fine when run locally → use Admin "Run Now" buttons.

    _clear_cache()
    logger.info("📅 [Scheduler] DAILY scrapers finished.")


# ─── Job: Weekly ───────────────────────────────────────────────
def job_weekly_scrapers():
    logger.info("📆 [Scheduler] WEEKLY scrapers starting…")
    for key in WEEKLY_SCRAPER_KEYS:
        _safe_job(key)
    _clear_cache()
    logger.info("📆 [Scheduler] WEEKLY scrapers finished.")



# ─── Job: Monthly ─────────────────────────────────────────────
def job_monthly_scrapers():
    logger.info("🗓️ [Scheduler] MONTHLY scrapers starting…")

    for key in MONTHLY_SCRAPER_KEYS:
        _safe_job(key)

    # GuruFocus uses heavy headless Chrome Selenium which consumes high RAM/CPU on server.
    # Kept available in app/scrapers/gurufocus_scraper.py for manual on-demand triggers.
    # _safe("GuruFocus EY", lambda: __import__(
    #     "app.scrapers.gurufocus_scraper", fromlist=["scrape_gurufocus_indicator"]
    # ).scrape_gurufocus_indicator(
    #     url="https://www.gurufocus.com/economic_indicators/151/sp-500-earnings-yield",
    #     indicator_code="SP500_EY",
    #     mode="incremental",
    # ))
    # _safe("GuruFocus PE", lambda: __import__(
    #     "app.scrapers.gurufocus_scraper", fromlist=["scrape_gurufocus_indicator"]
    # ).scrape_gurufocus_indicator(
    #     url="https://www.gurufocus.com/economic_indicators/57/sp-500-pe-ratio",
    #     indicator_code="SP500_PE",
    #     mode="incremental",
    # ))

    _clear_cache()
    logger.info("🗓️ [Scheduler] MONTHLY scrapers finished.")


# ─── Job: Market Reports ──────────────────────────────────────
def job_market_reports_scrapers():
    logger.info("=" * 50)
    logger.info("📊 [Scheduler] MARKET REPORTS scrapers starting…")
    _safe_job("market_reports")
    logger.info("📊 [Scheduler] MARKET REPORTS scrapers finished.")


# ─── Job: Daily Market Update (Prices & Indicators) ────────────
# NOT scheduled. This pipeline is long-running and Selenium-based, and it is
# started manually (scripts/daily_market_update.py) or from the Admin
# dashboard (POST /admin/pipeline/daily-market-update/run → runs on the server,
# next to the database). The mutex in update_status refuses concurrent runs.


# ─── Sukuk / Macro / Bank-lines wrapper jobs: RETIRED 2026-10-13 ───
# The ingestion itself runs manually via scripts/Sukuk&Bonds.py and
# scripts/SAMA&GaStat.py — each __main__ records its own job_runs row under
# the keys 'sukuk_importer' / 'macro_importer' (trigger='manual'). The
# bank-lines verification wrapper wrote nothing and was dropped entirely.


# ─── Job: Filings Importer (Phase 12) ──────────────────────────
def job_filings_importer():
    logger.info("📑 [Scheduler] Official Filings check starting…")
    _safe_job("filings_importer")
    logger.info("📑 [Scheduler] Official Filings check finished.")


# ─── Job: REBH Engine Vintage Snapshot (Phase 12) ─────────────
def job_engine_vintages():
    logger.info("⚙️ [Scheduler] REBH Engine Vintage Snapshot job starting…")
    _safe_job("engine_vintages")
    logger.info("⚙️ [Scheduler] REBH Engine Vintage Snapshot job finished.")


# ─── Job: Market Breadth Daily Engine (REBH Breadth, 2026-10-01) ─
# Runs AFTER daily_market_update (18:30) + engine_vintages (20:00).
# The engine is vendored into backend/app/services/breadth_engine and runs in-process
# inside breadth_job.py. SINGLE ENVIRONMENT (2026-10-07): everything runs on the one
# project interpreter — the default is sys.executable, never a hard-coded machine path.
# BREADTH_ENGINE_PY exists only as an escape hatch (e.g. CI with a dedicated runner);
# leave it unset in normal operation.
BREADTH_ENGINE_PY_DEFAULT = sys.executable


def _breadth_daily_body():
    """Run the breadth engine subprocess. Raises on a non-zero exit code.

    Deliberately NOT wrapped in track_job here: the cron entry point below and
    the admin run-now dispatcher each open their own job_runs row, so one click
    or one cron fire produces exactly one record.
    """
    import subprocess
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[2]          # .../backend
    py = os.getenv("BREADTH_ENGINE_PY") or BREADTH_ENGINE_PY_DEFAULT
    script = backend_dir / "app" / "services" / "breadth_job.py"
    # printed because "breadth failed" without the interpreter name is undebuggable.
    logger.info(f"🌐 [Scheduler] MARKET BREADTH starting… py={py}")
    proc = subprocess.run(
        [py, "-X", "utf8", str(script)], cwd=str(backend_dir),
        capture_output=True, text=True, timeout=1800,
    )
    tail = (proc.stdout or "")[-600:]
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode} tail={tail} err={(proc.stderr or '')[-600:]}")
    logger.info(f"  ✅ Market Breadth done: {tail[:200]}")
    _clear_breadth_cache()
    return tail


def job_breadth_daily():
    """Cron entry point: records the run under triggered_by='cron'."""
    from app.services.job_tracking import track_job

    try:
        # breadth_runs (app/models/breadth.py) holds the per-date detail; this
        # row only records that the cron job itself ran and how it ended.
        with track_job("breadth_daily", "cron"):
            _breadth_daily_body()
    except Exception as e:
        logger.error(f"  ❌ Market Breadth failed: {e}")
        raise                                              # let _safe/APScheduler record it


def _clear_breadth_cache():
    """Invalidate rebh:breadth:* Redis keys so the API rebuilds from fresh tables."""
    try:
        import redis as sync_redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = sync_redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        keys = r.keys("rebh:breadth:*")
        if keys:
            r.delete(*keys)
        logger.info(f"  🗑️ Cleared {len(keys)} breadth cache keys")
        r.close()
    except Exception as e:
        logger.warning(f"  ⚠️ Breadth cache clear skipped: {e}")


# ─── Helpers ───────────────────────────────────────────────────
def _slug(name: str) -> str:
    """Fallback job key when a call site does not pass an explicit one."""
    import re
    return re.sub(r"[^a-z0-9:._-]+", "_", name.strip().lower()).strip("_")[:100]


def _safe(name: str, fn, job_key: str | None = None):
    """Run a scraper function with error handling.

    `job_key` is the stable machine name recorded in job_runs (defaults to a
    slug of the human label). Tracking does not change behaviour: exceptions
    are still swallowed here exactly as before — track_job re-raises them to
    us and records the row as 'failed' first.
    """
    from app.services.job_tracking import track_job, with_result_handling
    key = job_key or _slug(name)
    try:
        logger.info(f"  🔄 {name}…")
        with track_job(key, "cron") as run:
            result = fn()
            with_result_handling(run, result)
        logger.info(f"  ✅ {name} done")
    except Exception as e:
        logger.error(f"  ❌ {name} failed: {e}")


# ─── Runnable units — single source of truth for cron AND "Run Now" ───
# Each value is a zero-arg callable that does the work and returns whatever the
# underlying script returns (job_tracking infers rows_written / failure from it).
# Imports stay lazy (__import__ inside the body) so importing this module never
# pulls in Selenium or the scrapers, exactly as before the refactor.
def _fred(code: str) -> Callable[[], Any]:
    def _run():
        return __import__(
            "app.scrapers.fred_scraper", fromlist=["scrape_fred_indicator"]
        ).scrape_fred_indicator(code)
    return _run


def _sp500():
    return __import__(
        "app.scrapers.sp500_scraper", fromlist=["scrape_sp500"]
    ).scrape_sp500(mode="incremental")


def _treasury_gov():
    return __import__(
        "app.scrapers.treasury_gov_scraper", fromlist=["scrape_treasury_gov"]
    ).scrape_treasury_gov(mode="incremental")


def _sp500_pe():
    return __import__(
        "app.scrapers.sp500_pe_scraper", fromlist=["scrape_sp500_pe"]
    ).scrape_sp500_pe()


def _market_reports():
    return __import__(
        "scripts.update_market_reports", fromlist=["main"]
    ).main()


def _importer(fn_name: str,
              module: str = "app.services.rebh_importers_service") -> Callable[[], Any]:
    def _run():
        return getattr(__import__(module, fromlist=[fn_name]), fn_name)()
    return _run


def _with_one_retry(fn: Callable[[], Any]) -> Any:
    """Run a dict-returning scraper once, retry once on failure.

    These manual jobs are the ones blocked/served slowly by their sources
    (Investing.com 403s, QuikStrike error pages, GuruFocus behind Cloudflare +
    headless Chrome). Render's slowness is bursty, so one retry with fresh
    process state converts most transient failures without doubling the
    worst-case runtime. Failure honesty comes from the dict shape itself.
    """
    result = fn()
    if isinstance(result, dict) and not result.get("ok"):
        logger.warning("  🔁 first attempt failed, retrying once…")
        result = fn()
    return result


def _eurodollar():
    return _with_one_retry(lambda: __import__(
        "app.scrapers.eurodollar_scraper", fromlist=["scrape_eurodollar_futures"]
    ).scrape_eurodollar_futures(save_db=True, force=True, print_results=False))


def _cme_fedwatch():
    return _with_one_retry(lambda: __import__(
        "app.scrapers.cmefedwatch_scraper", fromlist=["scrape_cme_fedwatch"]
    ).scrape_cme_fedwatch(force=True))


_GURUFOCUS_URLS = {
    "SP500_EY": "https://www.gurufocus.com/economic_indicators/151/sp-500-earnings-yield",
    "SP500_PE": "https://www.gurufocus.com/economic_indicators/57/sp-500-pe-ratio",
}


def _gurufocus():
    """Both GuruFocus series, incrementally, each with one retry; aggregates
    into the single {ok, rows, stage_failures} shape job_tracking reads."""
    mod = __import__(
        "app.scrapers.gurufocus_scraper", fromlist=["scrape_gurufocus_indicator"]
    )
    per: Dict[str, Any] = {}
    total = 0
    failures = []
    for code, url in _GURUFOCUS_URLS.items():
        res = _with_one_retry(
            lambda u=url, c=code: mod.scrape_gurufocus_indicator(u, c, "incremental")
        )
        if not isinstance(res, dict):  # defensive: unknown return shape = failed
            res = {"ok": False, "rows": 0,
                  "stage_failures": [{"stage": f"gurufocus:{code}",
                                      "error": "scraper returned no result dict"}]}
        per[code] = res
        total += int(res.get("rows") or 0)
        failures.extend(res.get("stage_failures") or [])
    out: Dict[str, Any] = {"ok": not failures, "rows": total, "per_indicator": per}
    if failures:
        out["stage_failures"] = failures
    return out


JOB_CALLABLES: Dict[str, Callable[[], Any]] = {
    # scrapers (steps of the daily / weekly / monthly groups)
    "sp500": _sp500,
    "treasury_gov": _treasury_gov,
    "fred:BAMLC0A3CA": _fred("BAMLC0A3CA"),
    "fred:BAMLC0A4CBBB": _fred("BAMLC0A4CBBB"),
    "fred:BAMLC0A3CAEY": _fred("BAMLC0A3CAEY"),
    "fred:IC4WSA": _fred("IC4WSA"),
    "fred:UNRATE": _fred("UNRATE"),
    "fred:PAYEMS": _fred("PAYEMS"),
    "fred:SAUCPALTT01GPM": _fred("SAUCPALTT01GPM"),
    "sp500_pe": _sp500_pe,
    # separately-scheduled jobs
    "market_reports": _market_reports,
    "filings_importer": _importer("run_filings_importer"),
    # decoupled 2026-10-13 — dedicated snapshot module, not the importers service
    "engine_vintages": _importer("run_engine_vintages_job",
                                 "app.services.rebh_vintage_service"),
    "breadth_daily": _breadth_daily_body,
    # economy-rates manual jobs — Run Now-able and tracked in job_runs under
    # the same keys the registry lists (deliberately NOT on any cron: their
    # sources block datacenter IPs / need headless Chrome)
    "manual:eurodollar": _eurodollar,
    "manual:cme_fedwatch": _cme_fedwatch,
    "manual:gurufocus": _gurufocus,
}

# Human labels, so refactoring the group functions did not change the logs.
JOB_LABELS: Dict[str, str] = {
    "sp500": "SP500",
    "treasury_gov": "Treasury.gov",
    "fred:BAMLC0A3CA": "FRED:BAMLC0A3CA",
    "fred:BAMLC0A4CBBB": "FRED:BAMLC0A4CBBB",
    "fred:BAMLC0A3CAEY": "FRED:BAMLC0A3CAEY",
    "fred:IC4WSA": "IC4WSA",
    "fred:UNRATE": "FRED:UNRATE",
    "fred:PAYEMS": "FRED:PAYEMS",
    "fred:SAUCPALTT01GPM": "FRED:SAUCPALTT01GPM (Saudi CPI)",
    "sp500_pe": "SP500 PE (Multpl)",
    "market_reports": "Market Reports (Tadawul)",
    "filings_importer": "Filings Importer",
    "engine_vintages": "Engine Vintage Snapshot",
    "breadth_daily": "Market Breadth Daily Engine",
    "manual:eurodollar": "Eurodollar Futures (Investing.com)",
    "manual:cme_fedwatch": "CME FedWatch (QuikStrike)",
    "manual:gurufocus": "GuruFocus (SP500 EY + SP500 PE)",
}

# Jobs whose output is served through a Redis cache. The cron groups clear it
# once at the end of the group; a single manual run has to clear it too, or the
# admin sees stale numbers right after a successful Run Now.
# The three manual:* keys serve economic-indicator / eurodollar / fedwatch
# caches as well, so they join the clear list even though no cron fires them.
MANUAL_ECONOMIC_KEYS: Tuple[str, ...] = (
    "manual:eurodollar", "manual:cme_fedwatch", "manual:gurufocus",
)
ECONOMIC_CACHE_JOBS: Tuple[str, ...] = DAILY_SCRAPER_KEYS + WEEKLY_SCRAPER_KEYS + MONTHLY_SCRAPER_KEYS + MANUAL_ECONOMIC_KEYS


def _safe_job(key: str) -> None:
    """Run one JOB_CALLABLES entry through _safe under its recorded job key."""
    _safe(JOB_LABELS.get(key, key), JOB_CALLABLES[key], job_key=key)


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
        job_market_reports_scrapers,
        CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=18, minute=0, timezone=EGYPT_TZ),
        id="market_reports", name="Saudi Market Reports Scrapers", replace_existing=True,
    )
    # daily_market_update is manual / admin-triggered — see job comment above.
    # Sukuk / Macro / Bank-lines wrapper crons retired 2026-10-13 — the
    # ingestion runs manually via scripts/Sukuk&Bonds.py & SAMA&GaStat.py.
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
    _scheduler.add_job(
        job_breadth_daily,
        CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=21, minute=15, timezone=EGYPT_TZ),
        id="breadth_daily", name="Market Breadth Daily Engine", replace_existing=True,
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
