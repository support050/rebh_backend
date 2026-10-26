"""saudi_economic_indicators: period varchar -> DATE (+ basis, canonical source)

WHY THIS FILE EXISTS
--------------------
``saudi_economic_indicators`` was never in Alembic's history: it was created at
runtime by scripts/SAMA&GaStat.py through ``__table__.create(checkfirst=True)``.
Audited live on 2026-10-26 (read-only): 19 rows / 7 keys, and the only
business-time column, ``period``, is ``varchar(50)`` holding six incompatible
shapes in those 19 rows:

    '2024-GaStat'    6 rows   a fallback STAMP, not a period
    '2024-Q4-GaStat' 1 row    fallback stamp, quarter form
    '2025 Q2'        1 row    real GaStat quarter, SPACE not hyphen
    '2025-Q4-TTM'    2 rows    real KAPSARC TTM quarter
    '2026-09'        5 rows
    '2026-10'        4 rows

The last two groups are the loudest bug: SAMA&GaStat.py stamps every rate with
``_CURRENT_PERIOD`` — the month *we ran the script*, never the month SAMA
published. Measured against the live bulletin (sheet 5-6 of the Aug-2026 issue):
row 'saibor_3m / 2026-09 / 3.905' is the **2026-08-31** observation, and
'saibor_3m / 2026-10 / 3.897' is a hard-coded fallback constant that is really
the **2026-07-31** value. So the newest row in the table is a stale July number
labelled October, and GET /api/rebh/market-macro (DISTINCT ON indicator_key,
ORDER BY coalesce(updated_at, retrieved_at) DESC) serves exactly that.

``UNIQUE (indicator_key, period, source)`` cannot protect this: ``source`` is
free text and drifted to two spellings ('SAMA' 1 row, 'SAMA <em-dash> Monthly
Bulletin' 12 rows), so repo_rate/2026-09/4.25 exists twice (ids 8 and 9) and the
constraint is satisfied because the source strings differ.

WHAT THIS MIGRATION DOES
------------------------
1. adds ``basis varchar(20)`` — the GDP derivation in app/api/routes/rebh_engine.py
   used to regex the period string ('2025-Q4-TTM') to tell TTM from
   Annualized; once period is a DATE that information has to live somewhere.
2. canonicalises ``source`` (LIKE 'SAMA%' -> 'SAMA Monthly Bulletin') so the
   composite key means what it says. ASCII spelling chosen deliberately.
3. deletes the 15 rows for the four SAMA rate keys + the Buffett ratio. Those
   are re-derivable in bulk and better: the very bulletin the script already
   downloads carries 236 MONTHLY observations (2007-01 .. 2026-08) for Repo,
   Reverse Repo and the SAIBOR tenors, and the old code kept ONE of them
   (``s.iloc[-1]``). The rewritten scraper writes all 236 with true month-end
   dates. Keeping scrape-stamped rows next to real ones would only re-pollute
   the series, so they go.
4. converts the 4 remaining rows (GDP level, unemployment) into real dates with
   the rules below and records their basis.
5. swaps the column: ``period varchar(50)`` -> ``period date NOT NULL`` and
   recreates UNIQUE (indicator_key, period, source).

    'YYYY-MM'          -> first of that month           basis Monthly
    'YYYY QN' / 'YYYY-QN[-x]' -> that quarter's end     basis Quarterly
                         ('...-TTM' keeps basis TTM)
    'YYYY-<label>' / 'YYYY'   -> 31 Dec of that year    basis Annual

After this migration the Market Monitor Saudi panel shows repo/SAIBOR as null
with an explicit "degraded" entry until the scraper is re-run — that is the
honest state, and it is strictly better than the stale-number-as-today state it
replaces. Run scripts/SAMA&GaStat.py (or daily_market_update.py) right after.

NOTE FOR THE OPERATOR
---------------------
The live database is at ``2026_10_12_weekly`` while the file chain head is
``2026_10_25_drop_unwritten``, whose parent ``b7f1drop20261018`` is destructive
and unapplied (company_financial_metrics 809,019 rows, financial_metric_categories
5,653, company_metric_display_settings 8). So "alembic upgrade head" must NOT be
used to apply this file. scripts/migrate_macro_period_date.py runs these same
statements directly against the database; every one of them is guarded, so a
later "alembic upgrade" re-running this revision is a no-op.

Revision ID: 2026_10_26_macro_period_date
Revises: 2026_10_25_drop_unwritten
Create Date: 2026-10-26
"""
from typing import List, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_26_macro_period_date"
down_revision: Union[str, Sequence[str], None] = "2026_10_25_drop_unwritten"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "saudi_economic_indicators"
UQ_NAME = "uq_indicator_period_source"

# Keys whose whole history the rewritten scraper re-creates from the bulletin,
# so their scrape-stamped legacy rows are dropped instead of guessed at.
REWRITTEN_KEYS = ("repo_rate", "reverse_repo_rate", "saibor_3m", "saibor_12m",
                  "saudi_buffett_indicator")
_IN_KEYS = "(" + ", ".join(f"'{k}'" for k in REWRITTEN_KEYS) + ")"

SOURCE_SAMA = "SAMA Monthly Bulletin"
# The scraper writes the unemployment series from the Labour Force Survey under
# this ASCII spelling; the table held the em-dash variant 'GaStat — Labour Force
# Survey', which would otherwise slip past ON CONFLICT and duplicate each row.
SOURCE_GASTAT_LFS = "GaStat Labour Force Survey"

