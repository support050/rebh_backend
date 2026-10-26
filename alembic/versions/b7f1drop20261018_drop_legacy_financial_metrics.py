"""drop legacy financial metrics tables

Removes the orphaned legacy extractor tables (XBRL investigation §9):
``company_financial_metrics``, ``financial_metric_categories`` and
``company_metric_display_settings``. Their only consumer router
(``financial_metrics_management``) was never registered in ``main.py`` and
the backing SQLAlchemy model classes had already been deleted, so these
tables are dead write-only surface.

Supersedes: 435c4faa7110 (company_financial_metrics) and
            f1a2b3c4d5e6 (categories + display settings + column alters).

Revision ID: b7f1drop20261018
Revises: 2026_10_12_weekly
Create Date: 2026-10-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7f1drop20261018'
down_revision: Union[str, Sequence[str], None] = '2026_10_12_weekly'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the three legacy tables in FK-safe order (children first).
    Uses IF EXISTS so missing indexes or pre-dropped artifacts don't crash.
    """
    # 1. company_metric_display_settings
    op.execute("DROP INDEX IF EXISTS ix_company_metric_display_settings_metric_name")
    op.execute("DROP INDEX IF EXISTS ix_company_metric_display_settings_company_symbol")
    op.execute("DROP TABLE IF EXISTS company_metric_display_settings CASCADE")

    # 2. company_financial_metrics
    op.execute("DROP INDEX IF EXISTS ix_company_financial_metrics_year")
    op.execute("DROP INDEX IF EXISTS ix_company_financial_metrics_period")
    op.execute("DROP INDEX IF EXISTS ix_company_financial_metrics_metric_name")
    op.execute("DROP INDEX IF EXISTS ix_company_financial_metrics_id")
    op.execute("DROP INDEX IF EXISTS ix_company_financial_metrics_company_symbol")
    op.execute("DROP TABLE IF EXISTS company_financial_metrics CASCADE")

    # 3. financial_metric_categories
    op.execute("DROP INDEX IF EXISTS ix_financial_metric_categories_subsection")
    op.execute("DROP INDEX IF EXISTS ix_financial_metric_categories_section")
    op.execute("DROP INDEX IF EXISTS ix_financial_metric_categories_metric_name")
    op.execute("DROP TABLE IF EXISTS financial_metric_categories CASCADE")


def downgrade() -> None:
    """Faithfully recreate the three tables (mirrors 435c4faa7110 + f1a2b3c4d5e6)."""
    # financial_metric_categories (parent first)
    op.create_table(
        'financial_metric_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('metric_name', sa.String(length=150), nullable=False),
        sa.Column('section', sa.String(length=50), nullable=False),
        sa.Column('subsection', sa.String(length=100), nullable=True),
        sa.Column('description_en', sa.String(length=500), nullable=True),
        sa.Column('description_ar', sa.String(length=500), nullable=True),
        sa.Column('unit', sa.String(length=50), server_default='SAR', nullable=True),
        sa.Column('display_order', sa.Integer(), server_default='0', nullable=True),
        sa.Column('is_key_metric', sa.Boolean(), server_default='false', nullable=True),
        sa.Column('is_calculated', sa.Boolean(), server_default='false', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_financial_metric_categories')),
        sa.UniqueConstraint('metric_name', name=op.f('uq_financial_metric_categories_metric_name')),
    )
    op.create_index(op.f('ix_financial_metric_categories_metric_name'), 'financial_metric_categories', ['metric_name'], unique=False)
    op.create_index(op.f('ix_financial_metric_categories_section'), 'financial_metric_categories', ['section'], unique=False)
    op.create_index(op.f('ix_financial_metric_categories_subsection'), 'financial_metric_categories', ['subsection'], unique=False)

    # company_metric_display_settings
    op.create_table(
        'company_metric_display_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_symbol', sa.String(length=20), nullable=False),
        sa.Column('metric_name', sa.String(length=150), nullable=False),
        sa.Column('is_visible', sa.Boolean(), server_default='true', nullable=True),
        sa.Column('custom_display_order', sa.Integer(), nullable=True),
        sa.Column('custom_display_label', sa.String(length=255), nullable=True),
        sa.Column('custom_unit', sa.String(length=50), nullable=True),
        sa.Column('notes', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['company_symbol'], ['companies.symbol'], name=op.f('fk_company_metric_display_settings_company_symbol_companies')),
        sa.ForeignKeyConstraint(['metric_name'], ['financial_metric_categories.metric_name'], name=op.f('fk_company_metric_display_settings_metric_name_financial_metric_categories')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_company_metric_display_settings')),
        sa.UniqueConstraint('company_symbol', 'metric_name', name=op.f('uix_company_metric_display')),
    )
    op.create_index(op.f('ix_company_metric_display_settings_company_symbol'), 'company_metric_display_settings', ['company_symbol'], unique=False)
    op.create_index(op.f('ix_company_metric_display_settings_metric_name'), 'company_metric_display_settings', ['metric_name'], unique=False)

    # company_financial_metrics (post f1a2b3c4d5e6 final shape)
    financialperiod = postgresql.ENUM('ANNUAL', 'Q1', 'Q2', 'Q3', 'Q4', 'UNKNOWN', name='financialperiod', create_type=False)
    financialperiod.create(op.get_bind(), checkfirst=True)
    op.create_table(
        'company_financial_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_symbol', sa.String(length=20), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('period', financialperiod, nullable=False),
        sa.Column('metric_name', sa.String(length=150), nullable=False),
        sa.Column('metric_value', sa.Float(), nullable=True),
        sa.Column('metric_text', sa.String(), nullable=True),
        sa.Column('label_en', sa.String(length=500), nullable=True),
        sa.Column('source_file', sa.String(), nullable=True),
        sa.Column('label_ar', sa.String(length=500), nullable=True),
        sa.Column('data_quality_score', sa.Float(), server_default='1.0', nullable=True),
        sa.Column('is_verified', sa.Boolean(), server_default='false', nullable=True),
        sa.Column('verification_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source_date', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['metric_name'], ['financial_metric_categories.metric_name'], name=op.f('fk_company_financial_metrics_metric_name_financial_metric_categories')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_symbol', 'year', 'period', 'metric_name', name='uix_financial_metric'),
    )
    op.create_index(op.f('ix_company_financial_metrics_company_symbol'), 'company_financial_metrics', ['company_symbol'], unique=False)
    op.create_index(op.f('ix_company_financial_metrics_id'), 'company_financial_metrics', ['id'], unique=False)
    op.create_index(op.f('ix_company_financial_metrics_metric_name'), 'company_financial_metrics', ['metric_name'], unique=False)
    op.create_index(op.f('ix_company_financial_metrics_period'), 'company_financial_metrics', ['period'], unique=False)
    op.create_index(op.f('ix_company_financial_metrics_year'), 'company_financial_metrics', ['year'], unique=False)
