import os, re
from dotenv import load_dotenv
load_dotenv(".env")
import psycopg2
url = os.environ["DATABASE_URL"].replace("postgresql+psycopg2://", "postgresql://")
conn = psycopg2.connect(url)
cur = conn.cursor()

print("== job_runs: the three local runs (id > 243) ==")
cur.execute("""SELECT id, job_name, status, rows_written, started_at, finished_at,
                      triggered_by, error_message
               FROM job_runs WHERE id > 243 ORDER BY id""")
for r in cur.fetchall():
    print("  ", r)

print("\n== sukuk_market_data state ==")
cur.execute("""SELECT COUNT(*), MAX(as_of), COUNT(*) FILTER (WHERE is_active),
                      COUNT(*) FILTER (WHERE NOT is_active)
               FROM sukuk_market_data""")
print("  rows / max_as_of / active / retired:", cur.fetchone())
cur.execute("SELECT symbol, as_of FROM sukuk_market_data WHERE NOT is_active")
print("  retired symbols:", cur.fetchall())
cur.execute("SELECT COUNT(*) FROM sukuk_market_data WHERE parent_company_symbol IS NULL OR parent_company_symbol=''")
print("  parent-NULL rows:", cur.fetchone()[0])
cur.execute("""SELECT symbol, COUNT(*) FROM sukuk_market_data
               GROUP BY 1, COALESCE(parent_company_symbol,'') HAVING COUNT(*)>1""")
print("  duplicate groups:", cur.fetchall() or "NONE")

print("\n== economic_indicators freshness (gurufocus keys) ==")
for code in ("SP500_EY", "SP500_PE"):
    cur.execute("""SELECT MAX(report_date), COUNT(*) FROM economic_indicators
                   WHERE indicator_code=%s""", (code,))
    print(f"  {code}: max_date / rows =", cur.fetchone())
cur.execute("""SELECT report_date, value FROM economic_indicators
               WHERE indicator_code='SP500_EY' ORDER BY report_date DESC LIMIT 3""")
print("  SP500_EY last 3:", cur.fetchall())
cur.execute("""SELECT report_date, value FROM economic_indicators
               WHERE indicator_code='SP500_PE' ORDER BY report_date DESC LIMIT 3""")
print("  SP500_PE last 3:", cur.fetchall())

conn.close()
print("\nAUDIT DONE")
