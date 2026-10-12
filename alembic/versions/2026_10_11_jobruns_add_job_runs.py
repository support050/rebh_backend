"""add job_runs table

Records every scheduled / manual script execution so the admin Runbook can
answer "did it run, when, how long, how many rows, did it fail".

Model: app/models/job_run.JobRun

Idempotent: the table creation is guarded by an inspector check, matching the
pattern used by k1a2b3c4d5f0.

Revision ID: 2026_10_11_jobruns
Revises: 2026_10_06_cfgsma20_prec
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_11_jobruns"
down_revision: Union[str, Sequence[str], None] = "2026_10_06_cfgsma20_prec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("job_runs"):
        return

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_name", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("rows_written", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(length=16), nullable=False, server_default="cron"),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed')", name="ck_job_runs_status"),
        sa.CheckConstraint(
            "triggered_by IN ('cron', 'manual', 'admin')", name="ck_job_runs_triggered_by"),
    )
    op.create_index("ix_job_runs_job_name", "job_runs", ["job_name"])
    # The Runbook read path is "latest run per job". Raw SQL because a DESC
    # component is not expressible through op.create_index on all backends.
    op.execute(
        "CREATE INDEX ix_job_runs_name_started "
        "ON job_runs (job_name, started_at DESC)"
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("job_runs"):
        return
    op.drop_index("ix_job_runs_name_started", table_name="job_runs")
    op.drop_index("ix_job_runs_job_name", table_name="job_runs")
    op.drop_table("job_runs")
