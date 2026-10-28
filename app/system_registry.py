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


# ─── Schedule-runbook vocabularies (asserted by tests/test_system_registry_shape) ─
CADENCES = ("daily", "weekly", "monthly", "quarterly", "manual")
ROLES = ("ingestion", "computation")
STORAGES = ("cumulative", "snapshot")
COMMAND_KINDS = ("incremental", "full", "backfill")


def _derive_cadence(expected_schedule: str, trigger: str) -> str:
    """Best-effort cadence from the schedule string, so annotating every legacy
    ``_j()`` call stays optional. An explicit ``cadence=`` always wins upstream."""
    s = (expected_schedule or "").lower()
    if "quarter" in s:
        return "quarterly"
    if "month" in s or "day 10" in s:
        return "monthly"
    # Weekday-range cron (Mon–Fri, Sun–Thu) is DAILY — test before the lone
    # "fri" weekly marker, or "Mon\u2013Fri 01:00" would read as weekly.
    if any(k in s for k in ("mon-fri", "mon\u2013fri", "sun-thu", "sun\u2013thu",
                            "sun,mon", "business day", "every business", "daily")):
        return "daily"
    if "week" in s or "fri 01" in s or "sunday-evening" in s:
        return "weekly"
    return "manual" if trigger != "cron" else "daily"


