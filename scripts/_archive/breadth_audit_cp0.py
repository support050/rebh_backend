# -*- coding: utf-8 -*-
"""CP-0 Audit (READ-ONLY): Market Breadth data readiness.

Checks:
  CP-0.1  Price continuity around corporate-action ex-dates (adjusted vs raw)
  CP-0.2  Coverage: prices since 2001, historical_reports (TASI) since 2007
  CP-0.3  Symbol format + delisted companies kept in history
  CP-0.4  industry_group = Tadawul 24 groups
No writes, no DDL. Safe to re-run.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from sqlalchemy import create_engine, text
from app.core.config import settings

eng = create_engine(settings.DATABASE_URL)

def q(sql, **params):
    with eng.connect() as c:
        return c.execute(text(sql), params).fetchall()

print("=" * 70)
print("CP-0.2  COVERAGE")
print("=" * 70)
r = q("""SELECT min(date), max(date), count(distinct date), count(distinct symbol), count(*) FROM prices""")[0]
print(f"prices: {r[0]} -> {r[1]} | days={r[2]} symbols={r[3]} rows={r[4]}")
r = q("""SELECT min(report_date), max(report_date), count(*) FROM historical_reports""")[0]
print(f"historical_reports(TASI): {r[0]} -> {r[1]} | rows={r[2]}")

print("\n-- per-year row counts (prices) --")
for row in q("""SELECT extract(year from date)::int y, count(*), count(distinct symbol)
                FROM prices GROUP BY 1 ORDER BY 1"""):
    print(f"  {row[0]}: rows={row[1]:>10} symbols={row[2]:>4}")

print("\n-- missing years check (2001..latest) --")
years = {row[0] for row in q("SELECT extract(year from date)::int FROM prices GROUP BY 1")}
gap = [y for y in range(2001, max(years) + 1) if y not in years or (years.__contains__(y) and
       q("SELECT count(*) FROM prices WHERE extract(year from date)::int=:y", y=y)[0][0] < 20000)]
print(f"  suspicious/missing years: {gap if gap else 'NONE'}")

print("=" * 70)
print("CP-0.3  SYMBOL FORMAT & DELISTED")
print("=" * 70)
for row in q("""SELECT symbol, count(*), min(date), max(date) FROM prices
                GROUP BY symbol ORDER BY count(*) DESC LIMIT 5"""):
    print(f"  sample: {row}")
bad = q("""SELECT count(distinct symbol) FROM prices WHERE symbol !~ '^[0-9]{4}$'""")[0][0]
print(f"  symbols not pure 4-digit: {bad}")
for row in q("""SELECT DISTINCT symbol FROM prices WHERE symbol !~ '^[0-9]{4}$' LIMIT 20"""):
    print(f"    -> {row[0]}")
print("\n-- symbols whose history ended before today (delisted kept?) --")
for row in q("""SELECT symbol, max(date) md FROM prices GROUP BY symbol HAVING max(date) < current_date - 30
                ORDER BY md DESC LIMIT 15"""):
    print(f"  {row[0]} last seen {row[1]}")

print("=" * 70)
print("CP-0.4  INDUSTRY GROUPS")
print("=" * 70)
n = q("SELECT count(distinct industry_group) FROM prices WHERE industry_group IS NOT NULL")[0][0]
print(f"distinct industry_group values: {n}")
for row in q("""SELECT industry_group, count(distinct symbol) FROM prices
                WHERE industry_group IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 30"""):
    print(f"  {row[1]:>4} | {row[0]}")
n = q("SELECT count(distinct sector) FROM prices WHERE sector IS NOT NULL")[0][0]
print(f"distinct sector (GICS) values: {n}")

print("=" * 70)
print("CP-0.1  ADJUSTED vs RAW — continuity around corporate actions")
print("=" * 70)
print("-- corporate_actions table overview --")
for row in q("SELECT issue_type, count(*) FROM corporate_actions GROUP BY 1 ORDER BY 2 DESC"):
    print(f"  {row[1]:>5} | {row[0]}")
print("\n-- continuity test: latest 10 events (close before vs open on ex-day) --")
print("   ratio ~1.0 => ADJUSTED at source | ratio != 1 => RAW (gap visible)")
evs = q("""SELECT ca.symbol, ca.issue_type, ca.eligibility_date
           FROM corporate_actions ca
           WHERE ca.eligibility_date IS NOT NULL
             AND EXISTS (SELECT 1 FROM prices p WHERE p.symbol=ca.symbol AND p.date=ca.eligibility_date)
             AND EXISTS (SELECT 1 FROM prices p WHERE p.symbol=ca.symbol AND p.date < ca.eligibility_date)
           ORDER BY ca.eligibility_date DESC LIMIT 10""")
for symbol, itype, exd in evs:
    prev = q("""SELECT close FROM prices WHERE symbol=:s AND date < :d ORDER BY date DESC LIMIT 1""", s=symbol, d=exd)
    ex = q("""SELECT open, close FROM prices WHERE symbol=:s AND date=:d""", s=symbol, d=exd)
    if prev and ex and prev[0][0] and ex[0][0]:
        ratio = float(ex[0][0]) / float(prev[0][0])
        verdict = "ADJUSTED(smooth)" if 0.90 < ratio < 1.10 else f"RAW? gap ratio={ratio:.3f}"
        print(f"  {symbol} {itype[:20]:<20} ex={exd} prev_close={prev[0][0]:>10} ex_open={ex[0][0]:>10} -> {verdict}")
    else:
        print(f"  {symbol} {itype[:20]:<20} ex={exd} -> data missing for test")

print("\n-- same test on 10 OLD events (pre-2024) to find where adjustment starts --")
evs = q("""SELECT ca.symbol, ca.issue_type, ca.eligibility_date
           FROM corporate_actions ca
           WHERE ca.eligibility_date < '2024-01-01'
             AND EXISTS (SELECT 1 FROM prices p WHERE p.symbol=ca.symbol AND p.date=ca.eligibility_date)
           ORDER BY ca.eligibility_date DESC LIMIT 10""")
for symbol, itype, exd in evs:
    prev = q("""SELECT close FROM prices WHERE symbol=:s AND date < :d ORDER BY date DESC LIMIT 1""", s=symbol, d=exd)
    ex = q("""SELECT open FROM prices WHERE symbol=:s AND date=:d""", s=symbol, d=exd)
    if prev and ex and prev[0][0] and ex[0][0]:
        ratio = float(ex[0][0]) / float(prev[0][0])
        verdict = "ADJUSTED(smooth)" if 0.90 < ratio < 1.10 else f"RAW? gap ratio={ratio:.3f}"
        print(f"  {symbol} {itype[:20]:<20} ex={exd} prev_close={prev[0][0]:>10} ex_open={ex[0][0]:>10} -> {verdict}")
    else:
        print(f"  {symbol} {itype[:20]:<20} ex={exd} -> data missing for test")

print("\nDone. (read-only audit, nothing was modified)")
