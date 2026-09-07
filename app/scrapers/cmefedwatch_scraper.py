"""
CME FedWatch Scraper — ASP.NET UpdatePanel (requests فقط، بدون Selenium)
=========================================================================

كيف يشتغل QuikStrike:
  1. الصفحة بتحمّل عبر GET → بترجع HTML فيه __VIEWSTATE + insid + qsid
  2. لما تضغط "Probabilities" في الـ sidebar:
     - المتصفح يبعت POST لنفس الـ URL
     - الـ body فيه: __VIEWSTATE + __EVENTTARGET=...lbPTree + باقي الـ fields
     - الـ response: UpdatePanel delta (HTML fragments مش JSON)
  3. نحلّل الـ HTML delta ونستخرج الجدول

المعطيات من DevTools (بتتغير كل session):
  - insid: 220458043  (أو 218288033 في القديم)
  - qsid:  e57fa5e6-90f6-4304-bff7-f6884355ac04
  - __EVENTTARGET: ctl00$MainContent$ucViewControl_IntegratedFedWatchTool$lbPTree
  
ملاحظة: insid وqsid بيتغيروا — لازم نجيبهم من الصفحة كل مرة.
"""

import re
import time
import logging
import urllib.parse
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─── URLs ─────────────────────────────────────────────────────────────────────
CME_MAIN_URL = (
    "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
)
# URL الـ QuikStrike الأساسي — الـ insid وqsid بيتجيبوا من الـ iframe src
QUIKSTRIKE_BASE = "https://cmegroup-tools.quikstrike.net/User/QuikStrikeView.aspx"

# الـ EVENTTARGET لما بتضغط "Probabilities" في الـ sidebar
EVENTTARGET_PROBABILITIES = (
    "ctl00$MainContent$ucViewControl_IntegratedFedWatchTool$lbPTree"
)

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ─── حفظ في DB ────────────────────────────────────────────────────────────────
def _save_records(records: list[dict], today: date) -> bool:
    if not records:
        logger.warning("⚠️ لا توجد سجلات للحفظ")
        return False

    from app.core.database import SessionLocal
    from app.models.economic_indicators import CmeFedwatch

    db = SessionLocal()
    try:
        ins = upd = 0
        for rec in records:
            existing = (
                db.query(CmeFedwatch)
                .filter(
                    CmeFedwatch.scrape_date  == rec["scrape_date"],
                    CmeFedwatch.meeting_date == rec["meeting_date"],
                    CmeFedwatch.rate_range   == rec["rate_range"],
                )
                .first()
            )
            if existing:
                existing.probability = rec["probability"]
                upd += 1
            else:
                db.add(CmeFedwatch(**rec))
                ins += 1
        db.commit()
        logger.info(f"💾 DB: {ins} inserted, {upd} updated (date: {today})")
        return True
    except Exception as exc:
        db.rollback()
        logger.error(f"❌ DB error: {exc}")
        return False
    finally:
        db.close()


