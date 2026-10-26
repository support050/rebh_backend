"""
run_migration.py
================
يشغّل migration الجداول الجديدة للـ normalization pipeline.

يعمل الآتي بالترتيب:
  1. raw_financial_metrics    — staging table
  2. metric_label_mappings    — label → canonical key
  3. unmapped_labels          — review queue
  4. Indexes
  5. Triggers (updated_at + backfill)
  6. Views
  7. UNIQUE constraint على company_financial_metrics
  8. updated_at column على company_financial_metrics (لو مش موجود)

Usage:
    python run_migration.py
    python run_migration.py --check     # بس يشوف الحالة بدون تغيير
    python run_migration.py --rollback  # يحذف الجداول الجديدة بس (خطر!)
"""

import os
import sys
import argparse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # حاول تبني الـ URL من المكونات المنفصلة
    host     = os.getenv("DB_HOST",     "localhost")
    port     = os.getenv("DB_PORT",     "5432")
    name     = os.getenv("DB_NAME",     "")
    user     = os.getenv("DB_USER",     "")
    password = os.getenv("DB_PASSWORD", "")
    DATABASE_URL = f"postgresql://{user}:{password}@{host}:{port}/{name}"

# ---------------------------------------------------------------------------
# Migration SQL (مقسّم لـ statements منفصلة)
# ---------------------------------------------------------------------------

