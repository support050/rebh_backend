"""System Registry — the single source of truth behind the admin Runbook.

WHY THIS FILE EXISTS
    Before this, "who owns what" lived in three places: the hand-maintained
    PIPELINES array in ``frontend/app/admin/page.tsx``, various ``.md`` notes and
    people's memory. All three drifted. This module is now authoritative:
    ``GET /api/admin/system-map/*`` renders straight from it, and the legacy
    /admin Pipeline tab reads its map from ``pipelines()`` below.

GROUND TRUTH RULE (project-wide)
    Every entry here was traced from *active code*, not from documentation:
      · job_name        → the key passed to track_job() in app/core/scheduler.py,
                          app/api/routes/admin.py, app/api/routes/rebh_engine.py,
                          app/services/scraper_service.py and the scripts' __main__
      · expected_schedule → the CronTrigger actually registered in start_scheduler()
      · tables[].table  → the real ``__tablename__`` (app/models/**), confirmed by
                          importing Base.metadata
      · tables[].date_column → a column that exists on that table
      · pages[]         → a real file under frontend/app/**/page.tsx
    Anything not provable that way carries ``"verified": False`` (tables) or an
    ``unverified`` word in its note. Treat those as leads to investigate, not facts.

MAINTENANCE
    When you add a scraper, a page or a table, add the entry here in the same
    commit. The Runbook UI shows the registry path so a future maintainer can
    find this file from the screen that is stale.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

# ─── Trading calendar ─────────────────────────────────────────────────────────
# Tadawul trades Sunday–Thursday; Friday and Saturday are the weekend.
# Public holidays are NOT modelled: no holiday table is populated in the active
# code (see the `note` issue on the `admin-ops` system), so a "holiday" is
# indistinguishable from a genuine outage. Freshness thresholds therefore carry
# one extra day of slack rather than trying to be exact.
SAUDI_TRADING_DAYS = ("sun", "mon", "tue", "wed", "thu")

# Days whose freshness must not be counted against a data source (weekend).
_WEEKEND = ("fri", "sat")


def trading_days_between(start: date, end: date) -> int:
    """Count Saudi trading days (Sun–Thu) in ``[start, end)``.

    Used to turn "the table's newest date is X" into "the table is N trading
    days behind". Returns 0 when ``start >= end`` so a fresh table is never
    reported as negative lag.
    """
    if start >= end:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        if cursor.strftime("%a").lower() not in _WEEKEND:
            days += 1
        cursor += timedelta(days=1)
    return days


def age_in_trading_days(latest: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """Trading days between ``latest`` and ``today`` (None when ``latest`` is None)."""
    if latest is None:
        return None
    return trading_days_between(latest, today or date.today())


# ─── Shorthand builders ───────────────────────────────────────────────────────
def _t(table: str, date_column: Optional[str], max_lag: Optional[int],
       source_job: Optional[str] = None, note: str = "", verified: bool = True,
       pages: Optional[List[str]] = None) -> Dict[str, Any]:
    """One table the system reads, with the column the Runbook probes for freshness.

    ``date_column=None`` means the table genuinely has no usable date column, so
    the Runbook can only count rows — never report "stale".
    ``pages`` optionally narrows the tab(s) that read it; used by the legacy
    pipelines map, which is table-shaped rather than system-shaped.
    """
    return {
        "table": table,
        "date_column": date_column,
        "max_lag_trading_days": max_lag,
        "source_job": source_job,
        "note": note,
        "verified": verified,
        "pages": pages or [],
    }


def _j(job_name: str, file: str, expected_schedule: str,
       max_allowed_age_hours: Optional[int] = None, trigger: str = "cron",
       writes: Optional[List[str]] = None, note: str = "",
       tracked: bool = True) -> Dict[str, Any]:
    """One job. ``job_name`` MUST equal the key written into ``job_runs``.

    ``tracked=False`` → the entry point is not wrapped by track_job(), so
    "did it run?" is unanswerable; the Runbook falls back to table freshness.
    ``max_allowed_age_hours=None`` → no schedule to be late for (manual-only).
    """
    return {
        "job_name": job_name,
        "file": file,
        "expected_schedule": expected_schedule,
        "max_allowed_age_hours": max_allowed_age_hours,
        "trigger": trigger,
        "tracked": tracked,
        "writes": writes or [],
        "note": note,
    }


def _b(layer: str, path: str, description: str) -> Dict[str, str]:
    return {"layer": layer, "path": path, "description": description}


def _i(severity: str, text: str) -> Dict[str, str]:
    """severity: 'bug' | 'watch' | 'note'."""
    return {"severity": severity, "text": text}


# ─── Hour budgets for the three cron cadences ─────────────────────────────────
_DAILY = 26          # Sun–Thu 18:00/19:45/20:00/21:15 jobs
_WEEKLY = 8 * 24     # Sunday-evening jobs get a week plus slack
_MONTHLY = 35 * 24   # day-of-month jobs
_USDAILY = 50        # Mon–Fri 01:00 US-source jobs (weekend gap makes 26h too tight)


# ══════════════════════════════════════════════════════════════════════════════
SYSTEMS: List[Dict[str, Any]] = [

    # ───────────────────────────── 1. Home ─────────────────────────────────
    {
        "id": "home",
        "name": "Home / Landing",
        "in_navbar": True,
        "navbar_label": "Home (logo)",
        "blurb": "Static landing dashboard. Hard-coded cards and links only — no "
                 "database or scraper dependency, so it can never be 'stale'.",
        "built": [
            _b("frontend", "frontend/app/page.tsx", "440-line static dashboard, no API calls"),
            _b("frontend", "frontend/components/Navbar.tsx", "site navigation, 9 dropdowns"),
        ],
        "pages": ["/"],
        "jobs": [],
        "tables": [],
        "issues": [
            _i("note", "No data dependency: the page renders the same whatever the "
                       "pipeline did or did not run. If the home page looks wrong, "
                       "it is a design question, never a data question."),
        ],
    },

    # ──────────────────── 2. Daily market data pipeline ────────────────────
    {
        "id": "market-data",
        "name": "Daily Market Data (prices · indicators · RS)",
        "in_navbar": False,
        "navbar_label": None,
        "blurb": "The spine of the platform: one long Selenium-driven run that "
                 "pulls Tadawul prices, then derives every daily indicator, RS "
                 "rating, breadth aggregate and sector rollup in a single guarded "
                 "sequence. Everything else on this page list reads its output.",
        "built": [
            _b("script", "backend/scripts/daily_market_update.py",
               "the pipeline; _guard() wraps every stage, latest_ready_date is held "
               "back when a critical stage fails"),
            _b("model", "backend/app/models/update_status.py",
               "single-row mutex + latest_ready_date (table update_status)"),
            _b("service", "backend/app/services/screener_daily_trend_service.py",
               "stage 8.1 — pre-aggregates Minervini trend counts"),
            _b("script", "backend/scripts/update_daily_market_breadth.py",
               "stage 8.x — raw SQL aggregate of stock_indicators into market_breadth"),
            _b("script", "backend/scripts/Scrape_aporia_saudi.py",
               "stage called by update_aporia_analytics() — Aporia Saudi table"),
            _b("script", "backend/scripts/verify_daily_update.py",
               "post-run checks incl. market_breadth row counts"),
            _b("route", "backend/app/api/routes/admin.py",
               "POST /api/admin/pipeline/daily-market-update/run|status|unlock"),
            _b("frontend", "frontend/app/admin/page.tsx",
               "'Daily Market Update' tab — trigger + live status + log tail"),
        ],
        "pages": [
            "/stocks", "/stocks/[symbol]", "/stocks/[symbol]/summary",
            "/stocks/[symbol]/financials", "/stocks/[symbol]/income-statement",
            "/stocks/[symbol]/balance-sheet", "/stocks/[symbol]/cash-flow",
            "/stocks/[symbol]/historical-price", "/stocks/[symbol]/earnings",
            "/stocks/[symbol]/dividends", "/stocks/[symbol]/ratings",
            "/stocks/[symbol]/peers", "/stocks/market-breadth",
            "/admin", "/market-pulse", "/industry-groups",
        ],
        "jobs": [
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — manual CLI or Admin dashboard only", None, "manual",
               writes=["prices", "stock_indicators", "rs_daily_v2", "market_breadth",
                       "screener_daily_trend_counts", "market_pulse",
                       "industry_group_history", "aporia_analytics"],
               note="Started by hand (python scripts/daily_market_update.py) or by "
                    "POST /api/admin/pipeline/daily-market-update/run. Both paths are "
                    "wrapped by track_job(), so job_runs does show the runs."),
        ],
        "tables": [
            _t("prices", "date", 1, "daily_market_update"),
            _t("stock_indicators", "date", 1, "daily_market_update"),
            _t("rs_daily_v2", "date", 1, "daily_market_update",
               note="model file is app/models/rs_daily.py — filename ≠ table name"),
            _t("market_breadth", "date", 1, "daily_market_update",
               note="written by raw SQL in scripts/update_daily_market_breadth.py; "
                    "model app/models/market_breadth.py is not imported by "
                    "app/core/database.py, so this table is NOT under Alembic"),
            _t("screener_daily_trend_counts", "date", 1, "daily_market_update"),
            _t("update_status", "latest_ready_date", 1, "daily_market_update",
               note="single row; the publish gate. latest_ready_date is what the "
                    "site treats as 'the last complete day'"),
            _t("aporia_analytics", "updated_at", 1, "daily_market_update",
               note="only updated when the pipeline stage runs; source is aporiaanalytics.com"),
            _t("static_stock_info", None, None, None,
               note="no date column at all — freshness is not measurable. Refreshed by "
                    "scripts/update_marginable_from_md.py (manual)"),
        ],
        "issues": [
            _i("bug", "daily_market_update.py is NOT in the APScheduler registry "
                      "(app/core/scheduler.py L295 comment says so explicitly). "
                      "prices / stock_indicators / rs_daily_v2 / market_breadth / "
                      "screener_daily_trend_counts / market_pulse / "
                      "industry_group_history all stop updating the moment nobody "
                      "clicks the Admin button. Deliberate for now (long Selenium run) "
                      "but it is the single biggest staleness risk on the platform."),
            _i("note", "2026-10-12 · Bug 1 remains as instructed — the scheduler still "
                       "does not add daily_market_update; Run now on this job is "
                       "DELEGATED to POST /api/admin/pipeline/daily-market-update/run "
                       "(that endpoint owns the update_status mutex + returns 409 on "
                       "double-fire). system_map.py exposes this via run.delegated_to."),
            _i("watch", "A successful job_runs row does NOT prove rows were written. "
                        "update_daily() traps its own stage errors and returns "
                        "{ok, stage_failures, quality}; track_job records that as "
                        "failed, but 'success with 0 rows' is still possible — that is "
                        "what the Tables panel's lag check is for."),
            _i("watch", "market_breadth is read by a live endpoint but its model is "
                        "not imported by app/core/database.py or app/models/__init__.py, "
                        "so Base.metadata (and therefore Alembic autogenerate) does not "
                        "know it. weekly_market_reports was fixed on 2026-10-12 — see "
                        "system 'weekly-pdf'. market_breadth is still not under "
                        "Alembic; a fresh environment will not have that table until "
                        "it is created by hand."),
        ],
    },

    # ───────────────────────── 3. Screeners ────────────────────────────────
    {
        "id": "screeners",
        "name": "Screeners & Technical Filters",
        "in_navbar": True,
        "navbar_label": "Screeners",
        "blurb": "Read-only views over stock_indicators / rs_daily_v2 / prices, plus "
                 "the pre-aggregated Minervini trend counts and the A/D + %-above-MA "
                 "breadth aggregates. No screener owns data — they all consume the "
                 "market-data pipeline.",
        "built": [
            _b("route", "backend/app/api/routes/screeners.py", "/api/screeners/* (public)"),
            _b("route", "backend/app/api/routes/technical_screener.py", "/api/technical-screener/*"),
            _b("route", "backend/app/api/routes/market_breadth.py", "/api/market-breadth/*"),
            _b("service", "backend/app/services/minervini_cache.py",
               "historical-trend responses, Redis cached"),
            _b("model", "backend/app/models/stock_indicators.py", "stock_indicators"),
            _b("model", "backend/app/models/market_breadth.py",
               "market_breadth — NOT registered in Base.metadata (see issues)"),
            _b("frontend", "frontend/app/api/screener/route.ts",
               "Next.js route handler — /screener does NOT call FastAPI"),
        ],
        "pages": [
            "/screeners", "/screeners/rsi", "/screeners/alrayan", "/screeners/alhussain",
            "/screeners/Alrayan&Alhussain", "/screeners/Composite", "/screeners/new-highs",
            "/screeners/ad-rating", "/technical-screener", "/screener",
            "/rs-screener", "/minervini-trend", "/Percent_of_Stocks_Above_MA",
        ],
        "jobs": [
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — see market-data", None, "manual",
               writes=["stock_indicators", "screener_daily_trend_counts", "market_breadth"],
               note="the only writer for everything this system reads"),
        ],
        "tables": [
            _t("stock_indicators", "date", 1, "daily_market_update"),
            _t("screener_daily_trend_counts", "date", 1, "daily_market_update"),
            _t("market_breadth", "date", 1, "daily_market_update"),
            _t("rs_daily_v2", "date", 1, "daily_market_update"),
            _t("prices", "date", 1, "daily_market_update"),
        ],
        "issues": [
            _i("watch", "/screener and /rs-screener are near-standalone pages: "
                        "/screener talks to the Next.js handler "
                        "frontend/app/api/screener/route.ts, not to FastAPI. Grepping "
                        "backend/ for those pages' data flow will find nothing."),
            _i("note", "/screeners/ad-rating exists in the tree but is commented out of "
                       "Navbar.tsx, so it is reachable only by URL."),
        ],
    },

    # ───────────────────────── 4. Watchlist / RS ───────────────────────────
    {
        "id": "watchlist",
        "name": "Watchlist & Relative Strength",
        "in_navbar": True,
        "navbar_label": "Watchlist",
        "blurb": "User watchlists plus the RS Rating / RS Line / Mansfield-RS / SATA "
                 "indicator family. All RS numbers are computed inside the "
                 "market-data pipeline; these endpoints only read and re-plot them.",
        "built": [
            _b("route", "backend/app/api/routes/rs.py", "/api/rs/* (protected)"),
            _b("route", "backend/app/api/routes/rs_v2.py", "/api/rs-v2/* (protected)"),
            _b("route", "backend/app/api/routes/rs_line.py", "RS Line indicators"),
            _b("route", "backend/app/api/routes/mansfield_rs.py", "/api/indicators/mansfield-rs"),
            _b("route", "backend/app/api/routes/sata.py", "/api/indicators/sata"),
            _b("service", "backend/app/services/rs_line.py", "RS line maths off prices"),
            _b("service", "backend/app/services/mansfield_rs.py", "Mansfield RS"),
            _b("service", "backend/app/services/sata.py", "SATA & stage analysis"),
            _b("model", "backend/app/models/rebh_user_data.py", "user_watchlists"),
            _b("model", "backend/app/models/user_prefs.py", "user_prefs"),
            _b("frontend", "frontend/app/watchlist/page.tsx",
               "tabs: Overview · RS Matrix · Matrix Chart · RS Screener"),
        ],
        "pages": [
            "/watchlist", "/rs-analysis", "/rs-line", "/market_rs",
        ],
        "jobs": [
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — see market-data", None, "manual",
               writes=["rs_daily_v2", "stock_indicators", "prices"]),
        ],
        "tables": [
            _t("rs_daily_v2", "date", 1, "daily_market_update"),
            _t("stock_indicators", "date", 1, "daily_market_update"),
            _t("prices", "date", 1, "daily_market_update"),
            _t("user_watchlists", "created_at", None, None,
               note="user data, not pipeline data — staleness is meaningless here"),
            _t("user_prefs", "updated_at", None, None, note="user data"),
        ],
        "issues": [
            _i("note", "rs_daily_v2 is served by three different routers (rs, rs_v2, "
                       "screeners). One table, three response shapes — a schema change "
                       "here must be checked against all three."),
        ],
    },

    # ─────────────────────────── 5. Learn ──────────────────────────────────
    {
        "id": "learn",
        "name": "Learn (static)",
        "in_navbar": True,
        "navbar_label": "Learn",
        "blurb": "Four prose pages with no API and no database. Registered so the "
                 "navbar has complete coverage, not because it can break.",
        "built": [
            _b("frontend", "frontend/app/learn/golden-rules/page.tsx", "Dan Zanger rules"),
            _b("frontend", "frontend/app/learn/chart-patterns/page.tsx", "chart patterns"),
            _b("frontend", "frontend/app/learn/useful-stock-sources/page.tsx", "resources"),
            _b("frontend", "frontend/app/learn/recommended-reading/page.tsx", "reading list"),
        ],
        "pages": [
            "/learn/golden-rules", "/learn/chart-patterns",
            "/learn/useful-stock-sources", "/learn/recommended-reading",
        ],
        "jobs": [],
        "tables": [],
        "issues": [
            _i("note", "/learn itself has no page.tsx — the navbar item points at a "
                       "folder with only children. Those four child pages are the "
                       "real targets."),
        ],
    },

    # ──────────────────────── 6. Saudi Market ──────────────────────────────
    {
        "id": "market",
        "name": "Saudi Market (reports · pulse · valuation)",
        "in_navbar": True,
        "navbar_label": "Market",
        "blurb": "The Saudi Exchange disclosure/report pages, the daily market pulse, "
                 "the sector rollups, the RS hub and the multi-tab valuation workbench. "
                 "Two different feeders: the nightly SaudiExchange scraper run and the "
                 "market-data pipeline.",
        "built": [
            _b("route", "backend/app/api/routes/market_reports.py",
               "/api/market-reports/* (substantial holders, net short, headroom, "
               "buybacks, SBL, historical reports, qfi-flows)"),
            _b("route", "backend/app/api/routes/market_pulse.py", "/api/market-pulse/*"),
            _b("route", "backend/app/api/routes/tasi_settings.py", "/api/tasi-settings"),
            _b("route", "backend/app/api/routes/industry_groups.py", "/api/industry-groups/*"),
            _b("route", "backend/app/api/routes/aporia.py", "/api/aporia/* (protected)"),
            _b("route", "backend/app/api/routes/valuation.py", "/api/valuation/*"),
            _b("route", "backend/app/api/routes/valuation_admin_config.py",
               "/api/admin/valuation-config (admin)"),
            _b("service", "backend/app/services/valuation_service.py",
               "the valuation engine behind /valuation tabs"),
            _b("service", "backend/app/services/market_pulse_calc.py", "pulse aggregates"),
            _b("scraper", "backend/app/scrapers/daily_financial_indicators_scraper.py",
               "TASI weights / EPS / prices — feeds tasi_components"),
            _b("script", "backend/scripts/update_market_reports.py",
               "the SaudiExchange reports orchestrator"),
            _b("model", "backend/app/models/market_reports.py",
               "substantial_shareholders, net_short_positions, foreign_headrooms, "
               "share_buybacks, sbl_positions, historical_reports, qfi_ownership_flows"),
        ],
        "pages": [
            "/market-reports", "/market-reports/substantial-shareholders",
            "/market-reports/net-short-positions", "/market-reports/foreign-headroom",
            "/market-reports/share-buybacks", "/market-reports/sbl-positions",
            "/market-reports/historical-reports",
            "/market-pulse", "/market-pulse/settings", "/saudi-analytics",
            "/industry-groups",
            "/valuation", "/valuation/economy", "/valuation/bond",
            "/valuation/treasury-curve", "/valuation/treasury-daily",
            "/valuation/historical-pe", "/valuation/sp500-scenarios",
            "/valuation/market-weight", "/valuation/report",
            "/weekly-study",
        ],
        "jobs": [
            _j("market_reports", "backend/scripts/update_market_reports.py",
               "cron Sun–Thu 18:00 (Africa/Cairo)", _DAILY, "cron",
               writes=["substantial_shareholders", "net_short_positions",
                       "foreign_headrooms", "share_buybacks", "sbl_positions",
                       "historical_reports"],
               note="2026-10-13 incident: Run Now showed 'Scraped 0 Substantial "
                    "Shareholders records' — Selenium's urllib3 client to the local "
                    "chromedriver hit its 120s read timeout while Chrome was still "
                    "loading the Tadawul page, and the run was recorded green with "
                    "0 rows. Fixed 2026-10-13: page_load_strategy='eager' + 300s "
                    "client timeout in build_driver, read-timeout falls through to "
                    "partial parse in market_reports_scraper, one fresh-driver retry "
                    "per scraper, and main() now returns "
                    "{ok, stage_failures, rows} so an all-zero run records as failed. "
                    "Partial zeros stay success (some reports legitimately publish "
                    "empty tables)."),
            _j("scraper:tasi_components", "backend/app/scrapers/daily_financial_indicators_scraper.py",
               "manual — Admin 'Daily Financial Indicators' button "
               "(POST /api/admin/system/run-scraper {\"scraper\":\"tasi\"})",
               None, "manual", writes=["tasi_components"],
               note="key recorded by app/services/scraper_service.py:run_tasi_components. "
                    "Before 2026-10-11 this path imported a module that does not exist "
                    "and did nothing."),
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — see market-data", None, "manual",
               writes=["market_pulse", "industry_group_history", "aporia_analytics"]),
        ],
        "tables": [
            _t("substantial_shareholders", "report_date", 3, "market_reports"),
            _t("net_short_positions", "report_date", 3, "market_reports"),
            _t("foreign_headrooms", "report_date", 3, "market_reports"),
            _t("share_buybacks", "report_date", 3, "market_reports"),
            _t("sbl_positions", "report_date", 3, "market_reports"),
            _t("historical_reports", "report_date", 7, "market_reports"),
            _t("qfi_ownership_flows", "report_date", None, None,
               verified=False,
               note="read by GET /api/market-reports/qfi-flows but NO writer exists in "
                    "active code — origin unverified"),
            _t("market_pulse", "date", 1, "daily_market_update",
               note="stage 3.5 of the manual pipeline"),
            _t("industry_group_history", "date", 1, "daily_market_update",
               note="stage 7. Model app/models/industry_group.py is not imported by "
                    "database.py, so the table is invisible to Alembic autogenerate "
                    "(it is created by migration f12345678901)"),
            _t("tasi_components", "updated_at", 5, "scraper:tasi_components",
               note="no business date column — updated_at is all there is",
               pages=["/valuation/market-weight", "/valuation/report"]),
            _t("aporia_charts", "updated_at", None, "daily_market_update",
               verified=False,
               note="read by /api/aporia/chart/{ticker}; no writer found in active code"),
            _t("tasi_settings", "updated_at", None, None,
               note="operator-maintained settings for market pulse"),
            _t("system_config", "updated_at", None, None,
               note="valuation configuration, edited from the admin config routes"),
            _t("valuation_zones", "created_at", None, None, verified=False,
               note="seeded reference data; no live writer identified"),
            _t("eps_estimates", "updated_at", None, None, verified=False,
               note="seeded reference data; no live writer identified"),
        ],
        "issues": [
            _i("watch", "market_pulse and industry_group_history look like standalone "
                        "daily features but are stages of the manual daily_market_update "
                        "run — so /market-pulse and /industry-groups go stale with it, "
                        "not with the reports scraper."),
            _i("note", "/valuation is one page with 8 tabs (economy, bond, "
                       "treasury-curve, treasury-daily, historical-pe, "
                       "sp500-scenarios, market-weight, report). The old admin map "
                       "listed only two of them."),
            _i("watch", "valuation_admin_system.py (GET /api/admin/system/stats, "
                        "POST /api/admin/system/run-scraper) overlaps this runbook and "
                        "keeps its own in-memory status. Left untouched by agreement; "
                        "job_runs is the durable source."),
        ],
    },

    # ──────────────────── 7. Economy, rates & macro ────────────────────────
    {
        "id": "economy-rates",
        "name": "Economy, Rates & Macro",
        "in_navbar": False,
        "navbar_label": None,
        "blurb": "US/global macro (FRED, Treasury.gov, Multpl, S&P 500) and Saudi macro "
                 "(SAMA, GaStat) plus the Sukuk/debt snapshot. Reached only by direct "
                 "URL — none of these pages appear in the navbar.",
        "built": [
            _b("route", "backend/app/api/routes/economic_indicators.py",
               "/api/economic-indicators/* — GET per code + POST /scrape/<source>"),
            _b("scraper", "backend/app/scrapers/fred_scraper.py", "FRED_API → economic_indicators"),
            _b("scraper", "backend/app/scrapers/treasury_gov_scraper.py",
               "Treasury.gov → treasury_yield_curves (the ONLY treasury module; there is "
               "no treasury_scraper.py)"),
            _b("scraper", "backend/app/scrapers/sp500_scraper.py", "OHLC + PE → sp500_history"),
            _b("scraper", "backend/app/scrapers/sp500_pe_scraper.py", "Multpl.com PE"),
            _b("scraper", "backend/app/scrapers/sp500_ey_scraper.py", "S&P 500 earnings yield"),
            _b("scraper", "backend/app/scrapers/eurodollar_scraper.py", "Investing.com snapshot"),
            _b("scraper", "backend/app/scrapers/cmefedwatch_scraper.py", "CME FedWatch snapshot"),
            _b("scraper", "backend/app/scrapers/gurufocus_scraper.py",
               "SP500_EY / SP500_PE (Selenium) — exists, not scheduled"),
            _b("service", "backend/app/services/rebh_importers_service.py",
               "health aggregation only — the sukuk/macro wrapper importers "
               "were retired 2026-10-13"),
            _b("script", "backend/scripts/Sukuk&Bonds.py",
               "THE sukuk ingestion entry point — run manually; records "
               "job_runs key 'sukuk_importer' (trigger='manual')"),
            _b("script", "backend/scripts/SAMA&GaStat.py",
               "THE SAMA/GaStat macro ingestion entry point — run manually; "
               "records job_runs key 'macro_importer' (trigger='manual')"),
            _b("model", "backend/app/models/economic_indicators.py",
               "5 models in one file: economic_indicators, treasury_yield_curves, "
               "sp500_history, eurodollar_futures, cme_fedwatch"),
            _b("model", "backend/app/models/saudi_macro.py", "saudi_economic_indicators"),
            _b("model", "backend/app/models/sukuk_bonds.py", "sukuk_market_data"),
        ],
        "pages": [
            "/economic-indicators", "/economic-indicators/unrate",
            "/economic-indicators/payems", "/economic-indicators/nfp-change",
            "/economic-indicators/ic4wsa",
            "/bonds/yield-curve", "/bonds/treasury-yield-curve",
            "/bonds/daily-treasury-yield-curve",
            "/spread/a-corporate", "/spread/bbb-corporate",
            "/interest-rate/eurodollar-futures", "/interest-rate/cme-fedwatch",
            "/market/a-effective-yield", "/market/sp500-earnings-yield",
            "/market/sp500-pe-ratio", "/market/yri-earnings-outlook",
        ],
        "jobs": [
            _j("sp500", "backend/app/scrapers/sp500_scraper.py",
               "cron Mon–Fri 01:00 (Africa/Cairo)", _USDAILY, "cron", ["sp500_history"]),
            _j("treasury_gov", "backend/app/scrapers/treasury_gov_scraper.py",
               "cron Mon–Fri 01:00 (Africa/Cairo)", _USDAILY, "cron", ["treasury_yield_curves"]),
            _j("fred:BAMLC0A3CA", "backend/app/scrapers/fred_scraper.py",
               "cron Mon–Fri 01:00", _USDAILY, "cron", ["economic_indicators"]),
            _j("fred:BAMLC0A4CBBB", "backend/app/scrapers/fred_scraper.py",
               "cron Mon–Fri 01:00", _USDAILY, "cron", ["economic_indicators"]),
            _j("fred:BAMLC0A3CAEY", "backend/app/scrapers/fred_scraper.py",
               "cron Mon–Fri 01:00", _USDAILY, "cron", ["economic_indicators"]),
            _j("fred:IC4WSA", "backend/app/scrapers/fred_scraper.py",
               "cron Fri 01:30", _WEEKLY, "cron", ["economic_indicators"]),
            _j("fred:UNRATE", "backend/app/scrapers/fred_scraper.py",
               "cron monthly, day 10 at 02:00", _MONTHLY, "cron", ["economic_indicators"]),
            _j("fred:PAYEMS", "backend/app/scrapers/fred_scraper.py",
               "cron monthly, day 10 at 02:00", _MONTHLY, "cron", ["economic_indicators"]),
            _j("fred:SAUCPALTT01GPM", "backend/app/scrapers/fred_scraper.py",
               "cron monthly, day 10 at 02:00", _MONTHLY, "cron", ["economic_indicators"],
               note="IMF Saudi Arabia CPI growth rate (YoY %) via FRED. Feeds "
                    "GET /api/rebh/market-macro -> saudi_macro.inflation_pct as a "
                    "fallback when saudi_economic_indicators has no 'saudi_inflation_cpi' "
                    "row. Series ID SAUCPALTT01GPM already carries the growth value; "
                    "no units=pc1 is applied. First run fetches full history; "
                    "subsequent runs use 365-day lookback for revisions."),
            _j("sp500_pe", "backend/app/scrapers/sp500_pe_scraper.py",
               "cron monthly, day 10 at 02:00", _MONTHLY, "cron", ["sp500_history"]),
            # sukuk_importer / macro_importer / bank_lines_importer wrapper
            # JOBS RETIRED 2026-10-13. Sukuk & macro ingestion runs manually via
            # scripts/Sukuk&Bonds.py and scripts/SAMA&GaStat.py — their __main__
            # blocks still record job_runs under 'sukuk_importer' /
            # 'macro_importer', so the freshness probes below keep answering
            # 'when did it last run?' from the tables and the ledger. The
            # bank_lines verification wrapper wrote nothing and is gone.
            _j("manual:eurodollar", "backend/app/scrapers/eurodollar_scraper.py",
               "manual only — Admin button POST /api/economic-indicators/scrape/eurodollar-futures "
               "or Run Now",
               None, "manual", writes=["eurodollar_futures"], tracked=True,
               note="not scheduled on purpose: Investing.com returns 403 from "
                    "datacenter IPs. Since 2026-10-13 the Admin endpoint wraps the "
                    "scrape in track_job('manual:eurodollar') and job_runner has a "
                    "Run Now callable, so runs land in job_runs."),
            _j("manual:cme_fedwatch", "backend/app/scrapers/cmefedwatch_scraper.py",
               "manual only — Admin button POST /api/economic-indicators/scrape/cme-fedwatch "
               "or Run Now",
               None, "manual", writes=["cme_fedwatch"], tracked=True,
               note="not scheduled: QuikStrike is flaky from server IPs. Since "
                    "2026-10-13 both the Admin endpoint and Run Now record under "
                    "track_job('manual:cme_fedwatch'); the scraper now returns "
                    "{ok, rows} so rows_written is real, and a failed snapshot is "
                    "recorded red instead of an empty success."),
            _j("manual:gurufocus", "backend/app/scrapers/gurufocus_scraper.py",
               "manual only — Admin monthly buttons /scrape/SP500_EY and /scrape/SP500_PE "
               "or Run Now",
               None, "manual", writes=["economic_indicators"], tracked=True,
               note="un-scheduled from the monthly cron for RAM/CPU (headless Chrome). "
                    "Since 2026-10-13 tracked (Admin endpoints + Run Now via "
                    "scheduler._gurufocus) and the driver comes from the shared "
                    "daily_detailed_scraper.build_driver (page_load_strategy=eager, "
                    "300s client timeout). sp500_ey_scraper.py is the lighter "
                    "alternative used by scraper_service.run_sp500_ey."),
            _j("scraper:sp500_ey", "backend/app/scrapers/sp500_ey_scraper.py",
               "manual — POST /api/admin/system/run-scraper {\"scraper\":\"sp500_ey\"}",
               None, "manual", writes=["economic_indicators"], tracked=True,
               note="key comes from app/services/scraper_service.py:_run(); the same "
                    "prefixing yields scraper:fred:<code>, scraper:sp500_price, "
                    "scraper:sp500_pe, scraper:treasury_gov. Source chain since "
                    "2026-10-13: GuruFocus (403 from datacenter IPs) → Yahoo "
                    "quoteSummary with cookie+crumb (plain GET = 401) → own "
                    "sp500_history.pe_ratio (EY=100/PE, sanity 0.5–15%)."),
        ],
        "tables": [
            _t("economic_indicators", "report_date", 7,
               "fred:UNRATE", note="FRED + GuruFocus series, one row per code per date",
               pages=["/economic-indicators", "/economic-indicators/unrate",
                      "/economic-indicators/payems", "/economic-indicators/nfp-change",
                      "/economic-indicators/ic4wsa", "/spread/a-corporate",
                      "/spread/bbb-corporate", "/market/a-effective-yield",
                      "/market/sp500-earnings-yield", "/market/sp500-pe-ratio",
                      "/market/yri-earnings-outlook", "/valuation/economy"]),
            _t("treasury_yield_curves", "report_date", 4, "treasury_gov",
               pages=["/bonds/yield-curve", "/bonds/treasury-yield-curve",
                      "/bonds/daily-treasury-yield-curve", "/valuation/treasury-curve",
                      "/valuation/treasury-daily", "/valuation/bond"]),
            _t("sp500_history", "trade_date", 4, "sp500",
               pages=["/bonds/yield-curve", "/valuation/sp500-scenarios",
                      "/valuation/historical-pe", "/market/sp500-pe-ratio"]),
            _t("eurodollar_futures", "scrape_date", None, "manual:eurodollar",
               note="snapshot only — Investing.com blocks server IPs, so the scheduler "
                    "deliberately does not run it; Admin button triggers it by hand",
               pages=["/interest-rate/eurodollar-futures"]),
            _t("cme_fedwatch", "scrape_date", None, "manual:cme_fedwatch",
               note="snapshot only — CME/QuikStrike blocks server IPs (Selenium)",
               pages=["/interest-rate/cme-fedwatch"]),
            _t("saudi_economic_indicators", "retrieved_at", 35, "macro_importer",
               note="NO business date column: `period` is a String like '2026-03' or "
                    "'2025-Q4'. retrieved_at = when we scraped it, not when SAMA "
                    "published it — so lag here measures our own run, not the data. "
                    "Written by scripts/SAMA&GaStat.py (manual), not by a cron job"),
            _t("sukuk_market_data", "updated_at", 10, "sukuk_importer",
               note="`as_of` is a String; updated_at is the only real timestamp. "
                    "Written by scripts/Sukuk&Bonds.py (manual), not by a cron job"),
        ],
        "issues": [
            _i("note", "2026-10-13 · Importer wrappers retired. The cron jobs and "
                       "admin triggers for sukuk_importer / macro_importer / "
                       "bank_lines_importer were removed — ingestion runs manually "
                       "via scripts/Sukuk&Bonds.py and scripts/SAMA&GaStat.py, "
                       "which still record job_runs under their old keys, so the "
                       "sukuk_market_data / saudi_economic_indicators freshness "
                       "probes stay meaningful. POST /api/rebh/importers/run now "
                       "accepts only 'filings' and 'vintages'; /importers/health "
                       "reads table freshness from Postgres directly. "
                       "engine_vintages moved to app/services/rebh_vintage_service.py "
                       "(same cron key, plus a CLI: python -m "
                       "app.services.rebh_vintage_service)."),
            _i("note", "2026-10-13 · economy-rates reporting overhaul. Every scraper "
                       "in this system (sp500, treasury_gov, fred, sp500_pe, sp500_ey, "
                       "eurodollar, cmefedwatch, gurufocus) now returns "
                       "{ok, rows, [stage_failures]} instead of a bare bool/None, so "
                       "job_tracking.with_result_handling fills job_runs.rows_written "
                       "(the System Map 'Rows' column was '—' because bools are skipped) "
                       "and a 0-row/failed run records status='failed' (red) instead of "
                       "an empty success. The three manual jobs became Run Now "
                       "callables in scheduler.JOB_CALLABLES and their Admin scrape "
                       "endpoints went through track_job, clearing the 'Untracked' "
                       "state. gurufocus switched to the shared hardened build_driver. "
                       "The 'Bad / Never' states seen for the other jobs simply mean "
                       "job_runs has no row for that key yet — the table only shipped "
                       "2026-10-11 and cron requires ENABLE_SCHEDULER=true on the "
                       "Render background worker."),
            _i("note", "2026-10-12 · Bug 2 FIXED. GET /api/rebh/market-macro used to "
                       "read SaudiEconomicIndicator.report_date (does not exist on "
                       "app/models/saudi_macro.py — real columns are period:String / "
                       "retrieved_at / updated_at). The AttributeError was swallowed "
                       "by a blanket except-pass, which then skipped the S&P 500 and "
                       "Treasury blocks below, so the endpoint always returned the "
                       "hard-coded fallbacks (repo 4.25 / SAIBOR 3.897 / GDP 4.2 / CPI "
                       "1.6 / UE 7.8). The endpoint now reads DISTINCT ON (indicator_key) "
                       "over coalesce(updated_at, retrieved_at) DESC NULLS LAST, loads "
                       "SP500History.trade_date + TreasuryYieldCurve.report_date for "
                       "real, derives gdp_growth_pct from the saudi_gdp_annual TTM "
                       "history (needs 2 rows 1 calendar year apart, same quarter), "
                       "wraps each section in its own try/except that appends to "
                       "a `degraded` array, and returns null for anything unresolved. "
                       "Frontend consumer app/rebh/tools/components/MarketMonitorTab.tsx "
                       "was cleaned of its fabricated fallbacks (\"Expansionary / "
                       "Healthy\" pill, `null%` for every null indicator, `+{...}%` "
                       "prefix, buffett fallback into liquidity_condition). It now "
                       "shows an em dash + a small caption for every unresolved "
                       "indicator and surfaces the degraded array in a details block."),
            _i("note", "2026-10-12 · Saudi inflation is now ingested live. "
                       "fred_scraper.py gained SAUCPALTT01GPM (IMF Saudi Arabia CPI "
                       "growth rate, YoY %) under FRED_SERIES_CONFIG and the "
                       "MONTHLY_REVISED_SERIES set (365-day lookback for revisions). "
                       "scheduler.py runs it as job key 'fred:SAUCPALTT01GPM' in "
                       "MONTHLY_SCRAPER_KEYS (day 10 at 02:00 Africa/Cairo). "
                       "rebh_engine.py:/market-macro reads the latest row from "
                       "economic_indicators WHERE indicator_code='SAUCPALTT01GPM' "
                       "as a fallback when saudi_economic_indicators has no "
                       "saudi_inflation_cpi key, populating inflation_pct and "
                       "inflation_source='fred:SAUCPALTT01GPM @ <report_date>'. "
                       "Upsert uses ON CONFLICT (report_date, indicator_code) "
                       "DO UPDATE via index_elements (NOT the named constraint "
                       "— see the pitfall watch below). Backfill: "
                       "`python -m app.scrapers.fred_scraper SAUCPALTT01GPM` "
                       "(full history on first run) — or admin → System map → "
                       "macro-scraper → Run now on fred:SAUCPALTT01GPM. Run it "
                       "once manually after deploy so inflation_pct is populated "
                       "immediately, without waiting for the day-10 cron."),
            _i("watch", "Pitfall 2026-10-12 · The unique constraint on "
                        "economic_indicators (report_date, indicator_code) is "
                        "declared in app/models/economic_indicators.py as "
                        "`uq_economic_indicator_date_code`, but the live "
                        "Render schema has it under a different name (an "
                        "earlier alembic autogenerate pass created it before "
                        "the name was set on the model). Any ON CONFLICT "
                        "ON CONSTRAINT <name> therefore fails with "
                        "'constraint ... does not exist'. Fix: target the "
                        "index by its column tuple — "
                        "on_conflict_do_update(index_elements=[\"report_date\", "
                        "\"indicator_code\"]) — which is what "
                        "app/scrapers/fred_scraper.py now does. If we ever "
                        "want to align the name, an alembic op.drop_constraint "
                        "followed by op.create_unique_constraint with "
                        "name='uq_economic_indicator_date_code' is the safe "
                        "path; until then, do NOT reintroduce the named "
                        "reference."),
            _i("watch", "`saudi_inflation_cpi` is still not written in "
                        "saudi_economic_indicators by scripts/SAMA&GaStat.py "
                        "(grep SaudiEconomicIndicator writes: repo_rate / "
                        "reverse_repo_rate / saibor_3m / saibor_12m / "
                        "saudi_gdp_annual / saudi_unemployment / "
                        "saudi_buffett_indicator). The market-macro endpoint "
                        "prefers the SAMA/GaStat row if it is ever added and "
                        "falls back to FRED SAUCPALTT01GPM in "
                        "economic_indicators. Both absent ⇒ degraded adds the "
                        "run-fred hint."),
            _i("watch", "`saudi_gdp_annual` is a GDP LEVEL in million SAR (unit='M SAR') "
                        "— not a growth rate. The old code put it into gdp_growth_pct and "
                        "compared >= 3.0 for the regime label; that was semantically wrong. "
                        "The endpoint now returns the level in gdp_level_m_sar / "
                        "gdp_level_unit / gdp_level_period and derives gdp_growth_pct "
                        "from the TTM history: two saudi_gdp_annual rows with period "
                        "like '2025-Q4-TTM' and '2024-Q4-TTM' (same quarter, year "
                        "offset 1, prev value > 0) yield a real YoY rate; anything "
                        "else leaves gdp_growth_pct null with a `degraded` note. "
                        "gdp_growth_origin tells the caller whether the value is "
                        "direct:<key> or derived:<reason>."),
            _i("watch", "Same hard-coded-default pattern still exists in "
                        "app/services/course_labs_service.py:calculate_economy_scorecard "
                        "(5.50 / 5.80 / 4.2 / 1.6 / 7.8). It is a DIFFERENT endpoint "
                        "outside the Bug-2 scope, so it was not touched — reported here "
                        "so nobody assumes market-macro was the only one."),
            _i("watch", "eurodollar + CME FedWatch have Admin buttons but are not "
                       "scheduled: both sources block datacenter IPs (403 / error page). "
                       "Their tables are snapshots, so a stale snapshot is expected, not "
                       "an outage — max_lag is intentionally None."),
            _i("note", "gurufocus_scraper.py and cmefedwatch_scraper.py need headless "
                       "Chrome; the scheduler comment says GuruFocus was un-scheduled for "
                       "RAM/CPU reasons on the server. The Admin monthly buttons still "
                       "call them."),
            _i("note", "/valuation/economy, /valuation/bond and /valuation/treasury-* "
                       "render the same tables from the REBH-side valuation service, so a "
                       "macro outage shows up in two different places of the UI."),
        ],
    },

    # ──────────────────── 8. REBH (company workbench) ──────────────────────
    {
        "id": "rebh",
        "name": "REBH Company Workbench",
        "in_navbar": True,
        "navbar_label": "REBH",
        "blurb": "The Khurafshi/REBH engine: one-company-deep analysis pages, the 31-rule "
                 "council, scorecards, labs, quarantine pile and the vintage snapshot that "
                 "freezes engine output per date for reproducibility.",
        "built": [
            _b("route", "backend/app/api/routes/rebh_engine.py",
               "/api/rebh/* (public) + /api/engine/* (the engine itself)"),
            _b("route", "backend/app/api/routes/official_filings.py",
               "/api/ingest/official-reports, /api/reports/{symbol}"),
            _b("service", "backend/app/services/khurafshi_engine_service.py",
               "live company stats, peers, classification"),
            _b("service", "backend/app/services/rebh_classification_service.py",
               "company classification"),
            _b("service", "backend/app/services/rebh_importers_service.py",
               "filings check + importers health (sukuk/macro/bank-lines "
               "wrappers retired 2026-10-13)"),
            _b("service", "backend/app/services/rebh_vintage_service.py",
               "run_engine_vintages_job — dedicated snapshot runner "
               "(cron / admin / CLI)"),
            _b("model", "backend/app/models/rebh_universe_snapshot.py", "rebh_universe_snapshots"),
            _b("model", "backend/app/models/rebh_engine_vintage.py", "rebh_engine_vintages"),
            _b("model", "backend/app/models/rebh_user_data.py",
               "analyst_notes, trade_journals, user_watchlists, council_company_checklists"),
            _b("model", "backend/app/models/Corporate_actions.py", "corporate_actions"),
            _b("script", "backend/scripts/corporate_actions_watcher.py",
               "the only writer of corporate_actions; CLI, manual"),
            _b("frontend", "frontend/app/rebh/tools/components/MarketMonitorTab.tsx",
               "the /api/rebh/market-macro consumer"),
        ],
        "pages": [
            "/rebh", "/rebh/[symbol]", "/rebh/company/[symbol]", "/rebh/report/[symbol]",
            "/rebh/studio/[symbol]", "/rebh/analyst/[symbol]", "/rebh/xray",
            "/rebh/xray/[symbol]", "/rebh/health", "/rebh/score", "/rebh/council",
            "/rebh/journal", "/rebh/quarantine", "/rebh/tools", "/rebh/watchlist",
            "/rebh/course-reports",
        ],
        "jobs": [
            _j("engine_vintages", "backend/app/services/rebh_vintage_service.py",
               "cron Sun–Thu 20:00", _DAILY, "cron", ["rebh_engine_vintages"],
               note="also POST /api/rebh/importers/run/vintages (trigger='manual', "
                    "same key) and the module CLI python -m "
                    "app.services.rebh_vintage_service --limit N"),
            _j("filings_importer", "backend/app/services/rebh_importers_service.py",
               "cron Sun–Thu 19:45", _DAILY, "cron", ["company_official_filings"]),
            _j("corporate_actions_watcher", "backend/scripts/corporate_actions_watcher.py",
               "manual only — never scheduled, not wrapped by track_job", None, "manual",
               ["corporate_actions"], tracked=False,
               note="run with python scripts/corporate_actions_watcher.py; it also calls "
                    "trigger_historical_pipeline() for newly listed actions"),
        ],
        "tables": [
            _t("rebh_universe_snapshots", "snapshot_date", 7, "engine_vintages",
               note="GET /api/rebh/universe"),
            _t("rebh_engine_vintages", "vintage_date", 2, "engine_vintages"),
            _t("company_official_filings", "published_date", 3, "filings_importer",
               note="`period` is a String column; published_date is the real date"),
            _t("corporate_actions", "eligibility_date", 14, "corporate_actions_watcher",
               note="detected_at is the scrape timestamp; eligibility_date is the "
                    "announcement/entitlement date"),
            _t("analyst_notes", "created_at", None, None, note="user data"),
            _t("trade_journals", "created_at", None, None,
               note="user data. `trade_date` is stored as String, not Date"),
            _t("council_company_checklists", "updated_at", None, None, note="user data"),
            _t("sukuk_market_data", "updated_at", 10, "sukuk_importer",
               note="shared with economy-rates — /rebh labs read it; written "
                    "manually via scripts/Sukuk&Bonds.py"),
            _t("saudi_economic_indicators", "retrieved_at", 35, "macro_importer",
               note="shared with economy-rates; written manually via "
                    "scripts/SAMA&GaStat.py; see the market-macro bug below"),
        ],
        "issues": [
            _i("bug", "GET /api/rebh/market-macro silently degrades to hard-coded macro "
                      "fallbacks because of a non-existent column plus a blanket "
                      "except-pass (full trace in the economy-rates system). "
                      "/rebh/tools → Market Monitor tab is where it surfaces."),
            _i("watch", "corporate_actions has no scheduler entry and no track_job "
                        "wrapper, so 'did it run?' cannot be answered from job_runs. "
                        "app/scrapers/Corporate_actions_watcher.py is 100% commented-out "
                        "dead code — the live script is backend/scripts/ version."),
            _i("note", "/rebh/[symbol] and /rebh/company/[symbol] are two different "
                       "generations of the same idea; both are live and both hit "
                       "/api/rebh/company/{symbol}."),
        ],
    },

    # ───────────────────────── 9. Terminal ─────────────────────────────────
    {
        "id": "terminal",
        "name": "Terminal & Forensic Audit",
        "in_navbar": True,
        "navbar_label": "Terminal",
        "blurb": "Aggregation layer: pulls ratios, fundamentals, council verdicts and "
                 "quant labs for one symbol out of the already-computed tables. Reads "
                 "prices/stock_indicators/rs_daily_v2 and the XBRL R2 payloads; owns no "
                 "pipeline of its own.",
        "built": [
            _b("route", "backend/app/api/routes/terminal.py",
               "/api/terminal/* (protected): all-ratios, audit-summary, council, "
               "market-machine, quant-lab, sector-templates, company-fundamental"),
            _b("route", "backend/app/api/routes/xbrl_companies.py", "/api/companies/*"),
            _b("route", "backend/app/api/routes/xbrl_financials.py", "/api/financials/*"),
            _b("route", "backend/app/api/routes/xbrl_upload.py", "/api/upload/*"),
            _b("service", "backend/app/services/xbrl_data_service.py",
               "financial statements served from Cloudflare R2 XBRL JSON, not Postgres"),
            _b("frontend", "frontend/app/terminal/page.tsx", "terminal shell"),
        ],
        "pages": [
            "/terminal", "/terminal/company-analyzer", "/terminal/forensic-audit",
            "/terminal/sector-templates", "/terminal/standards-guide",
        ],
        "jobs": [
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — see market-data", None, "manual",
               writes=["prices", "stock_indicators", "rs_daily_v2"]),
            _j("xbrl_upload", "backend/app/api/routes/xbrl_upload.py",
               "manual upload from the admin/XBRL flow, not wrapped by track_job",
               None, "manual", tracked=False,
               note="POST /api/upload/* — the report of what was imported is written to "
                    "logs, not to job_runs"),
        ],
        "tables": [
            _t("prices", "date", 1, "daily_market_update"),
            _t("stock_indicators", "date", 1, "daily_market_update"),
            _t("rs_daily_v2", "date", 1, "daily_market_update"),
            _t("companies", "last_scraped_at", 30, None,
               note="auto-created by official_filings.process_ingestion()"),
            _t("financial_reports", "period_end_date", 120, None,
               note="report calendar data, updated quarterly by ingestion"),
        ],
        "issues": [
            _i("note", "The three statement tables (IncomeStatement / BalanceSheet / "
                       "CashFlow) were REMOVED on 2026-10-06 — they held 0 rows. "
                       "Statements come from XBRL JSON in R2. Any doc mentioning those "
                       "Postgres tables is out of date."),
        ],
    },

    # ───────────────────── 10. Market Breadth engine ───────────────────────
    {
        "id": "breadth",
        "name": "REBH Market Breadth Engine",
        "in_navbar": False,
        "navbar_label": None,
        "blurb": "The vendored breadth engine: nightly parquet compute → 15 Postgres "
                 "result tables + one JSONB snapshot, served to 16 breadth pages. The "
                 "only subsystem with its own per-run detail table (breadth_runs).",
        "built": [
            _b("route", "backend/app/api/routes/rebh_breadth.py",
               "/api/rebh/breadth/today|series|events (protected)"),
            _b("service", "backend/app/services/breadth_job.py",
               "the orchestrator the cron job actually spawns"),
            _b("service", "backend/app/services/breadth_loader.py",
               "parquet → SQLAlchemy upserts + the breadth_data JSONB snapshot"),
            _b("service", "backend/app/services/breadth_engine/",
               "vendored engine (storage/features/*.parquet)"),
            _b("model", "backend/app/models/breadth.py",
               "breadth_runs + 15 result tables; deliberately independent of the old "
               "market_breadth table"),
            _b("frontend", "frontend/app/breadth/_lib/store.ts",
               "client store mapping /api/rebh/breadth/* into the panel contract"),
            _b("frontend", "frontend/app/breadth/_lib/tableApi.ts", "tabular breadth API"),
            _b("infra", "backend/app/core/scheduler.py",
               "job_breadth_daily() — subprocess run of breadth_job.py, 1800s "
               "timeout, then clears rebh:breadth:* Redis keys"),
        ],
        "pages": [
            "/breadth", "/breadth/ad", "/breadth/bpi", "/breadth/classic",
            "/breadth/conc", "/breadth/cycle", "/breadth/leaders", "/breadth/library",
            "/breadth/ma", "/breadth/mcclellan", "/breadth/nhnl", "/breadth/sectors",
            "/breadth/signals", "/breadth/stockbee", "/breadth/thresholds",
            "/breadth/widget",
        ],
        "jobs": [
            _j("breadth_daily", "backend/app/services/breadth_job.py",
               "cron Sun–Thu 21:15", _DAILY, "cron",
               writes=["breadth_runs"] + [
                   "breadth_participation", "breadth_advance_decline", "breadth_momentum",
                   "breadth_thrusts", "breadth_classic", "breadth_divergences",
                   "breadth_concentration", "breadth_widget", "breadth_stages_weekly",
                   "breadth_bpi", "breadth_sectors", "breadth_sectors_summary",
                   "breadth_composite", "breadth_composite_percentiles", "breadth_data"],
               note="job_runs records the process outcome only; per-date detail lives in "
                    "breadth_runs (asof, started_at, finished_at). The child script "
                    "breadth_job.py is intentionally NOT wrapped again, so there is no "
                    "double-counted row."),
        ],
        "tables": [
            _t("breadth_runs", "asof", 1, "breadth_daily"),
            _t("breadth_participation", "date", 1, "breadth_daily"),
            _t("breadth_advance_decline", "date", 1, "breadth_daily"),
            _t("breadth_momentum", "date", 1, "breadth_daily"),
            _t("breadth_thrusts", "date", 1, "breadth_daily"),
            _t("breadth_classic", "date", 1, "breadth_daily"),
            _t("breadth_divergences", "date", 1, "breadth_daily"),
            _t("breadth_concentration", "date", 1, "breadth_daily"),
            _t("breadth_widget", "date", 1, "breadth_daily"),
            _t("breadth_stages_weekly", "date", 8, "breadth_daily",
               note="weekly series — one trading day of lag is normal"),
            _t("breadth_bpi", "date", 1, "breadth_daily"),
            _t("breadth_sectors", "date", 1, "breadth_daily"),
            _t("breadth_sectors_summary", "date", 1, "breadth_daily"),
            _t("breadth_composite", "date", 1, "breadth_daily"),
            _t("breadth_composite_percentiles", "date", 8, "breadth_daily"),
            _t("breadth_data", "trading_date", 1, "breadth_daily",
               note="JSONB dashboard snapshot; pruned to a retention window by "
                    "breadth_loader.py"),
        ],
        "issues": [
            _i("watch", "breadth_job.py prints its own status and exits non-zero on "
                        "failure; scheduler.job_breadth_daily raises on rc != 0, which "
                        "track_job records as failed. But a run that exits 0 having "
                        "written nothing is only visible in the Tables panel."),
            _i("note", "Two unrelated 'breadth' systems coexist by design: this engine "
                       "(breadth_* tables) and the old market_breadth aggregate used by "
                       "/Percent_of_Stocks_Above_MA. app/models/breadth.py says so in its "
                       "docstring. Do not merge them."),
            _i("note", "2026-10-12 · Render deploy produced five 500s (breadth "
                       "meta/events/series/today + auth/me) during a ~25s window "
                       "while PG restarted ('FATAL: the database system is in "
                       "recovery mode'). The reason they PERSISTED was NOT the pool: "
                       "app/core/database.py already has pool_pre_ping=True + "
                       "keepalives + pool_recycle=1800. It was "
                       "app/core/cache_helpers.py:cache_read_through — its outer "
                       "try wrapped Redis GET and the DB fetch together, so the "
                       "OperationalError was logged as 'cache_error read' and the "
                       "fallback RE-invoked fetch_func on the same poisoned "
                       "session → PendingRollbackError → 500. FIXED 2026-10-13: "
                       "only Redis ops are wrapped now; fetch errors propagate "
                       "and get_db rolls the session back on exit. A PG restart "
                       "still fails the in-flight request once — unavoidable — "
                       "but it no longer double-fails."),
        ],
    },

    # ──────────────────── 11. Wallet / Portfolio tools ─────────────────────
    {
        "id": "portfolio",
        "name": "Wallet, Portfolio & Risk Tools",
        "in_navbar": True,
        "navbar_label": "Portfolio",
        "blurb": "User-held data only (positions, trades, RBAF, weekly studies, monthly "
                 "tracker) joined against market data for valuation. No scraper feeds it; "
                 "staleness here means the user has not entered trades.",
        "built": [
            _b("route", "backend/app/api/routes/wallet/portfolio.py", "/api/wallet/portfolio/*"),
            _b("route", "backend/app/api/routes/wallet/tracker.py", "/api/wallet/tracker/*"),
            _b("route", "backend/app/api/routes/wallet/rbaf.py", "/api/wallet/rbaf/*"),
            _b("route", "backend/app/api/routes/wallet/calculator.py", "/api/wallet/calculator/*"),
            _b("route", "backend/app/api/routes/wallet/weekly.py", "/api/wallet/weekly/*"),
            _b("model", "backend/app/models/wallet.py",
               "wallet_positions, wallet_trades, wallet_settings, wallet_weekly_studies"),
            _b("frontend", "frontend/store/portfolioStore.ts", "client-side portfolio store"),
            _b("frontend", "frontend/hooks/usePortfolio.ts", "portfolio data hook"),
        ],
        "pages": [
            "/portfolio", "/my-portfolio", "/rbaf", "/monthly-tracker",
            "/risk-calculator", "/Positionsizecalculator", "/weekly-study",
        ],
        "jobs": [],
        "tables": [
            _t("wallet_positions", "entry_date", None, None,
               note="user data — no pipeline lag concept"),
            _t("wallet_trades", "exit_date", None, None, note="user data"),
            _t("wallet_weekly_studies", "stem_date", None, None, note="user data"),
            _t("wallet_settings", "updated_at", None, None, note="user data"),
            _t("prices", "date", 1, "daily_market_update",
               note="shared with market-data: positions are marked to market from here"),
            _t("stock_indicators", "date", 1, "daily_market_update",
               note="shared — portfolio.py reads indicators per symbol"),
            _t("net_short_positions", "report_date", 3, "market_reports",
               note="shared — portfolio.py surfaces short interest"),
        ],
        "issues": [
            _i("watch", "/my-portfolio has no navbar entry (the item is commented out in "
                        "Navbar.tsx) but is fully built and reads "
                        "/api/wallet/portfolio/*. Treat it as live."),
            _i("note", "/weekly-study sits under the Market dropdown in the navbar but its "
                       "data is wallet_weekly_studies — it is listed here for ownership and "
                       "under 'market' for navigation. Same table either way."),
        ],
    },

    # ─────────────── 12. Official filings & disclosures ────────────────────
    {
        "id": "filings",
        "name": "Official Filings & Disclosures",
        "in_navbar": False,
        "navbar_label": None,
        "blurb": "Tadawul/Saudi Exchange official disclosures: ingestion check, per-company "
                 "report pages and the admin summary. Feeds the REBH quarantine/calendar "
                 "tabs as well.",
        "built": [
            _b("route", "backend/app/api/routes/official_filings.py",
               "POST /api/ingest/official-reports, GET /api/reports/{symbol}, "
               "/api/reports/admin/*"),
            _b("service", "backend/app/api/routes/official_filings.py",
               "process_ingestion() (async, L46) — download → S3/R2 → DB, auto-creates "
               "unknown companies in `companies`"),
            _b("model", "backend/app/models/official_filings.py",
               "company_official_filings (+ FilingCategory/Period/Type/Language enums)"),
            _b("frontend", "frontend/app/stocks/[symbol]/reports/page.tsx",
               "per-company disclosure tab"),
        ],
        "pages": [
            "/stocks/[symbol]/reports", "/stocks/[symbol]/news",
            "/admin/reports", "/rebh/quarantine",
        ],
        "jobs": [
            _j("filings_importer", "backend/app/services/rebh_importers_service.py",
               "cron Sun–Thu 19:45", _DAILY, "cron", ["company_official_filings"],
               note="also POST /api/rebh/importers/run/filings (trigger='manual')"),
        ],
        "tables": [
            _t("company_official_filings", "published_date", 2, "filings_importer"),
            _t("companies", "last_scraped_at", 30, "filings_importer",
               note="auto-created during ingestion when a symbol is unknown"),
        ],
        "issues": [
            _i("note", "This system has no navbar home: the navbar 'News' dropdown points "
                       "at /news, /news/earnings, /news/disclosures, /news/analyst — none "
                       "of which exist as pages. Disclosures are reached through the stock "
                       "page's reports tab and /admin/reports."),
        ],
    },

    # ─────────────── 13. Weekly Routine PDF (frontend-only) ────────────────
    {
        "id": "weekly-pdf",
        "name": "Weekly Routine PDF",
        "in_navbar": False,
        "navbar_label": None,
        "blurb": "A frontend server route that drives Puppeteer against the live site, "
                 "screenshots five pages and stitches them into Weekly_Routine.pdf. No "
                 "backend endpoint involved — but it fails whenever the pages it photographs "
                 "are stale or slow to paint.",
        "built": [
            _b("frontend", "frontend/app/api/generate-weekly-report/route.ts",
               "POST handler: puppeteer + pdf-lib, 1920x1080 screenshots"),
            _b("frontend", "frontend/app/industry-groups/page.tsx",
               "exposes window.__REPORT_PDF_BASE64__ for the jsPDF capture"),
            _b("script", "backend/scripts/generate_weekly_report.py",
               "the UNRELATED backend weekly report (writes weekly_market_reports) — "
               "same words, different system"),
        ],
        "pages": ["/industry-groups"],
        "jobs": [
            _j("weekly_pdf_capture", "frontend/app/api/generate-weekly-report/route.ts",
               "manual — clicked from /industry-groups", None, "manual", tracked=False,
               note="frontend route, cannot write job_runs; waits up to 30s for canvases "
                    "to paint and 90s for the industry-groups jsPDF"),
            _j("weekly_report_generator", "backend/scripts/generate_weekly_report.py",
               "manual CLI (python scripts/generate_weekly_report.py)", None, "manual",
               writes=["weekly_market_reports"], tracked=False,
               note="wrapping this script's __main__ was not part of Phase 1; job_runs "
                    "will not show it until it is"),
        ],
        "tables": [
            _t("weekly_market_reports", "week_end", 8, "weekly_report_generator",
               note="model is now imported by app/core/database.py + "
                    "app/models/__init__.py (2026-10-12). Alembic migration "
                    "2026_10_12_weekly_add_weekly_market_reports creates the table; "
                    "GET /api/weekly-market-update/latest returns 404 'No weekly "
                    "report generated yet' when the table is empty — that is normal "
                    "on a fresh env until scripts/generate_weekly_report.py runs."),
        ],
        "issues": [
            _i("note", "2026-10-12 · Bug 3 FIXED. app/api/routes/weekly_market_update.py "
                       "is now included in app/main.py with prefix=/api and "
                       "dependencies=protected_dependencies (get_current_user), so "
                       "/api/weekly-market-update/latest · /list · /{week_end} · "
                       "/generate all reach the handler instead of 404. The model "
                       "WeeklyMarketReport was also added to app/core/database.py and "
                       "app/models/__init__.py so Base.metadata + Alembic autogenerate "
                       "now see it. Migration "
                       "alembic/versions/2026_10_12_weekly_add_weekly_market_reports.py "
                       "creates the table idempotently (CREATE TABLE IF NOT EXISTS) — "
                       "run `alembic upgrade head` before opening /weekly-update on a "
                       "fresh environment, otherwise the endpoint will 500 with "
                       "'relation weekly_market_reports does not exist'."),
            _i("watch", "POST /api/weekly-market-update/generate is only gated by "
                        "get_current_user (not admin) — the mount inherits "
                        "protected_dependencies. That matches the live caller in "
                        "frontend/app/weekly-update/page.tsx; tighten to admin if the "
                        "product stance changes. /weekly-update is NOT in middleware "
                        "publicPaths, so the front door is session-gated."),
            _i("watch", "components/market-report/compact.tsx also calls "
                        "/api/weekly-market-update/latest but nothing imports it — dead "
                        "code, left in place. Delete when a decision is made."),
            _i("watch", "The PDF route needs Chromium in the frontend image and screenshots "
                        "PAGES BY URL — /Percent_of_Stocks_Above_MA, /minervini-trend, "
                        "/screeners/Alrayan&Alhussain, /industry-groups?autoReport=true, "
                        "/watchlist?tab=Matrix%20Chart. Rename or move any of those and the "
                        "PDF silently loses a section."),
        ],
    },

    # ─────────────── 14. Accounts, auth & site pages ───────────────────────
    {
        "id": "accounts",
        "name": "Accounts, Auth & Site Pages",
        "in_navbar": True,
        "navbar_label": "Portfolio→profile / footer links",
        "blurb": "Registration, approval, profile, contact and the legal/pricing pages. "
                 "User data only; the approval gate is what /admin pending-users works on.",
        "built": [
            _b("route", "backend/app/api/routes/auth.py", "/api/auth/*"),
            _b("route", "backend/app/api/routes/admin.py", "/api/admin/users|approve-user|..."),
            _b("route", "backend/app/api/routes/contact.py", "/api/contact/*"),
            _b("route", "backend/app/api/routes/cache.py", "/api/cache/* (admin)"),
            _b("model", "backend/app/models/user.py", "users"),
            _b("model", "backend/app/models/contact.py", "contact_messages"),
            _b("frontend", "frontend/middleware.ts",
               "cookie-presence gate only — real checks live in the API deps"),
            _b("frontend", "frontend/components/ProtectedRoute.tsx",
               "requireAdmin wrapper"),
        ],
        "pages": [
            "/login", "/register", "/profile", "/pending-approval", "/auth/callback/google",
            "/auth/callback/facebook", "/auth/forgot-password", "/auth/reset-password",
            "/auth/verify-email", "/pricing", "/contact", "/privacy-policy",
            "/terms-of-service", "/test-connection",
        ],
        "jobs": [],
        "tables": [
            _t("users", "created_at", None, None, note="transactional, not pipeline data"),
            _t("contact_messages", "created_at", None, None, note="transactional"),
        ],
        "issues": [
            _i("note", "middleware.ts checks cookie presence, not validity — a stale cookie "
                       "reaches the API and is rejected there. Keep both layers when "
                       "touching auth."),
        ],
    },

    # ─────────────── 15. Admin & operations surfaces ───────────────────────
    {
        "id": "admin-ops",
        "name": "Admin & Operations",
        "in_navbar": False,
        "navbar_label": None,
        "blurb": "The control room: this Runbook, the legacy admin dashboard, the daily "
                 "pipeline trigger, cache clearing and the job_runs ledger every tracked "
                 "script writes into.",
        "built": [
            _b("route", "backend/app/api/routes/admin.py",
               "/api/admin/* — users, pipeline trigger/status/unlock"),
            _b("route", "backend/app/api/routes/system_map.py",
               "/api/admin/system-map/* — this Runbook's API (registry + live freshness)"),
            _b("route", "backend/app/api/routes/valuation_admin_system.py",
               "/api/admin/system/stats + /run-scraper (older, in-memory status)"),
            _b("service", "backend/app/services/job_tracking.py",
               "track_job / tracked_job / with_result_handling — writes job_runs"),
            _b("service", "backend/app/services/job_runner.py",
               "Phase 5 · in-process dispatch table for 'Run now'; merges "
               "scheduler.JOB_CALLABLES with scraper_service runners and starts "
               "each callable in its own daemon thread"),
            _b("model", "backend/app/models/job_run.py", "job_runs"),
            _b("infra", "backend/alembic/versions/2026_10_11_jobruns_add_job_runs.py",
               "job_runs migration — applied 2026-10-11"),
            _b("infra",
               "backend/alembic/versions/2026_10_12_weekly_add_weekly_market_reports.py",
               "weekly_market_reports migration — written 2026-10-12, run "
               "alembic upgrade head before opening /weekly-update"),
            _b("frontend", "frontend/app/admin/page.tsx", "legacy dashboard"),
            _b("frontend", "frontend/app/admin/system-map/page.tsx", "this Runbook"),
            _b("infra", "backend/app/core/scheduler.py",
               "the ONLY scheduler in the project — APScheduler in-process"),
        ],
        "pages": ["/admin", "/admin/system-map", "/admin/users", "/admin/reports",
                  "/admin/dashboard", "/admin/login"],
        "jobs": [
            _j("job:all_cron", "backend/app/core/scheduler.py",
               "APScheduler, gated by ENABLE_SCHEDULER=true (Africa/Cairo)", None,
               "cron", tracked=False,
               note="not a job itself: if ENABLE_SCHEDULER is false, NO cron job has run "
                    "since deploy and every scheduled job will look 'bad' here. Check the "
                    "env before chasing individual scrapers."),
        ],
        "tables": [
            _t("job_runs", "started_at", None, None,
               note="the ledger. status running|success|failed, triggered_by "
                    "cron|manual|admin, rows_written may be NULL when the script did not "
                    "report a count"),
            _t("update_status", "latest_ready_date", 1, "daily_market_update",
               note="single-row mutex for the daily pipeline"),
        ],
        "issues": [
            _i("bug", "Nothing schedules daily_market_update (see market-data). This is the "
                      "number one thing to check when any TASI page looks stale."),
            _i("note", "2026-10-12 · Phase 5 shipped. POST /api/admin/system-map/jobs/"
                       "{job_name}/run starts a job in a daemon thread (guarded by "
                       "get_current_admin) and GET .../status reports the latest "
                       "job_runs row + in-flight flag; the Runbook Jobs table now "
                       "renders a Run now button using that. Runnable set = "
                       "scheduler.JOB_CALLABLES (16 keys) + 5 scraper:* keys from "
                       "app/services/scraper_service.py = 21 targets. "
                       "daily_market_update is delegated to "
                       "POST /api/admin/pipeline/daily-market-update/run (that "
                       "endpoint owns the update_status mutex + 409 on double-fire). "
                       "Bug 1 is unchanged — the scheduler still does NOT add it."),
            _i("watch", "Run-now double-click race: job_runner.is_in_flight() uses "
                        "an in-process dict + a job_runs 'running' row younger than "
                        "120 min. Two admin workers behind a load balancer will each "
                        "see their own _ACTIVE only, so a rapid double POST that lands "
                        "on different workers will start the job twice. Single "
                        "render instance today, so this is theoretical. Widen the "
                        "guard (advisory lock) only if the deployment changes."),
            _i("watch", "scraper:sp500_price · scraper:sp500_pe · scraper:treasury_gov "
                        "write job_runs rows via scraper_service._run() but are NOT "
                        "registered as jobs under any system here — they only appear "
                        "in the ledger when the Admin /run-scraper button (or "
                        "system-map Run now) fires them. Add _j entries if a UI "
                        "listing becomes necessary."),
            _i("watch", "There is no external cron anywhere: .github/workflows is empty, "
                        "render.yaml has no cron block and .ebextensions is empty. "
                        "APScheduler inside the web process is the only scheduler, so a "
                        "paused/free-tier instance silently stops ALL scraping."),
            _i("note", "holiday_calendar: there is no populated holiday table in the active "
                       "code, so trading-day math here only skips Fri/Sat. Unverified for "
                       "public holidays — a holiday Monday reads as one day of lag."),
            _i("note", "Older docs (.md) describe an '/admin/system' scraper status panel "
                       "and treasury_scraper.py; neither matches the code. job_runs + this "
                       "registry are the current truth."),
        ],
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  Lookup helpers used by the /api/admin/system-map routes
# ══════════════════════════════════════════════════════════════════════════════

_INDEX: Dict[str, Dict[str, Any]] = {s["id"]: s for s in SYSTEMS}


def list_systems() -> List[Dict[str, Any]]:
    """All registered systems, in the order they should appear in the sidebar."""
    return SYSTEMS


def get_system(system_id: str) -> Optional[Dict[str, Any]]:
    return _INDEX.get(system_id)


def system_summaries() -> List[Dict[str, Any]]:
    """The sidebar payload: id, name, blurb, in_navbar and counts."""
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "blurb": s["blurb"],
            "in_navbar": s.get("in_navbar", False),
            "navbar_label": s.get("navbar_label"),
            "pages_count": len(s["pages"]),
            "jobs_count": len(s["jobs"]),
            "tables_count": len(s["tables"]),
            "issues_count": len(s["issues"]),
            "bugs_count": sum(1 for i in s["issues"] if i["severity"] == "bug"),
        }
        for s in SYSTEMS
    ]


# ─── /api/admin/system-map/pipelines — the legacy admin tab's map ─────────────
def pipelines(include_unwired: bool = False) -> List[Dict[str, Any]]:
    """scraper → table → pages map in the shape the old /admin Pipeline tab renders.

    Derived from the registry so the two can never drift again. `scrapers` are the
    job entries that write the table (their file + expected schedule); `pages` are
    every registered page that reads it, taken from the owning system.

    Default = only tables a tracked/manual job actually writes, i.e. real data
    flows. ``include_unwired=True`` adds reference, seeded and user tables that
    have no writer — useful for an audit, far too noisy for the dashboard.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for system in SYSTEMS:
        page_list = system["pages"]
        for entry in system["tables"]:
            if not include_unwired and not entry.get("source_job"):
                continue
            table = entry["table"]
            row = rows.setdefault(table, {
                "table": table,
                "label": system["name"],
                "scrapers": [],
                "pages": [],
                "warn": None,
            })
            # table.pages wins: the legacy map is table-shaped, and several tables
            # live in one system (economy-rates covers 16 pages across 7 tables).
            for page in (entry["pages"] or page_list):
                if page not in row["pages"]:
                    row["pages"].append(page)
            job = _job_by_name(system, entry.get("source_job"))
            if job:
                text = f"{job['file'].split('/')[-1]} → {job['expected_schedule']}"
                if text not in row["scrapers"]:
                    row["scrapers"].append(text)
            elif not row["scrapers"]:
                # keep the legacy three-column layout meaningful for tables that
                # are reference/seed/user data rather than scraped output
                row["scrapers"] = ["no tracked writer (reference / user / seeded data)"]
            if entry.get("note"):
                row["warn"] = row["warn"] or entry["note"]
            elif entry.get("verified") is False:
                row["warn"] = row["warn"] or "origin unverified — no writer in active code"
    for row in rows.values():
        row["pages"].sort()
        real = [s for s in row["scrapers"] if not s.startswith("no tracked writer")]
        if real:
            row["scrapers"] = real
    return list(rows.values())