def _j(job_name: str, file: str, expected_schedule: str,
       max_allowed_age_hours: Optional[int] = None, trigger: str = "cron",
       writes: Optional[List[str]] = None, note: str = "",
       tracked: bool = True, cadence: Optional[str] = None,
       role: str = "ingestion", storage: str = "cumulative",
       commands: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """One job. ``job_name`` MUST equal the key written into ``job_runs``.

    ``tracked=False`` → the entry point is not wrapped by track_job(), so
    "did it run?" is unanswerable; the Runbook falls back to table freshness.
    ``max_allowed_age_hours=None`` → no schedule to be late for (manual-only).

    Schedule-runbook axes, read by GET /api/admin/system-map/schedule:
      · cadence  → daily | weekly | monthly | quarterly | manual (derived from
                   expected_schedule + trigger when omitted)
      · role     → ingestion (pulls an external source) | computation (derives
                   from local tables such as prices)
      · storage  → cumulative (appends, keeps history) | snapshot (overwrite)
      · commands → [{label, cli, kind}] the exact operator terminal commands,
                   kind ∈ incremental | full | backfill. Left empty for
                   cron-only jobs, whose door is the Run Now button instead.
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
        "cadence": cadence or _derive_cadence(expected_schedule, trigger),
        "role": role,
        "storage": storage,
        "commands": commands or [],
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
        "blurb": "Tadawul market dashboard. Server component that fans out to six "
                 "public read-through endpoints (latest quotes, market_pulse TASI, "
                 "industry-group RS, REBH universe, corporate actions) and derives "
                 "every panel — movers, sector table, breadth, TASI chart, screens "
                 "and announcements. Zero mock content since Phase 6 2026-10-15.",
        "built": [
            _b("frontend", "frontend/app/page.tsx",
               "async Server Component — Promise.all over the six endpoints, "
               "revalidate 300s; renders <HomeDashboard vm={…}> only"),
            _b("frontend", "frontend/app/_lib/homeData.ts",
               "pure view-model builders: cap-weighted sector change, breadth, "
               "movers, TASI index row, NCAV / owner-earnings / dividend-yield "
               "screens, announcements"),
            _b("frontend", "frontend/app/_components/HomeDashboard.tsx",
               "'use client' presentation — TASI|Sectors tabs, real range buttons "
               "(5D…MAX) on the TASI area chart, Day Watch, sector RS table, screens"),
            _b("frontend", "frontend/app/stocks/_lib/stockApi.ts",
               "fetchMarketSnapshot / fetchMarketPulseLatest / fetchIndustryGroups "
               "/ fetchRebhUniverse / fetchMarketCorporateActions / fetchTasiHistory"),
            _b("route", "backend/app/api/routes/prices.py",
               "GET /api/prices/latest · GET /api/prices/tasi/history (TASI line)"),
            _b("route", "backend/app/api/routes/market_pulse.py",
               "GET /api/market-pulse/?limit=1 (TASI level, change, regime signals)"),
            _b("route", "backend/app/api/routes/industry_groups.py",
               "GET /api/industry-groups/latest (sector RS rank / grade / YTD)"),
            _b("route", "backend/app/api/routes/rebh_engine.py",
               "GET /api/rebh/universe (screens) · /api/rebh/corporate-actions "
               "(announcements feed, symbol omitted = whole market)"),
            _b("frontend", "frontend/components/Navbar.tsx", "site navigation, 9 dropdowns"),
        ],
        "pages": ["/"],
        "jobs": [],
        "tables": [
            _t("prices", "date", 1, "daily_market_update",
               note="quote strip, movers and breadth counts all read the same "
                    "/api/prices/latest?limit=1000 the pipeline writes"),
            _t("market_pulse", "date", 1, "daily_market_update",
               note="stage 3.5 — the TRUE TASI close series behind the home chart "
                    "and every benchmark comparison"),
            _t("industry_group_history", "date", 1, "daily_market_update",
               note="stage 7 — the 22 Tadawul industry groups. Written the same "
                    "evening as prices/latest, so each panel prints its own as-of "
                    "date instead of pretending they agree"),
            _t("corporate_actions", "eligibility_date", 14, "corporate_actions_watcher",
               note="market-wide announcements feed on the home page; no forward "
                    "calendar exists, so the newest row may be in the past"),
            _t("static_stock_info", None, None, None,
               note="company names AR/EN; reference data, no date column to probe"),
        ],
        "issues": [
            _i("bug", "2026-10-18 FIXED · this system's 'tables' block was written as "
                      "five bare strings ('prices', 'market_pulse', …) while every "
                      "other system uses _t() dicts. The Runbook, the pipelines map "
                      "and checks_sql() all index entries as dicts, so "
                      "GET /api/admin/system-map/systems/<anything> died with "
                      "'TypeError: string indices must be integers' (checks_sql() "
                      "loops ALL systems, so one bad entry breaks every detail "
                      "page). The home tables were also invisible to the freshness "
                      "probe. They are proper _t() entries now, and "
                      "system_registry._normalise_tables() coerces any future bare "
                      "string at import time + prints a WARN, so a registry typo "
                      "degrades into an unprobed table instead of a 500."),
            _i("note", "2026-10-15 Phase 6: the former 440-line static dashboard "
                       "(US futures, Dow/S&P/Nasdaq, NVDA/AMZN/TSLA watchlists, fake "
                       "analyst articles and a hardcoded SVG curve) was deleted "
                       "end-to-end and rebuilt on real Saudi data. The page is now "
                       "data-dependent: if a pipeline has not published, the "
                       "affected panel shows its own as-of date or an em-dash."),
            _i("watch", "prices/latest serves update_status.latest_ready_date while "
                        "market_pulse and industry_group_history are written the same "
                        "evening — so the quote strip can trail the TASI row by one "
                        "session. Each panel prints its own date instead of pretending "
                        "they agree."),
            _i("note", "2026-10-18 · the home page gained a real 'Dividend Yield "
                       "Leaders' card: buildDividendScreen() in "
                       "frontend/app/_lib/homeData.ts ranks /api/rebh/universe rows "
                       "by div_yield — the cash actually distributed to shareholders "
                       "in the latest filing ÷ market cap, never extrapolated, with "
                       "full-year filings ranked ahead of year-to-date windows (the "
                       "latter divide a real payment by less than a year). The "
                       "backend withholds the ratio when the only disclosed payout "
                       "is older than 18 months, so 106 of 257 companies qualify and "
                       "an absent figure means 'not disclosed', not zero. The card "
                       "reuses the universe array page.tsx already fetches: zero "
                       "extra requests. Still absent: a FORWARD yield — Tadawul "
                       "publishes no consensus dividend estimate."),
            _i("watch", "The corporate-actions feed is ordered by eligibility_date "
                        "server-side; the home page re-sorts the fetched slice by "
                        "announcement_date. A limit/order query param would remove "
                        "that client-side step."),
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
            _b("script", "backend/scripts/verify_daily_update.py",
               "post-run checks incl. market_breadth row counts"),
            _b("route", "backend/app/api/routes/admin.py",
               "POST /api/admin/pipeline/daily-market-update/run|status|unlock"),
            _b("route", "backend/app/api/routes/prices.py",
               "GET /api/prices/history/{symbol} · /latest · "
               "GET /api/prices/seasonality/{symbol} (month-end close → MoM aggregates, "
               "Redis read-through; added Phase 3 2026-10-15) · "
               "GET /api/prices/tasi/history?days= (TRUE TASI index closes from the "
               "market_pulse table — same benchmark source as rs_line._fetch_tasi_close(); "
               "added Phase 5 2026-10-15)"),
            _b("frontend", "frontend/app/stocks/_components/Seasonality.tsx",
               "seasonality panel — sole consumer of /api/prices/seasonality/{symbol} "
               "(wired via StockOverview; replaced mock, Phase 3)"),
            _b("frontend", "frontend/app/admin/page.tsx",
               "'Daily Market Update' tab — trigger + live status + log tail"),
        ],
        "pages": [
            "/stocks", "/stocks/[symbol]", "/stocks/[symbol]/summary",
            "/stocks/[symbol]/financials", "/stocks/[symbol]/income-statement",
            "/stocks/[symbol]/balance-sheet", "/stocks/[symbol]/cash-flow",
            "/stocks/[symbol]/historical-price", "/stocks/[symbol]/earnings",
            "/stocks/[symbol]/dividends", "/stocks/[symbol]/ratings",
            "/stocks/[symbol]/peers", "/stocks/[symbol]/splits",
            "/stocks/market-breadth",
            "/admin", "/market-pulse", "/industry-groups",
        ],
        "jobs": [
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — manual CLI or Admin dashboard only", None, "manual",
               writes=["prices", "stock_indicators", "rs_daily_v2", "market_breadth",
                       "screener_daily_trend_counts", "market_pulse",
                       "industry_group_history"],
               cadence="daily", role="ingestion", storage="cumulative",
               commands=[{"label": "Run full daily update",
                          "kind": "full",
                          "cli": '.\\venv\\Scripts\\python.exe "scripts/daily_market_update.py"'}],
               note="Started by hand (python scripts/daily_market_update.py) or by "
                    "POST /api/admin/pipeline/daily-market-update/run. Both paths are "
                    "wrapped by track_job(), so job_runs does show the runs."),
            _j("industry_groups", "backend/scripts/calculate_industry_groups.py",
               "stage 7 of daily_market_update; also runnable standalone",
               _DAILY, "manual",
               writes=["industry_group_history"],
               cadence="daily", role="computation", storage="cumulative",
               commands=[{"label": "Compute today's group ranks",
                          "kind": "incremental",
                          "cli": '.\\venv\\Scripts\\python.exe "scripts/calculate_industry_groups.py"'},
                         {"label": "Backfill past dates",
                          "kind": "backfill",
                          "cli": '.\\venv\\Scripts\\python.exe "scripts/calculate_industry_groups.py" --backfill'}],
               note="Derives the 22 Tadawul industry-group RS ranks/grades from the "
                    "local prices + stock_indicators tables (no external pull). Reads "
                    "the DB, so it is a computation step, not an ingestion scraper."),
            _j("backfill_industry_group_history", "backend/scripts/backfill_industry_group_history.py",
               "manual — run when the gap scanner finds missing group-history days",
               None, "manual",
               writes=["industry_group_history"],
               cadence="manual", role="computation", storage="cumulative",
               commands=[{"label": "Fill missing trading days",
                          "kind": "backfill",
                          "cli": '.\\venv\\Scripts\\python.exe "scripts/backfill_industry_group_history.py" --fill-missing --execute'},
                         {"label": "Preview the work list (dry run)",
                          "kind": "backfill",
                          "cli": '.\\venv\\Scripts\\python.exe "scripts/backfill_industry_group_history.py" --fill-missing --list'}],
               note="Recomputes stale dates of industry_group_history, or with "
                    "--fill-missing the trading days that have NO row at all. Dry-run "
                    "by default; --execute writes. The fix the gap scanner points at "
                    "when group history has holes."),
            _j("historical_price_fetcher", "backend/scripts/Historical_fetcher.py",
               "manual - pull/repair one symbol's price history from Tadawul",
               None, "manual",
               writes=["prices"],
               cadence="manual", role="ingestion", storage="cumulative",
               commands=[{"label": "Fetch a date range for one symbol",
                          "kind": "backfill",
                          "cli": '$env:CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"; .\\venv\\Scripts\\python.exe "scripts/Historical_fetcher.py" --symbol 1150 --from 01-01-2024 --to 31-12-2024'},
                         {"label": "Fill everything after the last stored day",
                          "kind": "incremental",
                          "cli": '$env:CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"; .\\venv\\Scripts\\python.exe "scripts/Historical_fetcher.py" --symbol 1150'}],
               note="Selenium/Chrome scrape of the Tadawul historical endpoint for a "
                    "single symbol. --symbol is required; --from/--to are DD-MM-YYYY, "
                    "and omitting them fetches only what is newer than the last stored "
                    "date (it reads MIN/MAX(date) from prices first). Needs CHROME_BIN. "
                    "No job_runner callable, so the tab shows Copy-command only."),
            _j("targeted_date_price_refetch", "backend/scripts/fetch_targeted_dates_tadawul.py",
               "manual - re-scrape specific trading days and upsert prices",
               None, "manual",
               writes=["prices"],
               cadence="manual", role="ingestion", storage="cumulative",
               commands=[{"label": "Re-fetch one day for one symbol",
                          "kind": "incremental",
                          "cli": '$env:CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"; .\\venv\\Scripts\\python.exe "scripts/fetch_targeted_dates_tadawul.py" --symbol 1150 --date 04-10-2026'},
                         {"label": "Re-fetch a date range for ALL symbols",
                          "kind": "backfill",
                          "cli": '$env:CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"; .\\venv\\Scripts\\python.exe "scripts/fetch_targeted_dates_tadawul.py" --from 01-10-2026 --to 08-10-2026'}],
               note="Targeted-day repair tool: scrapes the chosen date(s) and does an "
                    "ON CONFLICT (symbol, date) upsert straight into prices. Dates are "
                    "DD-MM-YYYY; --date defaults to a fixed day, --from/--to widen to a "
                    "range, and omitting --symbol covers every listed symbol. Selenium/"
                    "Chrome -> needs CHROME_BIN. Copy-command only (no Run Now callable)."),
        ],
        "tables": [
            _t("prices", "date", 1, "daily_market_update"),
            _t("stock_indicators", "date", 1, "daily_market_update"),
            _t("rs_daily_v2", "date", 1, "daily_market_update",
               note="model file is app/models/rs_daily.py — filename ≠ table name"),
            _t("market_breadth", "date", 1, "daily_market_update",
               note="written by raw SQL in scripts/update_daily_market_breadth.py; "
                    "model app/models/market_breadth.py is now imported by "
                    "app/models/__init__.py, so Base.metadata + Alembic manage it "
                    "(migration 2026_10_26_mktbreadth — a guarded no-op where it exists)"),
            _t("screener_daily_trend_counts", "date", 1, "daily_market_update"),
            _t("update_status", "latest_ready_date", 1, "daily_market_update",
               note="single row; the publish gate. latest_ready_date is what the "
                    "site treats as 'the last complete day'"),
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
            _i("note", "2026-10-26 FIXED · market_breadth was read by a live endpoint "
                       "but its model was imported nowhere, so Base.metadata (and thus "
                       "Alembic) did not manage it and a fresh env had to create it by "
                       "hand. app/models/market_breadth.py is now imported by "
                       "app/models/__init__.py and a guarded migration "
                       "(2026_10_26_mktbreadth) creates the table only when absent, so "
                       "prod — where it already exists — is untouched. The model's two "
                       "phantom secondary indexes were dropped to match the live table "
                       "(date PK only). (weekly_market_reports, the other table once "
                       "flagged here, was dropped 2026-10-25 with the 'weekly-pdf' system.)"),
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
               "market_breadth — registered in Base.metadata + Alembic since 2026-10-26"),
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
            _b("frontend", "frontend/app/stocks/[symbol]/momentum/page.tsx",
               "GET /api/rs-v2/latest?limit=1000 consumer (client-side authFetch — "
               "protected route; TASI medians computed in-browser)"),
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
               "buybacks, SBL, historical reports)"),
            _b("route", "backend/app/api/routes/market_pulse.py", "/api/market-pulse/*"),
            _b("route", "backend/app/api/routes/tasi_settings.py", "/api/tasi-settings"),
            _b("route", "backend/app/api/routes/industry_groups.py", "/api/industry-groups/*"),
            _b("route", "backend/app/api/routes/valuation.py", "/api/valuation/*"),
            _b("route", "backend/app/api/routes/valuation_admin_config.py",
               "/api/admin/valuation-config (admin)"),
            _b("service", "backend/app/services/valuation_service.py",
               "the valuation engine behind /valuation tabs"),
            _b("service", "backend/app/services/market_pulse_calc.py", "pulse aggregates"),
            _b("scraper", "backend/app/scrapers/daily_financial_indicators_scraper.py",
               "TASI weights / EPS / prices — feeds tasi_components. Its source "
               "(DailyFinancialIndicators_en.html) has NO date parameter, so it "
               "serves only the latest day and a missed day cannot be backfilled."),
            _b("script", "backend/scripts/update_market_reports.py",
               "the SaudiExchange reports orchestrator"),
            _b("model", "backend/app/models/market_reports.py",
               "substantial_shareholders, net_short_positions, foreign_headrooms, "
               "share_buybacks, sbl_positions, historical_reports"),
        ],
        "pages": [
            "/market-reports", "/market-reports/substantial-shareholders",
            "/market-reports/net-short-positions", "/market-reports/foreign-headroom",
            "/market-reports/share-buybacks", "/market-reports/sbl-positions",
            "/market-reports/historical-reports",
            "/market-pulse", "/market-pulse/settings",
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
               cadence="daily", role="ingestion", storage="snapshot",
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
                    "and did nothing. 2026-10-25: run_scraper_and_save_to_db() ended in "
                    "a bare `return True` and save_to_db() swallowed its own rollback, "
                    "so a 0-row or failed-write run was recorded GREEN (same bug class "
                    "as the 2026-10-13 substantial-shareholders incident). It now "
                    "returns {ok, rows, added, updated, companies, report_date, partial, "
                    "stage_failures}; ok=False when the page served no rows or nothing "
                    "was written, and partial=True below MIN_COMPANIES_EXPECTED (150; "
                    "the 2026-08-25 capture held 270 rows). Contract locked by "
                    "tests/test_daily_indicators_contract.py."),
            _j("daily_market_update", "backend/scripts/daily_market_update.py",
               "NOT SCHEDULED — see market-data", None, "manual",
               writes=["market_pulse", "industry_group_history"]),
        ],
        "tables": [
            _t("substantial_shareholders", "report_date", 3, "market_reports"),
            _t("net_short_positions", "report_date", 3, "market_reports"),
            _t("foreign_headrooms", "report_date", 3, "market_reports"),
            _t("share_buybacks", "report_date", 3, "market_reports"),
            _t("sbl_positions", "report_date", 3, "market_reports"),
            _t("historical_reports", "report_date", 7, "market_reports"),
            # qfi_ownership_flows REMOVED 2026-10-25 (model, response schema,
            # GET /api/market-reports/qfi-flows, the qfi_flows counter in
            # /api/rebh/importers-status and the table itself). It had 0 rows, no
            # writer in scripts/update_market_reports.py (which saves only the six
            # tables above) and no frontend caller. Re-introduce it only with a
            # working scraper; per-symbol net inflow is not published by Tadawul.
            _t("market_pulse", "date", 1, "daily_market_update",
               note="stage 3.5 of the manual pipeline"),
            _t("industry_group_history", "date", 1, "daily_market_update",
               note="stage 7. Model app/models/industry_group.py is not imported by "
                    "database.py, so the table is invisible to Alembic autogenerate "
                    "(it is created by migration f12345678901)"),
            _t("tasi_components", "updated_at", 5, "scraper:tasi_components",
               note="no business date column — updated_at is all there is",
               pages=["/valuation/market-weight", "/valuation/report"]),
            _t("tasi_settings", "updated_at", None, None,
               note="operator-maintained settings for market pulse"),
            _t("system_config", "updated_at", None, None,
               note="valuation configuration, edited from the admin config routes"),
            _t("valuation_zones", "created_at", None, None, verified=False,
               note="seeded reference data; no live writer identified"),
            _t("eps_estimates", "updated_at", None, None, verified=False,
               note="seeded reference data; no live writer identified. AUDITED "
                    "2026-10-19: 4 rows only, and they are S&P 500 INDEX earnings "
                    "(2024 249.13 actual 'S&P', 2025 271.30, 2026 330.00, 2027 375.00 "
                    "'Yardeni') keyed by `year` with UNIQUE(year) and NO symbol column "
                    "— read by valuation_service for the /valuation index-level "
                    "scenarios. It is NOT and cannot be company consensus for a "
                    "Tadawul issuer; no analyst-consensus/revision/price-target source "
                    "exists in this schema (72 tables checked)."),
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
            _b("scraper", "backend/app/scrapers/scrapling_fetch.py",
               "shared anti-bot fetch layer (http_get / browser_get / browser_capture) "
               "used by eurodollar, gurufocus, SAMA and sukuk — degrades to plain "
               "requests when scrapling is absent"),
            _b("scraper", "backend/app/scrapers/fred_scraper.py",
               "FRED_API → economic_indicators; observation_start = last DB date − "
               "buffer (5d daily series, 90d monthly, so official revisions and a "
               "missed day-10 run are re-covered)"),
            _b("scraper", "backend/app/scrapers/treasury_gov_scraper.py",
               "Treasury.gov → treasury_yield_curves (the ONLY treasury module; there is "
               "no treasury_scraper.py). Fetches every month from last DB date − 7d to "
               "today, not one fixed month (capped by TREASURY_MAX_INCREMENTAL_MONTHS)"),
            _b("scraper", "backend/app/scrapers/sp500_scraper.py", "OHLC + PE → sp500_history"),
            _b("scraper", "backend/app/scrapers/sp500_pe_scraper.py",
               "Multpl.com PE — incremental window = last 3 months "
               "(SP500_PE_LOOKBACK_MONTHS), stretched back to the newest stored P/E "
               "whenever the DB is older, so any manual run closes missing months"),
            _b("scraper", "backend/app/scrapers/sp500_ey_scraper.py",
               "S&P 500 earnings yield: GuruFocus 151 (Nuxt payload) primary; window "
               "= today − SP500_EY_LOOKBACK_DAYS (90d), widened to the newest stored "
               "EY date when the DB is behind. 1031 was the wrong series "
               "(Quits Nonfarm) and is now guarded by a title check"),
            _b("scraper", "backend/app/scrapers/eurodollar_scraper.py",
               "Investing.com snapshot — Scrapling impersonated HTTP (rotating "
               "fingerprints) → stealth browser → requests"),
            _b("scraper", "backend/app/scrapers/cmefedwatch_scraper.py", "CME FedWatch snapshot"),
            _b("scraper", "backend/app/scrapers/gurufocus_scraper.py",
               "SP500_EY / SP500_PE — Scrapling HTTP + pure-Python __NUXT__ decoder, "
               "stealth-browser fallback; Selenium removed 2026-10-23 — exists, not "
               "scheduled (incremental page count comes from the DB's latest date)"),
            _b("service", "backend/app/services/rebh_importers_service.py",
               "health aggregation only — the sukuk/macro wrapper importers "
               "were retired 2026-10-13"),
            _b("script", "backend/scripts/Sukuk&Bonds.py",
               "THE sukuk ingestion entry point — run manually; records "
               "job_runs key 'sukuk_importer' (trigger='manual'). Playwright removed "
               "2026-10-23: replays the portal data URL over Scrapling HTTP, then "
               "stealth-session XHR capture, then DOM parsed by header name, then seed. "
               "2026-10-25 integrity pass: upsert key is now the real composite "
               "(symbol, parent_company_symbol) with NULL-parent rows normalised so "
               "Postgres' NULLS-DISTINCT constraint cannot inflate the table; "
               "lastTadeYield (53 of 62 live items) feeds yield_to_maturity, so "
               "Build-Up R gets is_ytm=True instead of the coupon; instruments the "
               "grid stops listing are flagged is_active=False, guarded by "
               "SUKUK_MAX_RETIRE_PER_RUN (5) + SUKUK_MIN_DEACTIVATE_COVERAGE (0.8) "
               "and never applied to the seed payload."),
            _b("script", "backend/scripts/SAMA&GaStat.py",
               "THE SAMA/GaStat macro ingestion entry point — run manually; "
               "records job_runs key 'macro_importer' (trigger='manual'). Selenium "
               "removed 2026-10-23: StealthyFetcher + page_action pulls the bulletin "
               ".xlsx into BytesIO for pandas"),
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
               cadence="daily", role="ingestion", storage="snapshot",
               note="not scheduled on purpose: Investing.com returns 403 from "
                    "datacenter IPs. Since 2026-10-13 the Admin endpoint wraps the "
                    "scrape in track_job('manual:eurodollar') and job_runner has a "
                    "Run Now callable, so runs land in job_runs. "
                    "2026-10-26 (the table sat frozen at 2026-06-30): the browser "
                    "channel now runs solve_cloudflare=True with a 120s budget "
                    "(Scrapling floors it at 60s when the solver is on), and every "
                    "channel — HTTP, browser, legacy requests — can carry a proxy "
                    "from EURODOLLAR_PROXY -> SCRAPLING_PROXY -> HTTPS_PROXY "
                    "(resolve_proxy in app/scrapers/scrapling_fetch.py; a datacenter "
                    "proxy only moves the 403, it must be residential/ISP). Measured "
                    "the same day: HTTP answers from a residential IP with all 25 "
                    "contracts, so the block is IP reputation, not markup. Under "
                    "EURODOLLAR_MIN_CONTRACTS (8; the page normally carries ~25) the "
                    "grid counts as truncated and the Yahoo SOFR fallback answers with "
                    "the ONE contract Yahoo still lists (SR3=F — per-code GE*/SR* "
                    "tickers are 404 since Eurodollar was delisted, and CME Group "
                    "refuses scripts by IP), flagged partial=True so the ledger reads "
                    "'PARTIAL: ...' instead of a fake full day."),
            _j("manual:cme_fedwatch", "backend/app/scrapers/cmefedwatch_scraper.py",
               "manual only — Admin button POST /api/economic-indicators/scrape/cme-fedwatch "
               "or Run Now",
               None, "manual", writes=["cme_fedwatch"], tracked=True,
               cadence="daily", role="ingestion", storage="snapshot",
               note="not scheduled: QuikStrike is flaky from server IPs. Since "
                    "2026-10-13 both the Admin endpoint and Run Now record under "
                    "track_job('manual:cme_fedwatch'); the scraper now returns "
                    "{ok, rows} so rows_written is real, and a failed snapshot is "
                    "recorded red instead of an empty success."),
            _j("manual:gurufocus", "backend/app/scrapers/gurufocus_scraper.py",
               "manual only — Admin monthly buttons /scrape/SP500_EY and /scrape/SP500_PE "
               "or Run Now",
               None, "manual", writes=["economic_indicators"], tracked=True,
               note="GuruFocus (primary) + sp500_ey (alternative) both feed the "
                    "S&P 500 earnings yield (SP500_EY) into economic_indicators; this "
                    "job stays active and is also the sole writer of the SP500_PE key. "
                    "Both scrape GuruFocus, which can 403 from datacenter IPs, hence "
                    "the lighter sp500_ey alternative. un-scheduled from the monthly cron for RAM/CPU (headless "
                    "Chrome). Since 2026-10-13 tracked (Admin endpoints + Run Now via "
                    "scheduler._gurufocus) and the driver comes from the shared "
                    "daily_detailed_scraper.build_driver (page_load_strategy=eager, "
                    "300s client timeout). sp500_ey_scraper.py is the lighter "
                    "alternative used by scraper_service.run_sp500_ey."),
            _j("scraper:sp500_ey", "backend/app/scrapers/sp500_ey_scraper.py",
               "manual — POST /api/admin/system/run-scraper {\"scraper\":\"sp500_ey\"}",
               None, "manual", writes=["economic_indicators"], tracked=True,
               note="One of two SP500_EY sources in economic_indicators - the lighter "
                    "alternative to manual:gurufocus (which stays active and is the sole "
                    "SP500_PE writer). key comes from "
                    "app/services/scraper_service.py:_run(); the same "
                    "prefixing yields scraper:fred:<code>, scraper:sp500_price, "
                    "scraper:sp500_pe, scraper:treasury_gov. Source chain since "
                    "2026-10-13: GuruFocus (403 from datacenter IPs) → Yahoo "
                    "quoteSummary with cookie+crumb (plain GET = 401) → own "
                    "sp500_history.pe_ratio (EY=100/PE, sanity 0.5–15%)."),
            _j("macro_importer", "backend/scripts/SAMA&GaStat.py",
               "manual — MONTHLY, run when a new SAMA bulletin is published",
               None, "manual", tracked=True,
               writes=["saudi_economic_indicators"],
               cadence="monthly", role="ingestion", storage="cumulative",
               commands=[{"label": "Full history (all SAMA months + GDP TTM)",
                          "kind": "full",
                          "cli": '$env:CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"; .\\venv\\Scripts\\python.exe "scripts/SAMA&GaStat.py" --execute'},
                         {"label": "This month only (latest observation)",
                          "kind": "incremental",
                          "cli": '$env:CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"; .\\venv\\Scripts\\python.exe "scripts/SAMA&GaStat.py" --execute --no-history'}],
               note="THE SAMA/GaStat macro ingestion entry point. --execute writes; "
                    "without it the run is a dry plan. The browser channel needs "
                    "CHROME_BIN (or GOOGLE_CHROME_BIN on Render) or Scrapling falls "
                    "back to an uninstalled Playwright chromium and only the dated "
                    "fallback is written. Records job_runs under 'macro_importer'. "
                    "Run Now fires the LATEST-ONLY pass (history=False — newest "
                    "observation per series); the full history re-pull stays a "
                    "Copy-command only (--execute). saudi_economic_indicators is "
                    "watched by the integrity scan. Removed from the "
                    "daily_market_update pipeline 2026-10-26 — a monthly source was "
                    "being re-pulled in full every day."),
            _j("sukuk_importer", "backend/scripts/Sukuk&Bonds.py",
               "manual — WEEKLY, refresh the sukuk/debt snapshot",
               None, "manual", tracked=True,
               writes=["sukuk_market_data"],
               cadence="weekly", role="ingestion", storage="snapshot",
               commands=[{"label": "Refresh sukuk snapshot",
                          "kind": "full",
                          "cli": '.\\venv\\Scripts\\python.exe "scripts/Sukuk&Bonds.py"'}],
               note="THE sukuk ingestion entry point — no flags, running it performs "
                    "the upsert. Snapshot semantics: the live grid is re-listed each "
                    "run; instruments the grid stops listing are flagged "
                    "is_active=False. Records job_runs under 'sukuk_importer'. Run Now "
                    "is enabled and sukuk_market_data is watched by the integrity scan "
                    "(weekly staleness budget). Removed from the daily_market_update "
                    "pipeline 2026-10-26."),
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
                    "deliberately does not run it; Admin button triggers it by hand. A "
                    "1-row 'Yahoo SR3=F' snapshot means the fallback ran, not a ladder.",
               pages=["/interest-rate/eurodollar-futures"]),
            _t("cme_fedwatch", "scrape_date", None, "manual:cme_fedwatch",
               note="snapshot only — CME/QuikStrike blocks server IPs (Selenium)",
               pages=["/interest-rate/cme-fedwatch"]),
            _t("saudi_economic_indicators", "period", 40, "macro_importer",
               note="`period` is a real DATE since the 2026-10-26 macro migration "
                    "(the business period SAMA/GaStat published, not when we scraped "
                    "it). Monthly/quarterly cadence, so the lag budget is a month "
                    "plus slack. Written by scripts/SAMA&GaStat.py (manual), not a "
                    "cron job"),
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
            _i("fixed", "2026-10-13 · engine_vintages '100% quarantine' was a "
                       "reporting conflation, not an engine failure. The runner "
                       "counted fresh=False (statements past the pricing gate) as "
                       "quarantined, so a routine Run-now logged '236 evaluated, "
                       "236 in quarantine' while only ~11 rows actually failed the "
                       "P0 guards. Staleness and guard-failure are now separate "
                       "buckets (quarantined / stale) and the Rows column reads "
                       "persisted snapshots. ROOT CAUSE of the staleness is data: "
                       "most output/*_financials.json stop at Q1-2026 (period end "
                       "2026-03-31 = 196d > 180d quarterly gate as of 2026-10-13); "
                       "only ~18 companies have Q2 on disk. Re-ingest Q2/Q3 2026 "
                       "XBRL, then re-sync R2 (scripts/upload_to_r2.py) so the "
                       "Render container — which has no local output/ — stops "
                       "reading stale R2 copies."),
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
            _b("service", "backend/app/services/market_universe_service.py",
               "GET /api/rebh/universe builder — TTM/Piotroski/grades and the "
               "unit conversion for every absolute figure. Units come ONLY from "
               "the artifact contract (meta.rounding=='normalized_sar' → fixed "
               "÷1e6), see backend/docs/XBRL_OUTPUT_POLICY.md"),
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
            _b("frontend", "frontend/app/stocks/_lib/stockApi.ts",
               "/stocks/[symbol] mock→real data layer: GET /api/engine/{symbol} "
               "(valuation, profitability, pe-ratio, capital-structure, peers, "
               "quarterly actuals + nine_box DPS), GET /api/rebh/peers?sector= "
               "(peers comparison), GET /api/rebh/sector-stats-full?sector= "
               "(profitability medians), /api/prices/* + "
               "/api/scraper/financials/*/table (Phase 1), Phase 3: "
               "/api/prices/seasonality/{symbol} (StockOverview), "
               "/api/prices/tasi/history (momentum benchmark matrix · peers "
               "PeerPerfChart · summary Momentum rows), "
               "/api/rebh/corporate-actions?symbol= (splits · dividends · "
               "dividends/history . news), /api/reports/{symbol} (earnings filing dates . news feed), Phase 4: engine quarterly -> StockOverview revenue rollup, /api/rebh/peers?sector= -> peers/related-stocks, annual Income_Statement -> growth"),
            _b("frontend", "frontend/app/stocks/_components/GradeChip.tsx",
               "shared grade letter + StatusBadge chips used by the overview summary "
               "cards and /ratings/quant-ratings — one colour map (A+/A/A−, B±, C, D, F, "
               "N/A → em-dash), never an empty square"),
            _b("frontend", "frontend/app/stocks/_components/EarningsHistoryTable.tsx",
               "reported earnings history accordion for /stocks/[symbol]/earnings — "
               "consumes server-formatted strings only (engine flows are FULL SAR)"),
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
                    "app.services.rebh_vintage_service --limit N. Full-universe "
                    "runs take ~6-7 min (236 companies). Rows column = snapshots "
                    "persisted; the job reports quarantined (P0 guards) and stale "
                    "(past the 180d/270d pricing gate) as SEPARATE buckets."),
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
               note="GET /api/rebh/universe reads this batch FIRST and only falls "
                    "back to live compute when it is empty. The whole row is stored "
                    "as payload_json, so the 2026-10-18 unit fix needed no schema "
                    "change — that batch was rebuilt and now carries "
                    "div_paid/div_yield/div_basis/div_period/scale_basis. Batches "
                    "persisted before 2026-10-18 still hold the pre-fix "
                    "(magnitude-guessed) mc/pe/roe/ncav figures and must not be "
                    "served as current."),
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
                        "The 100%-commented-out twin at app/scrapers/"
                        "Corporate_actions_watcher.py was deleted 2026-10-25; "
                        "backend/scripts/corporate_actions_watcher.py is the only "
                        "version, and its AUTO_ADJUST/NEEDS_REVIEW classification "
                        "already carries the design notes that file was keeping."),
            _i("note", "/rebh/[symbol] and /rebh/company/[symbol] are two different "
                       "generations of the same idea; both are live and both hit "
                       "/api/rebh/company/{symbol}."),
            _i("note", "2026-10-15 · app/stocks mock decommission complete (Phase 4): "
                       "stocks/data/mockData.ts deleted; US-only routes removed "
                       "(/stocks/[symbol]/options · /analysis · /transcripts · "
                       "/peers/related-etfs · /news/[id]); MainStockChart + "
                       "AnalysisList · NewsList · InsiderTrading · "
                       "StockFinancialSummary components deleted. Scan of "
                       "app/stocks for MOCK_STOCK_DATA / NVDA / S&P → 0 hits; "
                       "charting + growth + news + related-stocks now read real "
                       "endpoints (prices history, annual Income_Statement, "
                       "/api/reports + corporate actions, /api/rebh/peers)."),
            _i("note", "2026-10-15 · app/stocks Phase 5 RESTORATION — the Phase 4 "
                       "deletion of rich UI was a regression and was reverted: "
                       "StockFinancialSummary restored on the Overview via "
                       "_lib/summaryBuilders.ts (all 11 card groups, real engine/"
                       "universe/prices data, dash cells where no source); "
                       "peers/comparison expanded to the full institutional dataset "
                       "set + PeerPerfChart (recharts, rebased-to-100 vs TASI); "
                       "momentum gained the BenchmarkMatrix (1M→5Y vs TRUE TASI "
                       "returns from market_pulse via new GET /api/prices/tasi/"
                       "history); dividends restored DividendGrades (DERIVED letters, "
                       "documented thresholds — fabricated defaults removed), growth "
                       "& estimates pages rebuilt on the real annual DPS series; "
                       "earnings gained the rewritten real-data EarningsChart and the "
                       "estimates tab rebuilt with dash-consensus tables; "
                       "NoSourcePanel.tsx deleted (zero empty-state panels remain); "
                       "LightweightChart got the premium frame + timeframe toolbar. "
                       "CORRECTION to earlier assumption: a TASI historical series "
                       "DID exist all along in market_pulse (date/close) — see "
                       "app/services/rs_line.py::_fetch_tasi_close."),
            _i("bug", "2026-10-18 FIXED · market_universe_service.py was the only "
                      "consumer that ignored the XBRL artifact contract "
                      "(meta.rounding=='normalized_sar' means the file ALREADY holds "
                      "actual SAR). Two legacy magnitude guessers — a market-cap "
                      "anchor with 0.05/0.00005 ratios and an abs()-threshold branch "
                      "in _norm_period_val — re-scaled live values on top of "
                      "pre-normalized ones. Reproducing the old TTM path over the 257 "
                      "published artifacts mis-scaled 136 rows across 126 companies "
                      "(49%), factors 23x–1000x, including a SIGN FLIP for 1320 "
                      "(reported −6,964.66 M vs a true +8.94 M). Both guessers are "
                      "gone; the only conversion is a fixed ÷1e6, and an artifact "
                      "that violates the contract is logged + flagged "
                      "⚑scale-unverified instead of being silently re-anchored."),
            _i("note", "2026-10-18 · same file, three further unit defects fixed: "
                      "(a) NCAV mixed a millions-scaled current-asset with raw "
                      "total-assets/equity terms — 1e6 apart since normalization, "
                      "e.g. 2060 showed a fake +106,932 M net-net (18% of its own "
                      "market cap) where the honest figure is −5,360 M; now "
                      "NCAV=(CA−TA+TE)/1e6 from one unit. (b) ROE/ROA divided "
                      "millions earnings by raw-SAR equity, so 157 companies read "
                      "exactly 0.0% ROE and the Profitability ladder was dead (all "
                      "C+); both now convert to the same unit. (c) cash-flow fields "
                      "(cfo/fcf/div_paid/owner_yield) annualised the LATEST period "
                      "by 12/months — 58 of 73 dividend payers carried a 3/6/9-month "
                      "stub, so a single yearly distribution was multiplied ×4/×2/"
                      "×1.33 (7020's Q1-2026 payment of 1.196 bn read as 4.78 bn). "
                      "The service now prefers a complete 12-month statement, never "
                      "extrapolates dividends, and discloses the source via "
                      "scale_basis / div_basis / div_period plus the flags "
                      "⚑cashflow-stub-annualised ⚑dividend-ytd-window "
                      "⚑dividend-stale ⚑pncav-not-graham-applicable. New payload "
                      "keys: div_paid, div_yield, div_basis, div_period, scale_basis "
                      "(additive — no consumer broke; Terminal sector templates now "
                      "render a real yield where they printed an em-dash)."),
            _i("note", "2026-10-18 · post-fix verification over the live universe "
                      "(257 rows, read-only): scale_basis = declared_per_filing for "
                      "257/257 · P/E positive n=165, p10/median/p90 = 7.4/15.7/68.0, "
                      "only 2 above 500 · ROE n=252, median 6.5%, max 59.5%, "
                      "ROE>100% = 0, exact-zero ROE down from 157 to 46 · dividends: "
                      "176 rows carry a disclosed distribution, 106 with a usable "
                      "trailing yield (81 full-year + 25 ytd-partial once the "
                      "freshness gate applies), 64 withheld as "
                      "stale, 81 none, top yield 11.59% · grades now spread across "
                      "every letter. Verification scripts were scratch and deleted."),
            _i("note", "2026-10-18 · SNAPSHOT REBUILT (approved DB write). The active "
                      "rebh_universe_snapshots batch for 2026-10-18 (257 rows) was "
                      "regenerated through the production path "
                      "build_universe_snapshot(), so /api/rebh/universe, /api/rebh/peers, "
                      "the home screens and the sector medians now serve the post-fix "
                      "figures: 257/257 rows carry scale_basis="
                      "declared_per_filing, 176 carry div_paid and 106 a usable "
                      "div_yield (81 full-year + 25 year-to-date after the 18-month "
                      "freshness gate), and 2060's NCAV reads −5,360 M instead of the "
                      "fake +106,932 M. Rows were replaced in place (is_latest "
                      "demotion + re-insert); batches from earlier dates are untouched "
                      "and still hold pre-fix payloads — they are history, not the API "
                      "response. The pre-fix payload of today's batch was dumped to "
                      "backend/_universe_pre_fix_backup.json before the rewrite "
                      "(throw-away once the fix is signed off)."),
            _i("watch", "The rebuilt batch is a point-in-time copy: engine_vintages "
                      "must keep running (cron / admin / CLI) or /api/rebh/universe "
                      "silently reverts to serving stale numbers, because the endpoint "
                      "reads the snapshot FIRST and only falls back to live compute "
                      "when no batch exists."),
            _i("bug", "2026-10-18 FIXED · rebh_unified_engine.py (the engine behind "
                      "GET /api/engine/{symbol}) computed ROE/ROA as "
                      "latest_reported_IS_window ÷ full equity. Between filing cycles "
                      "that window is a 3-month cumulative stub, so the ratio was a "
                      "quarter of a year over a whole balance sheet: 4001 (Al Othaim) "
                      "published ROE 4.5% where the disclosed trailing year gives "
                      "18.7%, and every ROE-anchored letter moved with it — "
                      "Profitability, the Growth proxy and the financial-sector "
                      "Safety rule (4001 Profitability C+ → B+). ROE/ROA now take a "
                      "twelve-month numerator from _twelve_month_profit(): a complete "
                      "TTM of four discrete quarters ('ttm_4q') › the latest reported "
                      "12-month statement ('annual_fy') › the raw latest figure "
                      "labelled 'partial_window'. Nothing is multiplied by "
                      "12/months — margins stay on the raw ni/rev pair because both "
                      "come from the SAME window. The chosen basis is published as "
                      "provenance.profit_12m_basis / profit_12m_sar and the ratings "
                      "page reads it."),
            _i("watch", "2026-10-18 · XBRL artifacts that cannot support a "
                      "twelve-month numerator: 10 of 257 companies publish a "
                      "full-year REVENUE below a shorter cumulative window of the "
                      "same fiscal year, which is arithmetically impossible under "
                      "cumulative YTD reporting — 2060 FY2025 2,488.7 M against its "
                      "own disclosed 2,531.2 M nine-month figure (1212 2025, 1330 "
                      "2018, 2060 2025, 2120 2025, 2140 2025, 2250 2025, 8060 2021, "
                      "8160 2018, 8180 2022, 8260 2022). The derived Q4 "
                      "(annual − 9M) becomes a phantom negative quarter: 2060 would "
                      "have read ROE −39% against a disclosed +5.5%. The engine now "
                      "REFUSES ROE/ROA for those filings "
                      "(profit_12m_basis='rejected-corrupt-fy-<year>-revenue'); the "
                      "Profitability/Cash letters render 'N/A' and the page shows "
                      "em-dashes plus an amber disclosure. Net profit is deliberately "
                      "not used as the detector — a Q4 loss or impairment is "
                      "legitimate, revenue is not. The fix belongs upstream in the "
                      "parser/ingestion, not in the ratio: re-scrape or re-import "
                      "those ten artifacts and the guard releases them automatically."),
            _i("note", "2026-10-18 · LAST MOCK PAGE CLOSED · "
                      "frontend/app/stocks/[symbol]/ratings/quant-ratings/page.tsx "
                      "was serving hardcoded factor letters (F / A- / A-), fake 🔒 "
                      "paywall affordances and a Strong Sell→Strong Buy consensus "
                      "scale that no Tadawul source publishes. It is now a server "
                      "component over two real endpoints, fetched once each "
                      "(Promise.all, no duplicate requests): GET "
                      "/api/engine/{symbol} `grades` "
                      "(rebh_production_helper_service.py::compute_peer_relative_grades "
                      "— Cash, Profitability, Balance, Valuation, Growth, Safety) and "
                      "GET /api/rebh/vintages/{symbol} for the dated columns, which "
                      "print the real archived batch dates instead of an invented "
                      "3M/6M series (the table retains 2 batches per company today). "
                      "The left card shows the engine's actual verdict inputs (buy "
                      "gate, margin of safety, required return, Piotroski /9, Altman, "
                      "Beneish, predictability stars, red flags, owner yield, NCAV, "
                      "Shariah screen) with stale/quarantine banners. stockApi.ts "
                      "gained EngineSafetyDto / EngineBuyGateDto / EngineShariahDto / "
                      "EngineVintageGradesDto, 16 EngineSnapshotDto fields (incl. pe "
                      "derived exactly as _compat_payload does) and "
                      "fetchEngineGradeHistory(). Still unreferenced mock "
                      "components awaiting a delete decision: "
                      "stocks/_components/RatingsSummary.tsx (SA Analysts 3.74 / Wall "
                      "Street 4.67), AnnualEstimatesSummary.tsx, "
                      "RevisionsTrendTable.tsx — none is imported anywhere."),
            _i("note", "2026-10-18 · regression nets added (backend/tests, no DB, "
                      "no network): test_universe_unit_contract.py (17 tests) pins "
                      "the artifact unit contract — no 0.00005 anchor, no "
                      "magnitude-conditional branch, _norm_period_val is a fixed "
                      "÷1e6, NCAV/ROE/ROA single-unit, dividends never annualised "
                      "and the div_* keys published — plus "
                      "test_engine_twelve_month_profit.py (22 tests) for the "
                      "twelve-month numerator and the corrupt-filing guard. "
                      "Period labels are built relative to date.today() so the "
                      "files cannot rot."),
            _i("note", "2026-10-19 · /stocks/[symbol] presentation pass (frontend "
                      "only — no endpoint, table or job changed). (1) Grade letters "
                      "belong to five cards only (Valuation · Growth · Profitability · "
                      "Momentum · Earnings Revisions); the four statement cards and the "
                      "non-scored cards no longer render a badge node at all — the "
                      "placeholder square that read like a collapse button is removed "
                      "from the DOM (StockFinancialSummary.tsx takes `grades` as "
                      "optional and skips the span when undefined). (2) The Dividend "
                      "Growth History English raw message became a framed bilingual "
                      "empty state that prints the disclosed facts: for 1321 the "
                      "universe row (/api/rebh/peers) carries div_paid 141.75 M SAR · "
                      "div_yield 2.44 % · basis annual · window 2025-04_2026-03, while "
                      "corporate_actions holds only a 2022-10-19 bonus issue and the "
                      "annual IS has no per-share dividend row — so the bars are empty "
                      "for want of an annual DPS series, not for want of a distribution. "
                      "summaryBuilders.ts now exports DividendFacts + revisionsGrade() "
                      "+ revisionsCounts(). (3) The Revisions grade rule lives in one "
                      "place (sequential realised EPS direction over the last 4 "
                      "discrete quarters: 4→A, 3→B, 2→C, 1→D, 0→F, <4 measured → no "
                      "letter) and is labelled realised momentum, never analyst "
                      "consensus — the overview card, /earnings and /earnings/revisions "
                      "cannot disagree. (4) /ratings/quant-ratings: stale/quarantine "
                      "amber+red bars demoted to StatusBadge chips in one top strip; "
                      "Factor Grades is an aligned table (uppercase headers, divide-y "
                      "rows, tabular-nums, real vintage dates from /api/rebh/vintages/"
                      "{symbol}); Piotroski / Altman Z / Margin of Safety / Shariah "
                      "render as Bloomberg-style StatTiles. (5) Earnings Summary now "
                      "hosts both annual estimate tables (reported FY rows + dashed "
                      "consensus rows) beside the chart, and Earnings History is the "
                      "per-quarter disclosure accordion (XBRL filing date from the "
                      "scraper Quarterly Income_Statement `Last Update Date` row, "
                      "margins, vs prior quarter, vs year ago). Bug fixed on the way: "
                      "engine quarterly flows are FULL SAR, so money cells are "
                      "formatted server-side as fmtSarM(v / 1e6) — the first cut "
                      "printed '160.00T SAR'; and the P/E · P/S · forward multiples now "
                      "divide a FULL-SAR market cap (price × diluted_shares) by a "
                      "FULL-SAR flow instead of price ÷ a per-share sum. Superseded "
                      "orphan components awaiting a delete decision: "
                      "EarningsHistory.tsx, RevisionsTable.tsx, EarningsSummary.tsx "
                      "(plus the earlier RatingsSummary.tsx, "
                      "AnnualEstimatesSummary.tsx, RevisionsTrendTable.tsx) — none is "
                      "imported anywhere. Known open data gap: the engine publishes no "
                      "`grades.Momentum` key, so the Momentum letter is a permanent "
                      "neutral dash until the engine emits it. SUPERSEDED: Momentum "
                      "now reads the real sector_rs_rating (see Phase 6 note below)."),
            _i("note", "2026-10-19 · Phase 6 — the em-dash audit of the stock pages. "
                      "(1) ROOT CAUSE of the empty statement fields was a mapping-key "
                      "mismatch, not missing data: XBRL items whose raw label never "
                      "resolved were stored is_unmapped=True and every consumer "
                      "filtered them out, silently discarding disclosed figures — "
                      "'Finance leases, non-current' (154 issuers, blocklisted), "
                      "Rajhi's 'Borrowings - current' 56.17 B / '- non-current' "
                      "323.34 B, Almarai's plural 'Debt securities, term loans, "
                      "borrowings and sukuks in issue', and 'Profit (loss) from "
                      "operating activities' (12 issuers — a disclosed EBIT subtotal). "
                      "Fixed in synonyms.json (+32 bindings, 470→502) mirrored into "
                      "xbrl_mapping.PARAM_MAPPING, the finance-lease blocklist entry "
                      "deleted (proved against backend/data/downloads/1321/"
                      "2025_XBRL_Q3.xls that it rolls up under Non-current "
                      "liabilities), and a strictly additive read-time repair "
                      "_with_catalog_repair() in xbrl_data_service.get_company "
                      "(exact NORM_MAP only, status rebound@read). Coverage moved: "
                      "issuers with a borrowing value on the latest balance sheet "
                      "173→218 (217 non-zero of 237), issuers carrying an EBIT line "
                      "166→178. (2) The engine now publishes the provenance the UI "
                      "needs: CapitalStructure.borrowings_disclosed + "
                      "source_status, safety.interest_coverage_basis and "
                      "provenance.gross_profit_basis. Gross Profit is the disclosed "
                      "IS-030 subtotal where filed, else Revenue + Cost of Sales "
                      "(identity held on 4,506/4,512 period checks within 2%; "
                      "expenses are stored negative, hence the sum) — which keeps a "
                      "legitimately negative gross profit (2210 −108,441,000). "
                      "Operating profit is deliberately NEVER derived (Gross+S&D+G&A+"
                      "Other matched only 2,148/4,010 = 54%) because EBIT feeds "
                      "interest coverage → safety → compensation_pct → required "
                      "return → DCF/zones/MoS. (3) summaryBuilders.ts distinguishes a "
                      "VERIFIED zero debt from an UNDISCLOSED one (fmtSarM "
                      "treatZeroAsMissing driven by borrowings_disclosed) and prints "
                      "a note explaining that Net Debt is then net cash and D/E is 0; "
                      "Interest Coverage now comes from engine.safety with its basis. "
                      "(4) Dividend Growth History: swept all 237 issuers / 1,298 "
                      "annual Income_Statement rows — the 'Cash Dividends' row is a "
                      "PER-SHARE figure for exactly 1 issuer (2210), an AGGREGATE "
                      "amount for 80 (2010 = 13,500,000), blank for 120 (incl. 1321) "
                      "and absent for 36; corporate_actions has no amount column at "
                      "all and the archive stops near FY2022. So no DPS history is "
                      "computed for ANY Saudi stock and the Overview card was DELETED "
                      "on instruction; the same sweep found the aggregate figures "
                      "being printed as 'SAR / share' on the dividend pages, now "
                      "blocked by DPS_PLAUSIBILITY_CEILING = 250 in "
                      "_lib/dividendGrades.ts plus diagnoseDividendGap() driving "
                      "three distinct AR/EN empty states on /dividends/growth and "
                      "/dividends/history. (5) DISCLOSURE DATE: audited schema-wide — "
                      "company_official_filings.published_date is NULL on all 21,424 "
                      "rows and XBRL compiled meta carries no filing date, so the "
                      "only usable source is the scraped 'Last Update Date' row; "
                      "/earnings now merges the Quarterly row with the Annually row "
                      "(the annual one covers fiscal year-ends, where the quarterly "
                      "archive lost it). (6) EstimatesTable mapped columns by header "
                      "WORDING and required the exact strings 'P/E'/'P/S', so "
                      "'P/E (reported)' fell through to row[h], found no key and "
                      "dashed a multiple that HAD been computed — columns are now "
                      "declared by an explicit keys[] prop. (7) CONSENSUS VERDICT: "
                      "Tadawul publishes no analyst consensus and none is ingested "
                      "(72 tables, every route and every scraper checked), so "
                      "/earnings/estimates and /earnings/revisions were re-based on "
                      "realised figures on instruction: the Low/High/#-of-Analysts "
                      "columns, the FY+n placeholder rows, the all-dash 90-day "
                      "revisions table and the DashCard are gone; in their place are "
                      "realised FY growth, reported P/E and P/S on complete fiscal "
                      "years, a realised quarter table with the year-ago surprise, "
                      "and realised up/cut direction over the last 4 and 8 quarters "
                      "for EPS AND revenue (same revisionsCounts rule, revenue "
                      "passed as the series). (8) Momentum letter in the Overview is "
                      "no longer a permanent dash: /api/rs-v2/* is mounted with "
                      "protected_dependencies, so SSR cannot forward the session and "
                      "a 'use client' MomentumGradeBadge.tsx uses the existing "
                      "authFetch-based fetchRsLatest() and picks "
                      "sector_rs_rating for the symbol, with SummaryCard.gradesSlot "
                      "and a shared _lib/gradeBadge.ts (a function prop cannot cross "
                      "the server→client boundary). (9) Peers: /peers/comparison now "
                      "shows the universe div_yield (112 of 257 rows carry one; 176 "
                      "carry div_paid; 81 none; 64 stale with the yield withheld by "
                      "design), div_paid and the Distribution Window basis on every "
                      "column, and /peers/related-stocks gained a Div Yield % column. "
                      "The 100-cell dash farms 'Institutional Ownership' and 'EPS "
                      "Revisions (90-day consensus)' were DELETED on instruction, and "
                      "'Total Return & Risk' is now computed for real from the stored "
                      "daily closes (262 of 274 symbols hold ≥240 trading days in the "
                      "trailing year) against the true TASI benchmark — new "
                      "_lib/returnRisk.ts + PeerRiskRows.tsx, which reuse the exact "
                      "series the PeerPerfChart already fetches (getJson dedupes the "
                      "URL, so no extra request per symbol). (10) The six orphan "
                      "components (EarningsHistory, RevisionsTable, EarningsSummary, "
                      "RatingsSummary, AnnualEstimatesSummary, RevisionsTrendTable) "
                      "are deleted after grep-confirming zero importers. npx tsc "
                      "--noEmit clean. (11) VERIFIED by SSR render of 14 stock routes: "
                      "1321 Gross Profit 592.39 M / Operating Profit 569.83 M / Total "
                      "Debt 13.07 M / Net Debt -127.61 M / D-E 0.8% / Interest Coverage "
                      "62.30x; 2210 -135.48 M / -200.83 M / 113.78 M / -5.40x; 1010 "
                      "13.17 B / 51.43 B / 15.00x; earnings history detail margins 8/8 "
                      "and disclosure dates 3/3 filled; reported P/E 14.21 and P/S 3.64 "
                      "print in the FY tables. Two dashes are PROVEN honest, not bugs: "
                      "3001 discloses no borrowing line (footnote explains Net Debt = net "
                      "cash), and 2222 interest coverage is null with basis 'no source: "
                      "operating result not disclosed'. GOTCHA for future em-dash "
                      "triage: getJson() passes next:{revalidate:3600}, so the frontend "
                      "can serve a payload captured BEFORE a backend engine change for up "
                      "to an hour - a dash seen in SSR must be compared against "
                      "GET /api/engine/{symbol} before any mapping is blamed (that is "
                      "exactly how 1321 Total Debt looked unfixed). (12) ZERO-SUBSTITUTION "
                      "RESTRUCTURING (2026-10-19): routes /stocks/[symbol]/earnings/"
                      "estimates, /earnings/revisions, /dividends/growth and /dividends/"
                      "estimates deleted (verified 404); the realised quarter-vs-prior-"
                      "quarter delta table moved into the Earnings Summary (tabs now = "
                      "Summary + Surprise); dividend tabs now Scorecard/Yield/Safety/"
                      "History. Feature-level permanent dashes removed from the DOM: "
                      "Revenue/EPS Growth (FWD), Payout Frequency, FY1/FY2 (est.) rows "
                      "(summaryBuilders), Forward Yield cell (dividends/yield), "
                      "Dividend Growth grade row + dpsCagr/dpsLatestYoY/"
                      "consecutiveGrowthYears helpers (no per-share DPS history exists; "
                      "only 1 of 237 issuers files one). Honest per-company dashes "
                      "remain by decision. Backend fix: invalidate_industry_groups_data "
                      "was imported by prices.py + scraper.py but defined nowhere - now "
                      "implemented in cache_helpers.py (industry:groups:* pattern). SSR "
                      "smoke gotcha: middleware.ts redirects EVERY non-public path to "
                      "/login when no session_token/refresh_token cookie is present - "
                      "smoke scripts must send a dummy cookie or they validate the "
                      "login page, not the route. tsc clean; pytest 266 pass - the 28 "
                      "failures are pre-existing: async auth suites need pytest-asyncio "
                      "(not installed in this venv) and 3 REBH acceptance tests assert "
                      "against an older engine contract (label casing, .fresh gating)."),
            _i("note", "2026-10-20 · DIVIDEND-CASH DECOUPLING + ZERO-SUBSTITUTION "
                      "CUTS (1321 inquiry): (1) rebh_unified_engine.py §5b now computes "
                      "dps/dps_source/diluted_shares via derive_cash_dps "
                      "(rebh_production_helper_service.py, single CFF 'Dividends Paid' "
                      "source shared with extract_nine_box_inputs) BEFORE and OUTSIDE "
                      "the quarantine gate - a corrupted issuer EPS line can no longer "
                      "blank dividend pages; nine_box/DCF/IRR stay blocked (1321: "
                      "dps=4.50 SAR from 141.75M ÷ 31.5M shares while nine_box stays "
                      "null). Contract gained top-level dps/dps_source/diluted_shares "
                      "(rebh_contract.py); stockApi.ts prefers them, falling back to "
                      "nine_box for cached payloads. (2) G3 EPS_SHARES_CROSSCHECK now "
                      "prints a dedicated Arabic label when eps_reported == net "
                      "(issuer repeated net income inside the EPS cell - 1321's real "
                      "defect, ratio signature = share count exactly) instead of the "
                      "generic units fault. (3) Dividends hub: 'Corporate Actions on "
                      "Record' AND 'Last Announced Distribution' blocks DELETED (0 of "
                      "194 market-wide corporate-action rows is a cash-distribution "
                      "record; capital events live on /splits); real Sector Median "
                      "Yield added (medianNum over /api/rebh/peers div_yield - 1321: "
                      "2.57% vs 4.01% median of 22 Materials payers). (4) pe-ratio: "
                      "'Earnings Growth (Actual, TTM vs Prior Year)' substitute card "
                      "DELETED (zero-substitution: it stood in for consensus EPS "
                      "estimates; realized growth lives on /growth). Verified: tsc "
                      "clean, targeted pytest 57 pass (1 pre-existing acceptance "
                      "failure on growth-signal wording, unrelated), 15/15 SSR smoke "
                      "on 1321 dividends/yield/safety/pe-ratio; engine cache must be "
                      "invalidated after contract-field changes "
                      "(invalidate_rebh_engine_cache) and Next revalidate:3600 can "
                      "serve the pre-change payload once before the value lands."),
            _i("note", "2026-10-20 · FINANCIAL-CONCEPT-MISMATCH AUDIT (frontend only - no "
                      "endpoint, table or job changed): rule = a term with a precise global "
                      "meaning may not be stamped on unrelated equations/historical data; "
                      "where the market has no such data the feature is DELETED, not "
                      "relabelled. (1) 'Earnings Revisions' card on the overview + the "
                      "earnings page renamed to 'Quarterly Earnings Trend (QoQ)' / "
                      "'Quarterly EPS Trend (QoQ)' and computed from realised NET PROFIT "
                      "between consecutive quarters (the old code computed a revenue delta "
                      "but labelled the rows EPS); every 'Revisions' token struck. "
                      "(2) EarningsSurprise tab AND route /stocks/[symbol]/earnings/surprise "
                      "DELETED (verified 404) - surprise needs actual minus consensus and "
                      "Tadawul publishes no consensus; the tab showed plain YoY growth. "
                      "Earnings tabs are now Summary only. (3) Momentum: Buy/Sell -> "
                      "'Above SMA'/'Below SMA'/'At SMA' with column header 'Position vs "
                      "Average' (a price-vs-average position is not a licensed "
                      "recommendation). (4) /ratings/quant-ratings carries a bilingual "
                      "banner: REBH Algorithmic Peer-Relative Grades, not credit ratings, "
                      "not Tadawul classification, not analyst opinion; same provenance "
                      "now printed wherever a letter survives (new shared component "
                      "_components/GradeProvenanceNote.tsx wired into /valuation and "
                      "/profitability; MomentumGradeTable footnote; HomeDashboard Sector-RS "
                      "footnote). (5) Dividends: self-invented letters cancelled - "
                      "deriveDividendGrades/DividendGradeSet deleted from "
                      "_lib/dividendGrades.ts, DividendScorecard renders the accounting "
                      "ratios with their formulas (payout ratio, FCF coverage, fiscal "
                      "years with a filed dividend); tab 'Dividend Safety' -> 'Payout & "
                      "Coverage' (route /dividends/safety kept); yield page states 'No "
                      "yield grade is issued'. (6) GLOBAL SWEEP: 5 orphan mock-era "
                      "components deleted from app/stocks/_components (DividendGrades, "
                      "DividendGrowthBars, FactorGrades - defaulted to hardcoded letters "
                      "incl. a 'Revisions' row, QuantRanking - hardcoded 'Information "
                      "Technology / Semiconductors / 747 out of 4327', AnalysisList - "
                      "fabricated analyst posts with dicebear avatars); code-level misnomer "
                      "forwardPe/forwardPs -> peReported/psReported in earnings/page.tsx + "
                      "FiscalYearTable.tsx (values are current price/cap over FILED "
                      "figures); screener filter 'SmartSelect Ratings' -> 'Relative-"
                      "Strength Ratings'/'RS Ratings' (TopFilterPanel + FilterSidebar; "
                      "internal id 'smartselect' kept); rebh/[symbol] CapitalStructureCard "
                      "badge 'Seeking Alpha model - Verified' -> 'REBH engine - computed "
                      "from filed statements' and '0 M' -> em-dash; market-pulse column "
                      "groups 'Buy Signals'/'Sell Signals' -> 'Strength Patterns'/'Weakness "
                      "Patterns' and 'Buy Switch' -> 'Buy Switch (FTD gate)' (matches "
                      "market_pulse_calc.py =IF(T2=\"FTD\",\"ON\",\"\")); "
                      "/market/yri-earnings-outlook given an amber third-party-source "
                      "disclosure (static S&P 500 consensus PNGs/JSON, reproduced not "
                      "computed, no recommendation implied). Verified: tsc --noEmit exit 0; "
                      "34-route SSR smoke all 200 (surprise 404 by design, /ratings and "
                      "/peers 307 by design); case-sensitive DOM audit of /earnings, "
                      "/dividends and /quant-ratings HTML shows zero surviving 'Earnings "
                      "Revisions'/'Earnings Surprise'/'Consensus'/'Dividend Grades'/"
                      "'Safety Grade'/'Yield Grade'/'# of Analysts' - the only 'Strong Buy' "
                      "string left is inside the disclaimer sentence that denies it. "
                      "GOTCHA for repeats: PowerShell -match/-like are CASE-INSENSITIVE, "
                      "so forbidden-term sweeps over fetched HTML must use node or "
                      "Select-String -CaseSensitive or disclaimers read as violations; "
                      "Invoke-WebRequest does "
                      "not follow 307/308 - use curl.exe for route smoke."),
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
            # xbrl_upload.py (/api/upload/*) retired 2026-10-29 — zero callers;
            # ingestion goes through backend/scripts on the same parser/service layer.
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
               "/api/rebh/breadth/today|series|events|guru (protected)"),
            _b("service", "backend/app/services/breadth_job.py",
               "the orchestrator the cron job actually spawns"),
            _b("service", "backend/app/services/eps_growth_service.py",
               "XBRL quarterly basic-EPS YoY map feeding the Guru TML column"),
            _b("indicator", "backend/app/services/breadth_engine/breadth/indicators/guru.py",
               "Guru Stock Scans: 9 membership filters + RS/Stage, stored in breadth_data"),
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
               cadence="daily", role="computation", storage="cumulative",
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

    # ─────────────── 13. Weekly Routine PDF — REMOVED 2026-10-25 ────────────────
    # The whole system was deleted on 2026-10-25 at the owner's request (the
    # button was already off the page):
    #   · frontend/app/api/generate-weekly-report/route.ts (Puppeteer + pdf-lib,
    #     Weekly_Routine.pdf) — the ONLY user of both npm deps
    #   · the ?autoReport=true / window.__REPORT_PDF_BASE64__ hook in
    #     frontend/app/industry-groups/page.tsx (its only caller is gone; the
    #     page's own jsPDF export still saves a file directly)
    #   · backend/scripts/generate_weekly_report.py
    #   · the WeeklyMarketReport type in frontend/lib/market-report.ts
    #   · table weekly_market_reports (migration 2026_10_25_drop_unwritten_tables)
    # The backend half (app/api/routes/weekly_market_update.py,
    # app/models/weekly_market_report.py, app/services/weekly_report/) lived in
    # git HEAD but was already deleted from the working tree by the uncommitted
    # cleanup before this date, so the table stayed at 0 rows and the GET route's
    # own 404 "No weekly report generated yet. Run generate_weekly_report.py
    # first." was the page seeing an empty table — the message came from the
    # route itself, and the route was mounted in main.py at HEAD.

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
               "backend/alembic/versions/2026_10_25_drop_unwritten_tables.py",
               "drops weekly_market_reports + qfi_ownership_flows (2026-10-25). "
               "The older 2026_10_12_weekly migration is kept in the chain as "
               "history — live alembic_version still points at it."),
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

def _normalise_tables() -> None:
    """Coerce every ``tables`` entry into the dict _t() produces.

    The Runbook, the pipelines map and checks_sql() all index entries as dicts
    (entry["table"], entry["date_column"]). A bare string anywhere in the
    registry therefore took down GET /api/admin/system-map/systems/<id> with
    'TypeError: string indices must be integers' - the home system shipped five
    such strings. Fixing only home would leave the next hand-written entry one
    typo away from a 500 on the admin panel, so the registry normalises itself at
    import time and warns instead of throwing.
    """
    for system in SYSTEMS:
        fixed = []
        for entry in system.get("tables", []):
            if isinstance(entry, dict) and "table" in entry:
                fixed.append(entry)
                continue
            name = entry if isinstance(entry, str) else repr(entry)
            print(f"[system_registry] WARN - system '{system['id']}' table "
                  f"'{name}' is not a _t() entry; probed as 'no date column'")
            fixed.append(_t(name, None, None, None,
                            note="registry entry written as a bare string - add a "
                                 "_t(...) with its real date column", verified=False))
        system["tables"] = fixed


_normalise_tables()

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


# ══════════════════════════════════════════════════════════════════════════════
#  Schedule-runbook projections — read by GET /api/admin/system-map/schedule and
#  /integrity/scan. Pure (no DB, no job_runner import): the route layers the live
#  run-capability + last-run signals on top so this stays unit-testable offline.
# ══════════════════════════════════════════════════════════════════════════════
def schedule_jobs() -> List[Dict[str, Any]]:
    """Every job ONCE, carrying the cadence/role/storage/commands axes plus the
    owning system ids.

    A job such as ``daily_market_update`` is listed under several systems (each
    naming the tables that system reads); the Schedule tab wants a single card, so
    we dedupe by ``job_name`` and union the ``writes``. The entry with the richest
    ``commands`` list wins, so the operator-facing CLI never disappears.
    """
    by_name: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for system in SYSTEMS:
        for job in system["jobs"]:
            name = job["job_name"]
            row = by_name.get(name)
            if row is None:
                row = {
                    "job_name": name,
                    "file": job["file"],
                    "cadence": job.get("cadence") or _derive_cadence(
                        job["expected_schedule"], job.get("trigger", "cron")),
                    "role": job.get("role", "ingestion"),
                    "storage": job.get("storage", "cumulative"),
                    "commands": list(job.get("commands") or []),
                    "writes": list(job.get("writes") or []),
                    "expected_schedule": job["expected_schedule"],
                    "trigger": job.get("trigger", "cron"),
                    "tracked": job.get("tracked", True),
                    "note": job.get("note", ""),
                    "systems": [system["id"]],
                }
                by_name[name] = row
                order.append(name)
            else:
                if system["id"] not in row["systems"]:
                    row["systems"].append(system["id"])
                for tbl in (job.get("writes") or []):
                    if tbl not in row["writes"]:
                        row["writes"].append(tbl)
                if len(job.get("commands") or []) > len(row["commands"]):
                    row["commands"] = list(job["commands"])
    return [by_name[n] for n in order]


# Tables the gap scanner probes → the job that owns the fix. industry_group_history
# is *written* by daily_market_update but *repaired* by backfill_industry_group_history,
# so the owner here is the repair job (its commands are the actionable fix).
#
# The 5th field, ``count_gaps``, says whether a missing-trading-DAY count is
# meaningful for that table. Only cumulative daily-ingest tables qualify:
#   · saudi_economic_indicators is month-end periods → a day count is noise.
#   · eurodollar_futures is a manual snapshot (scrape_date only on demand) →
#     it will never hold every trading day, so only its lag is scored.
_INTEGRITY_TABLES = (
    ("prices", "date", "daily_market_update", "daily", True),
    ("industry_group_history", "date", "backfill_industry_group_history", "daily", True),
    ("saudi_economic_indicators", "period", "macro_importer", "monthly", False),
    ("sukuk_market_data", "updated_at", "sukuk_importer", "weekly", False),
    ("eurodollar_futures", "scrape_date", "manual:eurodollar", "daily", False),
    ("treasury_yield_curves", "report_date", "treasury_gov", "daily", True),
)


def _job_index() -> Dict[str, Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {}
    for system in SYSTEMS:
        for job in system["jobs"]:
            idx.setdefault(job["job_name"], job)
    return idx


def integrity_targets() -> List[Dict[str, Any]]:
    """The scanner's table list with the responsible job + its fix commands resolved
    from the registry. Pure — the route runs the actual SQL probes.

    ``cadence`` + ``count_gaps`` tell the scanner whether a trading-day gap count is
    meaningful (cumulative daily tables) or not (month-end periods, or an on-demand
    snapshot whose scrape_date will never cover every trading day)."""
    jobs = _job_index()
    out: List[Dict[str, Any]] = []
    for table, col, owner, cadence, count_gaps in _INTEGRITY_TABLES:
        job = jobs.get(owner, {})
        out.append({
            "table": table,
            "date_column": col,
            "cadence": cadence,
            "count_gaps": count_gaps,
            "responsible_job": owner,
            "responsible_file": job.get("file", ""),
            "fix_commands": list(job.get("commands") or []),
        })
    return out
