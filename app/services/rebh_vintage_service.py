"""REBH Engine Vintage Snapshot — the dedicated, standalone runner.

Split out of rebh_importers_service.py on 2026-10-13, when the sukuk / macro /
bank-lines wrapper importers were retired (their ingestion runs manually via
scripts/Sukuk&Bonds.py and scripts/SAMA&GaStat.py, which record their own
job_runs rows). Nothing in this module touches those wrappers: the vintage
snapshot depends only on the unified engine, the XBRL company list and the
rebh_engine_vintages / rebh_universe_snapshots tables.

Callable from everywhere through the same entry point:
  · cron  — app/core/scheduler.py job_engine_vintages  (key "engine_vintages")
  · admin — POST /api/rebh/importers/run/vintages      (key "engine_vintages")
  · CLI   — python -m app.services.rebh_vintage_service [--limit N]
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)

# In-memory telemetry for the health aggregator (rebh_importers_service reads
# it through get_vintage_status; job_runs remains the durable ledger).
_VINTAGE_STATUS: Dict[str, Any] = {
    "name": "REBH Unified Engine Vintage Snapshots",
    "last_run": None,
    "status": "idle",
    "records_processed": 0,
    "error": None,
    "frequency": "Daily Post-Close (20:00 UTC+2)",
}


def get_vintage_status() -> Dict[str, Any]:
    """Copy of the latest run telemetry, for /importers/health."""
    return dict(_VINTAGE_STATUS)


def run_engine_vintages_job(limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Execute point-in-time snapshot of the REBH Universal Engine calculations across covered companies.
    Validates P0 identity guards and records vintage status.
    """
    _VINTAGE_STATUS["status"] = "running"
    db = SessionLocal()
    try:
        from app.services.xbrl_data_service import list_companies
        from app.services.rebh_unified_engine import calculate_full_company_payload
        from app.models.rebh_engine_vintage import RebhEngineVintage

        companies = list_companies()
        if limit and limit > 0:
            companies = companies[:limit]
        processed = 0
        quarantined = 0   # P0 accounting-guard failures (the engine's own verdict)
        stale = 0         # statements older than the pricing gate — needs newer filings
        persisted = 0
        now_dt = datetime.now(timezone.utc)
        batch_id = f"batch_{now_dt.strftime('%Y%m%d_%H%M%S')}"

        for c in companies:
            try:
                payload = calculate_full_company_payload(c.symbol)
                processed += 1
                # The engine keeps two distinct concepts — do not conflate them:
                #   quarantine_reason ⇒ A=L+E / IS-plausibility guards failed (bad data)
                #   fresh=False       ⇒ statements aged past the pricing gate (old data)
                # Counting stale rows as "quarantined" made every routine run look
                # like a 100% integrity failure (2026-10-13: 236/236 "quarantined"
                # while only ~11 rows actually failed the P0 guards).
                if payload.quarantine_reason:
                    quarantined += 1
                elif not payload.fresh:
                    stale += 1
                is_q = bool(payload.quarantine_reason)

                # Check if identical record exists in this batch
                exists = db.query(RebhEngineVintage).filter(
                    RebhEngineVintage.symbol == payload.symbol,
                    RebhEngineVintage.batch_id == batch_id,
                    RebhEngineVintage.as_of_period == payload.as_of
                ).first()
                if exists:
                    continue

                # Persist point-in-time snapshot to DB
                v_record = RebhEngineVintage(
                    symbol=payload.symbol,
                    batch_id=batch_id,
                    vintage_date=now_dt,
                    as_of_period=payload.as_of,
                    fresh=payload.fresh,
                    quarantined=is_q,
                    quarantine_reason=payload.quarantine_reason,
                    required_return_r=payload.required_return,
                    gold_max=payload.zones.gold_max if payload.zones else None,
                    silver_max=payload.zones.silver_max if payload.zones else None,
                    bronze_max=payload.zones.bronze_max if payload.zones else None,
                    piotroski_score=payload.piotroski,
                    engine_version=payload.provenance.get("engine_version", "REBH-2.0") if payload.provenance else "REBH-2.0",
                    contract_json=payload.model_dump_json()
                )
                db.add(v_record)
                persisted += 1
            except Exception as item_err:
                # A hard computation error is its own bucket — count it alongside
                # the P0 guards (the row never reaches the DB) but name it in the
                # log so it cannot masquerade as an accounting-quarantine.
                quarantined += 1
                logger.warning(f"Vintage snapshot failed for {c.symbol} (counted as blocked, not quarantined-by-guards): {item_err}")

        db.commit()
        now_str = now_dt.isoformat()
        _VINTAGE_STATUS["last_run"] = now_str
        _VINTAGE_STATUS["status"] = "ok"
        _VINTAGE_STATUS["records_processed"] = processed
        _VINTAGE_STATUS["error"] = None
        logger.info(
            f"✅ Engine Vintage Snapshot complete: {processed} evaluated, {persisted} persisted to DB, "
            f"{quarantined} quarantined (P0 guards), {stale} stale (past pricing gate).")

        # Rebuild the DB-backed universe snapshot so /peers + /universe reflect the new batch.
        universe_persisted = None
        try:
            from app.services.khurafshi_engine_service import build_universe_snapshot
            universe_persisted = build_universe_snapshot(snapshot_date=now_dt.date(), batch_id=batch_id)
            logger.info(f"✅ Universe snapshot refreshed for {universe_persisted} companies.")
        except Exception as snap_err:
            logger.warning(f"⚠️ Universe snapshot refresh failed: {snap_err}")

        return {
            "status": "ok",
            # "rows" is the key job_tracking._infer_rows reads for the Runbook
            # Rows column — without it the tile shows "—" even on a green run
            # (2026-10-13 fix; the other keys name the same work in prose).
            "rows": persisted,
            "evaluated_companies": processed,
            "persisted_snapshots": persisted,
            "universe_snapshot": universe_persisted,
            "quarantined": quarantined,
            "stale": stale,
            "timestamp": now_str
        }
    except Exception as e:
        db.rollback()
        now_str = datetime.now(timezone.utc).isoformat()
        _VINTAGE_STATUS["last_run"] = now_str
        _VINTAGE_STATUS["status"] = "failed"
        _VINTAGE_STATUS["error"] = str(e)
        logger.error(f"❌ Engine Vintage Snapshot job failed: {e}")
        return {"status": "failed", "error": str(e), "timestamp": now_str}
    finally:
        db.close()


if __name__ == "__main__":
    # CLI twin of the cron/admin path — recorded under the SAME job key so the
    # Runbook sees one row per run whichever door it came through.
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="REBH Engine Vintage Snapshot")
    parser.add_argument("--limit", type=int, default=None,
                        help="Snapshot only the first N companies (smoke runs)")
    args = parser.parse_args()

    from app.services.job_tracking import track_job, with_result_handling
    with track_job("engine_vintages", "manual") as run:
        result = run_engine_vintages_job(limit=args.limit)
        with_result_handling(run, result)
    sys.exit(0 if result.get("status") == "ok" else 1)
