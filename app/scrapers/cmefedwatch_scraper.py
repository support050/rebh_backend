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

# القفل المقاس يوم 2026-10-31: صفحة cmegroup.com نفسها ورا Akamai وت بيرجع
# 403 لكل من requests و cloudscraper (600 بايت بس)، بينما الـ QuikStrike
# المباشر شغال و بيرجع الـ insid/qsid و __VIEWSTATE في نفس الـ response
# (101 KB، 200). يعني مفيش حاجة اسمها "نجيب الـ iframe الأول".
QUIKSTRIKE_VIEW = f"{QUIKSTRIKE_BASE}?viewitemid=IntegratedFedWatchTool"

# الـ EVENTTARGET لما بتضغط "Probabilities" في الـ sidebar
EVENTTARGET_PROBABILITIES = (
    "ctl00$MainContent$ucViewControl_IntegratedFedWatchTool$lbPTree"
)

# بقية تبويبات الـ sidebar — نفس الأداة، غير الـ EVENTTARGET بس.
TOOL_PREFIX = "ctl00$MainContent$ucViewControl_IntegratedFedWatchTool"
EVENTTARGET_DOTPLOT_TABLE = f"{TOOL_PREFIX}$lbDotPlotTable"
# تبويب كل اجتماع: uccv$lvMeetings$ctrl{N}$lbMeeting — بنكرّر الكل لتحصيل
# جدول الآفاق (Now/1D/1W/1M) ومعلومات الاجتماع لكل اجتماع، مش الأقرب بس.
MEETING_TAB_TMPL = f"{TOOL_PREFIX}$uccv$lvMeetings$ctrl{{n}}$lbMeeting"

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

# CME month codes — محددات عقد Fed Fund بتتحول لشهر الاستحقاق
CME_MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}


def _make_session():
    """curl_cffi بانتحال بصمة TLS حقيقية؛ requests كملاذ أخير.

    cloudscraper كان المسار القديم و مقاس راجع 403 من Akamai، ف اتسحب.
    """
    try:
        from curl_cffi import requests as cc_requests
        session = cc_requests.Session(impersonate="chrome", timeout=60)
        logger.info("️  Transport: curl_cffi (impersonate=chrome)")
        return session
    except ImportError as exc:
        logger.warning(f"⚠️  curl_cffi غير متوفر ({exc}) — رجوع إلى requests")
    return requests.Session()


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
def _cme_fail(error: str) -> dict:
    """Failure shape understood by job_tracking.with_result_handling."""
    return {"ok": False, "rows": 0, "stage_failures": [
        {"stage": "cme_fedwatch", "error": error[:400]}]}


