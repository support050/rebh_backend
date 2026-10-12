"""breadth engine tables (15): drop prototype + create from app.models.breadth

Revision ID: breadth001
Revises: j1a2b3c4d5e9
Create Date: 2026-10-01

Owns only breadth_* tables. up() removes any pre-existing prototype versions
(they were empty test artifacts created outside alembic) and recreates the 15
tables exactly as the SQLAlchemy models define them.
"""
from alembic import op
import sqlalchemy as sa

import app.models.breadth as bm  # registers tables on Base.metadata

revision = "breadth001"
down_revision = "j1a2b3c4d5e9"
branch_labels = None
depends_on = None

_TABLES = sorted([t for name, t in bm.Base.metadata.tables.items()
                  if name.startswith("breadth_")], key=lambda t: t.name)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for t in _TABLES:
        if t.name in existing:
            op.drop_table(t.name)
        t.create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for t in reversed(_TABLES):
        if t.name in existing:
            op.drop_table(t.name)
