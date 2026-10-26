"""
Eurodollar Futures Scraper — Investing.com [Scrapling v4]
=========================================================
الإصلاحات:
  ✅ تنظيف الكود القديم (إزالة Selenium و API البديلة التي لا تعمل).
  ✅ 2026-10-23: طبقة النقل أصبحت Scrapling (انتحال بصمة TLS للمتصفح مع تدوير
     المحركات) ثم تصعيد تلقائي إلى متصفح StealthyFetcher عند الحاجة، مع بقاء
     `requests` كملاذ أخير. السبب المقاس: investing.com يعيد 403 لـ requests
     العادي من عناوين datacenter، ويعيد 200 لبعض البصمات فقط (chrome131 كان
     403 بينما chrome120/chrome/safari180/firefox135/edge101 أعادت 200).
  ✅ موقع الجدول مرن: بحث برؤوس الأعمدة ("month" + "last") أولاً، ثم محددات
     Scrapling التكيفية (adaptive selectors) كطوق نجاة عند تغير الكلاسات.
  ⚠️ ملاحظة مقاسة: جدول العقود لا يظهر في استجابة HTTP عند بعض البصمات
     (الجدول يُبنى بالـ JS)، لذلك التصعيد للمتصفح ليس ترفاً بل هو المسار
     الأساسي عند فشل HTTP.

2026-10-26 — الحظر من عناوين datacenter (السطرة المعلقة عند 2026-06-30)
  1. solve_cloudflare مفعّل في مسار المتصفح، مع مهلة 120 ثانية (Scrapling نفسه
     يرفع الحد الأدنى إلى 60_000 م.ث عندما تكون الميزة تعمل — _validators).
  2. البروكسي اختياري عبر البيئة: EURODOLLAR_PROXY ثم SCRAPLING_PROXY ثم
     HTTPS_PROXY/ALL_PROXY/HTTP_PROXY، ويُمرَّر للقنوات الثلاث (HTTP والمتصفح
     وrequests). بدون بروكسي سكني/ISP يبقى الحظر قائماً — البروكسي Datacenter
     ينقل الـ 403 من عنوان لآخر فقط.
  3. مصدر احتياطي مقاس (انظر _fetch_yahoo_sofr): يعيد عقد SOFR الأقرب استحقاقاً
     فقط، ويُعلَّم في السجل كالتقاط ناقص حتى لا يُقرأ كمنظومة كاملة.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import date
from typing import Optional
import requests
from bs4 import BeautifulSoup

# Same convention as treasury_gov_scraper.py: make `app` importable when this
# file is executed directly (python app/scrapers/eurodollar_scraper.py), where
# sys.path[0] is the scrapers folder and `import app…` would otherwise fail.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from app.scrapers.scrapling_fetch import (
    PROXY_ENV_KEYS, adaptive_select, browser_get, http_get, resolve_proxy,
)

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

# اسم المتغير البيئي الأعلى أولوية في سلسلة resolve_proxy (EURODOLLAR_PROXY →
# SCRAPLING_PROXY → HTTPS_PROXY → ALL_PROXY → HTTP_PROXY).
PROXY_SCOPE = "EURODOLLAR"

# حلّ تحدي Cloudflare ليس تحميلاً سريعاً للصفحة: المهلة هنا تشمل وقت الانتظار
# داخل المتصفح للتحدي ثم تحميل الجدول، لذا 120 ثانية لا 30.
BROWSER_TIMEOUT_MS = int(os.getenv("EURODOLLAR_BROWSER_TIMEOUT_MS", "120000"))
# بلا حلّاء، الصفحة تُرسم عادة في ثوانٍ؛ إبقاء نفس المهلة الطويلة يضيّع وقت
# التشغيل عندما يكون المتصفح هو المسار الناجح.
BROWSER_TIMEOUT_PLAIN_MS = int(os.getenv("EURODOLLAR_BROWSER_TIMEOUT_PLAIN_MS", "60000"))

# الصفحة تنشر نحو 25 عقداً (قياس 2026-10-26: 25 من HTTP ببصمة chrome120).
# أقل من هذا الحد يعني جدولاً مبتوراً (تصفّح نصف مُحمَّل أو صفحة حظر رُدّت
# بـ 200) فيُجرَّب المصدر الاحتياطي بدلاً من حفظ لقطة ناقصة بصمت.
MIN_CONTRACTS = int(os.getenv("EURODOLLAR_MIN_CONTRACTS", "8"))

# المصدر الاحتياطي: عقود GE/SR الشهرية على Yahoo غير موجودة (مقاس:
# GEC26/GEX26/SRC26/SRC27/GE=F كلها 404) لأن عقد Eurodollar شُطِب في 2023،
# والرابط المستمر SR3=F هو كل ما يتوفر (سعر/فتح/عالي/واط/حجم ليوم واحد).
YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?range=1d&interval=1d"
)
YAHOO_SYMBOLS = ("SR3=F",)   # Three-Month SOFR Futures (المخلِّف المباشر لليورودولار)

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
def _acquire_page() -> tuple[Optional[str], Optional[object]]:
    """(html, scrapling_page) — cheapest transport that actually worked.

    Order: impersonated HTTP → stealth browser with the Cloudflare solver →
    stealth browser without it → legacy requests. The browser is only tried when
    HTTP fails or serves a page without the table, because the futures grid is
    JS-rendered on some responses (measured).

    Every channel carries the same optional proxy: a datacenter IP is blocked on
    reputation, not on fingerprint, so no amount of impersonation fixes it and
    the proxy is the only lever that can (see resolve_proxy in scrapling_fetch).
    """
    proxy = resolve_proxy(PROXY_SCOPE)
    logger.info(f"   proxy: {'مُفعّل — ' if proxy else 'غير مُضبوط (مباشر)؛ من datacenter يُرجَّح 403'}"
                + (proxy.split('@')[-1] if proxy else ''))

    # 1) Scrapling HTTP (rotating TLS fingerprints, see scrapling_fetch)
    res = http_get(PAGE_URL, referer="https://www.investing.com/",
                   proxy=proxy, timeout=45)
    if res is not None:
        html = _warm_and_fetch(res)
        if html is not None:
            return html, res.page

    # 2) Scrapling stealth browser + Cloudflare solver — the path that gets a
    #    datacenter IP through the Turnstile challenge. It is slow by design
    #    (BROWSER_TIMEOUT_MS), and "no challenge on this page" still returns
    #    the rendered HTML, so a solver attempt is never wasted work.
    bres = browser_get(PAGE_URL, wait_selector="table",
                       solve_cloudflare=True, timeout_ms=BROWSER_TIMEOUT_MS,
                       proxy=proxy)
    if bres is not None and _table_markup(bres.text) is not None:
        logger.info("    ✅ الجدول خرج من متصفح StealthyFetcher (+ حل Cloudflare)")
        return bres.text, bres.page

    # 3) Same browser without the solver: covers the case where the solver
    #    itself timed out on a challenge that was never really served (measured
    #    behaviour: it returns the page as-is after its own retries).
    bres = browser_get(PAGE_URL, wait_selector="table",
                       solve_cloudflare=False,
                       timeout_ms=BROWSER_TIMEOUT_PLAIN_MS,
                       proxy=proxy)
    if bres is not None and _table_markup(bres.text) is not None:
        logger.info("    ✅ الجدول خرج من متصفح StealthyFetcher (بلا حلّاء)")
        return bres.text, bres.page

    # 4) Legacy requests path (kept so an image without scraping still runs)
    try:
        session = requests.Session()
        session.headers.update(HEADERS_BROWSER)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        session.get("https://www.investing.com/", timeout=20)
        time.sleep(1.5)
        resp = session.get(PAGE_URL, timeout=30)
        resp.raise_for_status()
        logger.info(f"    HTTP {resp.status_code} | {len(resp.text):,} حرف (requests)")
        return resp.text, None
    except Exception as e:
        logger.warning(f"    جميع قنوات النقل فشلت: {e}")
        return None, None


def _warm_and_fetch(res) -> Optional[str]:
    """Accept an HTTP result only if it really carries the futures table."""
    if _table_markup(res.text) is not None:
        logger.info(f"    ✅ الجدول خرج من HTTP ({res.via}) — بدون متصفح")
        return res.text
    logger.info(f"    ⚠️ HTTP {res.status} ({len(res.text):,} حرف) بلا جدول — تصعيد للمتصفح")
    return None


def _table_markup(html: str) -> Optional[str]:
    t = _find_table(BeautifulSoup(html or "", "lxml"))
    return str(t) if t is not None else None


def _find_table(soup) -> Optional[object]:
    """Locate the contracts table by CONTENT, not by class names.

    Class-based lookup is what breaks when Investing.com ships a new bundle;
    header text ("month" + "last") is part of the data itself, so it survives
    restyling.
    """
    table = soup.find("table", {"id": "cr1"})
    if table is not None:
        return table
    return next(
        (t for t in soup.find_all("table")
         if "month" in t.get_text().lower() and "last" in t.get_text().lower()),
        None
    )


def _parse_html_table(html: str, today: str, page=None) -> Optional[list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    table = _find_table(soup)

    if table is None:
        # Rescue path: Scrapling adaptive selectors can relocate the table by
        # its stored fingerprint when the DOM/class names moved.
        for css, ident in (("table#cr1", "eurodollar_cr1"),
                           ("table", "eurodollar_grid")):
            hits = adaptive_select(page, css, ident)
            markup = getattr(hits[0], "html_content", "") if hits else ""
            if markup and "last" in markup.lower():
                logger.info(f"    ♻️ adaptive selector أعاد تحديد الجدول ({css})")
                table = _find_table(BeautifulSoup(markup, "lxml")) or BeautifulSoup(
                    markup, "lxml").find("table")
                if table is not None:
                    break

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
    if len(records) < MIN_CONTRACTS:
        logger.warning(
            f"    ⚠️ أقل من الحد الأدنى لعدد العقود "
            f"({len(records)} < {MIN_CONTRACTS}) — اللقطة مبتورة، "
            f"تُرفض ليُجرَّب المصدر الاحتياطي")
        return None
    return records

# ─── Method: Fallback source (Yahoo) ────────────────────────────────────────
def _contract_label(short_name: str, ref: date) -> str:
    """Build the 'Mon YY' Month label the grid shows (same shape as Investing).

    Yahoo labels the continuous contract inside `shortName`
    ("Three-Month SOFR Futures,Dec-20"), so that token is read straight out of
    the response. When it is missing, the nearest quarterly SOFR expiry
    (Mar/Jun/Sep/Dec — the same cycle Eurodollar used) is derived from `ref`,
    so the row still carries a real contract name instead of a blank.
    """
    m = re.search(r"([A-Za-z]{3})-(\d{2})", short_name or "")
    if m:
        return f"{m.group(1).title()} {m.group(2)}"
    months = {3: "Mar", 6: "Jun", 9: "Sep", 12: "Dec"}
    for year in (ref.year, ref.year + 1):
        for month in (3, 6, 9, 12):
            if date(year, month, 1) >= ref.replace(day=1):
                return f"{months[month]} {str(year)[-2:]}"
    return ref.strftime("%b %y")


def _fetch_yahoo_sofr(today: str) -> Optional[list[dict]]:
    """Fallback when investing.com is unreachable: nearest-expiry SOFR contract.

    Measured 2026-10-26: this is the ONLY futures quote Yahoo still serves —
    the per-contract codes (GEC26, SRC26, GE=F …) all answer 404 because
    Eurodollar was delisted in 2023. So the fallback deliberately returns one
    contract, not the ~25-row ladder, and the caller flags the snapshot as
    `partial` in job_runs; a thin-but-today row beats a full-but-frozen table.
    CME Group itself and Barchart were probed too and both refuse the request
    (403 "This IP address is blocked… web scraping") — hence Yahoo here.

    Yahoo answers 429 when the same IP hits the chart endpoint in a burst
    (measured, twice in a minute), so each symbol gets spaced retries.
    """
    out: list[dict] = []
    for symbol in YAHOO_SYMBOLS:
        q = _yahoo_quote(symbol)
        if q is None or q.get("last") is None:
            logger.warning(f"    Yahoo {symbol}: لا سعر في الاستجابة")
            continue

        prev = q.get("previous")
        out.append({
            "scrape_date": today,
            "symbol": symbol,
            "contract": _contract_label(q.get("short_name", ""), date.today()),
            "last_price": q["last"],
            "change": round(q["last"] - prev, 4) if prev else None,
            "open_price": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "previous": prev,
            "volume": q.get("volume"),
            "open_interest": None,
            "updated_time": f"Yahoo {symbol}",
        })

    if out:
        logger.info(f"    ♻️ مصدر احتياطي (Yahoo): {len(out)} عقد — لقطة ناقصة عمداً")
        return out
    logger.error("    ❌ المصدر الاحتياطي فشل أيضاً")
    return None


def _num(v) -> Optional[float]:
    """float() that traps both None and pandas NaN."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _yahoo_quote(symbol: str, attempts: int = 3) -> Optional[dict]:
    """One normalized SOFR quote: {last, open, high, low, volume, previous,
    short_name, via}, or None.

    Normalised on purpose: yfinance hands back a DataFrame and the chart
    endpoint a nested JSON payload, and the record builder must not care which
    door the number came in through.
    """
    return _yahoo_via_yfinance(symbol, attempts) or _yahoo_via_chart(symbol, attempts)