# ─── الدالة الرئيسية ──────────────────────────────────────────────────────────
def scrape_cme_fedwatch(force: bool = False, **kwargs) -> bool:
    logger.info("🚀 CME FedWatch scraper بيبدأ...")
    today = date.today()

    if not force:
        from app.core.database import SessionLocal as _SL
        from app.models.economic_indicators import CmeFedwatch as _CF
        _db = _SL()
        try:
            cnt = _db.query(_CF).filter(_CF.scrape_date == today).count()
            if cnt > 0:
                logger.info(f"ℹ️ تم السحب مسبقاً ({today}): {cnt} سجل.")
                return True
        finally:
            _db.close()

    # ── استخدام cloudscraper لتخطي الحماية ──
    try:
        import cloudscraper
        session = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'desktop': True
            }
        )
    except ImportError:
        logger.warning("⚠️ cloudscraper غير متوفر، سنستخدم requests العادي")
        session = requests.Session()

    session.headers.update(HEADERS_BROWSER)

    # ══ Step 1: نجيب iframe URL من CME الرئيسية ══════════════════════════════
    logger.info("🌐 Step 1: جلب CME الرئيسية لاستخراج iframe URL...")
    iframe_url = _get_iframe_url(session)

    if not iframe_url:
        logger.error("❌ لم نجد الـ iframe URL")
        return False

    logger.info(f"✅ iframe URL: {iframe_url[:100]}")

    # ══ Step 2: نفتح الـ iframe (GET) (الـ Shell) ══════════════════════════════
    logger.info("🌐 Step 2: تحميل QuikStrike iframe (shell)...")
    page_data = _load_quikstrike_page(session, iframe_url)

    if not page_data:
        logger.error("❌ فشل تحميل QuikStrike iframe")
        return False

    # ══ Step 2b: نبعت POST أولي عشان نجيب insid و qsid الجداد ═══════════════
    logger.info("📡 Step 2b: POST أولي لتهيئة الـ Session...")
    init_resp = _post_probabilities(session, page_data)
    new_insid = ""
    new_qsid = ""
    
    if init_resp:
        import re as _re
        fa_match = _re.search(r'formAction\|\|([^\|]+)', init_resp)
        if fa_match:
            new_action = fa_match.group(1)
            new_url = urllib.parse.urljoin(page_data['post_url'], new_action)
            parsed_new = urllib.parse.urlparse(new_url)
            qs_new = urllib.parse.parse_qs(parsed_new.query)
            new_insid = qs_new.get("insid", [""])[0]
            new_qsid  = qs_new.get("qsid",  [""])[0]
            logger.info(f"✅ تم سحب insid={new_insid} | qsid={new_qsid[:8]}...")
            
    if not new_insid or not new_qsid:
        logger.error("❌ فشل في جلب Session IDs")
        return False

    # ══ Step 3: تحميل صفحة الأداة الفعلية (QuikStrikeView.aspx) ════════════
    logger.info("🌐 Step 3: تحميل صفحة الأداة الفعلية...")
    view_url = (
        f"https://cmegroup-tools.quikstrike.net/User/QuikStrikeView.aspx"
        f"?viewitemid=IntegratedFedWatchTool"
        f"&insid={new_insid}"
        f"&qsid={new_qsid}"
    )
    
    view_data = _load_quikstrike_page(session, view_url)
    if not view_data:
        logger.error("❌ فشل تحميل صفحة الأداة")
        return False

    # ══ Step 4: نبعت POST لـ "Probabilities" من جوه صفحة الأداة ═════════════
    logger.info("📡 Step 4: POST → Probabilities tab...")
    html_delta = _post_probabilities(session, view_data)

    if not html_delta:
        logger.error("❌ فشل POST للـ Probabilities")
        return False

    # ══ Step 5: نحلّل الـ HTML ونستخرج الجدول ════════════════════════════════
    logger.info("📊 Step 5: تحليل الجدول...")
    records = _parse_probabilities_html(html_delta, today)

    if not records:
        logger.warning("⚠️ لم نجد Rate Ranges، نجرب Aggregated...")
        records = _parse_aggregated_html(html_delta, today)

    if not records:
        logger.error(f"❌ لا بيانات. HTML[0:2000]:\n{html_delta[:2000]}")
        return False

    logger.info(f"✅ {len(records)} سجل")
    return _save_records(records, today)