MIGRATION_STEPS = [

    # ── Step 1: raw_financial_metrics ────────────────────────────────────
    ("Create raw_financial_metrics", """
        CREATE TABLE IF NOT EXISTS raw_financial_metrics (
            id             BIGSERIAL PRIMARY KEY,
            company_symbol TEXT             NOT NULL,
            year           INTEGER          NOT NULL,
            period         TEXT             NOT NULL,
            raw_label      TEXT             NOT NULL,
            metric_value   DOUBLE PRECISION,
            metric_text    TEXT,
            section        TEXT,
            subsection     TEXT,
            block_code     TEXT,
            source_file    TEXT,
            extracted_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
            normalized     BOOLEAN          NOT NULL DEFAULT FALSE
        )
    """),

    ("Comment raw_financial_metrics", """
        COMMENT ON TABLE raw_financial_metrics IS
            'Staging table for XBRL extraction. Records move to company_financial_metrics after mapping.'
    """),

    # ── Step 2: metric_label_mappings ────────────────────────────────────
    ("Create metric_label_mappings", """
        CREATE TABLE IF NOT EXISTS metric_label_mappings (
            id             SERIAL PRIMARY KEY,
            raw_label      TEXT        NOT NULL,
            canonical_key  TEXT        NOT NULL,
            section        TEXT        NOT NULL
                CHECK (section IN (
                    'balance_sheet', 'income_statement', 'cash_flow',
                    'other_comprehensive_income', 'changes_in_equity',
                    'notes_to_accounts', 'filing_information',
                    'auditors_report', 'ratios', 'other'
                )),
            subsection     TEXT,
            company_symbol TEXT,
            confidence     FLOAT       NOT NULL DEFAULT 1.0
                CHECK (confidence BETWEEN 0.0 AND 1.0),
            verified       BOOLEAN     NOT NULL DEFAULT FALSE,
            verified_by    TEXT,
            verified_at    TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (raw_label, company_symbol)
        )
    """),

    # ── Step 3: unmapped_labels ──────────────────────────────────────────
    ("Create unmapped_labels", """
        CREATE TABLE IF NOT EXISTS unmapped_labels (
            id                  SERIAL PRIMARY KEY,
            raw_label           TEXT        NOT NULL,
            company_symbol      TEXT        NOT NULL,
            times_seen          INTEGER     NOT NULL DEFAULT 1,
            sample_file         TEXT,
            status              TEXT        NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'mapped', 'ignored')),
            resolved_mapping_id INTEGER REFERENCES metric_label_mappings (id),
            resolved_at         TIMESTAMPTZ,
            first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (raw_label, company_symbol)
        )
    """),

    # ── Step 4: Indexes ──────────────────────────────────────────────────
    ("Index: raw_fm_symbol",
        "CREATE INDEX IF NOT EXISTS idx_raw_fm_symbol ON raw_financial_metrics (company_symbol)"),

    ("Index: raw_fm_symbol_year_period",
        "CREATE INDEX IF NOT EXISTS idx_raw_fm_symbol_year_period ON raw_financial_metrics (company_symbol, year, period)"),

    ("Index: raw_fm_normalized (partial)",
        "CREATE INDEX IF NOT EXISTS idx_raw_fm_normalized ON raw_financial_metrics (normalized) WHERE normalized = FALSE"),

    ("Index: raw_fm_raw_label",
        "CREATE INDEX IF NOT EXISTS idx_raw_fm_raw_label ON raw_financial_metrics (raw_label)"),

    ("Index: mlm_raw_label_global",
        "CREATE INDEX IF NOT EXISTS idx_mlm_raw_label_global ON metric_label_mappings (raw_label) WHERE company_symbol IS NULL"),

    ("Index: mlm_raw_label_company",
        "CREATE INDEX IF NOT EXISTS idx_mlm_raw_label_company ON metric_label_mappings (raw_label, company_symbol) WHERE company_symbol IS NOT NULL"),

    ("Index: mlm_canonical_key",
        "CREATE INDEX IF NOT EXISTS idx_mlm_canonical_key ON metric_label_mappings (canonical_key)"),

    ("Index: mlm_section",
        "CREATE INDEX IF NOT EXISTS idx_mlm_section ON metric_label_mappings (section)"),

    ("Index: ul_status (partial)",
        "CREATE INDEX IF NOT EXISTS idx_ul_status ON unmapped_labels (status) WHERE status = 'pending'"),

    ("Index: ul_company",
        "CREATE INDEX IF NOT EXISTS idx_ul_company ON unmapped_labels (company_symbol)"),

    ("Index: ul_times_seen",
        "CREATE INDEX IF NOT EXISTS idx_ul_times_seen ON unmapped_labels (times_seen DESC)"),

    # ── Step 5: updated_at trigger function ──────────────────────────────
    ("Function: update_updated_at_column", """
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """),

    ("Trigger: mlm_updated_at",
        "DROP TRIGGER IF EXISTS trg_mlm_updated_at ON metric_label_mappings"),

    ("Trigger: mlm_updated_at (create)", """
        CREATE TRIGGER trg_mlm_updated_at
            BEFORE UPDATE ON metric_label_mappings
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
    """),

    # ── Step 6: Backfill trigger ─────────────────────────────────────────
    ("Function: backfill_normalized_metrics", """
        CREATE OR REPLACE FUNCTION backfill_normalized_metrics()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.verified = TRUE AND (OLD IS NULL OR OLD.verified = FALSE) THEN

                INSERT INTO company_financial_metrics
                    (company_symbol, year, period, metric_name,
                     metric_value, metric_text, label_en, source_file)
                SELECT
                    r.company_symbol, r.year, r.period,
                    NEW.canonical_key,
                    r.metric_value, r.metric_text, r.raw_label, r.source_file
                FROM raw_financial_metrics r
                WHERE r.raw_label = NEW.raw_label
                  AND (
                      NEW.company_symbol IS NULL
                      OR r.company_symbol = NEW.company_symbol
                  )
                  AND r.normalized = FALSE
                ON CONFLICT (company_symbol, year, period, metric_name)
                DO UPDATE SET
                    metric_value = EXCLUDED.metric_value,
                    metric_text  = EXCLUDED.metric_text,
                    updated_at   = NOW();

                UPDATE raw_financial_metrics
                SET    normalized = TRUE
                WHERE  raw_label = NEW.raw_label
                  AND (
                      NEW.company_symbol IS NULL
                      OR company_symbol = NEW.company_symbol
                  )
                  AND normalized = FALSE;

                UPDATE unmapped_labels
                SET    status               = 'mapped',
                       resolved_mapping_id  = NEW.id,
                       resolved_at          = NOW(),
                       last_seen_at         = NOW()
                WHERE  raw_label      = NEW.raw_label
                  AND  company_symbol = COALESCE(NEW.company_symbol, company_symbol)
                  AND  status         = 'pending';

            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """),

    ("Trigger: mlm_backfill (drop old)",
        "DROP TRIGGER IF EXISTS trg_mlm_backfill ON metric_label_mappings"),

    ("Trigger: mlm_backfill (create)", """
        CREATE TRIGGER trg_mlm_backfill
            AFTER INSERT OR UPDATE ON metric_label_mappings
            FOR EACH ROW EXECUTE FUNCTION backfill_normalized_metrics()
    """),

    # ── Step 7: Views ────────────────────────────────────────────────────
    ("View: v_unmapped_labels_pending", """
        CREATE OR REPLACE VIEW v_unmapped_labels_pending AS
        SELECT
            ul.id, ul.raw_label, ul.company_symbol,
            ul.times_seen, ul.sample_file,
            ul.first_seen_at, ul.last_seen_at,
            MODE() WITHIN GROUP (ORDER BY r.section) AS inferred_section
        FROM unmapped_labels ul
        LEFT JOIN raw_financial_metrics r
            ON  r.raw_label      = ul.raw_label
            AND r.company_symbol = ul.company_symbol
        WHERE ul.status = 'pending'
        GROUP BY ul.id, ul.raw_label, ul.company_symbol,
                 ul.times_seen, ul.sample_file,
                 ul.first_seen_at, ul.last_seen_at
        ORDER BY ul.times_seen DESC
    """),

    ("View: v_mapping_coverage", """
        CREATE OR REPLACE VIEW v_mapping_coverage AS
        SELECT
            company_symbol,
            COUNT(*)                                        AS total_raw_rows,
            COUNT(*) FILTER (WHERE normalized = TRUE)       AS normalized_rows,
            COUNT(*) FILTER (WHERE normalized = FALSE)      AS pending_rows,
            ROUND(
                COUNT(*) FILTER (WHERE normalized = TRUE)::NUMERIC
                / NULLIF(COUNT(*), 0) * 100, 1
            )                                               AS coverage_pct
        FROM raw_financial_metrics
        GROUP BY company_symbol
        ORDER BY coverage_pct
    """),

    # ── Step 8: Patch company_financial_metrics ───────────────────────────
    # updated_at column (لو مش موجود)
    ("Patch cfm: add updated_at column", """
        ALTER TABLE company_financial_metrics
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    """),

    # UNIQUE constraint (مطلوب للـ ON CONFLICT في normalize_all و الـ trigger)
    ("Patch cfm: unique constraint", """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_cfm_symbol_year_period_metric'
            ) THEN
                ALTER TABLE company_financial_metrics
                    ADD CONSTRAINT uq_cfm_symbol_year_period_metric
                    UNIQUE (company_symbol, year, period, metric_name);
            END IF;
        END
        $$
    """),

    # trigger لـ updated_at على company_financial_metrics
    ("Patch cfm: updated_at trigger (drop old)",
        "DROP TRIGGER IF EXISTS trg_cfm_updated_at ON company_financial_metrics"),

    ("Patch cfm: updated_at trigger (create)", """
        CREATE TRIGGER trg_cfm_updated_at
            BEFORE UPDATE ON company_financial_metrics
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
    """),
]

