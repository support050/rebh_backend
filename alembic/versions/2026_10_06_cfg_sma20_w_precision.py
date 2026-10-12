"""widen stock_indicators.cfg_sma20_w to NUMERIC(12, 4)

cfg_sma20_w was created as NUMERIC(5, 2) and never written, so nobody noticed that
every sibling CFG column is NUMERIC(12, 4). We are about to start persisting it
(the value is already computed every run in indicators_data_service.py and thrown
away), and a CFG of RSI14 - RSI14[9] + SMA(RSI3,3) can exceed the ±999.99 range of
(5, 2) — that would raise "numeric field overflow" and abort the whole batch insert.

The column is 100% NULL today, so the type change rewrites no data. This mirrors
what scripts/fix_indicator_precision_v3.py did for the other indicator columns.

Revision ID: 2026_10_06_cfgsma20_prec
Revises: 2026_10_06_drop_legacy
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa


revision = "2026_10_06_cfgsma20_prec"
down_revision = "2026_10_06_drop_legacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "stock_indicators", "cfg_sma20_w",
        existing_type=sa.Numeric(precision=5, scale=2),
        type_=sa.Numeric(precision=12, scale=4),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "stock_indicators", "cfg_sma20_w",
        existing_type=sa.Numeric(precision=12, scale=4),
        type_=sa.Numeric(precision=5, scale=2),
        existing_nullable=True,
    )
