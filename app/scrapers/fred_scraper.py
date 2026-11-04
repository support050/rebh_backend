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
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any
from sqlalchemy import func

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.economic_indicators import EconomicIndicator, FredSeriesMetadata

logger = logging.getLogger(__name__)

# Official FRED API settings — key must be set in .env as FRED_API_KEY
FRED_API_KEY: str = (settings.FRED_API_KEY or os.getenv("FRED_API_KEY") or "").strip()
if not FRED_API_KEY:
    raise RuntimeError(
        "FRED_API_KEY is not set. Add it to your .env file.\n"
        "Get a free key at: https://fredaccount.stlouisfed.org/apikeys"
    )
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
# Series details (units, frequency, seasonal adj, source, last_updated, notes)
# and release calendar — used to persist fred_series_metadata.
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"
# Singular "release/dates" DOES filter by release_id. The plural
# "releases/dates" endpoint silently IGNORES release_id (verified 2026-10-31 by
# comparing rows for different rids), so it can never answer "when is THIS
# release published next?" — that was the bug behind next_release=None.
FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
# Master list of every release (id -> name); used to sanity-check the map below
# so a renamed release surfaces in the log instead of pointing at a wrong date.
FRED_RELEASES_URL = "https://api.stlouisfed.org/fred/releases"
# The page FRED itself links to from "Next Release Date" on a series page.
FRED_CALENDAR_URL = "https://fred.stlouisfed.org/releases/calendar"
# FRED renders "Updated: ... CDT/CST" in U.S. Central time; the API returns an
# offset ISO stamp, so we re-express it in this zone to match the site exactly.
FRED_DISPLAY_TZ = ZoneInfo("America/Chicago")

# ── Smart Incremental Backfill windows ───────────────────────────────────────
# `observation_start` is only a LOWER bound: FRED answers with every observation
# from that date up to today. So a missed run never leaves a hole — the next run
# closes the gap by itself. The buffer below is therefore about official
# REVISIONS (stats offices rewrite recent months) and about a job that skipped
# its day, not about discovering gaps.
DAILY_LOOKBACK_DAYS = int(os.getenv("FRED_DAILY_LOOKBACK_DAYS", "5"))
WEEKLY_LOOKBACK_DAYS = int(os.getenv("FRED_WEEKLY_LOOKBACK_DAYS", "45"))
# Monthly macro series (UNRATE, PAYEMS, CPI, Saudi CPI, money supply) are revised
# for several months after release, and the day-10 cron can be missed entirely:
# re-covering the last 3 months catches both.
MONTHLY_LOOKBACK_DAYS = int(os.getenv("FRED_MONTHLY_LOOKBACK_DAYS", "90"))

# FRED publication frequency per series (the defaults that need the least
# re-covering). Anything not listed here is treated as daily.
MONTHLY_SERIES = {
    "UNRATE", "PAYEMS", "FEDFUNDS", "CPIAUCSL_PC1", "SAUCPALTT01GPM",
    "M1SL", "M2SL",
}
WEEKLY_SERIES = {
    "IC4WSA",            # initial claims are revised week after week
    "WALCL",             # Fed H.4.1 lines get restated
    "TLAACBW027SBOG", "TOTLL", "BOGMBASE",
}