# ─── Step 1: استخراج iframe URL من CME ───────────────────────────────────────
def _get_iframe_url(session: requests.Session) -> str | None:
    """
    يجيب URL الـ iframe الخاص بـ QuikStrike من صفحة CME الرئيسية.
    الـ URL فيه insid وqsid الخاصة بالـ session.
    """
    try:
        resp = session.get(CME_MAIN_URL, timeout=30)
        logger.info(f"   CME main: {resp.status_code}")

        if resp.status_code != 200:
            # بعض الـ servers بترفض — نجرب بـ headers مختلفة
            session.headers.update({"Accept": "text/html,application/xhtml+xml,*/*"})
            resp = session.get(CME_MAIN_URL, timeout=30)

        soup = BeautifulSoup(resp.text, "html.parser")

        # ابحث عن iframe بـ quikstrike
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if "quikstrike" in src.lower() or "QuikStrike" in src:
                return src if src.startswith("http") else f"https://cmegroup-tools.quikstrike.net{src}"

        # ابحث في الـ scripts كـ fallback
        for script in soup.find_all("script"):
            text = script.string or ""
            match = re.search(
                r'(https://cmegroup-tools\.quikstrike\.net[^\s\'"]+FedWatch[^\s\'"]+)',
                text
            )
            if match:
                return match.group(1)

        # fallback: نبني الـ URL من المعطيات المعروفة
        # نجيب insid وqsid من الـ source
        match_insid = re.search(r'insid[=:](\d+)', resp.text)
        match_qsid  = re.search(
            r'qsid[=:]["\']?([a-f0-9\-]{36})', resp.text, re.IGNORECASE
        )

        if match_insid and match_qsid:
            insid = match_insid.group(1)
            qsid  = match_qsid.group(1)
            url = (
                f"{QUIKSTRIKE_BASE}"
                f"?viewitemid=IntegratedFedWatchTool"
                f"&insid={insid}"
                f"&qsid={qsid}"
            )
            logger.info(f"   Built URL from page source: {url[:80]}")
            return url

    except Exception as e:
        logger.error(f"   ❌ _get_iframe_url error: {e}")

    return None


