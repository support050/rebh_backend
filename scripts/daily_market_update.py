import sys
import os

# Ensure Windows terminal standard streams handle Unicode characters safely
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import gc
import time
from pathlib import Path
import csv
import traceback
import json
import logging
import datetime
import pandas as pd
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy import create_engine, text, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

# Add parent directory to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

# ── Logging must be configured HERE, before any sub-module import ──────────
# Several imported modules call logging.basicConfig at their top (daily_detailed_
# scraper=INFO, calculate_rs_final_precise=WARNING, calculate_rs_line_metrics=INFO).
# basicConfig is a NO-OP once the root has handlers, so whichever runs FIRST owns
# the root — and the sub-module imports below would otherwise beat this script's
# own config. Configuring the root here (at CLI process start) makes THIS pipeline
# entrypoint own the timestamped format + level. The `if not root.handlers` guard
# keeps it a no-op when imported by the live server (uvicorn / admin trigger), so
# we never clobber the server's logging config — no handler conflict. Level INFO:
# the print→logger bridge routes routine detail to DEBUG (suppressed here → a
# clean log) and meaningful lines to INFO/WARNING/ERROR.   #L1 #E1
_root = logging.getLogger()
if not _root.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app.core.config import settings
from app.core.database import SessionLocal 
from app.models.price import Price
# Import the services used by this run
from app.services.daily_detailed_scraper import scrape_daily_details
# Use the final RS Calculator
from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
# Import the Reports scraper
from scrapers.Reports import main as run_reports_scraper
# Import the Market Pulse updater
from scripts.backfill_market_pulse import main as run_market_pulse_backfill
# Import the Daily Financial Indicators scraper
from app.scrapers.daily_financial_indicators_scraper import run_scraper_and_save_to_db as run_financial_indicators_scraper
# [C1] Per-cycle company cache control (top-level import so the outer finally can
# always call disable_company_cache(), even on the early lock-held return).
from app.services.xbrl_data_service import enable_company_cache, disable_company_cache

# Logger for this module. The root is already configured ABOVE, before the
# sub-module imports, so this entrypoint owns the format + level.   #L1
logger = logging.getLogger(__name__)

# Project root and file paths
project_root = Path(__file__).resolve().parent.parent

def _clean_cell(value):
    """pandas reads an empty cell as NaN (a float), not None — passing it to a
    text column would literally store "NaN" in the database. Return None for all
    of them."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text_value = str(value).strip()
    if not text_value or text_value.lower() in ("nan", "none", "null"):
        return None
    return text_value


# Hierarchy is read DB-first: the newest classified prices row per symbol wins,
# new.csv only fills symbols the DB has never seen (brand-new listings). 400
# calendar days is wide enough to cover every listed name (a symbol that traded
# in the last ~19 months has a row) while staying on idx_prices_symbol_date — a
# full-history DISTINCT ON was measured at 27.5s vs 3.0s for this window, same
# 274 symbols. Override via env if the market ever needs a deeper lookback.
HIERARCHY_LOOKBACK_DAYS = int(os.getenv("HIERARCHY_LOOKBACK_DAYS", "400"))


HIERARCHY_SQL = text("""
    SELECT DISTINCT ON (symbol) symbol, industry_group, sector,
                                  industry, sub_industry
    FROM prices
    WHERE date >= (SELECT MAX(date) FROM prices) - make_interval(days => :lookback)
      AND industry_group IS NOT NULL
      AND btrim(industry_group) <> ''
    ORDER BY symbol, date DESC
