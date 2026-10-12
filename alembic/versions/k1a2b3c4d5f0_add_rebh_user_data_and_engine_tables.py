"""add REBH user-data & engine support tables

Covers the tables the REBH module's models declare but that had never been
brought under migration control (they were only created via
Base.metadata.create_all):

  * analyst_notes              (app/models/rebh_user_data.AnalystNote)
  * trade_journals             (app/models/rebh_user_data.TradeJournal)
  * user_watchlists            (app/models/rebh_user_data.UserWatchlist)
  * council_company_checklists (app/models/rebh_user_data.CouncilCompanyChecklist)
  * rebh_engine_vintages       (app/models/rebh_engine_vintage.RebhEngineVintage)
  * corporate_actions          (app/models/Corporate_actions.CorporateAction)

Idempotent by design: every table/index creation is guarded by an inspector
check, so this is safe on databases where create_all already made the tables.

Revision ID: k1a2b3c4d5f0
Revises: breadth002
Create Date: 2026-10-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k1a2b3c4d5f0"
down_revision: Union[str, Sequence[str], None] = "breadth002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_index(name: str, table: str) -> bool:
    insp = _inspector()
    if not insp.has_table(table):
        return False
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def _create_index(name: str, table: str, cols: list) -> None:
    if not _has_index(name, table):
        op.create_index(name, table, cols)


def upgrade() -> None:
    # ── analyst_notes ───────────────────────────────────────────────────────
    if not _has_table("analyst_notes"):
        op.create_table(
            "analyst_notes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "symbol", name="uq_analyst_note_user_symbol"),
        )
    _create_index("ix_analyst_notes_id", "analyst_notes", ["id"])
    _create_index("ix_analyst_notes_user_id", "analyst_notes", ["user_id"])
    _create_index("ix_analyst_notes_symbol", "analyst_notes", ["symbol"])

    # ── trade_journals ──────────────────────────────────────────────────────
    if not _has_table("trade_journals"):
        op.create_table(
            "trade_journals",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("trade_type", sa.String(length=10), nullable=False),
            sa.Column("shares", sa.Float(), nullable=False),
            sa.Column("buy_price", sa.Float(), nullable=False),
            sa.Column("sell_price", sa.Float(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("exit_reason", sa.Text(), nullable=True),
            sa.Column("trade_date", sa.String(length=30), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_trade_journals_id", "trade_journals", ["id"])
    _create_index("ix_trade_journals_user_id", "trade_journals", ["user_id"])
    _create_index("ix_trade_journals_symbol", "trade_journals", ["symbol"])

    # ── user_watchlists ─────────────────────────────────────────────────────
    if not _has_table("user_watchlists"):
        op.create_table(
            "user_watchlists",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("notes", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
        )
    _create_index("ix_user_watchlists_id", "user_watchlists", ["id"])
    _create_index("ix_user_watchlists_user_id", "user_watchlists", ["user_id"])
    _create_index("ix_user_watchlists_symbol", "user_watchlists", ["symbol"])

    # ── council_company_checklists ──────────────────────────────────────────
    if not _has_table("council_company_checklists"):
        op.create_table(
            "council_company_checklists",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("fisher_scores", sa.Text(), nullable=True),
            sa.Column("danger_flags", sa.Text(), nullable=True),
            sa.Column("red_flags", sa.Text(), nullable=True),
            sa.Column("bank_flags", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "symbol", name="uq_council_checklist_user_symbol"),
        )
    _create_index("ix_council_company_checklists_id", "council_company_checklists", ["id"])
    _create_index("ix_council_company_checklists_user_id", "council_company_checklists", ["user_id"])
    _create_index("ix_council_company_checklists_symbol", "council_company_checklists", ["symbol"])

    # ── rebh_engine_vintages ────────────────────────────────────────────────
    if not _has_table("rebh_engine_vintages"):
        op.create_table(
            "rebh_engine_vintages",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("batch_id", sa.String(length=50), nullable=True),
            sa.Column("vintage_date", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("as_of_period", sa.String(length=50), nullable=True),
            sa.Column("fresh", sa.Boolean(), nullable=True),
            sa.Column("quarantined", sa.Boolean(), nullable=True),
            sa.Column("quarantine_reason", sa.String(length=500), nullable=True),
            sa.Column("required_return_r", sa.Float(), nullable=True),
            sa.Column("gold_max", sa.Float(), nullable=True),
            sa.Column("silver_max", sa.Float(), nullable=True),
            sa.Column("bronze_max", sa.Float(), nullable=True),
            sa.Column("piotroski_score", sa.Integer(), nullable=True),
            sa.Column("engine_version", sa.String(length=50), nullable=True),
            sa.Column("contract_json", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("symbol", "batch_id", "as_of_period", name="uq_vintages_symbol_batch_period"),
        )
    _create_index("ix_rebh_engine_vintages_symbol", "rebh_engine_vintages", ["symbol"])
    _create_index("ix_rebh_engine_vintages_batch_id", "rebh_engine_vintages", ["batch_id"])
    _create_index("ix_rebh_engine_vintages_vintage_date", "rebh_engine_vintages", ["vintage_date"])
    _create_index("ix_vintages_symbol_date", "rebh_engine_vintages", ["symbol", "vintage_date"])

    # ── corporate_actions ───────────────────────────────────────────────────
    if not _has_table("corporate_actions"):
        op.create_table(
            "corporate_actions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("symbol", sa.String(length=10), nullable=False),
            sa.Column("company_name", sa.String(length=255), nullable=True),
            sa.Column("recommendation_announcement_date", sa.Date(), nullable=True),
            sa.Column("issue_type", sa.String(length=100), nullable=False),
            sa.Column("eligibility_date", sa.Date(), nullable=False),
            sa.Column("new_capital", sa.BigInteger(), nullable=True),
            sa.Column("previous_capital", sa.BigInteger(), nullable=True),
            sa.Column("classification", sa.String(length=20), nullable=False),
            sa.Column("processed", sa.Boolean(), nullable=True),
            sa.Column("detected_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("symbol", "issue_type", "eligibility_date", name="uq_corporate_action"),
        )
    _create_index("ix_corporate_actions_symbol", "corporate_actions", ["symbol"])


def downgrade() -> None:
    for table in (
        "corporate_actions",
        "rebh_engine_vintages",
        "council_company_checklists",
        "user_watchlists",
        "trade_journals",
        "analyst_notes",
    ):
        if _has_table(table):
            op.drop_table(table)