def scrape_cme_fedwatch(force: bool = False, **kwargs) -> dict:
    """Returns {"ok": bool, "rows": n_records} for job_tracking — the old bool
    return left job_runs.rows_written NULL even on good snapshots."""
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
                return {"ok": True, "rows": 0, "skipped": "already scraped today"}
        finally:
            _db.close()

    # ── curl_cffi بانتحال TLS (cloudscraper كان بيرجع 403 من Akamai) ──
    session = _make_session()
    try:
        session.headers.update(HEADERS_BROWSER)
    except Exception:
        pass

    # ══ Step 1: نفتح الأداة مباشرة — الـ response نفسه فيه insid/qsid ════════
    logger.info("🌐 Step 1: تحميل QuikStrike FedWatch مباشرة...")
    view_data = _load_quikstrike_page(session, QUIKSTRIKE_VIEW)
    if not view_data:
        logger.error("❌ فشل تحميل الأداة")
        return _cme_fail("direct QuikStrike FedWatch load failed (see log)")

    logger.info(f"   ✅ insid={view_data['insid']} | qsid={str(view_data['qsid'])[:8]}...")

    # ══ Step 2: تحليل عرض "Current" من نفس الـ HTML ═════════════════════════
    logger.info("📊 Step 2: تحليل عرض Current (Meeting Info + Probabilities)...")
    current = _parse_current_view(view_data["html"])
    if current:
        logger.info(
            f"   ✅ meeting={current.get('meeting_label')} "
            f"contract={current.get('contract')} target={current.get('current_target_range')} "
            f"ease/chg/hike={current.get('prob_ease')}/{current.get('prob_no_change')}/{current.get('prob_hike')} "
            f"| horizon rows={len(current.get('horizons') or [])}"
        )
    else:
        logger.warning("⚠️  لم يتم استخراج عرض Current — الصفحات ستعرض المصفوفة فقط")

    # ══ Step 3: تكرار كل تبويبات الاجتماعات → آفاق + معلومات لكل اجتماع ══════
    # لازم تصير قبل نقرة Probabilities: الأداة بتتذكر العرض المختار على مستوى
    # الـ session، فأول ما نضغط lbPTree بيطير العرض Current و بعدها تبويبات
    # الاجتماعات بترجع المصفوفة الشرطية بدل جدول الآفاق (مقاس 2026-11-01).
    # بنمرر كل delta على نفس محلل Current — Now/1D/1W/1M و Ease/NoChange/Hike
    # لكل اجتماع زي ما يطبعها CME بالضبط (لا اشتقاق، لا تلفيق).
    n_tabs = _count_meeting_tabs(view_data["html"])
    meetings_saved = 0
    horizon_rows_total = 0
    logger.info(f"📡 Step 3: تكرار {n_tabs} تبويب اجتماع (أثناء عرض Current)...")
    for i in range(n_tabs):
        delta = _post_async(session, view_data, MEETING_TAB_TMPL.format(n=i))
        if not delta:
            logger.warning(f"   ⚠️ تبويب اجتماع {i} رجع فاضي — بيتخطّى")
            continue
        m = _parse_current_view(_extract_updatepanel_html(delta))
        if not m or not m.get("meeting_date"):
            logger.warning(f"   ⚠️ تبويب اجتماع {i} بلا بيانات مقروءة")
            continue
        hz = m.get("horizons") or []
        if _save_meeting(today, m):
            meetings_saved += 1
        if _save_horizons(today, m["meeting_date"], hz):
            horizon_rows_total += len(hz)
        logger.info(f"   ✅ اجتماع {m['meeting_date']} | عقد={m.get('contract')} "
                    f"ease/chg/hike={m.get('prob_ease')}/{m.get('prob_no_change')}/{m.get('prob_hike')} "
                    f"| آفاق={len(hz)}")
        time.sleep(1.0)  # لطف على الخادم؛ 9+ نقرات متتابعة

    # ══ Step 4: POST لـ "Probabilities" (المصفوفة الشرطية + سلع العقود) ══════
    logger.info("📡 Step 4: POST → Probabilities tab...")
    html_delta = _post_probabilities(session, view_data)

    if not html_delta:
        logger.error("❌ فشل POST للـ Probabilities")
        return _cme_fail("POST to Probabilities tab returned nothing")

    prob_html = _extract_updatepanel_html(html_delta)

    # ══ Step 5: تحليل المصفوفة الشرطية + سلع Fed Fund Futures ════════════════
    logger.info("📊 Step 5: تحليل المصفوفة الشرطية + Fed Fund Futures...")
    records = _parse_probabilities_html(html_delta, today)
    futures = _parse_futures_ladder(prob_html)

    if not records:
        logger.warning("⚠️ لم نجد Rate Ranges، نجرب Aggregated...")
        records = _parse_aggregated_html(html_delta, today)

    if not records and not current:
        logger.error(f"❌ لا بيانات. HTML[0:2000]:\n{prob_html[:2000]}")
        return _cme_fail("no probability rows parsed")

    logger.info(f"   matrix={len(records)} rows | futures={len(futures)} contracts")

    # ══ Step 6: Dot Plot (إسقاطات المشاركين) ═════════════════════════════════
    logger.info("📡 Step 6: POST → Dot Plot tab...")
    dot_rows: list[dict] = []
    dp_delta = _post_async(session, view_data, EVENTTARGET_DOTPLOT_TABLE)
    if dp_delta:
        dot_rows = _parse_dotplot(dp_delta)

    # ══ Step 7: الحفظ ════════════════════════════════════════════════════════
    logger.info("💾 Step 7: الحفظ في DB...")
    saved     = _save_records(records, today)
    ok_snap   = _save_snapshot(today, current)
    ok_fut    = _save_futures(today, futures)
    ok_dp     = _save_dotplot(today, dot_rows)
    if not saved:
        return _cme_fail(f"{len(records)} rows parsed but the DB save failed (see log)")
    return {
        "ok": True,
        "rows": (len(records) + len(futures) + horizon_rows_total
                 + meetings_saved + len(dot_rows) + (1 if current else 0)),
        "detail": {
            "matrix_rows": len(records), "futures": len(futures),
            "meetings_saved": meetings_saved, "meeting_tabs_seen": n_tabs,
            "horizon_rows": horizon_rows_total, "dotplot_cells": len(dot_rows),
            "snapshot_saved": ok_snap, "futures_saved": ok_fut, "dotplot_saved": ok_dp,
        },
    }


