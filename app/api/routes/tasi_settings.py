"""
tasi_settings.py  (route)
=========================
FastAPI router for /api/tasi-settings.

GET  → returns the current singleton settings row.
POST → updates (upserts) the settings row — admin-only, because this single row
        gates the buy switch / market exposure / disposal-day signals every
        customer sees. The /market-pulse/settings page that edits it is already
        wrapped in ``<ProtectedRoute requireAdmin>``.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.tasi_settings import TasiSettings

router = APIRouter()


# ── Pydantic schemas (co-located — small enough) ────────────────────────────
class TasiSettingsRead(BaseModel):
    id: int
    buy_switch: bool
    breathing_rule: bool
    power_trend: bool
    market_exposure: int
    disposal_days: int
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TasiSettingsUpdate(BaseModel):
    buy_switch: bool = Field(True, description="مفتاح الشراء")
    breathing_rule: bool = Field(False, description="قاعدة ضبط النفس")
    power_trend: bool = Field(True, description="الباور ترند")
    market_exposure: int = Field(100, ge=0, le=100, description="الانكشاف على السوق %")
    disposal_days: int = Field(5, ge=1, le=30, description="أيام التصريف")


# ── Default seed values ─────────────────────────────────────────────────────
DEFAULTS = dict(
    buy_switch=True,
    breathing_rule=False,
    power_trend=True,
    market_exposure=100,
    disposal_days=5,
)


def _get_or_seed(db: Session) -> TasiSettings:
    """Return the singleton row, creating it with defaults if missing."""
    row = db.query(TasiSettings).first()
    if row is None:
        row = TasiSettings(**DEFAULTS)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# ── GET /api/tasi-settings ──────────────────────────────────────────────────
@router.get(
    "",
    response_model=TasiSettingsRead,
    summary="Get current TASI market settings",
)
def get_settings(db: Session = Depends(get_db)):
    return _get_or_seed(db)


# ── POST /api/tasi-settings ─────────────────────────────────────────────────
@router.post(
    "",
    response_model=TasiSettingsRead,
    summary="Update TASI market settings",
    dependencies=[Depends(get_current_admin)],
)
def update_settings(payload: TasiSettingsUpdate, db: Session = Depends(get_db)):
    row = _get_or_seed(db)
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row
