"""
Admin API Router
CRUD endpoints for EPS estimates, system config, valuation zones, and TASI components.
Mount at: /api/admin
Protect these routes with your existing auth middleware.
"""

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from pydantic import BaseModel, Field

from app.core.database import SessionLocal
from app.models.eps_estimates import EpsEstimate
from app.models.system_config import SystemConfig
from app.models.valuation_zones import ValuationZone
from app.models.tasi_components import TasiComponent
from app.scrapers.daily_financial_indicators_scraper import run_scraper_and_save_to_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["Admin — Valuation Config"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class EpsEstimateIn(BaseModel):
    year:       int
    value:      float
    type:       Optional[str] = "estimate"
    source:     Optional[str] = None
    created_by: Optional[str] = None


class SystemConfigIn(BaseModel):
    value:       str
    description: Optional[str] = None


class ValuationZoneIn(BaseModel):
    label:          str
    label_ar:       Optional[str] = None
    price_from:     float
    price_to:       float
    return_pct_low: Optional[int] = None
    return_pct_high:Optional[int] = None
    color_code:     Optional[str] = None
    description:    Optional[str] = None
    order_seq:      Optional[int] = None


class TasiComponentIn(BaseModel):
    symbol:          str
    company_name:    str
    company_name_ar: Optional[str] = None
    sector:          Optional[str] = None
    sector_ar:       Optional[str] = None
    weight_in_index: Optional[float] = None
    eps:             Optional[float] = None
    is_active:       bool = True


# ─────────────────────────────────────────────────────────────────────────────
# EPS Estimates
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/eps-estimates", summary="List all EPS estimates")
def list_eps_estimates():
    db = SessionLocal()
    try:
        rows = db.query(EpsEstimate).order_by(EpsEstimate.year).all()
        return [
            {
                "id":         r.id,
                "year":       r.year,
                "value":      float(r.value),
                "type":       r.type,
                "source":     r.source,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@router.post("/eps-estimates", summary="Create or update an EPS estimate")
def upsert_eps_estimate(body: EpsEstimateIn):
    db = SessionLocal()
    try:
        existing = db.query(EpsEstimate).filter(EpsEstimate.year == body.year).first()
        if existing:
            existing.value      = body.value
            existing.type       = body.type
            existing.source     = body.source
            existing.created_by = body.created_by
            db.commit()
            return {"action": "updated", "year": body.year}
        else:
            db.add(EpsEstimate(**body.dict()))
            db.commit()
            return {"action": "created", "year": body.year}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/eps-estimates/{year}", summary="Update an EPS estimate by year")
def update_eps_estimate(year: int, body: EpsEstimateIn):
    db = SessionLocal()
    try:
        row = db.query(EpsEstimate).filter(EpsEstimate.year == year).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"No EPS estimate for year {year}")
        row.value      = body.value
        row.type       = body.type
        row.source     = body.source
        row.created_by = body.created_by
        db.commit()
        return {"action": "updated", "year": year}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.delete("/eps-estimates/{year}", summary="Delete an EPS estimate")
def delete_eps_estimate(year: int):
    db = SessionLocal()
    try:
        row = db.query(EpsEstimate).filter(EpsEstimate.year == year).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"No EPS estimate for year {year}")
        db.delete(row)
        db.commit()
        return {"action": "deleted", "year": year}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# System Config
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/system-config", summary="List all system config keys")
def list_system_config():
    db = SessionLocal()
    try:
        rows = db.query(SystemConfig).order_by(SystemConfig.key).all()
        return [
            {
                "key":         r.key,
                "value":       r.value,
                "data_type":   r.data_type,
                "description": r.description,
                "updated_at":  r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


@router.put("/system-config/{key}", summary="Update a system config value")
def update_system_config(key: str, body: SystemConfigIn):
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Config key '{key}' not found")
        row.value = body.value
        if body.description:
            row.description = body.description
        db.commit()
        return {"action": "updated", "key": key, "value": body.value}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Valuation Zones
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/valuation-zones", summary="List all valuation zones")
def list_valuation_zones():
    db = SessionLocal()
    try:
        rows = db.query(ValuationZone).order_by(ValuationZone.order_seq).all()
        return [
            {
                "id":             r.id,
                "label":          r.label,
                "label_ar":       r.label_ar,
                "price_from":     float(r.price_from),
                "price_to":       float(r.price_to),
                "return_pct_low": r.return_pct_low,
                "return_pct_high":r.return_pct_high,
                "color_code":     r.color_code,
                "description":    r.description,
                "order_seq":      r.order_seq,
            }
            for r in rows
        ]
    finally:
        db.close()


@router.post("/valuation-zones", summary="Create a valuation zone")
def create_valuation_zone(body: ValuationZoneIn):
    db = SessionLocal()
    try:
        zone = ValuationZone(**body.dict())
        db.add(zone)
        db.commit()
        db.refresh(zone)
        return {"action": "created", "id": zone.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/valuation-zones/{zone_id}", summary="Update a valuation zone")
def update_valuation_zone(zone_id: int, body: ValuationZoneIn):
    db = SessionLocal()
    try:
        row = db.query(ValuationZone).filter(ValuationZone.id == zone_id).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
        for field, val in body.dict(exclude_none=True).items():
            setattr(row, field, val)
        db.commit()
        return {"action": "updated", "id": zone_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.delete("/valuation-zones/{zone_id}", summary="Delete a valuation zone")
def delete_valuation_zone(zone_id: int):
    db = SessionLocal()
    try:
        row = db.query(ValuationZone).filter(ValuationZone.id == zone_id).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
        db.delete(row)
        db.commit()
        return {"action": "deleted", "id": zone_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# TASI Components
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/tasi-components", summary="List TASI components")
def list_tasi_components(active_only: bool = True):
    db = SessionLocal()
    try:
        query = db.query(TasiComponent)
        if active_only:
            query = query.filter(TasiComponent.is_active == True)
        rows = query.order_by(TasiComponent.weight_in_index.desc()).all()
        return [
            {
                "id":              r.id,
                "symbol":          r.symbol,
                "company_name":    r.company_name,
                "company_name_ar": r.company_name_ar,
                "sector":          r.sector,
                "weight_in_index": float(r.weight_in_index) if r.weight_in_index else None,
                "weight_adjusted": float(r.weight_adjusted) if r.weight_adjusted else None,
                "eps":             float(r.eps) if r.eps else None,
                "current_price":   float(r.current_price) if r.current_price else None,
                "pe_ratio":        float(r.pe_ratio) if r.pe_ratio else None,
                "updated_at":      r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


@router.post("/tasi-components", summary="Create or update a TASI component")
def upsert_tasi_component(body: TasiComponentIn):
    db = SessionLocal()
    try:
        existing = db.query(TasiComponent).filter(TasiComponent.symbol == body.symbol).first()
        if existing:
            for field, val in body.dict(exclude_none=True).items():
                setattr(existing, field, val)
            db.commit()
            return {"action": "updated", "symbol": body.symbol}
        else:
            db.add(TasiComponent(**body.dict()))
            db.commit()
            return {"action": "created", "symbol": body.symbol}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/tasi-components/refresh", summary="Trigger TASI market data refresh from Tadawul")
def refresh_tasi_market_data():
    """
    Calls the Daily Financial Indicators scraper to update current prices, EPS, P/E, 
    and index weights (Market Cap %) for all active components directly from Tadawul.
    """
    try:
        result = run_scraper_and_save_to_db() or {}
        if result.get("ok"):
            message = (f"TASI market data and weights refreshed — "
                       f"{result.get('rows', 0)} rows written")
            if result.get("partial"):
                message += (f" (partial page: only {result.get('companies')} companies; "
                            f"{result.get('added', 0)} added / {result.get('updated', 0)} updated)")
            return {"status": "success", "message": message}
        failures = result.get("stage_failures") or []
        detail = "; ".join(f"[{f.get('stage')}] {f.get('error')}" for f in failures) or \
            "Scraper executed but returned ok=False."
        raise HTTPException(status_code=502, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TASI refresh error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/tasi-components/{symbol}", summary="Deactivate a TASI component")
def deactivate_tasi_component(symbol: str):
    """Soft-delete: sets is_active=False rather than removing the row."""
    db = SessionLocal()
    try:
        row = db.query(TasiComponent).filter(TasiComponent.symbol == symbol).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")
        row.is_active = False
        db.commit()
        return {"action": "deactivated", "symbol": symbol}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
