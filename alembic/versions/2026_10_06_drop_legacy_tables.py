"""drop 5 legacy tables: income_statements, balance_sheets, cash_flows,
technical_indicators, calculation_checkpoint

Decision 2026-10-06 (user-approved after the read-only schema audit).

Why each one goes:
  * income_statements / balance_sheets / cash_flows — the financial statements are
    served entirely from the XBRL JSON store (app/services/xbrl_data_service.py
    reads local output/*.json and Cloudflare R2); nothing in the app ever read or
    wrote these three Postgres tables. All held 0 rows since creation.
    app/models/financials.py and app/schemas/financials.py are removed alongside.
  * technical_indicators — 0 rows, 35 columns, NO SQLAlchemy model, its router was
    commented out in app/api/routes/__init__.py while the module never existed.
    It only polluted every schema dump and alembic --autogenerate diff.
  * calculation_checkpoint — scratch state of the one-off backfill helper
    scripts/calculate_rs_final_precise.py, which CREATEs it itself when run
    (CREATE TABLE IF NOT EXISTS), so dropping the empty leftover is safe.

Verified before writing this migration (scripts/_pre_drop_safety.py):
  all five tables = 0 rows, no inbound FK, no outbound FK, no dependent VIEW.

downgrade() recreates the ORIGINAL structure exactly (from the live catalogue dump);
it can never restore data, and there was none.

Revision ID: 2026_10_06_drop_legacy
Revises: 2026_10_06_drop_naaim
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "2026_10_06_drop_legacy"
down_revision = "2026_10_06_drop_naaim"
branch_labels = None
depends_on = None


DROP_ORDER = [
    "income_statements",
    "balance_sheets",
    "cash_flows",
    "technical_indicators",
    "calculation_checkpoint",
]


def upgrade() -> None:
    # Every index (pkey + ix_*_symbol/country + the unique (symbol,date) on
    # technical_indicators) is dropped with its table.
    for name in DROP_ORDER:
        op.drop_table(name)


def _fin_common_columns():
    """Fresh Column objects per table — a sa.Column instance can only belong to one
    Table, so a shared module-level list would break downgrade."""
    return [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("country", sa.String(length=50), nullable=False),
        sa.Column("fiscal_date", sa.Date(), nullable=True),
        sa.Column("quarter", sa.String(length=10), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
    ]


def downgrade() -> None:
    # The *_id_seq sequences were owned by the dropped columns and went with them;
    # plain autoincrement recreates the serial default, so no nextval() text is used.
    op.create_table(
        "calculation_checkpoint",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("last_date", sa.Date(), nullable=True),
        sa.Column("updated_at", postgresql.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id", name="calculation_checkpoint_pkey"),
    )

    op.create_table(
        "technical_indicators",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("change", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_10", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_21", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_50", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_150", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_200", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("ema_21", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("ema_10", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_3", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("ema_20_sma3", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_4", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_9", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_18", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_4w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_9w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_18w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_30w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_40w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_200_1m_ago", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_200_2m_ago", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_200_3m_ago", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_200_4m_ago", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sma_200_5m_ago", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("cci_14", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("cci_ema_20", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("aroon_up", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("aroon_down", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("high_52w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("low_52w", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("from_52w_high", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("from_52w_low", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id", name="technical_indicators_pkey"),
        sa.UniqueConstraint("symbol", "date", name="technical_indicators_symbol_date_key"),
    )

    op.create_table(
        "cash_flows",
        *_fin_common_columns(),
        sa.Column("operating_activities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("investing_activities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("financing_activities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("net_cash_change", sa.Float(), nullable=True),
        sa.Column("free_cash_flow", sa.Float(), nullable=True),
        sa.Column("end_cash_position", sa.Float(), nullable=True),
        sa.Column("income_tax_paid", sa.Float(), nullable=True),
        sa.Column("interest_paid", sa.Float(), nullable=True),
        sa.Column("additional_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="cash_flows_pkey"),
    )
    op.create_index("ix_cash_flows_country", "cash_flows", ["country"], unique=False)
    op.create_index("ix_cash_flows_symbol", "cash_flows", ["symbol"], unique=False)

    op.create_table(
        "balance_sheets",
        *_fin_common_columns(),
        sa.Column("assets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("liabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("shareholders_equity", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("additional_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="balance_sheets_pkey"),
    )
    op.create_index("ix_balance_sheets_country", "balance_sheets", ["country"], unique=False)
    op.create_index("ix_balance_sheets_symbol", "balance_sheets", ["symbol"], unique=False)

    op.create_table(
        "income_statements",
        *_fin_common_columns(),
        sa.Column("revenue", sa.Float(), nullable=True),
        sa.Column("sales", sa.Float(), nullable=True),
        sa.Column("cost_of_goods", sa.Float(), nullable=True),
        sa.Column("gross_profit", sa.Float(), nullable=True),
        sa.Column("operating_expense", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("operating_income", sa.Float(), nullable=True),
        sa.Column("non_operating_interest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("other_income_expense", sa.Float(), nullable=True),
        sa.Column("pretax_income", sa.Float(), nullable=True),
        sa.Column("income_tax", sa.Float(), nullable=True),
        sa.Column("net_income", sa.Float(), nullable=True),
        sa.Column("net_income_continuous_operations", sa.Float(), nullable=True),
        sa.Column("minority_interests", sa.Float(), nullable=True),
        sa.Column("preferred_stock_dividends", sa.Float(), nullable=True),
        sa.Column("eps_basic", sa.Float(), nullable=True),
        sa.Column("eps_diluted", sa.Float(), nullable=True),
        sa.Column("basic_shares_outstanding", sa.Float(), nullable=True),
        sa.Column("diluted_shares_outstanding", sa.Float(), nullable=True),
        sa.Column("ebit", sa.Float(), nullable=True),
        sa.Column("ebitda", sa.Float(), nullable=True),
        sa.Column("additional_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="income_statements_pkey"),
    )
    op.create_index("ix_income_statements_country", "income_statements", ["country"], unique=False)
    op.create_index("ix_income_statements_symbol", "income_statements", ["symbol"], unique=False)