def lookback_days_for(indicator_code: str) -> int:
    """Revision buffer for one series, by publication frequency."""
    code = indicator_code.upper()
    if code in MONTHLY_SERIES:
        return MONTHLY_LOOKBACK_DAYS
    if code in WEEKLY_SERIES:
        return WEEKLY_LOOKBACK_DAYS
    return DAILY_LOOKBACK_DAYS

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
    # Saudi Arabia CPI growth rate (YoY %) — IMF-sourced, published by FRED.
    # The 'GPM' suffix already means 'Growth Rate Percent Monthly', so the
    # value returned by the API is the inflation percentage itself; no
    # units=pc1 param is needed (and FRED rejects units=pc1 on pre-computed
    # growth series). Feeds /api/rebh/market-macro -> saudi_macro.inflation_pct
    # when the SAMA/GaStat 'saudi_inflation_cpi' row is not present.
    "SAUCPALTT01GPM": {
        "series_id": "SAUCPALTT01GPM",
        "description": "Saudi Arabia Consumer Price Index, Growth Rate (YoY %)",
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


# ── Series metadata (persisted to fred_series_metadata) ──────────────────────
def _fred_get(url: str, params: dict, max_retries: int = 3) -> Optional[dict]:
    """GET a FRED API endpoint with retry; returns parsed JSON or None."""
    base = dict(params)
    base.update({"api_key": FRED_API_KEY, "file_type": "json"})
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=base, timeout=30)
            if resp.status_code == 404:
                logger.warning(f"FRED endpoint 404: {url}")
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            if attempt < max_retries:
                wait = attempt * 3
                logger.warning(f"FRED GET {url} attempt {attempt}/{max_retries} failed: {e}; retry in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"FRED GET {url} failed after {max_retries} attempts: {e}")
                return None
    return None


def format_fred_updated(iso_stamp: Optional[str]) -> Optional[str]:
    """'2026-10-02T08:24:00-05:00' -> 'Oct 2, 2026 8:24 AM CDT' (Central time).

    Built manually (no %-d / %-I) so it is identical on Windows and Linux.
    Returns None on any parse failure so the caller can leave the field blank
    rather than store a raw ISO string that would not match FRED's display.
    """
    if not iso_stamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_stamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        ct = dt.astimezone(FRED_DISPLAY_TZ)
        hour = ct.strftime("%I").lstrip("0") or "0"
        return f"{ct.strftime('%b')} {ct.day}, {ct.year} {hour}:{ct.strftime('%M')} {ct.strftime('%p')} {ct.tzname()}"
    except Exception:
        return None


# ── Release calendars ────────────────────────────────────────────────────────
# GET /fred/series does NOT expose release_id (its payload keys are frequency,
# frequency_short, id, last_updated, notes, observation_start/end, popularity,
# realtime_start/end, seasonal_adjustment(_short), title, units(_short) —
# verified 2026-10-31), and FRED has no series->release endpoint. So the
# series -> release id map was resolved once against
# GET /fred/release/series?release_id=<id> (the release's own series list):
#   50  Employment Situation                    <- UNRATE, PAYEMS
#   180 Unemployment Insurance Weekly Claims    <- IC4WSA
#   18  H.15 Selected Interest Rates            <- FEDFUNDS
#   10  Consumer Price Index                    <- CPIAUCSL
#   20  H.4.1 Factors Affecting Reserve Bal.    <- WALCL, TREAST
#   21  H.6 Money Stock Measures                <- BOGMBASE, M1SL, M2SL
#   22  H.8 Assets/Liabilities of Comm. Banks   <- TLAACBW027SBOG, TOTLL
#   209 ICE BofA Indices                        <- every BAML* spread/yield
SERIES_RELEASE_IDS: Dict[str, int] = {
    "UNRATE": 50, "PAYEMS": 50,
    "IC4WSA": 180,
    "FEDFUNDS": 18,
    "CPIAUCSL": 10,
    "WALCL": 20, "TREAST": 20,
    "BOGMBASE": 21, "M1SL": 21, "M2SL": 21,
    "TLAACBW027SBOG": 22, "TOTLL": 22,
}
# All ICE BofA / Merrill Lynch index series share one release calendar.
SERIES_RELEASE_PREFIXES: Dict[str, int] = {"BAML": 209}
# Names the ids above are expected to carry; only used to log a warning.
RELEASE_NAMES: Dict[int, str] = {
    50: "Employment Situation",
    180: "Unemployment Insurance Weekly Claims Report",
    18: "H.15 Selected Interest Rates",
    10: "Consumer Price Index",
    20: "H.4.1 Factors Affecting Reserve Balances",
    21: "H.6 Money Stock Measures",
    22: "H.8 Assets and Liabilities of Commercial Banks in the United States",
    209: "ICE BofA Indices",
}

_release_names_cache: Optional[Dict[int, str]] = None


def _live_release_names() -> Dict[int, str]:
    """id -> name for every FRED release, fetched once per process."""
    global _release_names_cache
    if _release_names_cache is None:
        data = _fred_get(FRED_RELEASES_URL, {"limit": 1000}) or {}
        _release_names_cache = {
            int(r["id"]): (r.get("name") or "").strip()
            for r in data.get("releases", []) if r.get("id") is not None
        }
    return _release_names_cache


def release_id_for(series_id: str) -> Optional[int]:
    """FRED release id for a series, or None when the series is unmapped."""
    sid = (series_id or "").upper()
    rid = SERIES_RELEASE_IDS.get(sid)
    if rid:
        return rid
    for prefix, mapped in SERIES_RELEASE_PREFIXES.items():
        if sid.startswith(prefix):
            return mapped
    return None


def _check_release_name(series_id: str, release_id: int) -> None:
    """Warn (never fail) when a mapped release id no longer matches its name."""
    names = _live_release_names()
    if not names:
        return
    live = names.get(release_id)
    expected = RELEASE_NAMES.get(release_id)
    if live is None:
        logger.warning(f"FRED release id {release_id} for {series_id} is no longer listed")
    elif expected and live != expected:
        logger.warning(f"FRED release {release_id} renamed: {expected!r} -> {live!r}")


def fetch_release_schedule(
    release_id: Optional[int],
    today: Optional[date] = None,
) -> tuple:
    """Return (next release date, FRED calendar url) for one release.

    Scheduled-but-not-yet-published dates only exist with
    include_release_dates_with_no_data=true: without the flag the calendar stops
    at the last release that already has data (rid=209 ended 2026-10-29 with the
    flag off vs 2026-12-31 with it on — verified 2026-10-31). (None, None) when
    the release is unmapped or the calendar is unreachable.
    """
    if not release_id:
        return None, None
    today = today or date.today()
    candidates: List[date] = []
    offset = 0
    while True:
        data = _fred_get(FRED_RELEASE_DATES_URL, {
            "release_id": release_id,
            "include_release_dates_with_no_data": "true",
            "limit": 1000,
            "offset": offset,
        })
        if not data:
            break
        rows = data.get("release_dates", [])
        for row in rows:
            ds = row.get("date")
            if not ds:
                continue
            try:
                d = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= today:
                candidates.append(d)
        offset += len(rows)
        # Full calendars are < 1000 rows; the guard only trips on a huge release.
        if len(rows) < 1000 or offset >= 20000:
            break
    if not candidates:
        return None, None
    nxt = min(candidates)
    return nxt, f"{FRED_CALENDAR_URL}?rid={release_id}&y={nxt.year}"


def scrape_fred_metadata(indicator_code: str) -> bool:
    """Fetch series details + next release date and UPSERT one metadata row.

    Best-effort: returns False (never raises) so a metadata hiccup cannot undo
    a successful observations scrape. Latest observation date/value are read
    back from the economic_indicators table (authoritative, post-upsert).
    """
    indicator_code = indicator_code.upper()
    config = FRED_SERIES_CONFIG.get(indicator_code)
    if not config:
        return False
    series_id = config["series_id"]

    data = _fred_get(FRED_SERIES_URL, {"series_id": series_id})
    if not data or not data.get("seriess"):
        logger.warning(f"No FRED series metadata for {series_id}")
        return False
    s = data["seriess"][0]
    # /fred/series carries no release_id, so fall back to the verified map;
    # without it next_release can never be resolved (that was the None bug).
    rid = s.get("release_id") or release_id_for(series_id)
    if rid:
        _check_release_name(series_id, rid)
    else:
        logger.warning(f"{series_id}: no FRED release mapping — next release date unavailable")
    next_release, release_calendar_url = fetch_release_schedule(rid)

    db = SessionLocal()
    try:
        latest = (
            db.query(EconomicIndicator.report_date, EconomicIndicator.value)
            .filter(EconomicIndicator.indicator_code == indicator_code)
            .order_by(EconomicIndicator.report_date.desc())
            .first()
        )
        payload = {
            "series_id": series_id,
            "title": s.get("title"),
            "units": s.get("units"),
            "frequency": s.get("frequency"),
            "seasonal_adjustment": s.get("seasonal_adjustment"),
            "source_agency": s.get("source"),
            "fred_url": f"https://fred.stlouisfed.org/series/{series_id}",
            "latest_observation_date": latest.report_date if latest else None,
            "latest_observation_value": float(latest.value) if latest and latest.value is not None else None,
            "last_updated_time": format_fred_updated(s.get("last_updated")),
            "next_release_date": next_release,
            "release_id": rid,
            "release_calendar_url": release_calendar_url,
            # The API field is "notes"; the previous s.get("note") read a key that
            # does not exist, so notes were never persisted.
            "notes": s.get("notes"),
        }
        row = db.query(FredSeriesMetadata).filter(
            FredSeriesMetadata.indicator_code == indicator_code
        ).first()
        if row:
            for k, v in payload.items():
                setattr(row, k, v)
        else:
            db.add(FredSeriesMetadata(indicator_code=indicator_code, **payload))
        db.commit()
        logger.info(f"\u2139\ufe0f {indicator_code}: fred_series_metadata upserted "
                    f"(next_release={next_release}, rid={rid}, "
                    f"calendar={release_calendar_url}, updated={payload['last_updated_time']})")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"DB error saving FRED metadata for {indicator_code}: {e}", exc_info=True)
        return False
    finally:
        db.close()


