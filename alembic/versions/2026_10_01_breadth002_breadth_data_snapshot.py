"""breadth_data snapshot table (rolling 14-day DATA JSONB)

Revision ID: breadth002
Revises: breadth001
Create Date: 2026-10-01

Creates ONLY breadth_data from the SQLAlchemy model — the 14 result tables and
breadth_runs from breadth001 are untouched (they hold real data now).
"""
from alembic import op
import sqlalchemy as sa

import app.models.breadth as bm  # registers tables on Base.metadata

revision = "breadth002"
down_revision = "breadth001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    t = bm.Base.metadata.tables["breadth_data"]
    if t.name not in set(sa.inspect(bind).get_table_names()):
        t.create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    if "breadth_data" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("breadth_data")
