-- verify_economy_rates.sql — READ ONLY. Nothing here writes.
-- Run against the live Render DB yourself (never the assistant):
--   cd d:\Work\LUMIVST\backend
--   # take DATABASE_URL from backend/.env, then:
--   & ".\venv\Scripts\psql.exe" "$env:DATABASE_URL" -f scripts\verify_economy_rates.sql
-- (or paste the queries into the Render shell / any SQL client)

-- 1) sp500_history — the Run Now log claimed "11 inserted". Expect the newest
--    trade_date to be the most recent US market close (Fri 2026-10-10 or later).
SELECT 'sp500_history' AS table_name,
       MAX(trade_date) AS latest_date,
       COUNT(*)        AS total_rows,
       COUNT(*) FILTER (WHERE trade_date >= CURRENT_DATE - 7) AS rows_last_7d
FROM sp500_history;

SELECT trade_date, close, pe_ratio
FROM sp500_history
ORDER BY trade_date DESC
LIMIT 12;

-- 2) treasury_yield_curves — the log claimed "7 updated". Expect the newest
--    report_date within ~2 business days of today.
SELECT 'treasury_yield_curves' AS table_name,
       MAX(report_date) AS latest_date,
       COUNT(*)         AS total_rows,
       COUNT(*) FILTER (WHERE report_date >= CURRENT_DATE - 7) AS rows_last_7d
FROM treasury_yield_curves;

SELECT report_date, year_2, year_10, year_30
FROM treasury_yield_curves
ORDER BY report_date DESC
LIMIT 8;

-- 3) economic_indicators BAMLC0A3CA — the log claimed "75 upserted".
SELECT 'economic_indicators:BAMLC0A3CA' AS table_name,
       MAX(report_date) AS latest_date,
       COUNT(*)         AS total_rows,
       COUNT(*) FILTER (WHERE report_date >= CURRENT_DATE - 7) AS rows_last_7d
FROM economic_indicators
WHERE indicator_code = 'BAMLC0A3CA';

SELECT report_date, value
FROM economic_indicators
WHERE indicator_code = 'BAMLC0A3CA'
ORDER BY report_date DESC
LIMIT 8;

-- 4) job_runs — the three Run Now jobs you fired. This is what the System Map
--    Rows column reads. runs before 2026-10-13 will show rows_written = NULL
--    (the bool-return bug); after the next deploy + a fresh Run Now they show
--    real numbers.
SELECT job_name, started_at, finished_at, status, triggered_by,
       rows_written, LEFT(COALESCE(error_message, ''), 120) AS error_head
FROM job_runs
WHERE job_name IN ('sp500', 'treasury_gov', 'fred:BAMLC0A3CA')
ORDER BY started_at DESC
LIMIT 15;

-- 5) Any red/failed economy runs at all (checks the honest-reporting path):
SELECT job_name, started_at, status, LEFT(COALESCE(error_message, ''), 160) AS error_head
FROM job_runs
WHERE status = 'failed'
  AND started_at >= CURRENT_DATE - 3
ORDER BY started_at DESC;

-- 6) Which economy job keys have NEVER been recorded (these are the tiles that
--    show "Bad / Never recorded" on the System Map). sukuk_importer /
--    macro_importer are listed too: the cron tiles were retired 2026-10-13 but
--    the standalone scripts still record those keys on manual runs.
SELECT k.job_name AS expected_job, r.last_run, r.last_status
FROM (VALUES ('sp500'), ('treasury_gov'), ('fred:BAMLC0A3CA'), ('fred:BAMLC0A4CBBB'),
             ('fred:BAMLC0A3CAEY'), ('fred:IC4WSA'), ('fred:UNRATE'), ('fred:PAYEMS'),
             ('fred:SAUCPALTT01GPM'), ('sp500_pe'), ('sukuk_importer'), ('macro_importer'),
             ('manual:eurodollar'), ('manual:cme_fedwatch'),
             ('manual:gurufocus')) AS k(job_name)
LEFT JOIN LATERAL (
    SELECT started_at AS last_run, status AS last_status
    FROM job_runs jr
    WHERE jr.job_name = k.job_name
    ORDER BY jr.started_at DESC
    LIMIT 1
) r ON TRUE
ORDER BY r.last_run NULLS FIRST;