def _job_result(code: str, ok: bool, rows: int = 0, error: str = "") -> dict:
    """Return shape understood by job_tracking.with_result_handling:
    'rows' feeds job_runs.rows_written; ok=False + stage_failures turns the
    System Map entry red with the reason. Replaces the old bool return, which
    made _infer_rows() give up (bools are explicitly skipped) so the Rows
    column showed an em dash even when 75 records landed.
    """
    out: Dict[str, Any] = {"ok": bool(ok), "rows": int(rows)}
    if not ok:
        out["stage_failures"] = [{"stage": f"fred:{code}",
                                  "error": (error or "failed")[:400]}]
    return out


def scrape_fred_indicator(indicator_code: str) -> dict:
    """
    Fetch and store historical data from the official FRED API for one indicator code.
    Uses bulk insert with existing-date check to avoid duplicating rows.
    Returns {"ok": bool, "rows": n} (plus stage_failures on failure).
    """
    indicator_code = indicator_code.upper()
    if indicator_code not in FRED_SERIES_CONFIG:
        logger.error(f"Unknown indicator code: {indicator_code}")
        return _job_result(indicator_code, False, 0,
                           f"unknown indicator code {indicator_code}")

    config = FRED_SERIES_CONFIG[indicator_code]
    series_id = config["series_id"]
    extra_params = config.get("params")

    db = SessionLocal()
    try:
        # Check latest date in DB to fetch only incremental new records
        latest_date = db.query(func.max(EconomicIndicator.report_date)).filter(
            EconomicIndicator.indicator_code == indicator_code
        ).scalar()

        # ── Determine Lookback Window for Incremental Fetch ──
        # Frequency-aware: daily series re-cover a few days, weekly a few weeks,
        # revised monthly series re-cover a quarter. Everything from there to
        # today is returned by FRED, so gaps self-heal on the next run.
        lookback_days = lookback_days_for(indicator_code)

        observation_start = None
        if latest_date:
            safe_start = latest_date - timedelta(days=lookback_days)
            observation_start = safe_start.strftime("%Y-%m-%d")
            logger.info(f"   📅 Latest in DB: {latest_date}. Fetching from "
                        f"{observation_start} ({lookback_days}d revision buffer)")
        else:
            logger.info(f"   📅 First run for {indicator_code} — fetching full historical series...")

        parsed_data = fetch_fred_observations(
            series_id,
            extra_params=extra_params,
            observation_start=observation_start
        )
        logger.info(f"Parsed {len(parsed_data)} valid records for {indicator_code}")

        if not parsed_data:
            logger.info(f"ℹ️ {indicator_code}: already up-to-date (0 new records)")
            scrape_fred_metadata(indicator_code)  # refresh metadata even with no new rows
            return _job_result(indicator_code, True, 0)

        # ── Atomic UPSERT ──
        # We target the (report_date, indicator_code) unique index by its
        # columns, not by name. The SQLAlchemy model declares the constraint
        # as `uq_economic_indicator_date_code`, but the live Render schema
        # has it under a different identifier (created by an earlier alembic
        # autogenerate pass), and referencing the name fails with
        # "constraint ... does not exist". Postgres resolves the correct
        # index from the column list, so this works regardless of the
        # constraint's internal name.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        records_to_insert = [
            {
                "report_date": item["date"],
                "indicator_code": indicator_code,
                "value": item["value"],
            }
            for item in parsed_data
        ]

        stmt = pg_insert(EconomicIndicator).values(records_to_insert)
        stmt = stmt.on_conflict_do_update(
            index_elements=["report_date", "indicator_code"],
            set_={"value": stmt.excluded.value}
        )

        db.execute(stmt)
        db.commit()
        logger.info(f"✅ {indicator_code}: processed {len(records_to_insert)} records via atomic UPSERT (new & revised values updated).")
        scrape_fred_metadata(indicator_code)  # keep fred_series_metadata in sync
        return _job_result(indicator_code, True, len(records_to_insert))

    except Exception as e:
        db.rollback()
        logger.error(f"DB error saving {indicator_code}: {e}", exc_info=True)
        return _job_result(indicator_code, False, 0, str(e))
    finally:
        db.close()