# ─── Step 1 (legacy): استخراج iframe URL من CME ─────────────────────────────
# متروكة كمرجع: cmegroup.com ورا Akamai وترجع 403 بدون بصمة TLS حقيقية،
# والمسار المباشر إلى QuikStrike غنى عنها (انظر QUIKSTRIKE_VIEW).
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
            "html":        resp.text,
        }

    except Exception as e:
        logger.error(f"   ❌ _load_quikstrike_page error: {e}", exc_info=True)
        return None


# ─── Step 3: POST → أي تبويب (Probabilities / اجتماع N / Dot Plot) ────────────
def _post_async(session: requests.Session, page_data: dict, event_target: str) -> str | None:
    """
    يبعت POST async بنفس شكل المتصفح لأي UpdatePanel trigger.

    الـ __EVENTTARGET يحدد التبويب:
      lbPTree            -> المصفوفة الشرطية + Fed Fund Futures
      lbDotPlotTable     -> جدول Dot Plot (إسقاطات المشاركين)
      uccv$lvMeetings$ctrl{N}$lbMeeting -> شاشة Current لاجتماع بعينه
    """
    form_fields = page_data["form_fields"]
    post_url    = page_data["post_url"]
    referer     = page_data["referer"]

    # ── بناء الـ POST body ────────────────────────────────────────────────────
    body = dict(form_fields)
    body.update({
        # UpdatePanel trigger
        "ctl00$smPublic": f"ctl00$upMain|{event_target}",
        "__EVENTTARGET":  event_target,
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
        resp = session.post(post_url, data=body, headers=headers, timeout=45)
        logger.info(f"   POST {event_target.split('$')[-1]}: "
                    f"{resp.status_code} | {len(resp.text)} chars")
        if resp.status_code != 200:
            logger.error(f"   ❌ POST failed: {resp.status_code}")
            logger.error(f"   Response: {resp.text[:500]}")
            return None
        return resp.text
    except Exception as e:
        logger.error(f"   ❌ POST error: {e}", exc_info=True)
        return None


def _post_probabilities(session: requests.Session, page_data: dict) -> str | None:
    """Wrapper: الـ Probabilities tab وحده (يبقى اسمًا واضحًا في الـ flow)."""
    return _post_async(session, page_data, EVENTTARGET_PROBABILITIES)


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


# ─── عرض Current: Meeting Information + Ease/No Change/Hike + جدول الآفاق ────
def _parse_current_view(html: str) -> dict | None:
    """يستخرج كل ما في شاشة "Current" من الـ GET نفسه (بدون POST إضافي).

    العناصر مقاسة من response الأداة:
      • جدول Meeting Information: Meeting Date | Contract | Expires | Mid Price
        | Prior Volume | Prior OI
      • جدول Probabilities: Ease | No Change | Hike
      • جدول Target Rate (bps) / Probability(%) بأعمدة Now / 1 Day / 1 Week / 1 Month
      • إعدادات الرسم (JSON متّبع داخل الـ script): العنوان + xAxisLabels +
        "Current target rate is NNN-NNN"
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {"horizons": []}

    # ── 1) إعدادات الرسم: العنوان/العنوان الفرعي/التسميات ────────────────────
    # الـ JSON متّبع داخل script نصه "... {\"Title\":\"...\"} ..."، نفكّه أولاً.
    flat = html.replace('\\"', '"')
    m_title = re.search(r'"Title"\s*:\s*"([^"]+)"', flat)
    m_sub = re.search(r'"Subtitle"\s*:\s*"([^"]+)"', flat)
    m_labels = re.search(r'"xAxisLabels"\s*:\s*\[([^\]]*)\]', flat)
    if m_title:
        out["chart_title"] = m_title.group(1).strip()
    if m_sub:
        m_tr = re.search(r"Current target rate is\s*([0-9]+\s*-\s*[0-9]+)", m_sub.group(1))
        if m_tr:
            out["current_target_range"] = re.sub(r"\s+", "", m_tr.group(1))
    if m_labels:
        out["chart_labels"] = [x.strip().strip('"') for x in m_labels.group(1).split(",") if x.strip()]

    m_asof = re.search(r"Data as of\s*([0-9]{1,2}\s+\w{3}\s+\d{4}\s+[0-9:]{5,8}\s*CT)", html, re.I)
    if m_asof:
        out["data_as_of"] = m_asof.group(1).strip()

    # ── 2) جداول الأداة ─────────────────────────────────────────────────────
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        grid = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in rows]
        flat_txt = " | ".join(" | ".join(r) for r in grid).lower()

        # Meeting Information — صف واحد بعد راسين (عنوان المجموعة + الأعمدة)
        if "meeting date" in flat_txt and "contract" in flat_txt and "mid price" in flat_txt:
            for r in grid:
                vals = [v for v in r if v]
                if len(vals) >= 6 and _parse_date(vals[0] if vals[0] != "Meeting Date" else vals[1]):
                    d = None
                    i = next((k for k, v in enumerate(vals) if (d := _parse_date(v))), None)
                    if i is None:
                        continue
                    out.update({
                        "meeting_label": vals[i], "meeting_date": d,
                        "contract": vals[i + 1] if i + 1 < len(vals) else None,
                        "expires":  vals[i + 2] if i + 2 < len(vals) else None,
                        "mid_price": _to_float(vals[i + 3]) if i + 3 < len(vals) else None,
                        "prior_volume": _to_int(vals[i + 4]) if i + 4 < len(vals) else None,
                        "prior_oi":     _to_int(vals[i + 5]) if i + 5 < len(vals) else None,
                    })
                    break

        # Probabilities: Ease / No Change / Hike
        elif "ease" in flat_txt and "hike" in flat_txt and "no change" in flat_txt:
            hdr_i = next((k for k, r in enumerate(grid)
                          if any(c.lower() == "ease" for c in r)), None)
            if hdr_i is not None and hdr_i + 1 < len(grid):
                hdr, vals = grid[hdr_i], grid[hdr_i + 1]
                for ci, name in enumerate(hdr):
                    key = {"ease": "prob_ease", "no change": "prob_no_change",
                           "hike": "prob_hike"}.get(name.lower())
                    if key and ci < len(vals):
                        p = _to_float(vals[ci])
                        if p is not None:
                            out[key] = p

        # Target Rate (bps) / Probability(%) — راس مزدوج ثم صف لكل شريحة
        elif any(r and r[0].startswith("Target Rate") for r in grid):
            hdr_i = next((k for k, r in enumerate(grid)
                          if r and r[0].startswith("Target Rate")), None)
            if hdr_i is None or hdr_i + 1 >= len(grid):
                continue
            horizons = [h for h in grid[hdr_i + 1] if h]
            keys = _horizon_keys(len(horizons))
            for r in grid[hdr_i + 2:]:
                if not r:
                    continue
                label = re.sub(r"\s*\(Current\)\s*", "", r[0]).strip()
                if not re.match(r"^\d{3}-\d{3}$", label):
                    continue
                is_cur = "(Current)" in r[0]
                cells = r[1:]
                # الأعمدة الناقصة تُحاذى لليسار (الأقدم تُحذف أولاً)
                pad = len(keys) - len(cells)
                if pad < 0:
                    cells = cells[:len(keys)]
                elif pad > 0:
                    cells = [""] * pad + cells
                for hi, hkey in enumerate(keys):
                    p = _to_float(cells[hi]) if hi < len(cells) else None
                    if p is None:
                        continue
                    out["horizons"].append({
                        "rate_range": label, "is_current": is_cur,
                        "horizon": hkey, "horizon_label": horizons[hi] if hi < len(horizons) else hkey,
                        "probability": p,
                    })

    has_any = any(k in out for k in ("meeting_date", "current_target_range", "prob_ease"))
    if not has_any and not out["horizons"]:
        return None
    return out


def _horizon_keys(n: int) -> list[str]:
    """أعمدة الجدول بترتيبها في الأداة: Now, 1 Day, 1 Week, 1 Month."""
    order = ["now", "1d", "1w", "1m", "3m", "6m", "1y"]
    return order[:n] if n <= len(order) else order + [f"x{i}" for i in range(n - len(order))]


# ─── سلع Fed Fund Futures من عرض Probabilities ───────────────────────────────
def _parse_futures_ladder(html_content: str) -> list[dict]:
    """جدول "Fed Fund Futures": صف محددات (ZQX6…) وصف أسعار مقابلة.

    شهر الاستحقاق يُستنتج من كود CME (Z=ديسمبر، F=يناير…) حتى تربط كل اجتماع
    بالعقود ده من غير ما نحتاج نقرة لكل تبويب.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    out: list[dict] = []
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True)
        if "Fed Fund Futures" not in txt:
            continue
        grid = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                for tr in tbl.find_all("tr")]
        contracts, prices = None, None
        for r in grid:
            cs = [v for v in r if re.match(r"^[A-Z]{2}[FGHJKMNQUVXZ]\d{1,2}$", v)]
            if len(cs) >= 2:
                contracts = cs
            elif contracts and r is not contracts:
                ps = [v for v in r if re.match(r"^\d+\.\d+$", v)]
                if ps and len(ps) == len(contracts):
                    prices = ps
                    break
        if not contracts or not prices:
            continue
        for c, p in zip(contracts, prices):
            out.append({"contract": c, "price": _to_float(p),
                        "contract_month": _contract_expiry(c)})
        logger.info(f"   ✅ Fed Fund Futures ladder: {len(out)} contracts")
        break
    return out