STEPS: List[str] = [
    # 1 — basis, the TTM / Annualized / Quarterly / Monthly / Annual distinction.
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS basis VARCHAR(20)",

    # 2 — Delete re-derivable legacy rows FIRST.
    #     The table has pre-existing duplicate rows (e.g. repo_rate 2026-09 under both 'SAMA'
    #     and 'SAMA Monthly Bulletin'). Deleting the rewritten keys first avoids UniqueViolation
    #     when unifying the source spelling.
    f"""
    DELETE FROM {TABLE}
     WHERE indicator_key IN {_IN_KEYS}
    """,

    # 3 — one spelling per provider for surviving rows.
    f"""
    UPDATE {TABLE}
       SET source = '{SOURCE_SAMA}'
     WHERE indicator_key IN ('repo_rate', 'reverse_repo_rate', 'saibor_3m', 'saibor_12m')
       AND source LIKE 'SAMA%'
    """,

    # 3b — the Labour Force Survey spelling for surviving rows.
    f"""
    UPDATE {TABLE}
       SET source = '{SOURCE_GASTAT_LFS}'
     WHERE indicator_key = 'saudi_unemployment'
       AND source LIKE 'GaStat%Labour%'
    """,

    # 4 — new column, then map the surviving period text onto a real date.
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS period_new DATE",

    f"""
    UPDATE {TABLE}
       SET basis = CASE
             WHEN period ~* 'TTM'                        THEN 'TTM'
             WHEN period ~* 'Annualized'                 THEN 'Annualized'
             WHEN period ~ 'Q[1-4]'                      THEN 'Quarterly'
             WHEN period ~ '^[0-9]{{4}}-[0-9]{{2}}$'      THEN 'Monthly'
             ELSE 'Annual'
           END
     WHERE basis IS NULL
    """,

    f"""
    UPDATE {TABLE}
       SET period_new = CASE
             -- '2026-09' -> 2026-09-01
             WHEN period ~ '^[0-9]{{4}}-[0-9]{{2}}$'
               THEN make_date(left(period, 4)::int, substr(period, 6, 2)::int, 1)
             -- '2025-Q4-TTM' / '2025 Q2' / '2024-Q4-GaStat' -> quarter end
             WHEN period ~* 'Q[1-4]'
               THEN (date_trunc('quarter',
                       make_date(left(period, 4)::int,
                                 ((substring(period from 'Q([1-4])')::int - 1) * 3) + 1,
                                 1))
                     + interval '3 months' - interval '1 day')::date
             -- '2024-GaStat' / '2024' -> 31 Dec of that year
             WHEN period ~ '^[0-9]{{4}}'
               THEN make_date(left(period, 4)::int, 12, 31)
             ELSE NULL
           END
     WHERE period_new IS NULL
    """,

    # 5 — anything still unmapped has no business date at all; drop rather than
    #     invent one (zero-substitution policy).
    f"DELETE FROM {TABLE} WHERE period_new IS NULL",

    f"ALTER TABLE {TABLE} ALTER COLUMN period_new SET NOT NULL",
    f"ALTER TABLE {TABLE} DROP COLUMN period",
    f"ALTER TABLE {TABLE} RENAME COLUMN period_new TO period",

    # 6 — the constraint, now actually meaningful: one row per indicator per
    #     observation date per provider.
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {UQ_NAME}",
    f"""
    ALTER TABLE {TABLE}
      ADD CONSTRAINT {UQ_NAME}
      UNIQUE (indicator_key, period, source)
    """,

    # 7 — history is read as a series, so give the range scans an index.
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_key_period "
    f"ON {TABLE} (indicator_key, period DESC)",
]

# Guard shared by upgrade() and the standalone runner scripts/migrate_macro_period_date.py.
def _already_applied(bind) -> bool:
    """True when period is already a DATE column — nothing left to do."""
    row = bind.execute(sa.text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = 'period' AND data_type = 'date'"
    ), {"t": TABLE}).scalar()
    return bool(row)


def apply_to(bind) -> List[str]:
    """Run every statement against `bind` (an Alembic op bind or a plain engine
    connection). Returns the statements that actually ran, so the standalone
    runner can print exactly what it did. Idempotent: a no-op once applied."""
    if _already_applied(bind):
        return []
    executed: List[str] = []
    for stmt in STEPS:
        sql = " ".join(stmt.split())
        bind.execute(sa.text(stmt))
        executed.append(sql)
    return executed


def upgrade() -> None:
    bind = op.get_bind()
    if _already_applied(bind):
        return
    for stmt in STEPS:
        op.execute(stmt)


def downgrade() -> None:
    """period back to varchar(50) holding an ISO label; basis stays (harmless).
    The deleted rate history is NOT restored — it never had a real period."""
    op.execute(
        f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {UQ_NAME}"
    )
    op.execute(f"DROP INDEX IF EXISTS ix_{TABLE}_key_period")
    op.execute(
        f"""
        ALTER TABLE {TABLE}
          ALTER COLUMN period TYPE VARCHAR(50)
          USING to_char(period, 'YYYY-MM-DD')
        """
    )
    op.execute(
        f"""
        ALTER TABLE {TABLE}
          ADD CONSTRAINT {UQ_NAME}
          UNIQUE (indicator_key, period, source)
        """
    )