def scrape_all_fred() -> Dict[str, Any]:
    """Run all configured FRED indicators; returns an aggregate in the same
    {ok, rows, ...} shape, with per-indicator detail under 'per_indicator'."""
    logger.info(f"Starting FRED API scrape for all {len(FRED_SERIES_CONFIG)} indicators...")
    results: Dict[str, Any] = {}
    total = 0
    all_ok = True
    for code in FRED_SERIES_CONFIG:
        res = scrape_fred_indicator(code)
        results[code] = res
        total += res.get("rows", 0)
        all_ok = all_ok and bool(res.get("ok"))
        time.sleep(0.5)  # Respect FRED API rate limit (120 requests/minute)
    return {"ok": all_ok, "rows": total, "per_indicator": results}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Optional argv: one or more indicator codes to run in isolation.
    # Example:  python -m app.scrapers.fred_scraper SAUCPALTT01GPM
    # No args → run everything (existing behaviour).
    codes = [c.upper() for c in sys.argv[1:]]
    if codes:
        unknown = [c for c in codes if c not in FRED_SERIES_CONFIG]
        if unknown:
            print(f"Unknown codes: {unknown}")
            print(f"Known: {sorted(FRED_SERIES_CONFIG)}")
            sys.exit(2)
        results = {c: scrape_fred_indicator(c) for c in codes}
    else:
        results = scrape_all_fred()["per_indicator"]
    print("\nSummary of FRED scraping:")
    for code, res in results.items():
        print(f"  {'✅' if res.get('ok') else '❌'} {code}: {res.get('rows', 0)} rows")
    sys.exit(0 if all(r.get("ok") for r in results.values()) else 1)
