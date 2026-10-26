"""drop qfi_ownership_flows + weekly_market_reports

Two tables that existed only as an API promise, removed on 2026-10-25:

``qfi_ownership_flows`` — declared in app/models/market_reports.py as "Phase 6",
read by GET /api/market-reports/qfi-flows and counted in the
/api/rebh/importers-status payload ("qfi_flows": 0). It had 0 rows since it was
created: no scraper in app/services/market_reports_scraper.py and no writer in
scripts/update_market_reports.py ever touched it, and no frontend page called
the endpoint. The model, response schema, endpoint and counter were deleted in
the same change. Rebuild it only together with a working reader — and note that
Tadawul publishes foreign ownership in aggregate, so per-symbol net inflow
(shares / SAR) would have to be derived rather than scraped.

``weekly_market_reports`` — created by 2026_10_12_weekly for the backend weekly
report system (app/models/weekly_market_report.py,
app/api/routes/weekly_market_update.py, app/services/weekly_report/). That
system was removed from the working tree during the uncommitted cleanup before
2026-10-25 (it still exists in git HEAD), leaving the table with 0 rows; the GET
route legitimately answered 404 "No weekly report generated yet. Run
generate_weekly_report.py first." whenever the table was empty. The owner
confirmed no weekly PDF report is wanted, so the last pieces still on disk were
deleted with this migration: scripts/generate_weekly_report.py, the
WeeklyMarketReport TS type, the Puppeteer PDF route
(app/api/generate-weekly-report/route.ts — the only consumer of the
puppeteer + pdf-lib deps) and the ?autoReport=true hook in
app/industry-groups/page.tsx.

Both statements use IF EXISTS because the tables may already be gone by hand.

NOTE FOR THE OPERATOR: this revision sits on top of b7f1drop20261018, which has
NOT been applied to production yet (live alembic_version = 2026_10_12_weekly).
That pending revision drops company_financial_metrics (809,019 rows),
financial_metric_categories (5,653) and company_metric_display_settings (8) —
so "alembic upgrade head" does far more than this file. If only these two empty
tables are wanted now, run the two DROP statements in downgrade() form by hand
and leave the chain for the legacy-metrics decision.

Revision ID: 2026_10_25_drop_unwritten
Revises: b7f1drop20261018
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "2026_10_25_drop_unwritten"
down_revision: Union[str, Sequence[str], None] = "b7f1drop20261018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DROP_ORDER = ("qfi_ownership_flows", "weekly_market_reports")


def upgrade() -> None:
    # Indexes and the unique constraints ride along with the table.
    for table in DROP_ORDER:
        op.execute("DROP TABLE IF EXISTS %s CASCADE" % table)


def downgrade() -> None:
    """Recreate both tables empty, exactly as they were before the drop."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_market_reports (
            id           SERIAL,
            week_start   DATE                     NOT NULL,
            week_end     DATE                     NOT NULL,
            week_label   VARCHAR(120),
            report_data  JSON                     NOT NULL,
            generated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            CONSTRAINT pk_weekly_market_reports PRIMARY KEY (id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_weekly_market_reports_week_end "
        "ON weekly_market_reports (week_end)"
    )

    op.create_table(
        "qfi_ownership_flows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("normalized_symbol", sa.String(length=20), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("qfi_holding_percent", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("total_foreign_percent", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("net_inflow_shares", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("net_inflow_sar", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("retrieval_timestamp", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("qfi_ownership_flows_pkey")),
        sa.UniqueConstraint("report_date", "symbol",
                            name="uq_qfi_ownership_date_symbol"),
    )
    for col in ("id", "report_date", "symbol", "normalized_symbol"):
        op.create_index("ix_qfi_ownership_flows_" + col, "qfi_ownership_flows", [col])
