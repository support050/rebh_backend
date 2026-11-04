"""add release_id / release_calendar_url to fred_series_metadata

The "Next Release Date" line on an indicator page comes from FRED's release
calendar, which is keyed by release id (rid 209 = ICE BofA Indices,
50 = Employment Situation, 180 = Unemployment Insurance Weekly Claims Report).
/fred/series does not expose that id, so it has to be resolved by the scraper
and persisted, together with the calendar URL FRED itself links to
(https://fred.stlouisfed.org/releases/calendar?rid=<id>&y=<year>).

IDEMPOTENT and prod-safe, mirroring 2026_10_31_fredmeta: adds the columns only
when the table exists and the column is absent, so a re-run is a no-op.

Revision ID: 2026_10_31_fredrel
Revises: 2026_10_31_fredmeta
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_31_fredrel"
down_revision: Union[str, Sequence[str], None] = "2026_10_31_fredmeta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = {
    "release_id": sa.Integer(),
    "release_calendar_url": sa.String(length=300),
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("fred_series_metadata"):
        return
    existing = {c["name"] for c in insp.get_columns("fred_series_metadata")}
    for name, type_ in _COLUMNS.items():
        if name not in existing:
            op.add_column("fred_series_metadata", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("fred_series_metadata"):
        return
    existing = {c["name"] for c in insp.get_columns("fred_series_metadata")}
    for name in reversed(list(_COLUMNS)):
        if name in existing:
            op.drop_column("fred_series_metadata", name)