def _contract_expiry(code: str) -> date | None:
    """ZQZ6 → ديسمبر 2026 (أول الشهر؛ كود CME يحدد الشهر فقط).

    السنة في رمز CME رقمان في آخرها بس المتعارف عليه رقم واحد (ZQZ6 = 2026)،
    ف بنلف حوالين العقد الحالي: ZQZ6 سنة 6 و إحنا 2026 → 2026، و ZQZ7 → 2027.
    """
    m = re.match(r"^[A-Z]{2}([FGHJKMNQUVXZ])(\d{1,2})$", code or "")
    if not m:
        return None
    month = CME_MONTH_CODES.get(m.group(1))
    if not month:
        return None
    digits, cy = m.group(2), date.today().year
    if len(digits) == 1:                       # سنة من رقم واحد: ZQZ6 → 2026
        year = (cy // 10) * 10 + int(digits)
        if year < cy - 5:
            year += 10
        elif year > cy + 5:
            year -= 10
    else:                                      # ZQZ26 → 2026
        year = (cy // 100) * 100 + int(digits)
        if year < cy - 5:
            year += 100
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def _to_int(text) -> int | None:
    if text is None:
        return None
    s = str(text).replace(",", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _count_meeting_tabs(html: str) -> int:
    """كم تبويب اجتماع موجود في الـ bootstrap؟ (lvMeetings$ctrlN$lbMeeting).

    بنقرأ العدد من الـ HTML نفسه بدل ما نفترضه؛ الأداة تعرض اجتماعات الفيدرالية
    القادمة و عددها بيتغير مع الوقت.
    """
    nums = re.findall(r"lvMeetings\$ctrl(\d+)\$lbMeeting", html)
    return (max(int(n) for n in nums) + 1) if nums else 0


def _parse_dotplot(raw_delta: str) -> list[dict]:
    """يحلّل جدول "Dot Plot - Table": صفوف Target Rate × أعمدة سنوات.

    الشكل المقاس (probe_lbDotPlotTable.html):
      <table class="grid-thm grid-thm-v2">
        <tr><th>Target<br/>Rate</th><th>2026</th>…<th>Longer run</th></tr>
        <tr><td class='number'>4.375</td>
            <td class='center' style='color:#000;background-color:#B9EDB9'>4</td>…</tr>
        <tr><td class='number highlight2'>4.125</td>…   # الوسيط مميّز
    كل خانة رقمية = عدد المشاركين؛ الفارغة = بلا نقطة. لون الخلفية (heat) بنحتفظ
    به زي ما هو عشان الواجهة ارسمه بنفس تظليل الأداة.
    """
    html = _extract_updatepanel_html(raw_delta)
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        head = rows[0].find_all(["th", "td"])
        head_txt = [c.get_text(" ", strip=True) for c in head]
        # الجدول الصح: أول رأس "Target Rate" و بقية رؤوسها سنوات/Longer run
        if not (head_txt and "target" in head_txt[0].lower()
                and re.match(r"^(\d{4}|Longer run)$", head_txt[-1] or "", re.I)):
            continue
        years = [(i, h) for i, h in enumerate(head_txt) if i > 0 and h]
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            rate = _to_float(cells[0].get_text(strip=True))
            if rate is None:
                continue
            cls = " ".join(cells[0].get("class") or [])
            is_median = "highlight2" in cls
            for order, (ci, ylabel) in enumerate(years):
                if ci >= len(cells):
                    continue
                c = cells[ci]
                txt = c.get_text(strip=True)
                cnt = _to_int(txt)
                style = (c.get("style") or "")
                m_bg = re.search(r"background-color:\s*(#[0-9A-Fa-f]{3,6})", style)
                out.append({
                    "target_rate": rate, "year_label": ylabel, "year_order": order,
                    "count": cnt, "bg_color": (m_bg.group(1).upper() if m_bg else None),
                    "is_median": is_median,
                })
        if out:
            logger.info(f"   ✅ Dot Plot: {len(out)} خانة")
            break
    return out


# ─── حفظ الجداول الجديدة ──────────────────────────────────────────────────
def _save_snapshot(today: date, current: dict | None) -> bool:
    if not current:
        return False
    from app.core.database import SessionLocal
    from app.models.economic_indicators import CmeFedwatchSnapshot

    db = SessionLocal()
    try:
        row = db.query(CmeFedwatchSnapshot).filter(CmeFedwatchSnapshot.scrape_date == today).first()
        if not row:
            row = CmeFedwatchSnapshot(scrape_date=today)
            db.add(row)
        for f in ("current_target_range", "chart_title", "chart_labels", "meeting_label",
                  "meeting_date", "contract", "expires", "mid_price", "prior_volume",
                  "prior_oi", "prob_ease", "prob_no_change", "prob_hike", "data_as_of"):
            if current.get(f) is not None:
                setattr(row, f, current[f])
        db.commit()
        logger.info("   💾 snapshot محفوظ")
        return True
    except Exception as exc:
        db.rollback()
        logger.error(f"   ❌ snapshot save: {exc}")
        return False
    finally:
        db.close()


def _save_horizons(today: date, meeting_date, horizons: list[dict]) -> bool:
    if not horizons or not meeting_date:
        return False
    from app.core.database import SessionLocal
    from app.models.economic_indicators import CmeFedwatchHorizon

    db = SessionLocal()
    try:
        n = 0
        for h in horizons:
            row = (db.query(CmeFedwatchHorizon)
                   .filter(CmeFedwatchHorizon.scrape_date == today,
                           CmeFedwatchHorizon.meeting_date == meeting_date,
                           CmeFedwatchHorizon.rate_range == h["rate_range"],
                           CmeFedwatchHorizon.horizon == h["horizon"])
                   .first())
            if row:
                row.probability = h["probability"]
                row.is_current = h["is_current"]
            else:
                db.add(CmeFedwatchHorizon(
                    scrape_date=today, meeting_date=meeting_date,
                    rate_range=h["rate_range"], horizon=h["horizon"],
                    horizon_label=h["horizon_label"], is_current=h["is_current"],
                    probability=h["probability"]))
            n += 1
        db.commit()
        logger.info(f"   💾 {n} صف آفاق (horizon) محفوظ")
        return True
    except Exception as exc:
        db.rollback()
        logger.error(f"   ❌ horizons save: {exc}")
        return False
    finally:
        db.close()


def _save_futures(today: date, futures: list[dict]) -> bool:
    if not futures:
        return False
    from app.core.database import SessionLocal
    from app.models.economic_indicators import CmeFedwatchFutures

    db = SessionLocal()
    try:
        n = 0
        for f in futures:
            row = (db.query(CmeFedwatchFutures)
                   .filter(CmeFedwatchFutures.scrape_date == today,
                           CmeFedwatchFutures.contract == f["contract"])
                   .first())
            if row:
                row.price = f["price"]
                row.contract_month = f["contract_month"]
            else:
                db.add(CmeFedwatchFutures(scrape_date=today, contract=f["contract"],
                                          price=f["price"], contract_month=f["contract_month"]))
            n += 1
        db.commit()
        logger.info(f"   💾 {n} عقد Fed Fund محفوظ")
        return True
    except Exception as exc:
        db.rollback()
        logger.error(f"   ❌ futures save: {exc}")
        return False
    finally:
        db.close()


def _save_meeting(today: date, meeting: dict | None) -> bool:
    """Upserts one meeting tab's scraped Current header into cme_fedwatch_meeting."""
    if not meeting or not meeting.get("meeting_date"):
        return False
    from app.core.database import SessionLocal
    from app.models.economic_indicators import CmeFedwatchMeeting

    md = meeting["meeting_date"]
    if isinstance(md, str):
        md = datetime.strptime(md[:10], "%Y-%m-%d").date()

    db = SessionLocal()
    try:
        row = (db.query(CmeFedwatchMeeting)
               .filter(CmeFedwatchMeeting.scrape_date == today,
                       CmeFedwatchMeeting.meeting_date == md).first())
        if not row:
            row = CmeFedwatchMeeting(scrape_date=today, meeting_date=md)
            db.add(row)
        for f in ("meeting_label", "contract", "expires", "mid_price", "prior_volume",
                  "prior_oi", "prob_ease", "prob_no_change", "prob_hike", "data_as_of"):
            if meeting.get(f) is not None:
                setattr(row, f, meeting[f])
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error(f"   ❌ meeting save ({md}): {exc}")
        return False
    finally:
        db.close()


def _save_dotplot(today: date, rows: list[dict]) -> bool:
    if not rows:
        return False
    from app.core.database import SessionLocal
    from app.models.economic_indicators import CmeFedwatchDotPlot

    db = SessionLocal()
    try:
        n = 0
        for r in rows:
            row = (db.query(CmeFedwatchDotPlot)
                   .filter(CmeFedwatchDotPlot.scrape_date == today,
                           CmeFedwatchDotPlot.target_rate == r["target_rate"],
                           CmeFedwatchDotPlot.year_label == r["year_label"]).first())
            if row:
                row.count = r["count"]
                row.bg_color = r["bg_color"]
                row.is_median = r["is_median"]
                row.year_order = r["year_order"]
            else:
                db.add(CmeFedwatchDotPlot(scrape_date=today, **r))
            n += 1
        db.commit()
        logger.info(f"   💾 {n} خانة Dot Plot محفوظة")
        return True
    except Exception as exc:
        db.rollback()
        logger.error(f"   ❌ dotplot save: {exc}")
        return False
    finally:
        db.close()


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

    res = scrape_cme_fedwatch(force=args.force)
    print("\n" + ("✅ نجح" if res.get("ok") else f"❌ فشل: {res.get('stage_failures')}"))