# ---------------------------------------------------------------------------
# Check query — يتحقق إيه اللي موجود بالفعل
# ---------------------------------------------------------------------------

CHECK_QUERIES = [
    ("raw_financial_metrics",    "SELECT to_regclass('public.raw_financial_metrics')"),
    ("metric_label_mappings",    "SELECT to_regclass('public.metric_label_mappings')"),
    ("unmapped_labels",          "SELECT to_regclass('public.unmapped_labels')"),
    ("v_unmapped_labels_pending","SELECT to_regclass('public.v_unmapped_labels_pending')"),
    ("v_mapping_coverage",       "SELECT to_regclass('public.v_mapping_coverage')"),
    ("cfm updated_at column",    """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'company_financial_metrics' AND column_name = 'updated_at'
    """),
    ("cfm unique constraint",    """
        SELECT conname FROM pg_constraint
        WHERE conname = 'uq_cfm_symbol_year_period_metric'
    """),
    ("backfill trigger",         """
        SELECT tgname FROM pg_trigger
        WHERE tgname = 'trg_mlm_backfill'
    """),
]

# ---------------------------------------------------------------------------
# Rollback (حذف الجداول الجديدة بس — لا يمس company_financial_metrics)
# ---------------------------------------------------------------------------

ROLLBACK_STEPS = [
    "DROP VIEW  IF EXISTS v_unmapped_labels_pending CASCADE",
    "DROP VIEW  IF EXISTS v_mapping_coverage         CASCADE",
    "DROP TABLE IF EXISTS unmapped_labels            CASCADE",
    "DROP TABLE IF EXISTS metric_label_mappings      CASCADE",
    "DROP TABLE IF EXISTS raw_financial_metrics      CASCADE",
    "DROP FUNCTION IF EXISTS backfill_normalized_metrics() CASCADE",
    # update_updated_at_column ممكن تكون مستخدمة في triggers تانية — مش بنحذفها
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def run_check(conn):
    cur = conn.cursor()
    print("\n📋 حالة الجداول الحالية:")
    print("─" * 50)
    all_ok = True
    for name, query in CHECK_QUERIES:
        try:
            cur.execute(query)
            result = cur.fetchone()
            exists = result and result[0] is not None
            status = "✅ موجود" if exists else "❌ مش موجود"
            if not exists:
                all_ok = False
        except Exception as e:
            status = f"⚠️  خطأ: {str(e)[:40]}"
            all_ok = False
        print(f"  {status:<20} {name}")
    print()
    if all_ok:
        print("✅ كل الجداول والـ constraints موجودة.")
    else:
        print("⚠️  في حاجات ناقصة — شغّل بدون --check لإنشاؤها.")
    cur.close()


def run_migration(conn):
    cur = conn.cursor()
    print("\n🚀 بدء الـ migration...\n")
    success = 0
    failed  = 0

    for name, sql in MIGRATION_STEPS:
        try:
            cur.execute(sql)
            print(f"  ✅ {name}")
            success += 1
        except Exception as e:
            err = str(e).strip().replace('\n', ' ')[:80]
            print(f"  ⚠️  {name}")
            print(f"      {err}")
            failed += 1

    print(f"\n{'─'*50}")
    print(f"✅ نجح   : {success}")
    print(f"⚠️  فيه خطأ: {failed}")

    if failed == 0:
        print("\n🎉 Migration اتنفذت بنجاح كامل!")
        print("\n📌 الخطوة الجاية:")
        print("   python scripts/generate_all_companies_json.py")
    else:
        print("\n⚠️  بعض الـ steps فشلت.")
        print("   ممكن تكون بعض الـ objects موجودة بالفعل (IF NOT EXISTS بيتعامل معاها تلقائياً).")
        print("   افحص الأخطاء فوق — لو كلها 'already exists' الأمور تمام.")

    cur.close()


def run_rollback(conn):
    cur = conn.cursor()
    print("\n🗑️  ROLLBACK — حذف الجداول الجديدة...\n")
    confirm = input("⚠️  هتحذف raw_financial_metrics و metric_label_mappings و unmapped_labels.\n"
                    "   اكتب 'yes' للتأكيد: ")
    if confirm.strip().lower() != 'yes':
        print("ألغيت.")
        return

    for sql in ROLLBACK_STEPS:
        try:
            cur.execute(sql)
            name = sql.split("IF EXISTS")[-1].strip().split()[0]
            print(f"  🗑️  Dropped: {name}")
        except Exception as e:
            print(f"  ⚠️  {sql[:60]} → {str(e)[:60]}")

    print("\n✅ Rollback اتنفذ.")
    cur.close()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="XBRL Normalization Migration Runner")
    parser.add_argument("--check",    action="store_true", help="بس شوف الحالة بدون تغيير")
    parser.add_argument("--rollback", action="store_true", help="احذف الجداول الجديدة (خطر!)")
    args = parser.parse_args()

    if not DATABASE_URL or "://:@" in DATABASE_URL:
        print("❌ مفيش DATABASE_URL في الـ .env")
        print("   أضف: DATABASE_URL=postgresql://user:password@host:5432/dbname")
        sys.exit(1)

    try:
        conn = get_connection()
        print(f"✅ اتوصل للـ database بنجاح")
    except Exception as e:
        print(f"❌ فشل الاتصال بالـ database: {e}")
        sys.exit(1)

    try:
        if args.check:
            run_check(conn)
        elif args.rollback:
            run_rollback(conn)
        else:
            run_check(conn)
            print("\n" + "─" * 50)
            run_migration(conn)
            print()
            run_check(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()