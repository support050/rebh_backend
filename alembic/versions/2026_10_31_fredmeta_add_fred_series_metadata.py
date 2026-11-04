"""add fred_series_metadata (persisted FRED series metadata)

Phase 2 of the FRED integration: the indicator pages must render live
metadata (units, frequency, seasonal adjustment, latest observation,
last-updated stamp, next release date, source agency, FRED url) that is
PERSISTED per series, not a transient cache and not the old hardcoded
frontend indicatorMetadata.ts.

Table fred_series_metadata holds one row per indicator_code and is written by
app/scrapers/fred_scraper.py alongside the historical observations.

IDEMPOTENT and prod-safe, mirroring 2026_10_27_userdash / 2026_10_26_mktbreadth:
creates the table ONLY when absent, so on production (where it does not exist
yet) it creates it, and a re-run is a no-op. It never ALTERs or drops an
existing table on upgrade.

Revision ID: 2026_10_31_fredmeta
Revises: 2026_10_27_userdash
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_31_fredmeta"
down_revision: Union[str, Sequence[str], None] = "2026_10_27_userdash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Prod-safe: only create when absent; never touch a pre-existing table.
    if sa.inspect(op.get_bind()).has_table("fred_series_metadata"):
        return

    op.create_table(
        "fred_series_metadata",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("indicator_code", sa.String(length=50), nullable=False),
        sa.Column("series_id", sa.String(length=50), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("units", sa.String(length=100), nullable=True),
        sa.Column("frequency", sa.String(length=50), nullable=True),
        sa.Column("seasonal_adjustment", sa.String(length=50), nullable=True),
        sa.Column("source_agency", sa.String(length=200), nullable=True),
        sa.Column("fred_url", sa.String(length=300), nullable=True),
        sa.Column("latest_observation_date", sa.Date(), nullable=True),
        sa.Column("latest_observation_value", sa.Numeric(precision=15, scale=6), nullable=True),
        sa.Column("last_updated_time", sa.String(length=60), nullable=True),
        sa.Column("next_release_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("indicator_code", name="uq_fred_series_metadata_code"),
    )
    op.create_index("ix_fred_series_metadata_id", "fred_series_metadata", ["id"])
    op.create_index("ix_fred_series_metadata_indicator_code", "fred_series_metadata", ["indicator_code"])


def downgrade() -> None:
    # Only drop a table this migration created; never touch a pre-existing one.
    if not sa.inspect(op.get_bind()).has_table("fred_series_metadata"):
        return
    op.drop_index("ix_fred_series_metadata_indicator_code", table_name="fred_series_metadata")
    op.drop_index("ix_fred_series_metadata_id", table_name="fred_series_metadata")
    op.drop_table("fred_series_metadata")