""")


def _load_hierarchy_from_db(db):
    """Latest classified row per symbol straight from prices (source of truth).

    Returns {symbol: {industry_group, sector, industry, sub_industry}}. Only rows
    that actually carry an industry_group are considered, so a one-off NULL
    cannot shadow a symbol's real, established classification.

    This is the first DB read after the session has idled through the report and
    financial-indicator scrapers, so it is where a WAN idle-drop surfaces first.
    The failure MUST be rolled back: an un-rolled-back dead connection poisons
    the shared session and every later query dies with PendingRollbackError (this
    silently broke market-date detection and the prev_close map in the 2026-10-07
    run). rollback() discards the dead connection so the pool (pool_pre_ping) can
    hand out a fresh one, then we retry once before degrading to the CSV."""
    for attempt in (1, 2):
        try:
            rows = db.execute(HIERARCHY_SQL, {"lookback": HIERARCHY_LOOKBACK_DAYS}).fetchall()
            mapping = {
                str(r[0]): {
                    "industry_group": _clean_cell(r[1]),
                    "sector": _clean_cell(r[2]),
                    "industry": _clean_cell(r[3]),
                    "sub_industry": _clean_cell(r[4]),
                }
                for r in rows
            }
            logger.info(f"🗂️ Hierarchy DB-first: {len(mapping)} symbols resolved from "
                        f"prices (last {HIERARCHY_LOOKBACK_DAYS} days).")
            return mapping
        except Exception as e:
            _rollback_session(db)
            if attempt == 2:
                logger.warning(f"⚠️ Could not read hierarchy from prices ({e}); "
                               f"falling back to new.csv only.")
                return {}
            logger.warning(f"⚠️ Hierarchy read hit a dead connection "
                           f"({type(e).__name__}) — rolled back and retrying once.")
    return {}


def _load_hierarchy_from_csv():
    """new.csv as the secondary fallback (brand-new listings absent from the DB)."""
    mapping = {}
    csv_path = project_root / "new.csv"
    try:
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            # to_dict("records") instead of iterrows(): same values and keys,
            # without building a Series per row (iterrows slows down as the row
            # gets wider).
            for row in df.to_dict("records"):
                symbol = str(row['Symbol'])
                mapping[symbol] = {
                    "industry_group": _clean_cell(row.get('Industry Group')),
                    "sector": _clean_cell(row.get('Sector')),
                    "industry": _clean_cell(row.get('Industry')),
                    "sub_industry": _clean_cell(row.get('Sub-Industry'))
                }
        else:
            logger.warning("⚠️ new.csv not found at project root.")
    except Exception as e:
        logger.error(f"❌ Error loading hierarchy mapping from new.csv: {e}")
    return mapping


def load_full_hierarchy_mapping(db=None):
    """Symbol -> hierarchy, with the DATABASE as the source of truth.

    Any manual correction made in prices is authoritative and survives a daily
    run; new.csv is consulted only to classify symbols the DB has never recorded.
    A CSV value never overwrites a DB value — it can only fill a gap."""
    db_map = _load_hierarchy_from_db(db) if db is not None else {}
    csv_map = _load_hierarchy_from_csv()

    # CSV fills only symbols the DB doesn't have (brand-new listings).
    from_csv = 0
    for symbol, hier in csv_map.items():
        if symbol not in db_map:
            db_map[symbol] = hier
            from_csv += 1

    still_missing = sum(1 for v in db_map.values() if not v.get("industry_group"))
    logger.info(f"Loaded {len(db_map)} symbols with full hierarchy "
                f"(DB-first: {len(db_map) - from_csv}, new.csv fallback: {from_csv}).")
    if still_missing:
        logger.warning(f"⚠️ {still_missing} symbol(s) have no Industry Group in either "
                       f"source — they will be excluded from Group RS.")
    return db_map

def run_historical_reports_scraper():
    """Run the historical reports aggregation script for the Saudi market (TASI)"""
    try:
        logger.info("🏭 Starting Historical Reports Scraper...")
        run_reports_scraper()
        logger.info("✅ Historical Reports Scraper completed successfully!")
        return True
    except Exception as e:
        # The error used to be swallowed here while the run was still reported as
        # "successful" — it is now recorded in the run report, printed at the end
        # of the log and surfaced in the admin panel.
        _record_stage_failure("01 historical reports scraper", e)
        return False

def update_market_pulse():
    """Run backfill_market_pulse to compute Market Pulse signals for new days only"""
    try:
        logger.info("📈 Starting Market Pulse Daily Update...")
        run_market_pulse_backfill()
        logger.info("✅ Market Pulse Daily Update completed successfully!")
        return True
    except Exception as e:
        _record_stage_failure("08 market pulse update", e)
        return False

def update_financial_indicators():
    """Run the Daily Financial Indicators scraper to refresh market weights and P/E ratios"""
    try:
        logger.info("💰 Starting Daily Financial Indicators Scraper...")
        result = run_financial_indicators_scraper() or {}
        if not result.get("ok"):
            # The scraper traps its own errors, so a failed run comes back as a
            # dict — turn it into a stage failure instead of a green stage.
            failures = result.get("stage_failures") or []
            msg = ("; ".join(f"[{f.get('stage')}] {f.get('error')}" for f in failures)
                   or "run_scraper_and_save_to_db() returned ok=False")
            _record_stage_failure("02 daily financial indicators", msg, critical=True)
            return False
        if result.get("partial"):
            _record_stage_failure(
                "02 daily financial indicators",
                f"partial page: only {result.get('companies')} company rows",
                advisory=True)
        logger.info(f"✅ Daily Financial Indicators completed — "
                    f"{result.get('rows', 0)} rows written!")
        return True
    except Exception as e:
        _record_stage_failure("02 daily financial indicators", e)
        return False



PRICE_UPSERT_CHUNK = 100   # rows per multi-VALUES upsert

# Columns that get overwritten on (symbol, date) conflict — same list the
# per-row upsert used, so ON CONFLICT behaviour is unchanged.
_PRICE_UPDATE_COLS = (
    "open", "high", "low", "close", "change", "change_percent",
    "volume_traded", "value_traded_sar", "no_of_trades", "company_name",
    "industry_group", "sector", "industry", "sub_industry", "market_cap",
)

# Hierarchy is owned by the DB (see load_full_hierarchy_mapping): these columns
# are written back with a COALESCE(NULLIF(...)) guard so a scrape that arrives
# without a classification can never wipe an existing / manually corrected one.
_HIERARCHY_COLS = ("industry_group", "sector", "industry", "sub_industry")


def _upsert_price_rows(db, rows, guard_market_cap=True) -> int:
    """
    Write price rows as chunked multi-VALUES upserts and return the number of
    rows actually written. A chunk that fails is retried row by row, so one
    bad symbol can never discard the whole day (the old per-row loop gave us
    that isolation for free — we keep it).

    guard_market_cap=True renders the market_cap update as
        COALESCE(NULLIF(incoming, 0), prices.market_cap)
    which means a NULL/0 coming from a broken scrape can NEVER overwrite a
    good stored value. Real values still overwrite normally. This is the fix
    for the 2026-10-04 incident (272 NULLs silently replaced a full day).
    """
    if not rows:
        return 0

    def _build_set(stmt):
        set_ = {c: getattr(stmt.excluded, c) for c in _PRICE_UPDATE_COLS
                if c not in ("market_cap",) + _HIERARCHY_COLS}
        # Hierarchy columns are DB-owned: an empty/NULL value from the scrape must
        # never erase a stored or manually-corrected classification. COALESCE keeps
        # the existing row when the incoming value is NULL or '' (NULLIF turns ''
        # into NULL), and still lets a real new classification overwrite normally.
        for col in _HIERARCHY_COLS:
            set_[col] = func.coalesce(
                func.nullif(getattr(stmt.excluded, col), ''), getattr(Price, col)
            )
        if guard_market_cap:
            set_["market_cap"] = func.coalesce(
                func.nullif(stmt.excluded.market_cap, 0), Price.market_cap
            )
        else:
            set_["market_cap"] = stmt.excluded.market_cap
        return set_

    written = 0
    for i in range(0, len(rows), PRICE_UPSERT_CHUNK):
        chunk = rows[i:i + PRICE_UPSERT_CHUNK]
        try:
            stmt = insert(Price).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=['symbol', 'date'], set_=_build_set(stmt)
            )
            db.execute(stmt)
            written += len(chunk)
        except Exception as chunk_err:
            logger.warning(
                "⚠️ Chunked price upsert failed (%s) — retrying %d rows one by one"
                % (type(chunk_err).__name__, len(chunk))
            )
            _rollback_session(db)
            for price_data in chunk:
                try:
                    stmt = insert(Price).values(price_data)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['symbol', 'date'], set_=_build_set(stmt)
                    )
                    db.execute(stmt)
                    written += 1
                except Exception as row_error:
                    logger.warning(f"⚠️ Skipped row {price_data.get('symbol')}: {row_error}")
                    _rollback_session(db)
    return written


# ═══════════════════════════════════════════════════════════════
#  Run report: failed stages + post-scrape quality gate
#  No stage is allowed to fail silently any more — every exception is
#  recorded here and printed as a banner at the end of the run, and the
#  admin status endpoint reads it back.
# ═══════════════════════════════════════════════════════════════
MARKET_CAP_GUARD_RATIO = float(os.getenv("MARKET_CAP_GUARD_RATIO", "0.20"))
SCRAPER_MIN_ROWS_RATIO = float(os.getenv("SCRAPER_MIN_ROWS_RATIO", "0.80"))

# When a CRITICAL stage (prices / rs_daily_v2 / stock indicators) failed, the
# day is NOT complete — so latest_ready_date must NOT advance over it. Set
# PIPELINE_STRICT_FINALIZE=0 to restore the old "advance anyway" behaviour.
PIPELINE_STRICT_FINALIZE = os.getenv("PIPELINE_STRICT_FINALIZE", "1") == "1"

RUN_REPORT: dict = {"market_date": None, "stage_failures": [], "quality": {},
                    "ready_date_advanced": None, "critical_stages": [],
                    "stage_advisories": [], "stage_timings": [],
                    # None until step 10 runs; False means the data is written but
                    # the API may still serve the cached payload (see cache_bust).
                    "cache_busted": None, "cache_summary": None}


def reset_run_report(market_date=None):
    RUN_REPORT["market_date"] = str(market_date) if market_date else None
    RUN_REPORT["stage_failures"] = []
    RUN_REPORT["stage_advisories"] = []
    RUN_REPORT["stage_timings"] = []
    RUN_REPORT["quality"] = {}
    RUN_REPORT["ready_date_advanced"] = None
    RUN_REPORT["critical_stages"] = []
    RUN_REPORT["cache_busted"] = None
    RUN_REPORT["cache_summary"] = None


def _record_stage_failure(stage, exc, critical=False, advisory=False):
    """Log a stage failure WITH traceback and add it to the run report.

    `exc` may be an exception instance (traceback is captured) or a plain
    string describing a non-exception problem (e.g. "0 rows were written").

    `advisory=True` is used for a justified condition that must not fail the day —
    a symbol listed yesterday cannot have an SMA200 or a weekly indicator yet. It
    is recorded as 📋 ADVISORY at WARN level in stage_advisories, and the run
    status stays ok.
    """
    tb = traceback.format_exc()
    if isinstance(exc, str):
        error_text, tb_last = exc, ""
    else:
        error_text = f"{type(exc).__name__}: {exc}"
        tb_last = tb.strip().splitlines()[-1][:200] if tb.strip() else ""

    if advisory:
        logger.warning("📋 ADVISORY [%s] %s", stage, error_text)
        RUN_REPORT.setdefault("stage_advisories", []).append({
            "stage": stage,
            "error": error_text,
        })
        return

    if isinstance(exc, str):
        logger.error("❌ STAGE FAILED [%s]%s %s", stage,
                     " (CRITICAL)" if critical else "", error_text)
    else:
        logger.error("❌ STAGE FAILED [%s]%s %s",
                     stage, " (CRITICAL)" if critical else "", error_text)
        logger.error(tb)
    RUN_REPORT["stage_failures"].append({
        "stage": stage,
        "critical": critical,
        "error": error_text,
        "traceback_last_line": tb_last,
    })


def _stamp_run_date(market_date):
    """Attach the market date to the run report WITHOUT erasing failures that
    the pre-scrape stages (0.1–0.3) already recorded."""
    RUN_REPORT["market_date"] = str(market_date) if market_date else None


def _record_stage_timing(stage, seconds, status="ok"):
    """Append a per-stage duration to the run report (display only — never
    affects ok/critical). Recorded for every _guard-wrapped stage, success or
    failure, because the timing lives in _guard's finally block."""
    RUN_REPORT.setdefault("stage_timings", []).append({
        "stage": stage,
        "seconds": round(float(seconds), 3),
        "status": status,
    })


def get_run_report():
    failures = list(RUN_REPORT.get("stage_failures", []))
    advisories = list(RUN_REPORT.get("stage_advisories", []))
    critical = [f["stage"] for f in failures if f.get("critical")]
    return {
        "market_date": RUN_REPORT.get("market_date"),
        "stage_failures": failures,
        "stage_advisories": advisories,
        "stage_timings": list(RUN_REPORT.get("stage_timings", [])),
        "quality": dict(RUN_REPORT.get("quality", {})),
        "ready_date_advanced": RUN_REPORT.get("ready_date_advanced"),
        "critical_stages": critical,
        "cache_busted": RUN_REPORT.get("cache_busted"),
        "cache_summary": RUN_REPORT.get("cache_summary"),
        "ok": not failures,
    }


