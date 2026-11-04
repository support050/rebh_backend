"""add cme_fedwatch_meeting + cme_fedwatch_dotplot

Follows 2026_10_31_fedwatch. The scraper now replays every QuikStrike control
via its ASP.NET meeting/view postback, which exposes two datasets the previous
one-snapshot model could not hold:

  cme_fedwatch_meeting  - one row per FOMC meeting (all nine tabs), carrying
                          the scraped Meeting Information + Ease/No Change/Hike
                          for THAT meeting instead of only the nearest one.
  cme_fedwatch_dotplot  - the "Dot Plot - Table" grid: FOMC participants'
                          year-end rate projections (Target Rate x year -> count
                          + the tool's own heat colour).

IDEMPOTENT and prod-safe: create_table only when absent, so a re-run is a no-op.

Revision ID: 2026_11_01_fedwatch2
Revises: 2026_10_31_fedwatch
Create Date: 2026-11-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2026_11_01_fedwatch2"
down_revision: Union[str, Sequence[str], None] = "2026_10_31_fedwatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("cme_fedwatch_meeting", "cme_fedwatch_dotplot")


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    if not insp.has_table("cme_fedwatch_meeting"):
        op.create_table(
            "cme_fedwatch_meeting",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scrape_date", sa.Date(), nullable=False),
            sa.Column("meeting_date", sa.Date(), nullable=False),
            sa.Column("meeting_label", sa.String(length=40), nullable=True),
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
            sa.UniqueConstraint("scrape_date", "meeting_date",
                                name="uq_fedwatch_meeting_date_meeting"),
        )
        op.create_index("ix_fedwatch_meeting_scrape_date", "cme_fedwatch_meeting", ["scrape_date"])
        op.create_index("ix_fedwatch_meeting_meeting_date", "cme_fedwatch_meeting", ["meeting_date"])

    if not insp.has_table("cme_fedwatch_dotplot"):
        op.create_table(
            "cme_fedwatch_dotplot",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scrape_date", sa.Date(), nullable=False),
            sa.Column("target_rate", sa.Numeric(precision=8, scale=3), nullable=False),
            sa.Column("year_label", sa.String(length=20), nullable=False),
            sa.Column("year_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("count", sa.Integer(), nullable=True),
            sa.Column("bg_color", sa.String(length=20), nullable=True),
            sa.Column("is_median", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=True),
            sa.UniqueConstraint("scrape_date", "target_rate", "year_label",
                                name="uq_fedwatch_dotplot_date_rate_year"),
        )
        op.create_index("ix_fedwatch_dotplot_scrape_date", "cme_fedwatch_dotplot", ["scrape_date"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for name in reversed(_TABLES):
        if insp.has_table(name):
            op.drop_table(name)
