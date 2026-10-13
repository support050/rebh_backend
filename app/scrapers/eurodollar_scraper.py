"""
Eurodollar Futures Scraper — Investing.com [Cleaned v3]
=======================================================
الإصلاحات:
  ✅ تنظيف الكود القديم (إزالة Selenium و API البديلة التي لا تعمل).
  ✅ الاعتماد حصرياً على جلب HTML باستخدام requests + BeautifulSoup (الطريقة التي نجحت).
"""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date
from typing import Optional
import requests
from bs4 import BeautifulSoup

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("eurodollar_scraper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
PAGE_URL = "https://www.investing.com/rates-bonds/eurodollar-futures"

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
        "Gecko/20100101 Firefox/135.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.investing.com/",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def _f(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().rstrip("s").replace(",", "").lstrip("+")
    if s.upper() in ("N/A", "-", "", "NONE", "N/D"):
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _i(v) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s.upper() in ("N/A", "-", "", "NONE", "N/D"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None

# ─── Method: HTML Table Parsing ───────────────────────────────────────────────
def _fetch_via_html() -> Optional[list[dict]]:
    today = date.today().isoformat()
    logger.info("  محاولة جلب HTML عبر requests…")
    
    try:
        session = requests.Session()
        session.headers.update(HEADERS_BROWSER)
        session.get("https://www.investing.com/", timeout=20)
        time.sleep(1.5)
        resp = session.get(PAGE_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text
        logger.info(f"    HTTP {resp.status_code} | {len(html):,} حرف")
    except Exception as e:
        logger.warning(f"    requests فشل: {e}")
        return None

    if not html or len(html) < 100_000:
        logger.warning(f"  ⚠️  HTML قصير جداً ({len(html) if html else 0} حرف) — الصفحة تُعرض بـ JS")
        return None

    return _parse_html_table(html, today)

def _parse_html_table(html: str, today: str) -> Optional[list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    table = (
        soup.find("table", {"id": "cr1"}) or
        next(
            (t for t in soup.find_all("table")
             if "month" in t.get_text().lower() and "last" in t.get_text().lower()),
            None
        )
    )

    if not table:
        logger.error("    ❌ الجدول مش موجود في HTML")
        return None

    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")
    records = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        off = 1 if len(cells) >= 10 else 0
        month_cell = cells[off]
        contract = month_cell.get_text(strip=True)

        symbol = None
        link = month_cell.find("a")
        if link:
            href = link.get("href", "")
            if "symbol=" in href:
                symbol = href.split("symbol=")[-1].strip()

        i = off + 1
        last  = _f(cells[i].get_text(strip=True)) if len(cells) > i else None; i += 1
        chg   = _f(cells[i].get_text(strip=True)) if len(cells) > i else None; i += 1
        open_ = _f(cells[i].get_text(strip=True)) if len(cells) > i else None; i += 1
        high  = _f(cells[i].get_text(strip=True)) if len(cells) > i else None; i += 1
        low   = _f(cells[i].get_text(strip=True)) if len(cells) > i else None; i += 1
        vol   = _i(cells[i].get_text(strip=True)) if len(cells) > i else None; i += 1
        upd   = cells[i].get_text(strip=True)     if len(cells) > i else None

        if not contract or last is None:
            continue
        if contract.lower() in ("month", "contract", "name", "symbol"):
            continue

        records.append({
            "scrape_date": today,  "symbol": symbol,
            "contract": contract,  "last_price": last,
            "change": chg,         "open_price": open_,
            "high": high,          "low": low,
            "previous": None,      "volume": vol,
            "open_interest": None, "updated_time": upd,
        })

    logger.info(f"    📊 {len(records)} عقد مستخرج")
    return records if len(records) >= 3 else None

# ─── Output ───────────────────────────────────────────────────────────────────
def save_csv(records: list[dict], path: str) -> bool:
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        logger.info(f"💾 CSV محفوظ: {path} ({len(records)} صف)")
        return True
    except Exception as e:
        logger.error(f"❌ خطأ CSV: {e}")
        return False

def save_json(records: list[dict], path: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 JSON محفوظ: {path} ({len(records)} سجل)")
        return True
    except Exception as e:
        logger.error(f"❌ خطأ JSON: {e}")
        return False

def print_table(records: list[dict]) -> None:
    if not records:
        return
    w = 110
    print("\n" + "═" * w)
    print(
        f"  {'Symbol':<8} {'Contract':<12} {'Last':>9} {'Chg':>8} "
        f"{'Open':>9} {'High':>9} {'Low':>9} {'Volume':>9} {'Time':>10}"
    )
    print("─" * w)
    for r in records:
        chg = r.get("change") or 0
        sign = "+" if chg > 0 else ""
        chg_str = f"{sign}{chg:.4f}" if chg != 0 else "0.0000"
        print(
            f"  {str(r.get('symbol') or ''):8} "
            f"{str(r['contract']):<12} "
            f"{str(r['last_price'] or ''):>9} "
            f"{chg_str:>8} "
            f"{str(r['open_price'] or ''):>9} "
            f"{str(r['high'] or ''):>9} "
            f"{str(r['low'] or ''):>9} "
            f"{str(r['volume'] or 0):>9} "
            f"{str(r['updated_time'] or ''):>10}"
        )
    print("═" * w)
    print(f"  ✅ {len(records)} عقد | تاريخ السحب: {records[0]['scrape_date']}\n")

def upsert_db(records: list[dict]) -> bool:
    try:
        from app.core.database import SessionLocal
        from app.models.economic_indicators import EurodollarFutures
    except ImportError:
        logger.warning("⚠️  DB models مش موجودة — تم تخطي DB")
        return False

    db = SessionLocal()
    try:
        ins = upd = 0
        for rec in records:
            existing = (
                db.query(EurodollarFutures)
                .filter(
                    EurodollarFutures.scrape_date == rec["scrape_date"],
                    EurodollarFutures.contract == rec["contract"],
                )
                .first()
            )
            if existing:
                for k, v in rec.items():
                    if k not in ("scrape_date", "contract"):
                        setattr(existing, k, v)
                upd += 1
            else:
                db.add(EurodollarFutures(**rec))
                ins += 1
        db.commit()
        logger.info(f"💾 DB: {ins} جديد | {upd} محدث")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"❌ خطأ DB: {e}")
        return False
    finally:
        db.close()

# ─── Main Entry Point ─────────────────────────────────────────────────────────
def scrape_eurodollar_futures(
    output_csv: Optional[str] = None,
    output_json: Optional[str] = None,
    save_db: bool = False,
    print_results: bool = True,
    force: bool = False,
) -> dict:
    """Returns {"ok": bool, "rows": n} for job_tracking (the old Optional[list]
    return was neither bool nor dict, so rows_written stayed NULL)."""
    logger.info("=" * 60)
    logger.info("🚀 Eurodollar Futures Scraper [Cleaned v3]")
    logger.info(f"   التاريخ: {date.today()}")
    logger.info("=" * 60)

    # ── فحص إذا تم السحب اليوم مسبقاً ──
    if save_db and not force:
        try:
            from app.core.database import SessionLocal as _SL
            from app.models.economic_indicators import EurodollarFutures as _EF
            _db = _SL()
            try:
                existing_count = _db.query(_EF).filter(
                    _EF.scrape_date == date.today()
                ).count()
                if existing_count > 0:
                    logger.info(f"ℹ️ تم السحب مسبقاً اليوم ({date.today()}). يوجد {existing_count} سجل. استخدم --force لإعادة السحب.")
                    return {"ok": True, "rows": 0, "skipped": "already scraped today"}
            finally:
                _db.close()
        except ImportError:
            pass

    records = _fetch_via_html()

    if not records:
        logger.error("❌ فشل السحب!")
        return {"ok": False, "rows": 0, "stage_failures": [{
            "stage": "eurodollar",
            "error": "Investing.com served no parseable snapshot "
                     "(403-from-datacenter known failure mode)"}]}

    logger.info(f"\n✅ تم سحب {len(records)} عقد بنجاح!")

    if print_results:
        print_table(records)
    if output_csv:
        save_csv(records, output_csv)
    if output_json:
        save_json(records, output_json)
    db_ok = True
    if save_db:
        db_ok = upsert_db(records)

    out: dict = {"ok": bool(db_ok), "rows": len(records) if (save_db and db_ok) else 0}
    if save_db and not db_ok:
        out["stage_failures"] = [{
            "stage": "eurodollar_db",
            "error": f"DB upsert failed for {len(records)} parsed rows (see log)"}]
    return out

# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Eurodollar Futures Scraper [Cleaned v3]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE.csv",
        default=f"eurodollar_{date.today().isoformat()}.csv",
        help="مسار ملف CSV",
    )
    parser.add_argument(
        "--json", "-j",
        metavar="FILE.json",
        default=None,
        help="مسار ملف JSON (اختياري)",
    )
    parser.add_argument("--db",    action="store_true", help="حفظ في DB")
    parser.add_argument("--quiet", "-q", action="store_true", help="بدون طباعة")
    parser.add_argument("--force", action="store_true", help="إعادة السحب حتى لو تم السحب اليوم")

    args = parser.parse_args()
    result = scrape_eurodollar_futures(
        output_csv=args.output,
        output_json=args.json,
        save_db=args.db,
        print_results=not args.quiet,
        force=args.force,
    )
    sys.exit(0 if result.get("ok") else 1)