def _job_by_name(system: Dict[str, Any], job_name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not job_name:
        return None
    for job in system["jobs"]:
        if job["job_name"] == job_name:
            return job
    return None


def all_job_names() -> List[str]:
    """Every job_name the registry expects to see in job_runs (tracked entries only)."""
    seen: List[str] = []
    for system in SYSTEMS:
        for job in system["jobs"]:
            if job.get("tracked", True) and job["job_name"] not in seen:
                seen.append(job["job_name"])
    return seen


def checks_sql() -> List[Dict[str, str]]:
    """Copy-pasteable self-check queries for the 'Check it yourself' panel."""
    out: List[Dict[str, str]] = [
        {
            "label": "Last 20 runs of anything",
            "sql": "SELECT job_name, status, triggered_by, started_at, finished_at, "
                   "rows_written, LEFT(error_message, 120) AS err "
                   "FROM job_runs ORDER BY id DESC LIMIT 20;",
        },
        {
            "label": "Runs of one job",
            "sql": "SELECT * FROM job_runs WHERE job_name = 'daily_market_update' "
                   "ORDER BY started_at DESC LIMIT 10;",
        },
        {
            "label": "Failed runs in the last 3 days",
            "sql": "SELECT job_name, started_at, LEFT(error_message, 200) AS err FROM job_runs "
                   "WHERE status = 'failed' AND started_at > NOW() - INTERVAL '3 days' "
                   "ORDER BY started_at DESC;",
        },
        {
            "label": "Runs still marked 'running' (orphaned by a restart)",
            "sql": "SELECT job_name, started_at, EXTRACT(EPOCH FROM (NOW() - started_at))/60 AS "
                   "minutes FROM job_runs WHERE status = 'running' ORDER BY started_at;",
        },
    ]
    for system in SYSTEMS:
        for entry in system["tables"]:
            col = entry["date_column"]
            if not col:
                continue
            out.append({
                "label": f"Newest row in {entry['table']}",
                "sql": f"SELECT MAX({col}) FROM {entry['table']};",
            })
    return out


def registry_stats() -> Dict[str, int]:
    """Counts used as a self-test: does the registry cover the navbar?"""
    return {
        "systems": len(SYSTEMS),
        "pages": len({p for s in SYSTEMS for p in s["pages"]}),
        "tracked_jobs": len(all_job_names()),
        "tables": len({t["table"] for s in SYSTEMS for t in s["tables"]}),
        "issues": sum(len(s["issues"]) for s in SYSTEMS),
        "bugs": sum(1 for s in SYSTEMS for i in s["issues"] if i["severity"] == "bug"),
    }