def _to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def assess_price_quality(db, rows, market_date) -> dict:
    """
    Post-scrape sanity check. Reads the batch it is about to write and compares
    the row count against the previous trading day. Writes nothing and changes
    no value — it raises alarms (and feeds the run report) so a silent scraper
    failure cannot pass as a successful run again.
    """
    n = len(rows)
    q = {"rows": n}
    if n == 0:
        q["error"] = "scrape returned 0 price rows"
        RUN_REPORT["quality"] = q
        logger.error("🛑 QUALITY GATE: scraped 0 price rows for %s", market_date)
        return q

    mcap_bad = sum(1 for r in rows if not r.get("market_cap"))
    mcap_ratio = mcap_bad / n
    inverted_hl = 0
    close_out_of_range = 0
    missing_ohlc = 0
    zero_volume = 0
    for r in rows:
        o, h, l, c = (_to_float(r.get(k)) for k in ("open", "high", "low", "close"))
        if None in (h, l, c):
            missing_ohlc += 1
        elif h < l:
            inverted_hl += 1
        elif c > h or c < l:
            # counted for reporting ONLY. Nothing is clamped, snapped or dropped:
            # the close is the series every indicator and the RS line consume, and the
            # rule for this pipeline is that prices stay exactly as the exchange
            # published them. A close outside [low, high] is a rounding /
            # consolidated-print artefact of the source, so it is surfaced in the admin
            # run report and left alone.
            close_out_of_range += 1
        vol = _to_float(r.get("volume_traded"))
        if vol is not None and vol <= 0:
            zero_volume += 1

    prev_day = db.execute(text("""
        SELECT count(*) FROM prices
        WHERE date = (SELECT MAX(date) FROM prices WHERE date < :d)
    """), {"d": market_date}).scalar() or 0
    row_ratio = (n / prev_day) if prev_day else None

    q.update({
        "market_cap_missing": mcap_bad,
        "market_cap_missing_ratio": round(mcap_ratio, 4),
        "high_below_low": inverted_hl,
        "close_out_of_range": close_out_of_range,
        "missing_ohlc": missing_ohlc,
        "zero_volume": zero_volume,
        "prev_day_rows": int(prev_day),
        "rows_vs_prev_day": round(row_ratio, 4) if row_ratio is not None else None,
        "market_cap_anomaly": mcap_ratio > MARKET_CAP_GUARD_RATIO,
        "volume_anomaly": False,
        "row_count_anomaly": row_ratio is not None and row_ratio < SCRAPER_MIN_ROWS_RATIO,
    })

    # ── alerts (ERROR level, so they are impossible to miss in the log) ──
    if q["market_cap_anomaly"]:
        logger.error(
            "🛑 QUALITY GATE — market_cap: %d/%d rows (%.1f%%) came NULL/0 from the "
            "scraper (threshold %.0f%%). Existing DB values are protected by the "
            "COALESCE write guard, but the source page is broken — re-check "
            "daily_detailed_scraper.",
            mcap_bad, n, mcap_ratio * 100, MARKET_CAP_GUARD_RATIO * 100
        )
    elif mcap_bad:
        logger.warning("⚠️ QUALITY — market_cap missing for %d/%d rows." % (mcap_bad, n))
    if q["row_count_anomaly"]:
        logger.error(
            "🛑 QUALITY GATE — row count: scraped %d rows vs %d on the previous "
            "trading day (%.0f%%). The scraper probably got a partial / blocked page.",
            n, prev_day, row_ratio * 100
        )
    if inverted_hl:
        logger.error("🛑 QUALITY GATE — %d rows have high < low (corrupt OHLC)." % inverted_hl)
    if missing_ohlc:
        logger.warning("⚠️ QUALITY — %d rows missing open/high/low/close values." % missing_ohlc)
    if close_out_of_range:
        # ℹ️ informational: never critical=True and never a plain _record_stage_failure —
        # the point is to be seen without ever intervening in the numbers.
        logger.info(
            "ℹ️ QUALITY (informational) — %d/%d rows have a close outside [low, high]. "
            "Values were NOT modified anywhere, in the DB or in memory; flagged for review only.",
            close_out_of_range, n
        )
        # Same message through the advisory channel, which is the part of the run report
        # the admin panel actually renders. advisory=True keeps `ok` untouched, so this
        # can never fail a run — it is a note, not a gate.
        _record_stage_failure(
            "price quality (OHLC range)",
            f"{close_out_of_range}/{n} row(s) have a close outside [low, high] — "
            f"for information only, no value was modified",
            advisory=True,
        )

    RUN_REPORT["quality"] = q
    return q


def _rollback_session(db):
    try:
        db.rollback()
    except Exception:
        pass


# ── Session-level tuning (P1-2) ───────────────────────────
# work_mem is PER sort/hash node, per connection. The production instance is
# small (shared_buffers=64MB, effective_cache_size=192MB), so 64MB is applied
# ONLY to this pipeline session, and it is RESET in the finally block —
# otherwise a pooled connection would leak the setting into web requests
# running in the same worker (the admin trigger runs in-process).
# Lower it with env PIPELINE_WORK_MEM (e.g. "8MB") if the instance ever OOMs.
# jit=off: measured on production (full-table sort of prices, 400k rows) —
# work_mem=64MB with jit=on took 63.3s, the same query with jit=off took 47.7s.
# LLVM compilation costs more than it saves on this small instance once sorts
# run entirely in RAM. Override with env PIPELINE_JIT=on.
# NOTE: `min_temp_file_write_size` / `temp_file_limit` are NOT usable here —
# temp_file_limit is superuser-only on this server and the first name does not
# exist in PostgreSQL 18 (verified against pg_settings).
PIPELINE_WORK_MEM = os.getenv("PIPELINE_WORK_MEM", "64MB")
PIPELINE_JIT = os.getenv("PIPELINE_JIT", "off")
_TUNING_ORIGINALS: dict = {}


def apply_session_tuning(db):
    """Raise work_mem for this session only; remember the originals."""
    for name, value in (("work_mem", PIPELINE_WORK_MEM),
                        ("jit", PIPELINE_JIT)):
        try:
            _TUNING_ORIGINALS[name] = db.execute(text("SHOW " + name)).scalar()
            db.execute(text("SELECT set_config(:k, :v, false)"),
                       {"k": name, "v": value})
        except Exception as e:
            _rollback_session(db)
            logger.warning("⚠️ Could not set %s=%s (%s) — continuing with server default."
                           % (name, value, e))
    try:
        db.execute(text("SELECT set_config('application_name', 'daily_market_update', false)"))
    except Exception:
        pass
    logger.info("🛠  Session tuning: work_mem=%s (was %s), jit=%s (was %s)",
                PIPELINE_WORK_MEM, _TUNING_ORIGINALS.get("work_mem"),
                PIPELINE_JIT, _TUNING_ORIGINALS.get("jit"))


def reset_session_tuning(db):
    """Put every changed GUC back before the connection returns to the pool.

    The value restored is the server's own boot_val (read live), NOT the value
    we remembered at start: if an earlier run ever died before resetting, the
    remembered value would itself be the leaked one and the pool would stay
    poisoned forever.
    """
    for name in list(_TUNING_ORIGINALS):
        try:
            db.execute(text("SELECT set_config(:k, "
                            "(SELECT boot_val FROM pg_settings WHERE name = :k2), false)"),
                       {"k": name, "k2": name})
        except Exception:
            _rollback_session(db)
    _TUNING_ORIGINALS.clear()


@contextmanager
def _guard(stage: str, critical: bool = False, save_db=None):
    """
    Wrap one pipeline stage so a failure can be neither silent nor fatal to the
    rest of the run:
      • logged at ERROR level with the full traceback,
      • recorded in RUN_REPORT (printed as a banner at the end of the run and
        read back by GET /api/admin/pipeline/daily-market-update/status),
      • the session is rolled back when save_db is passed, so a poisoned
        transaction cannot contaminate the following stages or the finalize.

    The wrapped block keeps its own code untouched — this guards execution and
    reporting only, never the calculation logic.

    [L2] The stage duration is measured with time.perf_counter() and recorded in
    the finally block, so it is captured whether the stage succeeds or fails.
    """
    _t0 = time.perf_counter()
    _status = "ok"
    try:
        yield
    except Exception as exc:
        _status = "failed"
        _record_stage_failure(stage, exc, critical=critical)
        if save_db is not None:
            _rollback_session(save_db)
    finally:
        _record_stage_timing(stage, time.perf_counter() - _t0, _status)


# ── Pipeline mutex (update_status row id=1) ───────────────────────────
# A run killed mid-way (Ctrl+C, laptop sleep, server restart) leaves
# is_updating = TRUE forever and would block every later manual run.
# An unfinished lock older than LOCK_STALE_MINUTES is treated as stale and
# taken over. Override with env PIPELINE_LOCK_STALE_MINUTES.
LOCK_STALE_MINUTES = int(os.getenv("PIPELINE_LOCK_STALE_MINUTES", "120"))


