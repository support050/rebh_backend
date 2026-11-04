"""add updated_at to treasury_yield_curves

Follows 2026_11_01_fedwatch2. The treasury_gov upsert rewrites rows in place
(Treasury revises recent sessions), so created_at — the original insert stamp —
can never answer "when was this curve last fetched". The scraper now sets
updated_at inside ON CONFLICT DO UPDATE, and /yield-curve/summary exposes it.

IDEMPOTENT and prod-safe: the column is added only when absent.

Revision ID: 2026_11_03_treasury_upd
Revises: 2026_11_01_fedwatch2
Create Date: 2026-11-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2026_11_03_treasury_upd"
down_revision: Union[str, Sequence[str], None] = "2026_11_01_fedwatch2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("treasury_yield_curves")}
    if "updated_at" not in cols:
        op.add_column(
            "treasury_yield_curves",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("treasury_yield_curves")}
    if "updated_at" in cols:
        op.drop_column("treasury_yield_curves", "updated_at")