# ─── Step 2: تحميل صفحة QuikStrike واستخراج الـ form fields ──────────────────
def _load_quikstrike_page(session: requests.Session, url: str) -> dict | None:
    """
    يحمّل صفحة QuikStrike ويستخرج:
      - __VIEWSTATE
      - __VIEWSTATEGENERATOR
      - __EVENTVALIDATION (لو موجود)
      - insid, qsid (من الـ form action)
      - كل الـ hidden fields في الـ form
    """
    try:
        headers = {
            **HEADERS_BROWSER,
            "Accept":  "text/html,application/xhtml+xml,*/*;q=0.9",
            "Referer": CME_MAIN_URL,
        }
        resp = session.get(url, headers=headers, timeout=40)
        logger.info(f"   QuikStrike GET: {resp.status_code} | {len(resp.text)} chars")

        if resp.status_code != 200:
            logger.error(f"   ❌ Status {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # ── استخراج post_url من الـ form action ──
        post_url = resp.url
        form = soup.find("form")
        if form and form.get("action"):
            action = form["action"]
            if action.startswith("./"):
                action = action[2:]
            post_url = urllib.parse.urljoin(resp.url, action)

        # استخرج insid وqsid من الـ post_url
        parsed = urllib.parse.urlparse(post_url)
        qs     = urllib.parse.parse_qs(parsed.query)
        insid  = qs.get("insid", [""])[0]
        qsid   = qs.get("qsid",  [""])[0]

        # استخرج كل الـ hidden inputs
        form_fields: dict[str, str] = {}
        for inp in soup.find_all("input", type="hidden"):
            name  = inp.get("name", "")
            value = inp.get("value", "")
            if name:
                form_fields[name] = value

        # تحقق من وجود __VIEWSTATE
        if "__VIEWSTATE" not in form_fields:
            logger.error("   ❌ __VIEWSTATE غير موجود في الصفحة")
            logger.error(f"   DOM[0:500]: {resp.text[:500]}")
            return None

        logger.info(
            f"   ✅ form_fields: {list(form_fields.keys())[:8]}... "
            f"(__VIEWSTATE len={len(form_fields['__VIEWSTATE'])})"
        )

        return {
            "post_url":    post_url,
            "insid":       insid,
            "qsid":        qsid,
            "form_fields": form_fields,
            "referer":     url,
            "soup":        soup,
        }

    except Exception as e:
        logger.error(f"   ❌ _load_quikstrike_page error: {e}", exc_info=True)
        return None


# ─── Step 3: POST → Probabilities tab ─────────────────────────────────────────
def _post_probabilities(session: requests.Session, page_data: dict) -> str | None:
    """
    يبعت POST request بنفس الـ body اللي شفناه في DevTools
    لـ trigger الـ "Probabilities" UpdatePanel.

    الـ __EVENTTARGET هو المفتاح:
      ctl00$MainContent$ucViewControl_IntegratedFedWatchTool$lbPTree
    """
    insid       = page_data["insid"]
    qsid        = page_data["qsid"]
    form_fields = page_data["form_fields"]
    post_url    = page_data["post_url"]
    referer     = page_data["referer"]

    # ── بناء الـ POST body ────────────────────────────────────────────────────
    # نبدأ بكل الـ hidden fields من الصفحة
    body = dict(form_fields)

    # الحقول الإضافية اللي بيبعتها المتصفح
    body.update({
        # UpdatePanel trigger
        "ctl00$smPublic": (
            f"ctl00$upMain|{EVENTTARGET_PROBABILITIES}"
        ),
        "__EVENTTARGET":  EVENTTARGET_PROBABILITIES,
        "__EVENTARGUMENT": "",
        "__LASTFOCUS":    "",
        "__ASYNCPOST":    "true",

        # حقول ثابتة من الـ form
        "ctl00$global_attributes": "",
        "ctl00$global_mobile":     "",
        "ctl00$page_title":        "",
        "ctl00$MainContent$global_viewAttributes": "",
        "ctl00$MainContent$ucViewControl_IntegratedFedWatchTool$ucTweet$twittercard_title": (
            "FedWatch Tool"
        ),
        "ctl00$ucFix$calendarFix": "",
    })

    headers = {
        **HEADERS_BROWSER,
        "Accept":               "*/*",
        "Content-Type":         "application/x-www-form-urlencoded; charset=UTF-8",
        "X-MicrosoftAjax":      "Delta=true",
        "X-Requested-With":     "XMLHttpRequest",
        "Referer":              referer,
        "Origin":               "https://cmegroup-tools.quikstrike.net",
        "Sec-Fetch-Dest":       "empty",
        "Sec-Fetch-Mode":       "cors",
        "Sec-Fetch-Site":       "same-origin",
        "Sec-Fetch-Storage-Access": "active",
    }

    try:
        logger.info(f"   POST → {post_url[:80]}")
        resp = session.post(
            post_url,
            data=body,
            headers=headers,
            timeout=40,
        )
        logger.info(f"   POST response: {resp.status_code} | {len(resp.text)} chars")

        if resp.status_code != 200:
            logger.error(f"   ❌ POST failed: {resp.status_code}")
            logger.error(f"   Response: {resp.text[:500]}")
            return None

        return resp.text

    except Exception as e:
        logger.error(f"   ❌ POST error: {e}", exc_info=True)
        return None


# ─── Step 4a: تحليل Conditional Meeting Probabilities (Rate Ranges) ───────────
def _parse_probabilities_html(html: str, today: date) -> list[dict]:
    """
    يحلّل الـ HTML Delta اللي بيرجع من UpdatePanel.

    الـ UpdatePanel response بيكون بالشكل ده:
      length|updatePanel|panelId|HTML_CONTENT|...

    نستخرج HTML_CONTENT ونحلّل الجدول منه:
        MEETING DATE | 250-275 | 275-300 | ... | 425-450
        6/17/2026    |   0.0%  |   0.0%  | ... |   0.0%
    """
    # ── استخرج HTML من UpdatePanel delta format ───────────────────────────────
    html_content = _extract_updatepanel_html(html)

    # ── حلّل كل الجداول في الـ HTML ──────────────────────────────────────────
    soup = BeautifulSoup(html_content, "html.parser")
    records = []

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue

        # ابحث عن header row بـ rate ranges
        header_idx   = None
        rate_cols    = []  # list of (col_index, "NNN-NNN")

        for ri, tr in enumerate(rows):
            cells = tr.find_all(["td", "th"])
            cell_texts = [c.get_text(strip=True) for c in cells]

            has_meeting_date = any(
                "meeting" in t.lower() for t in cell_texts
            )
            ranges_in_row = [
                (ci, t)
                for ci, t in enumerate(cell_texts)
                if ci > 0 and re.match(r'^\d{3}-\d{3}$', t)
            ]

            if has_meeting_date and ranges_in_row:
                header_idx = ri
                rate_cols  = ranges_in_row
                logger.info(
                    f"   ✅ Found Probabilities table "
                    f"(row {ri}, {len(rate_cols)} rate ranges)"
                )
                break

        if header_idx is None:
            continue

        # استخرج البيانات
        for tr in rows[header_idx + 1:]:
            cells     = tr.find_all(["td", "th"])
            if not cells:
                continue
            date_text = cells[0].get_text(strip=True)
            d         = _parse_date(date_text)
            if not d:
                continue
            date_str = d.strftime("%Y-%m-%d")

            for ci, label in rate_cols:
                if ci >= len(cells):
                    continue
                prob = _to_float(cells[ci].get_text(strip=True))
                if prob is None:
                    continue
                records.append({
                    "scrape_date":  today,
                    "meeting_date": date_str,
                    "rate_range":   label,
                    "probability":  prob,
                })

        if records:
            break  # وجدنا الجدول الصح

    logger.info(f"   Rate ranges records: {len(records)}")
    return records


# ─── Step 4b: تحليل Aggregated (Ease/No Change/Hike) كـ Fallback ──────────────
def _parse_aggregated_html(html: str, today: date) -> list[dict]:
    """
    Fallback: يحلّل جدول Aggregated (Ease / No Change / Hike).
    """
    html_content = _extract_updatepanel_html(html)
    soup         = BeautifulSoup(html_content, "html.parser")
    records      = []

    AGG_LABELS = {
        "ease":      "Ease",
        "no change": "No Change",
        "hike":      "Hike",
    }

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue

        header_idx = None
        agg_cols   = []

        for ri, tr in enumerate(rows):
            cells      = tr.find_all(["td", "th"])
            cell_texts = [c.get_text(strip=True) for c in cells]

            has_meeting = any("meeting" in t.lower() for t in cell_texts)
            agg_found   = [
                (ci, AGG_LABELS[t.lower()])
                for ci, t in enumerate(cell_texts)
                if t.lower() in AGG_LABELS
            ]

            if has_meeting and agg_found:
                header_idx = ri
                agg_cols   = agg_found
                break

        if header_idx is None:
            continue

        for tr in rows[header_idx + 1:]:
            cells     = tr.find_all(["td", "th"])
            if not cells:
                continue
            date_text = cells[0].get_text(strip=True)
            d         = _parse_date(date_text)
            if not d:
                continue
            date_str = d.strftime("%Y-%m-%d")

            for ci, label in agg_cols:
                if ci >= len(cells):
                    continue
                prob = _to_float(cells[ci].get_text(strip=True))
                if prob is None:
                    continue
                records.append({
                    "scrape_date":  today,
                    "meeting_date": date_str,
                    "rate_range":   label,
                    "probability":  prob,
                })

        if records:
            break

    logger.info(f"   Aggregated records: {len(records)}")
    return records


# ─── استخراج HTML من UpdatePanel delta ────────────────────────────────────────
def _extract_updatepanel_html(raw: str) -> str:
    """
    الـ UpdatePanel بيرجع response بالشكل ده:
      1234|updatePanel|panelId|<HTML CONTENT>|0|hiddenField|...|

    نستخرج كل الـ HTML من الـ updatePanel sections.
    لو مش UpdatePanel format نرجع الـ raw كما هو.
    """
    # Pattern: NNN|updatePanel|ID|CONTENT|
    parts = re.findall(
        r'\d+\|updatePanel\|[^|]+\|(.*?)(?=\d+\|(?:updatePanel|hiddenField|scriptBlock|pageTitle|focus)|$)',
        raw,
        re.DOTALL,
    )

    if parts:
        combined = "\n".join(parts)
        logger.info(f"   UpdatePanel delta: {len(parts)} panel(s), {len(combined)} chars")
        return combined

    # لو مش delta format، رجّع الـ raw مباشرة
    logger.info(f"   Non-delta response: {len(raw)} chars")
    return raw


# ─── أدوات مساعدة ─────────────────────────────────────────────────────────────
def _parse_date(text: str) -> date | None:
    clean = text.strip()
    if not clean or len(clean) > 20:
        return None
    for fmt in (
        "%m/%d/%Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            pass
    return None


def _to_float(text: str) -> float | None:
    clean = str(text).replace("%", "").strip()
    if not clean:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="CME FedWatch Scraper")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    success = scrape_cme_fedwatch(force=args.force)
    print("\n" + ("✅ نجح" if success else "❌ فشل"))