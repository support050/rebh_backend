"""add weekly_market_reports table

The table backing app/models/weekly_market_report.py (used by
/api/weekly-market-update/* and app/services/weekly_report/persistence.py) was
never created through a migration: the model was not imported in
app/core/database.py, so it was invisible to Base.metadata and to autogenerate.

Idempotent: every statement uses IF NOT EXISTS, because the table may already
exist in some environments (it was created by hand or by an old create_all run).

Revision ID: 2026_10_12_weekly
Revises: 2026_10_11_jobruns
Create Date: 2026-10-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_12_weekly"
down_revision: Union[str, Sequence[str], None] = "2026_10_11_jobruns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_market_reports (
            id           SERIAL,
            week_start   DATE                     NOT NULL,
            week_end     DATE                     NOT NULL,
            week_label   VARCHAR(120),
            report_data  JSON                     NOT NULL,
            generated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            CONSTRAINT pk_weekly_market_reports PRIMARY KEY (id)
        )
        """
    )
    # week_end is the lookup key for every read path (latest / by week) and the
    # natural upsert target — one unique index serves both, so the extra
    # non-unique ix_weekly_reports_week_end declared in the model is NOT created.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_weekly_market_reports_week_end
        ON weekly_market_reports (week_end)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_weekly_market_reports_week_start
        ON weekly_market_reports (week_start)
        """
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("weekly_market_reports"):
        return
    op.drop_index("ix_weekly_market_reports_week_start",
                  table_name="weekly_market_reports")
    op.drop_index("ix_weekly_market_reports_week_end",
                  table_name="weekly_market_reports")
    op.drop_table("weekly_market_reports")