def _acquire_update_lock(db) -> bool:
    """
    Stage-0 lock acquisition with stale-lock takeover.

    Returns False only when a *live* run (started less than LOCK_STALE_MINUTES
    ago) still holds the lock — i.e. a second concurrent run is refused.
    """
    now = datetime.datetime.utcnow()
    row = db.execute(text(
        "SELECT is_updating, started_at FROM update_status WHERE id = 1"
    )).fetchone()

    if row and row[0]:
        started = row[1]
        if started is not None and getattr(started, "tzinfo", None) is not None:
            started = started.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        age_min = (now - started).total_seconds() / 60.0 if started else None
        if age_min is not None and age_min < LOCK_STALE_MINUTES:
            logger.error(
                "🔒 Pipeline already running (lock age %.0f min) — refusing to "
                "start a second run." % age_min
            )
            return False
        logger.warning(
            "⚠️ Stale lock found (age=%s, threshold=%d min) — taking over."
            % ("unknown" if age_min is None else "%.0f min" % age_min, LOCK_STALE_MINUTES)
        )

    db.execute(text("""
        UPDATE update_status
        SET is_updating = TRUE,
            started_at = :now
        WHERE id = 1
    """), {"now": now})
    db.commit()
    return True


def get_pipeline_status():
    """Read the mutex row — used by the admin status endpoint."""
    s = SessionLocal()
    try:
        row = s.execute(text("""
            SELECT latest_ready_date, is_updating, started_at, completed_at
            FROM update_status WHERE id = 1
        """)).fetchone()
        # Durable cross-process alert: days whose prices are in the DB but are
        # NOT published because a critical stage failed (strict finalize held
        # latest_ready_date back). Visible even for runs started from the CLI.
        unpublished = None
        if row and row[0]:
            unpublished = s.execute(text("""
                SELECT count(*) FROM (
                    SELECT DISTINCT date FROM prices
                    WHERE date > (SELECT latest_ready_date FROM update_status WHERE id = 1)
                ) p
            """)).scalar()
    finally:
        s.close()
    if not row:
        return {"latest_ready_date": None, "is_updating": False,
                "started_at": None, "completed_at": None, "lock_age_minutes": None}
    started = row[2]
    age_min = None
    if started:
        if getattr(started, "tzinfo", None) is not None:
            started = started.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        age_min = round((datetime.datetime.utcnow() - started).total_seconds() / 60.0, 1)
    return {
        "latest_ready_date": str(row[0]) if row[0] else None,
        "is_updating": bool(row[1]),
        "started_at": str(row[2]) if row[2] else None,
        "completed_at": str(row[3]) if row[3] else None,
        "lock_age_minutes": age_min,
        "stale_after_minutes": LOCK_STALE_MINUTES,
        "unpublished_days": int(unpublished or 0),
    }


def _release_update_lock(market_date=None):
    """Clear is_updating using a fresh session so a poisoned transaction cannot block unlock."""
    lock_db = SessionLocal()
    try:
        if market_date is not None:
            lock_db.execute(text("""
                UPDATE update_status
                SET latest_ready_date = :market_date,
                    is_updating = FALSE,
                    completed_at = :now
                WHERE id = 1
            """), {"market_date": market_date, "now": datetime.datetime.utcnow()})
        else:
            lock_db.execute(text("UPDATE update_status SET is_updating = FALSE WHERE id = 1"))
        lock_db.commit()
    except Exception as e:
        logger.error(f"⚠️ Failed to release update lock: {e}")
        _rollback_session(lock_db)
    finally:
        lock_db.close()


