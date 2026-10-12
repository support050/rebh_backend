"""drop naaim_exposure and naaim_metadata tables

The NAAIM Exposure Index feature was removed end-to-end:
scraper, API router, Pydantic schemas, SQLAlchemy models, scheduler job
(Thursdays 01:45) and the two frontend pages. This migration drops the
two tables that backed it.

Revision ID: 2026_10_06_drop_naaim
Revises: k1a2b3c4d5f0
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2026_10_06_drop_naaim"
down_revision = "k1a2b3c4d5f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ix_naaim_exposure_date / ix_naaim_exposure_id are dropped with the table.
    op.drop_table("naaim_exposure")
    op.drop_table("naaim_metadata")


def downgrade() -> None:
    """Recreates the two tables with their original structure (data is NOT restored)."""
    op.create_table(
        "naaim_exposure",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("naaim_index", sa.Float(), nullable=False),
        sa.Column("sp500", sa.Float(), nullable=True),
        sa.Column("bearish", sa.Float(), nullable=True),
        sa.Column("quartile_1", sa.Float(), nullable=True),
        sa.Column("quartile_2", sa.Float(), nullable=True),
        sa.Column("quartile_3", sa.Float(), nullable=True),
        sa.Column("bullish", sa.Float(), nullable=True),
        sa.Column("std_deviation", sa.Float(), nullable=True),
        sa.Column("yoy_pct", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_naaim_exposure_date", "naaim_exposure", ["date"], unique=True)
    op.create_index("ix_naaim_exposure_id", "naaim_exposure", ["id"], unique=False)

    op.create_table(
        "naaim_metadata",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("last_quarter_avg", sa.Float(), nullable=True),
        sa.Column("last_quarter_label", sa.String(length=50), nullable=True),
        sa.Column("posted_on", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
