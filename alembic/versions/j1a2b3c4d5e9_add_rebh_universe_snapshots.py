"""add rebh_universe_snapshots table

Revision ID: j1a2b3c4d5e9
Revises: b9e4d2f3a6c7
Create Date: 2026-09-29

Pre-computed DB-backed universe snapshot powering fast /api/rebh/universe and
/api/rebh/peers reads (no per-request full-market recompute).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "j1a2b3c4d5e9"
down_revision: Union[str, Sequence[str], None] = "b9e4d2f3a6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rebh_universe_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("batch_id", sa.String(length=50), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("sector", sa.String(length=255), nullable=True),
        sa.Column("px", sa.Float(), nullable=True),
        sa.Column("mc", sa.Float(), nullable=True),
        sa.Column("pe", sa.Float(), nullable=True),
        sa.Column("pb", sa.Float(), nullable=True),
        sa.Column("roe", sa.Float(), nullable=True),
        sa.Column("nm", sa.Float(), nullable=True),
        sa.Column("de", sa.Float(), nullable=True),
        sa.Column("g_net", sa.Float(), nullable=True),
        sa.Column("fcf_yield", sa.Float(), nullable=True),
        sa.Column("f_score", sa.Integer(), nullable=True),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_date", "symbol", name="uq_universe_snapshot_date_symbol"),
    )
    op.create_index("ix_rebh_universe_snapshots_snapshot_date", "rebh_universe_snapshots", ["snapshot_date"])
    op.create_index("ix_rebh_universe_snapshots_batch_id", "rebh_universe_snapshots", ["batch_id"])
    op.create_index("ix_rebh_universe_snapshots_symbol", "rebh_universe_snapshots", ["symbol"])
    op.create_index("ix_rebh_universe_snapshots_sector", "rebh_universe_snapshots", ["sector"])
    op.create_index("ix_rebh_universe_snapshots_is_latest", "rebh_universe_snapshots", ["is_latest"])
    op.create_index("ix_universe_latest_sector", "rebh_universe_snapshots", ["is_latest", "sector"])


def downgrade() -> None:
    op.drop_index("ix_universe_latest_sector", table_name="rebh_universe_snapshots")
    op.drop_index("ix_rebh_universe_snapshots_is_latest", table_name="rebh_universe_snapshots")
    op.drop_index("ix_rebh_universe_snapshots_sector", table_name="rebh_universe_snapshots")
    op.drop_index("ix_rebh_universe_snapshots_symbol", table_name="rebh_universe_snapshots")
    op.drop_index("ix_rebh_universe_snapshots_batch_id", table_name="rebh_universe_snapshots")
    op.drop_index("ix_rebh_universe_snapshots_snapshot_date", table_name="rebh_universe_snapshots")
    op.drop_table("rebh_universe_snapshots")