def update_daily(target_date_str=None):
    """
    1. Scrape Daily Data
    2. Save to DB (with correct Industry Group)
    3. Calculate RS (Incremental)
    """
    # Open the database connection
    db = SessionLocal()
    
    try:
        logger.info(f"🚀 Starting Daily Market Update...")
        
        # 0. Set Update Status to Updating (with stale-lock takeover)
        if not _acquire_update_lock(db):
            return {"skipped": True, "reason": "pipeline lock held by a live run"}
        reset_run_report()
        apply_session_tuning(db)

        # [C1] Turn on the per-cycle company cache for THIS run only (off by
        # default → the long-lived server never serves a stale file). disabled in
        # the outer finally, which is the single guaranteed exit path.
        enable_company_cache()
        
        logger.info("=" * 80)
        # 0.1 Run Historical Reports Scraper
        run_historical_reports_scraper()
        
        
        logger.info("=" * 80)
        # 0.3 Run Daily Financial Indicators
        update_financial_indicators()

        logger.info("=" * 80)
        # 0.6 Load Mappings (DB-first: prices is the source of truth, new.csv only fills gaps)
        hierarchy_map = load_full_hierarchy_mapping(db)
        if not hierarchy_map:
            # This used to be silent: without the hierarchy map, prices are written with
            # sector/industry = NULL for every symbol and the sector pages break with
            # no warning at all.
            _record_stage_failure("03 hierarchy mapping",
                                  "empty hierarchy map (prices has no classified rows and "
                                  "new.csv is missing/unreadable) — prices would be saved "
                                  "without sector/industry for every symbol")
        
        # 1. Determine Date
        if target_date_str:
            market_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
            logger.info(f"📅 User provided custom date: {market_date}")
        else:
            # Auto-detect: use the latest date from historical_reports (the actual last
            # trading day). This is more accurate than date.today(), because the script
            # can run at dawn before the trading day has started.
            try:
                from app.models import HistoricalReport
                latest_hist = db.query(HistoricalReport.report_date)\
                    .order_by(HistoricalReport.report_date.desc())\
                    .first()
                if latest_hist:
                    market_date = latest_hist[0]
                    logger.info(f"📅 Auto-detected latest trading day from DB: {market_date}")
                else:
                    market_date = date.today()
                    logger.info(f"📅 No historical data found, using today: {market_date}")
            except Exception as e:
                market_date = date.today()
                # Roll back first: if the drop surfaced here, the shared session is
                # poisoned and everything downstream dies with PendingRollbackError.
                _rollback_session(db)
                _record_stage_failure("04 market date detection", e)
                logger.warning(f"⚠️ Could not detect market date from DB ({e}), falling back to today: {market_date}")

        logger.info("=" * 80)
        # 2. Scraping
        logger.info("📡 Scraping daily detailed report...")
        _stamp_run_date(market_date)
        scraped_data = scrape_daily_details(headless=True)
        
        if not scraped_data:
            _record_stage_failure("05 daily detailed scrape",
                                  "scraping returned 0 records — no prices written for "
                                  f"{market_date}", critical=True)
            return get_run_report()

        logger.info(f"📊 Scraped {len(scraped_data)} records.")



        # 3. Pre-load previous close for the traded symbols (one DB query)
        # ─────────────────────────────────────────────────────────
        # This allows us to calculate change & change_percent ourselves
        # instead of trusting the scraper's unreliable "Change %" value.
        #
        # [Perf] The old `SELECT DISTINCT ON (symbol) ... ORDER BY symbol, date DESC`
        # could NOT use idx_prices_symbol_date: mixed ASC/DESC ordering needs a
        # (symbol, date DESC) index, so Postgres seq-scanned and externally sorted
        # ~990K rows (24.5s measured, 25MB of temp disk). Probing the previous close
        # per symbol with a LATERAL + LIMIT 1 walks that index backwards instead
        # (4ms for 273 symbols on production data), and only the symbols in today's
        # feed are needed — prev_close_map is read exclusively inside the scraped_data
        # loop below.
        logger.info("📦 Loading previous close prices from DB...")
        prev_close_map = {}
        _feed_symbols = sorted({
            str(item.get("Symbol")).strip() for item in scraped_data if item.get("Symbol")
        })
        try:
            if _feed_symbols:
                prev_close_rows = db.execute(text("""
                    SELECT s.symbol, p.close
                    FROM (SELECT UNNEST(CAST(:syms AS text[])) AS symbol) s
                    CROSS JOIN LATERAL (
                        SELECT close
                        FROM prices
                        WHERE prices.symbol = s.symbol
                          AND prices.date < :market_date
                          AND prices.close > 0
                        ORDER BY prices.date DESC
                        LIMIT 1
                    ) p
                """), {"syms": _feed_symbols, "market_date": market_date}).fetchall()
                prev_close_map = {str(row[0]): float(row[1]) for row in prev_close_rows}
            logger.info(f"  ← Loaded prev_close for {len(prev_close_map)} symbols.")
        except Exception as e:
            _rollback_session(db)   # don't let a dropped read poison the session
            logger.warning(f"⚠️ Could not load prev_close map: {e}. Will fallback to scraper values.")

        # 4. Saving Prices
        price_rows = []
        for item in scraped_data:
            symbol = str(item.get("Symbol"))
            company = item.get("Company")
            
            if not symbol: continue

            # Get Detailed Hierarchy
            h = hierarchy_map.get(symbol, {})
            
            try:
                close_val = float(item.get("Close", 0.0) or 0)

                # Skip corrupt data: close=0 or negative is never a real price
                if close_val <= 0:
                    logger.warning(f"⚠️ Skipped {symbol}: close={close_val} (corrupt/missing data)")
                    continue

                # Calculate change & change_percent from prev_close in DB
                # (not from the scraper's "Change %" which is unreliable)
                prev_close = prev_close_map.get(symbol)
                if prev_close and prev_close > 0:
                    calc_change = round(close_val - prev_close, 4)
                    calc_change_pct = round(((close_val / prev_close) - 1.0) * 100.0, 4)
                else:
                    # Fallback for first-ever trading session (no prev_close)
                    scraper_pct = float(item.get("Change %", 0) or 0)
                    calc_change_pct = round(scraper_pct, 4)
                    if abs(100.0 + scraper_pct) > 0.001:
                        calc_change = round(close_val * scraper_pct / (100.0 + scraper_pct), 4)
                    else:
                        calc_change = 0.0

                price_data = {
                    "symbol": symbol,
                    "date": market_date,
                    "open": item.get("Open", 0.0),
                    "high": item.get("Highest", 0.0),
                    "low": item.get("Lowest", 0.0),
                    "close": close_val,
                    "change": calc_change,
                    "change_percent": calc_change_pct,
                    "volume_traded": int(item.get("Volume Traded", 0)),
                    "value_traded_sar": float(item.get("Value Traded", 0.0)),
                    "no_of_trades": int(item.get("No. of Trades", 0)),
                    "company_name": company,
                    "industry_group": h.get("industry_group"),
                    "sector": h.get("sector"),
                    "industry": h.get("industry"),
                    "sub_industry": h.get("sub_industry"),
                    "market_cap": float(item.get("Market Cap", 0.0))
                }
                
                # Accumulate — the write happens below in chunked upserts
                price_rows.append(price_data)
            except Exception as row_error:
                logger.warning(f"⚠️ Skipped row {symbol}: {row_error}")
                continue

        # 4b. Quality gate — a broken scrape must raise an ALARM, not a silent
        #     NULL that overwrites a good stored value. The check itself never fails
        #     the write.
        with _guard("06 price quality gate", save_db=db):
            assess_price_quality(db, price_rows, market_date)

        # 4c. Write in chunked multi-row upserts.
        #     Used to be one INSERT ... ON CONFLICT per symbol (272 network
        #     round-trips per run). Now: one statement per 100 rows.
        success_count = 0   # default: if the stage fails before assignment, the check below sees 0
        with _guard("07 price write", critical=True, save_db=db):
            success_count = _upsert_price_rows(db, price_rows)
            db.commit()
        if price_rows and not success_count:
            # We must not keep computing for a day that was never written
            _record_stage_failure("07 price write",
                                  f"0/{len(price_rows)} price rows written for {market_date}",
                                  critical=True)
            logger.error("🛑 PIPELINE ALERT — no prices were written for %s; stopping "
                         "before the indicator stages so the day is not half-built.",
                         market_date)
            return get_run_report()
        logger.info(f"✅ Successfully saved/updated {success_count} price records for {market_date} "
                    f"({-(-len(price_rows) // PRICE_UPSERT_CHUNK)} chunked upserts).")

        # 🧹 Free memory after saving prices
        del scraped_data, hierarchy_map, price_rows
        gc.collect()

        # 3.5 Market Pulse — runs here, right after today's prices are saved
        # (it used to sit in step 0.2 before the scraper, so it always missed the
        # current trading day)
        logger.info("=" * 80)
        with _guard("08 Market Pulse daily update", save_db=db):
            update_market_pulse()
        
        logger.info("=" * 80)
        # 4. RS Calculation (Optimized Final) — CALCULATE ONLY, DO NOT SAVE YET
        # -------------------------------------------------------------------
        logger.info(f"🧮 Starting RS Calculation for {market_date} (Daily Single-Day Mode)...")
        from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
        import pandas as pd

        # Defining the defaults first keeps the final `del` safe even if an exception fires
        calculator = None
        df_rs_today = None

        try:
            calculator = RSCalculatorUltraFast(str(settings.DATABASE_URL))
            results = calculator.calculate_daily_rs_ultrafast(market_date)
            if results and len(results) > 0:
                df_rs_today = pd.DataFrame(results)
                logger.info(f"✅ Calculated RS for {len(df_rs_today)} stocks (held in memory, not saved yet).")
            else:
                # Silence here means a missing day in rs_daily_v2 — recorded as a critical failure
                _record_stage_failure("09 RS calculation (rs_daily_v2)",
                                      f"calculator returned 0 RS rows for {market_date}",
                                      critical=True)
            del results
        except Exception as rs_err:
            _record_stage_failure("09 RS calculation (rs_daily_v2)", rs_err, critical=True)

        gc.collect()
            
        logger.info("=" * 80)
        # 5. Calculate Technical Indicators — ATOMIC: only save prices.change, keep tech data in memory
        # -------------------------------------------------------------------
        logger.info("🧮 Calculating Technical Indicators (SMAs, 52W High/Low)...")
        # Defaults before the guard: if the stage fails, stage 8 finds an empty dict
        # instead of a NameError
        tech_map = {}
        df_tech = df_tech_res = tech_calc = None
        with _guard("10 Technical indicators", critical=True, save_db=db):
            from scripts.calculate_technicals import TechnicalCalculator
            tech_calc = TechnicalCalculator(str(settings.DATABASE_URL))
            df_tech = tech_calc.load_data()
            df_tech_res = tech_calc.calculate(df_tech)
            # Save ONLY prices.change, return tech data as dict for later merging
            tech_map = tech_calc.save_change_only_and_return_tech_map(df_tech_res)
            logger.info(f"✅ Technical Indicators calculated (held in memory: {len(tech_map)} stocks).")

        # 🧹 Free memory: release raw technical DataFrames, keep only tech_map dict
        del df_tech, df_tech_res, tech_calc
        gc.collect()

        # 6. Calculate IBD Metrics (Group RS, Acc/Dis) — CALCULATE ONLY
        # -------------------------------------------------------------------
        logger.info("📊 Calculating IBD Metrics (Group RS, Acc/Dis)...")
        # Defaults before the guard (they used to be defined mid-block) — they keep the
        # 6.5 merge and the later `del` safe even if this whole stage fails
        group_rs_map = {}
        acc_dis_map = {}
        df_ibd_prices = None
        ibd_calc = None
        with _guard("11 IBD metrics (group RS, acc/dis)", save_db=db):
            from scripts.calculate_ibd_metrics import IBDMetricsCalculator
        
            ibd_calc = IBDMetricsCalculator(db)
            df_ibd_prices = ibd_calc.load_data(lookback_days=230)
        
            # (The variables are defined above the guard as safe defaults)
        
            if df_ibd_prices is not None and not df_ibd_prices.empty:
                group_rs_map = ibd_calc.calculate_group_rs(df_ibd_prices, market_date) or {}
                acc_dis_map = ibd_calc.calculate_acc_dis(df_ibd_prices, market_date) or {}
                logger.info(f"✅ Calculated IBD Metrics: {len(group_rs_map)} group RS, {len(acc_dis_map)} Acc/Dis.")
            else:
                logger.warning("⚠️ No price data found for IBD Metrics.")

        # 🧹 Free memory: release the large IBD prices DataFrame
        del df_ibd_prices, ibd_calc
        gc.collect()

        # 6.5 ATOMIC SAVE: Merge RS + IBD → Save to rs_daily_v2 in ONE shot
        # -------------------------------------------------------------------
        with _guard("12 rs_daily_v2 atomic save", critical=True, save_db=db):
            if df_rs_today is not None and len(df_rs_today) > 0 and calculator is not None:
                logger.info("💾 Merging RS + IBD data and saving atomically to rs_daily_v2...")

                # Vectorized merge — instead of iterrows + .at[] per symbol
                df_rs_today['sector_rs_rating']         = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sector_rs_rating'))
                df_rs_today['industry_group_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_group_rs_rating'))
                df_rs_today['industry_rs_rating']       = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_rs_rating'))
                df_rs_today['sub_industry_rs_rating']   = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sub_industry_rs_rating'))
                df_rs_today['acc_dis_rating']           = df_rs_today['symbol'].map(acc_dis_map)

                # Now save everything at once — the row is COMPLETE from the start
                calculator.save_bulk_results_with_ibd(df_rs_today)
                logger.info(f"✅ RS + IBD Data saved atomically for {market_date} ({len(df_rs_today)} stocks).")
            else:
                # No DataFrame available → rs_daily_v2 was not written for this day
                _record_stage_failure("12 rs_daily_v2 atomic save",
                                      f"no RS dataframe available for {market_date} — table not written",
                                      critical=True)

        # 🧹 Free memory: RS, IBD maps, and calculator no longer needed
        # (All of them are defined as defaults above, so the `del` is always safe)
        del df_rs_today, group_rs_map, acc_dis_map, calculator
        gc.collect()

        # 7. Calculate Industry Group Metrics
        # -------------------------------------------------------------------
        logger.info("🏭 Calculating Industry Group Metrics (IBD Score, Rank, YTD)...")
        ig_calc = None
        with _guard("13 Industry group metrics", save_db=db):
            from scripts.calculate_industry_groups import IndustryGroupCalculator
        
            ig_calc = IndustryGroupCalculator(db)
        
            # Step 1: Calculate group index prices
            group_indices = ig_calc.calculate_group_index_prices(market_date)
        
            if group_indices:
                # Step 2: Calculate IBD scores
                group_df = ig_calc.calculate_ibd_group_score(group_indices)
            
                if not group_df.empty:
                    # Step 3: Prepare summary with YTD and details
                    summary_ig = ig_calc.prepare_summary_data(group_df, market_date)
                
                    if not summary_ig.empty:
                        # Step 4: Save to database
                        ig_calc.save(summary_ig, market_date)
                        logger.info(f"✅ Industry Group Metrics Updated ({len(summary_ig)} groups).")
                    else:
                        _record_stage_failure("13 Industry group metrics",
                                              f"empty summary dataframe for {market_date}")
                else:
                    logger.warning("⚠️ No IBD scores calculated for Industry Groups.")
            else:
                _record_stage_failure("13 Industry group metrics",
                                      f"no group indices computed for {market_date}")

        # 🧹 Free memory: industry group objects no longer needed
        del ig_calc
        gc.collect()
        
        # 8. Calculate and Store Stock Technical Indicators — BATCHED (2 × ~140 stocks)
        # -------------------------------------------------------------------
        logger.info("📈 Calculating and Storing Stock Technical Indicators (Batched Mode)...")
        from scripts.calculate_stock_indicators import calculate_and_store_indicators

        BATCH_SIZE = 140
        all_symbols = list(tech_map.keys())
        total_processed = total_errors = total_successful = 0
        if not all_symbols:
            _record_stage_failure("14 stock indicators",
                                  "no technical map available — stage skipped for "
                                  f"{market_date}", critical=True)

        for batch_num, batch_start in enumerate(range(0, len(all_symbols), BATCH_SIZE), start=1):
            batch_symbols = all_symbols[batch_start : batch_start + BATCH_SIZE]
            batch_tech_map = {sym: tech_map[sym] for sym in batch_symbols}

            logger.info(
                f"  ⚙️  Batch {batch_num}: symbols {batch_start + 1}–"
                f"{batch_start + len(batch_symbols)} ({len(batch_symbols)} stocks)..."
            )

            # Per-batch guard: one failed batch must not cancel the other batches or the finalize
            b_processed = b_errors = b_successful = 0
            try:
                b_processed, b_errors, b_successful = calculate_and_store_indicators(
                    db, market_date, tech_map=batch_tech_map
                )
            except Exception as batch_err:
                _record_stage_failure(f"14 stock indicators batch {batch_num}",
                                      batch_err, critical=True)
                _rollback_session(db)
            total_processed  += b_processed
            total_errors     += b_errors
            total_successful += b_successful

            logger.info(
                f"  ✅ Batch {batch_num} done — "
                f"Processed: {b_processed}, Successful: {b_successful}, Errors: {b_errors}"
            )

            # 🧹 Free this batch from memory before loading the next one
            del batch_tech_map, batch_symbols
            gc.collect()

        logger.info(
            f"✅ Stock Indicators Updated — Total: "
            f"Processed: {total_processed}, Successful: {total_successful}, Errors: {total_errors}"
        )
        if all_symbols and total_processed < len(all_symbols):
            # 🧮 The correct coverage baseline is the symbols that actually have a price row
            # on this day (stage 8 processes exactly those — see cn_query around S288 in
            # calculate_stock_indicators), not every symbol in tech_map. Otherwise the guard
            # would fail every single day without exception: 294 symbols in tech_map versus
            # 273 that traded today, and the missing ones include recently listed stocks whose
            # history is under 100 bars, which are rejected by design (Not enough data).
            _MIN_BARS = 100        # same threshold as calculate_all_indicators_for_stock
            try:
                _expected = db.execute(text(
                    "SELECT count(DISTINCT symbol) FROM prices WHERE date = :d"
                ), {"d": market_date}).scalar() or 0
                _missing = db.execute(text("""
                    SELECT p.symbol,
                           (SELECT count(*) FROM prices h WHERE h.symbol = p.symbol) AS bars
                    FROM prices p
                    WHERE p.date = :d
                      AND NOT EXISTS (SELECT 1 FROM stock_indicators s
                                      WHERE s.symbol = p.symbol AND s.date = :d)
                    GROUP BY p.symbol
                """), {"d": market_date}).fetchall()
            except Exception as cov_err:
                _expected, _missing = len(all_symbols), []
                logger.warning("⚠️ Coverage audit query failed (%s) — "
                               "falling back to tech_map count." % cov_err)

            _short = sorted(s for s, _b in _missing if _b < _MIN_BARS)
            _real = sorted(s for s, _b in _missing if _b >= _MIN_BARS)
            RUN_REPORT["indicators_skipped_short_history"] = _short

            if _real:
                # A symbol with enough history but no indicator row → real gap that blocks the day
                _record_stage_failure("14 stock indicators coverage",
                                      f"only {total_processed}/{_expected} symbols "
                                      f"processed for {market_date}; missing: "
                                      f"{', '.join(_real[:10])}", critical=True)
            elif _short:
                # Newly listed symbols have no indicators yet — reported, but the day stays ok
                logger.warning("⚠️ %d symbol(s) skipped, fewer than %d daily bars: %s",
                               len(_short), _MIN_BARS, ", ".join(_short))
                _record_stage_failure("14 stock indicators",
                                      f"{len(_short)} newly listed symbol(s) skipped "
                                      f"(<{_MIN_BARS} daily bars): {', '.join(_short)}",
                                      advisory=True)
        elif total_errors:
            # Individual errors (new listings / short history) are reported but do not fail the day
            _record_stage_failure("14 stock indicators",
                                  f"{total_errors} symbol(s) failed indicator calc "
                                  f"for {market_date}")

        # 🧹 Free memory: full tech_map no longer needed
        del tech_map, all_symbols
        gc.collect()

        # 8.1 Pre-aggregate Minervini trend counts for this date (fast chart API reads)
        with _guard("15 screener_daily_trend_counts", save_db=db):
            from app.services.screener_daily_trend_service import update_market_date
            update_market_date(db, market_date)
            logger.info("✅ Screener daily trend row updated for chart API.")
        
        # 8.5 Calculate Daily Market Breadth — ATOMIC
        # -------------------------------------------------------------------
        # Guarded: previously an exception here aborted the whole rest of the
        # run (8.6 → 10), so latest_ready_date never advanced and the lock
        # stayed held. Non-critical stages must not be able to kill finalize.
        try:
            from scripts.update_daily_market_breadth import update_todays_market_breadth
            update_todays_market_breadth(db, market_date)
        except Exception as breadth_err:
            _record_stage_failure("16 daily market breadth", breadth_err)
            _rollback_session(db)

        # 8.6 Calculate RS Line Metrics (TraderLion scale)
        # -------------------------------------------------------------------
        try:
            logger.info("📈 Calculating RS Line Metrics...")
            from scripts.calculate_rs_line_metrics import calculate_and_store_rs_line_metrics
            calculate_and_store_rs_line_metrics(db, market_date)
            logger.info("✅ RS Line Metrics calculated and stored successfully.")
        except Exception as rs_err:
            _record_stage_failure("17 RS line metrics", rs_err)
            _rollback_session(db)

        # 8.7 Export RS Hub cached JSON data
        # -------------------------------------------------------------------
        # ── Sukuk & SAMA/Macro sync REMOVED from the daily pipeline (2026-10-26) ──
        # Neither is a daily-cadence source, and run_economic_sync() defaults to
        # write_db=True/history=True, so firing them on every daily run re-pulled
        # the whole SAMA bulletin + sukuk grid (and the inline call bypassed the
        # __main__ track_job wrapper, so those writes never showed in job_runs).
        # They now run on their own cadence from the Admin Schedule tab
        # (sukuk_importer weekly / macro_importer monthly, Run Now) or via the
        # scripts/Sukuk&Bonds.py & scripts/SAMA&GaStat.py __main__ directly.

        try:
            logger.info("📦 Exporting unified RS Hub data to JSON...")
            from scripts.export_rs_hub_data import export_rs_hub_data
            export_rs_hub_data(market_date)
            logger.info("✅ Unified RS Hub data exported successfully.")
        except Exception as exp_err:
            _record_stage_failure("18 RS Hub export", exp_err)

        # 8.8 Pre-calculate and Upload Valuation Models Summary to R2
        # -------------------------------------------------------------------
        # [Batch-3 S1] Wrapped in _guard so it is TIMED like every other stage
        # (perf_counter recorded in the finally) and any failure is logged +
        # rolled back uniformly. Same label as the R3 advisory so the timings
        # table and the run report line up on one name. Non-critical: a failure
        # here must not block the rest of the run.
        with _guard("19 valuation models → R2", save_db=db):
            logger.info("☁️ Pre-calculating valuation models summary & uploading to R2...")
            from app.services.xbrl_data_service import OUTPUT_DIR, _get_r2_client, R2_BUCKET_NAME, list_companies
            from app.services.rebh_engine_service import calculate_valuation_models

            companies = list_companies()

            # [C3] Load the REAL market cap from prices so valuation stops guessing
            # via te*1.5. prices.market_cap is SAR absolute → divide by 1e6 to feed
            # market_cap_m (the models work in millions).
            #
            # [Perf 2026-11-03] This used to be
            #   SELECT DISTINCT ON (symbol) … ORDER BY symbol, date DESC
            # which CANNOT walk idx_prices_symbol_date: the mixed ASC/DESC ordering
            # needs a (symbol, date DESC) index, so Postgres parallel-seq-scanned the
            # whole table and sorted it — 9,266 ms measured on production. Probing the
            # newest non-zero cap per symbol with LATERAL + LIMIT 1 reads the existing
            # index backwards instead: 139 ms, value-identical (273/273 keys, 0 value
            # mismatches). The symbol list comes from the recursive skip-scan, which is
            # 6 ms versus 2.3 s for SELECT DISTINCT symbol on 993K rows.
            mcap_by_symbol: dict = {}
            try:
                _mc_rows = db.execute(text("""
                    WITH RECURSIVE syms AS (
                        (SELECT symbol FROM prices ORDER BY symbol LIMIT 1)
                        UNION ALL
                        SELECT (SELECT symbol FROM prices
                                WHERE symbol > syms.symbol ORDER BY symbol LIMIT 1)
                        FROM syms WHERE syms.symbol IS NOT NULL
                    )
                    SELECT s.symbol, p.market_cap
                    FROM syms s
                    CROSS JOIN LATERAL (
                        SELECT market_cap
                        FROM prices
                        WHERE prices.symbol = s.symbol
                          AND prices.date <= :d
                          AND prices.market_cap IS NOT NULL
                          AND prices.market_cap > 0
                        ORDER BY prices.date DESC
                        LIMIT 1
                    ) p
                    WHERE s.symbol IS NOT NULL
                """), {"d": market_date}).fetchall()
                mcap_by_symbol = {r.symbol: float(r.market_cap) for r in _mc_rows if r.market_cap}
                logger.info("📈 Loaded real market cap for %d/%d companies from prices (≤ %s).",
                            len(mcap_by_symbol), len(companies), market_date)
            except Exception as mc_err:
                logger.warning("⚠️ Could not load market caps from prices — valuation will "
                               "fall back to te*1.5 for ALL companies: %s", mc_err)

            # [Data quality] Only value symbols that actually traded today. A company
            # with no price row on market_date is suspended/delisted: its XBRL history
            # still exists, so it used to be re-valued every day with a te*1.5 guess and
            # surfaced as a misleading "missing market_cap" warning (8011, 8080, 8090,
            # 8110…). NOTHING historical is deleted here — the symbol is simply left out
            # of today's valuation loop. If this read fails we keep the old behaviour
            # (evaluate the whole list) rather than silently dropping companies.
            active_symbols: set = set()
            try:
                active_symbols = {
                    r.symbol for r in db.execute(
                        text("SELECT DISTINCT symbol FROM prices WHERE date = :d"),
                        {"d": market_date},
                    ).fetchall()
                }
            except Exception as act_err:
                logger.warning("⚠️ Could not read today's traded symbols from prices — "
                               "valuating the full company list instead: %s", act_err)

            all_models = []
            _mcap_fallback: list = []
            _excluded_inactive: list = []
            for c in companies:
                try:
                    if active_symbols and c.symbol not in active_symbols:
                        _excluded_inactive.append(c.symbol)
                        continue
                    _mc = mcap_by_symbol.get(c.symbol)
                    _market_cap_m = (_mc / 1_000_000.0) if _mc else None
                    if _market_cap_m is None:
                        _mcap_fallback.append(c.symbol)
                    m = calculate_valuation_models(c.symbol, market_cap_m=_market_cap_m)
                    all_models.append({
                        "symbol": c.symbol,
                        "company_name": c.company_name,
                        "sector": c.sector,
                        "models": m.get("models")
                    })
                except Exception as model_err:
                    logger.warning(f"⚠️ Error compiling model for {c.symbol}: {model_err}")

            # [R3 / Data Quality] Loud, aggregated warning in BOTH the log and the
            # run report whenever any TRADING company had to use the te*1.5 fallback.
            if _excluded_inactive:
                _ex_preview = ", ".join(_excluded_inactive[:25]) + ("…" if len(_excluded_inactive) > 25 else "")
                logger.info(
                    "⏭️ Valuation skipped %d non-trading symbols (no price row on %s) — "
                    "suspended/delisted, historical data untouched: %s",
                    len(_excluded_inactive), market_date, _ex_preview,
                )
            if _mcap_fallback:
                _preview = ", ".join(_mcap_fallback[:25]) + ("…" if len(_mcap_fallback) > 25 else "")
                logger.warning(
                    "⚠️ VALUATION DATA-QUALITY: %d/%d traded companies had NO real market_cap in "
                    "prices (date ≤ %s) — their valuation used the te*1.5 ESTIMATE, not "
                    "actual market data. Symbols: %s",
                    len(_mcap_fallback), len(all_models), market_date, _preview,
                )
                _record_stage_failure(
                    "19 valuation models → R2",
                    f"{len(_mcap_fallback)}/{len(all_models)} traded companies missing market_cap "
                    f"in prices → te*1.5 fallback used",
                    advisory=True,
                )
            else:
                logger.info("✅ Valuation used real market cap from prices for all %d traded companies.", len(all_models))

            models_summary_path = OUTPUT_DIR / "all_models_summary.json"
            with open(models_summary_path, "w", encoding="utf-8") as f:
                json.dump(all_models, f, ensure_ascii=False, indent=2)

            r2_client = _get_r2_client()
            if r2_client:
                r2_client.upload_file(
                    Filename=str(models_summary_path),
                    Bucket=R2_BUCKET_NAME,
                    Key="all_models_summary.json",
                    ExtraArgs={"ContentType": "application/json"}
                )
                logger.info(f"✅ Pre-compiled models summary ({len(all_models)} companies) uploaded to R2.")
            else:
                logger.info(f"✅ Pre-compiled models summary saved locally ({len(all_models)} companies).")

        # 8.9 Khurafshi Universal Engine Vintage Generation (Optimized Parallel Pipeline Stage)
        # -------------------------------------------------------------------
        try:
            logger.info("🏛️ Generating Point-In-Time Engine Vintages for universe...")
            from app.services.rebh_unified_engine import calculate_full_company_payload
            from app.services.xbrl_data_service import list_companies
            from app.services.rebh_production_helper_service import warm_sector_medians_cache
            from app.services.khurafshi_engine_service import _get_latest_prices_map
            from concurrent.futures import ThreadPoolExecutor, as_completed

            # 1. Pre-load sector medians in ONE pass before the loop
            n_sectors = warm_sector_medians_cache()
            logger.info(f"⚡ Sector medians pre-loaded for {n_sectors} sectors (single-pass).")

            # 2. Pre-load prices map once in memory
            prices_map = _get_latest_prices_map()

            companies = list_companies()
            vintages_file = OUTPUT_DIR / f"rebh_engine_vintage_{market_date}.json"
            engine_vintages = {}

            def _process_company_vintage(c_item):
                try:
                    payload = calculate_full_company_payload(c_item.symbol, prices_map=prices_map)
                    return c_item.symbol, payload.model_dump(mode="json")
                except Exception as c_err:
                    logger.debug(f"Engine payload skip for {c_item.symbol}: {c_err}")
                    return c_item.symbol, None

            # Execute parallelized across 8 threads
            with ThreadPoolExecutor(max_workers=8) as executor:
                future_to_sym = {executor.submit(_process_company_vintage, c): c.symbol for c in companies}
                for f in as_completed(future_to_sym):
                    sym, res = f.result()
                    if res is not None:
                        engine_vintages[sym] = res
            
            with open(vintages_file, "w", encoding="utf-8") as vf:
                json.dump(engine_vintages, vf, ensure_ascii=False)
            logger.info(f"✅ Generated and archived engine vintage for {len(engine_vintages)} companies as of {market_date}.")
        except Exception as vintage_err:
            _record_stage_failure("20 engine vintages", vintage_err)

        # 8.10 Universe Snapshot Persistence (DB-backed, powers /peers + /universe reads)
        # -------------------------------------------------------------------
        try:
            logger.info("🌐 Building REBH universe snapshot for DB-backed reads...")
            from app.services.khurafshi_engine_service import build_universe_snapshot
            # The imported module at the top of this file is named `datetime`.
            # `dt_module` was a local name that only existed inside
            # run_analytics_pipeline.py; using it here raised a NameError that the
            # except clause swallowed: snap_date = None → the snapshot got stamped
            # with today's date instead of the market date being processed (a real
            # difference for every backfill of a past date).
            snap_date = datetime.datetime.strptime(str(market_date), "%Y-%m-%d").date()
            n_snap = build_universe_snapshot(snapshot_date=snap_date, batch_id=str(market_date))
            logger.info(f"✅ Universe snapshot persisted for {n_snap} companies (sector peers now served from DB).")
        except Exception as snap_err:
            _record_stage_failure("21 universe snapshot", snap_err)


        # ── Run report: nothing may fail silently ───────────────────
        _fails = RUN_REPORT["stage_failures"]
        _advisories = RUN_REPORT.get("stage_advisories", [])
        if _fails:
            logger.error("=" * 80)
            logger.error("⚠️ %d STAGE(S) FAILED for %s — the day is NOT fully complete:",
                         len(_fails), market_date)
            for f in _fails:
                logger.error("   • [%s]%s %s", f["stage"],
                             " CRITICAL" if f["critical"] else "        ", f["error"])
            logger.error("=" * 80)
        else:
            logger.info("✅ Run report: all stages completed without failures%s.",
                        f" — {len(_advisories)} advisory note(s)" if _advisories else "")
        for a in _advisories:
            logger.info("   • 📋 [%s] %s", a["stage"], a["error"])
        _q = RUN_REPORT["quality"]
        if _q:
            logger.info("📋 Quality: rows=%s mcap_missing=%s (%s%%) high<low=%s "
                        "missing_ohlc=%s rows_vs_prev=%s",
                        _q.get("rows"), _q.get("market_cap_missing"),
                        round((_q.get("market_cap_missing_ratio") or 0) * 100, 1),
                        _q.get("high_below_low"), _q.get("missing_ohlc"),
                        _q.get("rows_vs_prev_day"))

        # [L2] Timings summary — measured per _guard-wrapped stage (in finally, so
        # failed stages are timed too). Display only; the list is appended as each
        # stage completes, so it is already in execution order.
        _timings = RUN_REPORT.get("stage_timings", [])
        if _timings:
            _total = sum(t["seconds"] for t in _timings)
            _icon = {"ok": "✅", "failed": "❌"}
            logger.info("=" * 64)
            logger.info("⏱️  STAGE TIMINGS — %.1fs across %d measured stage(s):",
                        _total, len(_timings))
            for _t in _timings:
                logger.info("   %8.3fs  %s  %s",
                            _t["seconds"], _icon.get(_t["status"], "•"), _t["stage"])
            logger.info("=" * 64)

        # 9. Finalize Update Status (Atomic Switch)
        # -------------------------------------------------------------------
        # Guard: if a critical stage failed, the day is incomplete — latest_ready_date
        # must not advance past a partial day. Keep the current date (no-op) and release the lock.
        _critical = [f for f in RUN_REPORT["stage_failures"] if f["critical"]]
        RUN_REPORT["critical_stages"] = [c["stage"] for c in _critical]
        _ready_date = market_date
        if _critical and PIPELINE_STRICT_FINALIZE:
            _held = None
            try:
                _held = db.execute(text(
                    "SELECT latest_ready_date FROM update_status WHERE id = 1"
                )).scalar()
            except Exception as hold_err:  # the guard itself must never block the finalize
                logger.warning("⚠️ Could not read current latest_ready_date (%s) — "
                               "advancing normally." % hold_err)
            if _held is not None:
                _ready_date = _held
                RUN_REPORT["ready_date_advanced"] = False
                logger.error(
                    "🛑 PIPELINE ALERT — latest_ready_date NOT advanced for %s: %d "
                    "critical stage(s) failed → %s. The site keeps showing the last "
                    "complete day (%s). Re-run after fixing, or set "
                    "PIPELINE_STRICT_FINALIZE=0 to override.",
                    market_date, len(_critical),
                    ", ".join(RUN_REPORT["critical_stages"]), _ready_date
                )
            else:
                RUN_REPORT["ready_date_advanced"] = True
        else:
            _ready_date = market_date
            RUN_REPORT["ready_date_advanced"] = True
        try:
            db.execute(text("""
                UPDATE update_status 
                SET latest_ready_date = :market_date, 
                    is_updating = FALSE, 
                    completed_at = :now 
                WHERE id = 1
            """), {"market_date": _ready_date, "now": datetime.datetime.utcnow()})
            db.commit()
        except Exception as finalize_err:
            logger.error(f"⚠️ Finalize on existing session failed: {finalize_err}")
            _rollback_session(db)
            _release_update_lock(_ready_date)

        # -------------------------------------------------------------------
        # 10. Invalidate Caches so new data shows up immediately
        # -------------------------------------------------------------------
        # Delegated to app.core.cache_bust. The hand-copied SCAN/UNLINK loop that
        # used to sit here reported EVERY failure as an informational "redis is not
        # running here, so the flush was skipped, and that is safe in dev" line —
        # which is how a run that rewrote production rows could quietly leave the
        # production cache serving the old payload, and how flushing from a Render
        # shell became part of the routine. cache_bust evicts this host's Redis
        # when it can reach it and otherwise knocks the authenticated HTTP door on
        # the deployed API; either way the outcome lands in the run report.
        # (It evicts BY PREFIX, never "*": the old wildcard pass deleted other
        # apps' keys and rate-limit buckets too and needed a hand-maintained auth
        # exclusion list to avoid logging users out.)
        try:
            # Part 2 / Phase 4: warm the hot keys of the namespaces this step is
            # about to clear, then evict the rest. Wraps the SAME bust (the
            # eviction stays cache_bust's single implementation); if the warm-up
            # layer cannot even be imported the run degrades to the plain bust,
            # because an optimisation must never be what leaves stale data live.
            try:
                from app.services import cache_warmers
                cache_report = cache_warmers.bust_job_caches("daily_market_update")
            except Exception as warm_err:                       # noqa: BLE001
                logger.info("warm-up unavailable (%s: %s) — busting without it",
                            type(warm_err).__name__, str(warm_err)[:120])
                from app.core import cache_bust
                cache_report = cache_bust.bust(job="daily_market_update")
            RUN_REPORT["cache_busted"] = cache_report["ok"]
            RUN_REPORT["cache_summary"] = cache_report["summary"]
            if cache_report["ok"]:
                logger.info("✅ Caches busted (%s) — new data is live.",
                            cache_report["summary"])
            else:
                # Advisory, not a stage failure: the day's rows ARE written, so a
                # cache outage must not mark a good data day as failed. It is
                # recorded and flagged because "ran fine, site unchanged" is the
                # one outcome the operator cannot be left to guess about.
                _record_stage_failure(
                    "10 cache invalidation",
                    "no cache reachable (%s): %s" % (
                        cache_report["summary"],
                        "; ".join(str(c.get("error")) for c in cache_report["channels"])),
                    advisory=True,
                )
        except Exception as cache_err:
            RUN_REPORT["cache_busted"] = False
            _record_stage_failure("10 cache invalidation", cache_err, advisory=True)

        if RUN_REPORT["stage_failures"]:
            logger.info("🏁 Daily Update Workflow completed WITH %d failed stage(s) — "
                        "read the 🛑/⚠️ alerts above." % len(RUN_REPORT["stage_failures"]))
        else:
            logger.info("🎉 Daily Update Workflow Completed Successfully!")
        return get_run_report()

    except Exception as e:
        _record_stage_failure("pipeline (uncaught top-level)", e, critical=True)
        _rollback_session(db)
        return get_run_report()
    finally:
        # Single release point — guarantees the mutex is freed on success,
        # failure, early return (e.g. scrape returned nothing) or Ctrl+C.
        # [C1] Always drop the per-cycle company cache; disable() only touches
        # module globals so it is safe even if enable() never ran (early return).
        disable_company_cache()
        reset_session_tuning(db)
        _release_update_lock()
        try:
            db.close()
        except Exception:
            pass


def main(target_date_str=None):
    """
    Programmatic entry point (admin API trigger / CLI).
    Kept argument-optional so callers can do: main() or main("2026-10-05").
    """
    return update_daily(target_date_str)

if __name__ == "__main__":
    import argparse
    from app.services.job_tracking import track_job, with_result_handling
    parser = argparse.ArgumentParser(description='Run Daily Market Update')
    parser.add_argument('--date', type=str, help='Target date in YYYY-MM-DD format (overrides today)')
    
    args = parser.parse_args()
    
    # Tracked only on the CLI path — the admin endpoint already wraps its own
    # call in app/api/routes/admin.py, so wrapping update_daily() itself would
    # write two rows for one run.
    with track_job("daily_market_update", "manual") as run:
        report = update_daily(args.date)
        with_result_handling(run, report)