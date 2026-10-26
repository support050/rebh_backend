"""register market_breadth under Alembic

The legacy MA-breadth table (model app/models/market_breadth.py, read by the
/stocks/market-breadth page via routes/market_breadth.py, written by raw SQL in
scripts/update_daily_market_breadth.py) was never in Base.metadata, so a fresh
environment had to create it by hand. The model is now imported in
app/models/__init__.py, and this migration brings the table under Alembic.

It is IDEMPOTENT and prod-safe: it creates the table ONLY when absent, so on
production (where market_breadth already exists, ~6.4k rows back to 2002) it is
a no-op. The declared shape mirrors the LIVE table exactly — date PK + four
Numeric(5,2) nullable percentages, and NO secondary index (the live table has
none; the date PK covers the ORDER BY date the API uses).

Revision ID: 2026_10_26_mktbreadth
Revises: 2026_10_26_macro_period_date
Create Date: 2026-10-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_26_mktbreadth"
down_revision: Union[str, Sequence[str], None] = "2026_10_26_macro_period_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Prod-safe: the table pre-exists everywhere it matters. Only a fresh DB
    # reaches the create path. Never ALTER the existing table.
    if sa.inspect(op.get_bind()).has_table("market_breadth"):
        return

    op.create_table(
        "market_breadth",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("pct_above_20", sa.Numeric(5, 2), nullable=True),
        sa.Column("pct_above_50", sa.Numeric(5, 2), nullable=True),
        sa.Column("pct_above_150", sa.Numeric(5, 2), nullable=True),
        sa.Column("pct_above_200", sa.Numeric(5, 2), nullable=True),
        sa.PrimaryKeyConstraint("date"),
    )


def downgrade() -> None:
    # Only drop a table this migration created; never touch a pre-existing one.
    if not sa.inspect(op.get_bind()).has_table("market_breadth"):
        return
    op.drop_table("market_breadth")
