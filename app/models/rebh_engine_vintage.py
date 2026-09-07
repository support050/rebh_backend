from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, Index, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base


class RebhEngineVintage(Base):
    """
    Point-in-Time Historical Vintage Record for REBH Universal Engine Snapshots.
    Persists audit-trail evaluation contracts per company per batch execution.
    """
    __tablename__ = "rebh_engine_vintages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    batch_id = Column(String(50), nullable=True, index=True)
    vintage_date = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    as_of_period = Column(String(50), nullable=True)
    fresh = Column(Boolean, default=True)
    quarantined = Column(Boolean, default=False)
    quarantine_reason = Column(String(500), nullable=True)
    required_return_r = Column(Float, nullable=True)
    gold_max = Column(Float, nullable=True)
    silver_max = Column(Float, nullable=True)
    bronze_max = Column(Float, nullable=True)
    piotroski_score = Column(Integer, nullable=True)
    engine_version = Column(String(50), default="REBH-2.0")
    contract_json = Column(Text, nullable=False)

    __table_args__ = (
        Index("ix_vintages_symbol_date", "symbol", "vintage_date"),
        UniqueConstraint("symbol", "batch_id", "as_of_period", name="uq_vintages_symbol_batch_period")
    )

    def __repr__(self):
        return f"<RebhEngineVintage symbol={self.symbol} vintage_date={self.vintage_date} fresh={self.fresh}>"