def _yahoo_via_yfinance(symbol: str, attempts: int) -> Optional[dict]:
    """yfinance owns the cookie/crumb handshake, so it hits Yahoo lightest."""
    try:
        import yfinance as yf
    except ImportError:
        logger.info("    yfinance غير مثبت — يُجرَّب رابط chart مباشرة")
        return None

    ticker = yf.Ticker(symbol)
    for i in range(attempts):
        try:
            hist = ticker.history(period="1d", interval="1d")
            if hist is not None and not hist.empty:
                row = hist.iloc[-1]
                vol = _num(row.get("Volume"))
                # fast_info carries the session close, which turns the row from
                # a price into a price + change (the grid's second column).
                try:
                    prev = _num(ticker.fast_info["previous_close"])
                except Exception:                   # noqa: BLE001 — optional field
                    prev = None
                return {
                    "last": _num(row.get("Close")), "open": _num(row.get("Open")),
                    "high": _num(row.get("High")), "low": _num(row.get("Low")),
                    "volume": int(vol) if vol is not None else None,
                    "previous": prev, "short_name": symbol, "via": "yfinance",
                }
        except Exception as e:
            logger.debug(f"    yfinance {symbol} attempt {i + 1}: "
                         f"{type(e).__name__}: {str(e)[:100]}")
        time.sleep(3 * (i + 1))
    return None


