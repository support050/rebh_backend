"""Job run tracking.

Single table that answers "did this script actually run, when, and did it
succeed?". Nothing else in the codebase recorded it:

  * app/services/scraper_service.py   -> in-memory dict, wiped on restart
  * app/services/rebh_importers_service.py -> in-memory dict, wiped on restart
  * app/models/update_status.py       -> one row, mutex only, daily_market_update
  * app/models/breadth.py::BreadthRun -> breadth job only

This model generalises BreadthRun's shape (status / started_at / finished_at)
to every scheduled or manually triggered job.
"""
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, CheckConstraint, Index,
)
from sqlalchemy.sql import func

from app.core.database import Base


class JobRun(Base):
    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Stable machine key agreed with the system registry (e.g. "sukuk_importer",
    # "daily_market_update", "fred:UNRATE"). Never the human label.
    job_name = Column(String(100), nullable=False, index=True)

    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)

    # A row left in "running" means the process died before it could close it.
    status = Column(String(16), nullable=False, default="running", server_default="running")
    rows_written = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    triggered_by = Column(String(16), nullable=False, default="cron", server_default="cron")

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed')",
            name="ck_job_runs_status",
        ),
        CheckConstraint(
            "triggered_by IN ('cron', 'manual', 'admin')",
            name="ck_job_runs_triggered_by",
        ),
        # The Runbook always asks "latest run per job", so that is the read path.
        Index("ix_job_runs_name_started", "job_name", started_at.desc()),
    )

    def __repr__(self):
        return (
            f"<JobRun(id={self.id}, job={self.job_name!r}, "
            f"status={self.status!r}, started_at={self.started_at})>"
        )
