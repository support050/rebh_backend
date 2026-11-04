"""sukuk_market_data: legacy UNIQUE(symbol) -> composite (symbol, parent)

The model (app/models/sukuk_bonds.py) has declared
UniqueConstraint('symbol','parent_company_symbol', 'uq_sukuk_symbol_parent')
since the composite-key rewrite, but production was never migrated: it still
carries the older UNIQUE index on symbol alone (ix_sukuk_market_data_symbol)
and no composite constraint at all. scripts/Sukuk&Bonds.py looks rows up by
the composite key, so every INSERT of an already-listed symbol died on the
legacy index (job_runs id=231, 2026-11-04: "duplicate key value violates
unique constraint ix_sukuk_market_data_symbol", 0 of 63 seed rows written —
while the ledger said success).

This migration aligns production with the model:
  1. verifies no duplicate (symbol, parent) pairs exist — refuses loudly if
     any do, rather than crashing upgrade later;
  2. replaces UNIQUE(symbol) with the plain index the model's index=True
     implies;
  3. adds the composite UNIQUE constraint 'uq_sukuk_symbol_parent'.

IDEMPOTENT and prod-safe: every step checks the live schema first.

Revision ID: 2026_11_04_sukuk01
Revises: 2026_11_03_treasury_upd
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2026_11_04_sukuk01"
down_revision: Union[str, Sequence[str], None] = "2026_11_03_treasury_upd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "sukuk_market_data"
_SYMBOL_INDEX = "ix_sukuk_market_data_symbol"
_UQ = "uq_sukuk_symbol_parent"


def _indexes(bind):
    return {i["name"]: i for i in sa.inspect(bind).get_indexes(_TABLE)}


def _unique_constraints(bind):
    return {c["name"] for c in sa.inspect(bind).get_unique_constraints(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()

    # 0. Pre-flight: the composite key must already be unique in the data.
    #    COALESCE keeps NULL-parent rows in the same bucket the Python
    #    upsert uses (_sukuk_key normalises None to ''), so the migration
    #    refuses exactly the rows the code would have duplicated.
    dupes = bind.execute(sa.text(
        "SELECT symbol, COALESCE(parent_company_symbol, '') AS parent, COUNT(*) "
        f"FROM {_TABLE} GROUP BY 1, 2 HAVING COUNT(*) > 1"
    )).fetchall()
    if dupes:
        raise RuntimeError(
            "sukuk_market_data holds duplicate (symbol, parent) rows; resolve "
            f"them before this constraint can exist: "
            f"{[(d[0], d[1], d[2]) for d in dupes]}")

    # 1. Legacy UNIQUE(symbol) -> plain index (the model's index=True shape).
    idx = _indexes(bind)
    if _SYMBOL_INDEX in idx and idx[_SYMBOL_INDEX].get("unique"):
        op.drop_index(_SYMBOL_INDEX, table_name=_TABLE)
        op.create_index(_SYMBOL_INDEX, _TABLE, ["symbol"], unique=False)

    # 2. The composite constraint the model already declares.
    if _UQ not in _unique_constraints(bind):
        op.create_unique_constraint(_UQ, _TABLE,
                                    ["symbol", "parent_company_symbol"])


def downgrade() -> None:
    bind = op.get_bind()

    if _UQ in _unique_constraints(bind):
        op.drop_constraint(_UQ, _TABLE, type_="unique")

    idx = _indexes(bind)
    if _SYMBOL_INDEX in idx and not idx[_SYMBOL_INDEX].get("unique"):
        op.drop_index(_SYMBOL_INDEX, table_name=_TABLE)
        idx.pop(_SYMBOL_INDEX)
    if _SYMBOL_INDEX not in idx:
        op.create_index(_SYMBOL_INDEX, _TABLE, ["symbol"], unique=True)