def _yahoo_via_chart(symbol: str, attempts: int) -> Optional[dict]:
    """Raw v8 chart endpoint. Yahoo answers 429 on bursts (measured), so the
    retry sleeps between attempts instead of hammering."""
    url = YAHOO_CHART_URL.format(symbol=symbol.replace("=", "%3D"))
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (i + 1)
                logger.warning(f"    Yahoo 429 لـ {symbol} — انتظار {wait}s ثم إعادة المحاولة")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            meta = result.get("meta") or {}
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]

            def _last(key):
                vals = [v for v in (quote.get(key) or []) if v is not None]
                return vals[-1] if vals else None

            vol = _num(_last("volume"))
            return {
                "last": _num(_last("close")) or _num(meta.get("regularMarketPrice")),
                "open": _num(_last("open")), "high": _num(_last("high")),
                "low": _num(_last("low")),
                "volume": int(vol) if vol is not None else None,
                "previous": _num(meta.get("chartPreviousClose")),
                "short_name": meta.get("shortName", ""), "via": "chart",
            }
        except Exception as e:
            logger.warning(f"    Yahoo {symbol}: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(3 * (i + 1))
    return None

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
    logger.info("🚀 Eurodollar Futures Scraper [Scrapling v4]")
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

    html, sl_page = _acquire_page()
    records = _parse_html_table(html, date.today().isoformat(), page=sl_page) if html else None
    partial = False

    if not records:
        # Every investing.com channel failed (or served a truncated grid). The
        # fallback is deliberately last: it is one contract, not the ladder.
        records = _fetch_yahoo_sofr(date.today().isoformat())
        partial = bool(records)

    if not records:
        logger.error("❌ فشل السحب!")
        return {"ok": False, "rows": 0, "stage_failures": [{
            "stage": "eurodollar",
            "error": "Investing.com served no parseable snapshot "
                     "(403-from-datacenter known failure mode) and the Yahoo "
                     "SOFR fallback returned nothing either"}]}

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
    if partial:
        # Honest bookkeeping: a 1-row SOFR snapshot is NOT the 25-row grid. It
        # lands in job_runs so /admin shows the run as degraded instead of
        # mistaking it for a normal day.
        out["partial"] = True
        out["note"] = ("snapshot came from the Yahoo SOFR fallback "
                       f"({len(records)} contract(s), not the investing.com ladder)")
    if save_db and not db_ok:
        out["stage_failures"] = [{
            "stage": "eurodollar_db",
            "error": f"DB upsert failed for {len(records)} parsed rows (see log)"}]
    return out

# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Eurodollar Futures Scraper [Scrapling v4]",
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
    parser.add_argument("--db", "--execute", "-x", dest="save_db",
                        action="store_true",
                        help="حفظ في DB (كتب جدول eurodollar_futures)")
    parser.add_argument("--quiet", "-q", action="store_true", help="بدون طباعة")
    parser.add_argument("--force", action="store_true", help="إعادة السحب حتى لو تم السحب اليوم")
    parser.add_argument("--no-proxy", dest="use_proxy", action="store_false",
                        default=True,
                        help="تجاهل متغيرات البروكسي في البيئة (اختبار مباشر)")

    args = parser.parse_args()
    if not args.use_proxy:
        # المسار الوحيد الذي يقرأ البروكسي هو resolve_proxy؛ إفراغ المفاتيح
        # لهذه العملية فقط لا يلمس بيئة التشغيل.
        for _k in (f"{PROXY_SCOPE}_PROXY",) + PROXY_ENV_KEYS:
            os.environ.pop(_k, None)
    result = scrape_eurodollar_futures(
        output_csv=args.output,
        output_json=args.json,
        save_db=args.save_db,
        print_results=not args.quiet,
        force=args.force,
    )
    sys.exit(0 if result.get("ok") else 1)