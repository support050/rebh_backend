"""
REBH Universe Snapshot Model.

Persists the pre-computed market-wide universe dataset (per company, per day) so that
heavy read paths (Sector Peers table, screener, sector comparisons) never recompute the
full TASI universe from hundreds of XBRL JSON files at request time.

Design:
- Scalar columns (symbol, sector, px, mc, pe, ...) exist ONLY for fast filtering /
  indexing / sorting (e.g. `WHERE is_latest AND sector = :sector`).
- `payload_json` stores the EXACT universe item dict produced by
  `get_khurafshi_universe_data()`, so the `/api/rebh/universe` contract stays byte-identical
  when served from the DB.
- `is_latest` marks the current active batch; older dates remain for history/backfill.
"""
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Date, DateTime, Text, Index, UniqueConstraint,
)
from sqlalchemy.sql import func
from app.core.database import Base


class RebhUniverseSnapshot(Base):
    __tablename__ = "rebh_universe_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    batch_id = Column(String(50), nullable=True, index=True)

    symbol = Column(String(20), nullable=False, index=True)
    company_name = Column(String(255), nullable=True)
    sector = Column(String(255), nullable=True, index=True)

    # Lightweight metrics for filtering / ordering / peer tables
    px = Column(Float, nullable=True)
    mc = Column(Float, nullable=True)
    pe = Column(Float, nullable=True)
    pb = Column(Float, nullable=True)
    roe = Column(Float, nullable=True)
    nm = Column(Float, nullable=True)
    de = Column(Float, nullable=True)
    g_net = Column(Float, nullable=True)
    fcf_yield = Column(Float, nullable=True)
    f_score = Column(Integer, nullable=True)

    is_latest = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    payload_json = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("snapshot_date", "symbol", name="uq_universe_snapshot_date_symbol"),
        Index("ix_universe_latest_sector", "is_latest", "sector"),
    )

    def __repr__(self):
        return f"<RebhUniverseSnapshot symbol={self.symbol} date={self.snapshot_date} latest={self.is_latest}>"
