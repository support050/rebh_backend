"""
Importer health & the filings readiness check for REBH & TASI.
==============================================================
Retired 2026-10-13: the wrapper importers `run_sukuk_importer`,
`run_macro_importer` and `run_bank_lines_importer` and the engine-vintage job.
Why:
  · Sukuk / SAMA-GaStat ingestion runs MANUALLY via the standalone scripts
    (scripts/Sukuk&Bonds.py, scripts/SAMA&GaStat.py). Each script's __main__
    wraps itself in track_job, so job_runs keeps its history under the same
    keys ('sukuk_importer', 'macro_importer') without any cron wrapper.
  · The bank-lines "verification importer" wrote nothing and duplicated
    bank_analytics_service — dropped outright.
  · The vintage snapshot moved to its own module,
    app/services/rebh_vintage_service.py (cron key 'engine_vintages' unchanged).

What remains here:
  1. Daily Market Update readiness (UpdateStatus row, manual/admin-triggered)
  2. Official Filings ingestion check (daily cron, key 'filings_importer')
  3. get_importers_health_status() — DB-direct freshness for every table the
     retired wrappers used to proxy, joined with the real last script run
     read from job_runs. No in-memory-only claims survive a restart anymore.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from app.core.database import SessionLocal
from app.models.sukuk_bonds import SukukMarketData
from app.models.saudi_macro import SaudiEconomicIndicator
from app.models.market_reports import SubstantialShareholder
from app.models.official_filings import CompanyOfficialFiling
from app.models.update_status import UpdateStatus
from app.models.job_run import JobRun

logger = logging.getLogger(__name__)

# In-memory telemetry for the jobs that still run inside this process.
# Everything else is answered from the database (tables + job_runs).
_IMPORTER_STATUS: Dict[str, Dict[str, Any]] = {
    "daily_market_update": {
        "name": "Daily Market Pipeline (Tadawul Prices & RS)",
        "last_run": None,
        "status": "idle",
        "records_processed": 0,
        "error": None,
        "frequency": "Manual / Admin-triggered (Post-Market)"
    },
    "filings_importer": {
        "name": "Official Corporate Filings & XBRL Ingestion",
        "last_run": None,
        "status": "idle",
        "records_processed": 0,
        "error": None,
        "frequency": "Daily / Continuous Sync"
    },
}


# ─────────────────────────────────────────────────────────────
# Filings Importer (daily cron — key 'filings_importer')
# ─────────────────────────────────────────────────────────────
def run_filings_importer() -> Dict[str, Any]:
    """Scan and verify availability of official filings and XBRL reports."""
    _IMPORTER_STATUS["filings_importer"]["status"] = "running"
    db = SessionLocal()
    try:
        filing_count = db.query(CompanyOfficialFiling).count()
        now_str = datetime.now(timezone.utc).isoformat()
        _IMPORTER_STATUS["filings_importer"]["last_run"] = now_str
        _IMPORTER_STATUS["filings_importer"]["status"] = "ok"
        _IMPORTER_STATUS["filings_importer"]["records_processed"] = filing_count
        _IMPORTER_STATUS["filings_importer"]["error"] = None
        logger.info(f"✅ Filings Importer checked. Current filings stored: {filing_count}")
        return {"status": "ok", "total_filings": filing_count, "timestamp": now_str}
    except Exception as e:
        now_str = datetime.now(timezone.utc).isoformat()
        _IMPORTER_STATUS["filings_importer"]["last_run"] = now_str
        _IMPORTER_STATUS["filings_importer"]["status"] = "failed"
        _IMPORTER_STATUS["filings_importer"]["error"] = str(e)
        logger.error(f"❌ Filings Importer failed: {e}")
        return {"status": "failed", "error": str(e), "timestamp": now_str}
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# Health & Readiness Status Aggregator (DB-direct)
# ─────────────────────────────────────────────────────────────
def _latest_job_run(db, job_name: str) -> Dict[str, Any]:
    """Newest job_runs row for one key — how the manually-run scripts report."""
    row = (
        db.query(JobRun)
          .filter(JobRun.job_name == job_name)
          .order_by(JobRun.started_at.desc(), JobRun.id.desc())
          .first()
    )
    if row is None:
        return {"last_run": None, "status": "never", "rows_written": None}
    return {
        "last_run": row.started_at.isoformat() if row.started_at else None,
        "status": row.status,
        "rows_written": row.rows_written,
        "triggered_by": row.triggered_by,
    }


def get_importers_health_status() -> Dict[str, Any]:
    """
    Database-backed health for every ingestion path: freshness read straight
    from Postgres (tables + job_runs) so it stays accurate across restarts.
    """
    from app.services.rebh_vintage_service import get_vintage_status

    db = SessionLocal()
    try:
        # 1. Sukuk table freshness (written by scripts/Sukuk&Bonds.py)
        sukuk_count = db.query(SukukMarketData).count()
        latest_sukuk = db.query(SukukMarketData).order_by(SukukMarketData.updated_at.desc()).first()
        sukuk_last_date = latest_sukuk.updated_at.isoformat() if latest_sukuk and latest_sukuk.updated_at else None

        # 2. Macro indicators freshness (written by scripts/SAMA&GaStat.py)
        macro_count = db.query(SaudiEconomicIndicator).count()
        latest_macro = db.query(SaudiEconomicIndicator).order_by(SaudiEconomicIndicator.updated_at.desc()).first()
        macro_last_date = latest_macro.updated_at.isoformat() if latest_macro and latest_macro.updated_at else None

        # 3. Filings count
        filings_count = db.query(CompanyOfficialFiling).count()
        latest_filing = db.query(CompanyOfficialFiling).order_by(CompanyOfficialFiling.created_at.desc()).first()
        filing_last_date = latest_filing.created_at.isoformat() if latest_filing and latest_filing.created_at else None

        # 4. Daily update status
        update_status = db.query(UpdateStatus).filter(UpdateStatus.id == 1).first()
        daily_ready_date = update_status.latest_ready_date.isoformat() if update_status and update_status.latest_ready_date else None
        daily_updating = update_status.is_updating if update_status else False

        # 5. Market reports (substantial shareholders)
        #    qfi_flows was reported here until 2026-10-25: qfi_ownership_flows had
        #    0 rows and no writer, so the counter was a permanent 0 that read as
        #    "broken scraper". The model, endpoint and table were removed.
        shareholders_count = db.query(SubstantialShareholder).count()

        # 6. Last real run of the standalone ingestion scripts (job_runs ledger)
        sukuk_run = _latest_job_run(db, "sukuk_importer")
        macro_run = _latest_job_run(db, "macro_importer")

        daily_stat = dict(_IMPORTER_STATUS["daily_market_update"])
        if not daily_stat["last_run"] and daily_ready_date:
            daily_stat["last_run"] = daily_ready_date
            daily_stat["status"] = "running" if daily_updating else "ok"

        filings_stat = dict(_IMPORTER_STATUS["filings_importer"])
        if not filings_stat["last_run"] and filing_last_date:
            filings_stat["last_run"] = filing_last_date
            filings_stat["records_processed"] = filings_count
            filings_stat["status"] = "ok" if filings_count > 0 else "idle"

        vintage_stat = get_vintage_status()

        all_ok = all(
            s["status"] in ("ok", "idle", "running", "success")
            for s in (daily_stat, filings_stat, vintage_stat)
        )

        return {
            "overall_status": "healthy" if all_ok else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database_totals": {
                "sukuk_instruments": sukuk_count,
                "macro_indicators": macro_count,
                "official_filings": filings_count,
                "substantial_shareholders": shareholders_count,
                "pipeline_latest_date": daily_ready_date,
                "sukuk_latest_update": sukuk_last_date,
                "macro_latest_update": macro_last_date,
            },
            "importers": {
                "daily_market_update": daily_stat,
                "filings_importer": filings_stat,
                "engine_vintages": vintage_stat,
            },
            # Ingestion that runs as standalone scripts — freshness comes from
            # the tables themselves and the last script run from job_runs.
            "manual_ingestion": {
                "sukuk": {
                    "script": "backend/scripts/Sukuk&Bonds.py",
                    "table": "sukuk_market_data",
                    "records": sukuk_count,
                    "latest_update": sukuk_last_date,
                    "last_run": sukuk_run,
                },
                "macro": {
                    "script": "backend/scripts/SAMA&GaStat.py",
                    "table": "saudi_economic_indicators",
                    "records": macro_count,
                    "latest_update": macro_last_date,
                    "last_run": macro_run,
                },
            },
        }
    except Exception as e:
        logger.error(f"Error computing importers health status: {e}")
        return {
            "overall_status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "importers": _IMPORTER_STATUS,
        }
    finally:
        db.close()
