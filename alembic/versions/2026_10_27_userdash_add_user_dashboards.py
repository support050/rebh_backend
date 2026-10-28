"""add user_dashboards (CR3 Custom Dashboard persistence)

Client CR3 (§2, إصلاحات يوم 27): the ★ Custom Dashboard grids must survive
across devices, so the reference's localStorage `tb-custom` moves server-side:
table user_dashboards {user_id, name, items JSON, is_default, updated_at},
served by GET/PUT/DELETE /api/rebh/breadth/dashboards (protected router).

IDEMPOTENT and prod-safe, mirroring 2026_10_26_mktbreadth: creates the table
only when absent. user_prefs already exists and is NOT touched — breadth prefs
live namespaced under its `breadth` key (no schema change needed).

Revision ID: 2026_10_27_userdash
Revises: 2026_10_26_mktbreadth
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "2026_10_27_userdash"
down_revision: Union[str, Sequence[str], None] = "2026_10_26_mktbreadth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("user_dashboards"):
        return

    op.create_table(
        "user_dashboards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("items", JSONB(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_dashboards_user_name"),
    )
    op.create_index("ix_user_dashboards_user_id", "user_dashboards", ["user_id"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("user_dashboards"):
        return
    op.drop_index("ix_user_dashboards_user_id", table_name="user_dashboards")
    op.drop_table("user_dashboards")
