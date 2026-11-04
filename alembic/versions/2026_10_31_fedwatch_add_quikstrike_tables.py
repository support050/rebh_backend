"""add the three CME FedWatch tables behind the "Current" and "Probabilities" views

The QuikStrike tool exposes more than the conditional matrix already stored in
cme_fedwatch. Reproducing the two official screens needs:

  cme_fedwatch_snapshot  - the "Current" header: Meeting Information
                           (date/contract/expiry/mid price/prior volume/prior OI),
                           the Ease / No Change / Hike aggregates, the current
                           target range and the chart's own axis labels.
  cme_fedwatch_horizon   - the "Target Rate (bps) / Probability(%)" table, i.e.
                           every bucket across Now / 1 Day / 1 Week / 1 Month.
  cme_fedwatch_futures   - the "Fed Fund Futures" ladder above the conditional
                           matrix, which is what lets a meeting tab resolve to a
                           contract + mid price without a click per tab.

IDEMPOTENT and prod-safe: create_table only when the table is absent, so a
re-run is a no-op.

Revision ID: 2026_10_31_fedwatch
Revises: 2026_10_31_fredrel
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_31_fedwatch"
down_revision: Union[str, Sequence[str], None] = "2026_10_31_fredrel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("cme_fedwatch_snapshot", "cme_fedwatch_horizon", "cme_fedwatch_futures")


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    if not insp.has_table("cme_fedwatch_snapshot"):
        op.create_table(
            "cme_fedwatch_snapshot",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scrape_date", sa.Date(), nullable=False),
            sa.Column("current_target_range", sa.String(length=20), nullable=True),
            sa.Column("chart_title", sa.String(length=120), nullable=True),
            sa.Column("chart_labels", sa.JSON(), nullable=True),
            sa.Column("meeting_label", sa.String(length=40), nullable=True),
            sa.Column("meeting_date", sa.Date(), nullable=True),
            sa.Column("contract", sa.String(length=10), nullable=True),
            sa.Column("expires", sa.String(length=30), nullable=True),
            sa.Column("mid_price", sa.Numeric(precision=10, scale=4), nullable=True),
            sa.Column("prior_volume", sa.BigInteger(), nullable=True),
            sa.Column("prior_oi", sa.BigInteger(), nullable=True),
            sa.Column("prob_ease", sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column("prob_no_change", sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column("prob_hike", sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column("data_as_of", sa.String(length=60), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=True),
            sa.UniqueConstraint("scrape_date", name="uq_fedwatch_snapshot_date"),
        )
        op.create_index("ix_fedwatch_snapshot_scrape_date", "cme_fedwatch_snapshot", ["scrape_date"])
        op.create_index("ix_fedwatch_snapshot_meeting_date", "cme_fedwatch_snapshot", ["meeting_date"])

    if not insp.has_table("cme_fedwatch_horizon"):
        op.create_table(
            "cme_fedwatch_horizon",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scrape_date", sa.Date(), nullable=False),
            sa.Column("meeting_date", sa.Date(), nullable=False),
            sa.Column("rate_range", sa.String(length=20), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("horizon", sa.String(length=10), nullable=False),
            sa.Column("horizon_label", sa.String(length=30), nullable=True),
            sa.Column("probability", sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=True),
            sa.UniqueConstraint("scrape_date", "meeting_date", "rate_range", "horizon",
                                name="uq_fedwatch_horizon_date_meeting_range_h"),
        )
        op.create_index("ix_fedwatch_horizon_scrape_date", "cme_fedwatch_horizon", ["scrape_date"])
        op.create_index("ix_fedwatch_horizon_meeting_date", "cme_fedwatch_horizon", ["meeting_date"])

    if not insp.has_table("cme_fedwatch_futures"):
        op.create_table(
            "cme_fedwatch_futures",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scrape_date", sa.Date(), nullable=False),
            sa.Column("contract", sa.String(length=10), nullable=False),
            sa.Column("price", sa.Numeric(precision=10, scale=4), nullable=True),
            sa.Column("contract_month", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=True),
            sa.UniqueConstraint("scrape_date", "contract", name="uq_fedwatch_futures_date_contract"),
        )
        op.create_index("ix_fedwatch_futures_scrape_date", "cme_fedwatch_futures", ["scrape_date"])
        op.create_index("ix_fedwatch_futures_contract", "cme_fedwatch_futures", ["contract"])
        op.create_index("ix_fedwatch_futures_contract_month", "cme_fedwatch_futures", ["contract_month"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for name in reversed(_TABLES):
        if insp.has_table(name):
            op.drop_table(name)
