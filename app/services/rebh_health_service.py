"""
REBH Unified Data-Health Gate (Errors 1b & 5).

Single source of truth for whether a company's stored XBRL record is trustworthy
enough to price. Both the pricing engine and the quarantine endpoint call this —
they no longer carry two disconnected sets of health checks.

    assess_health(symbol) -> { ok, bucket, reasons[], structured[], metrics{} }

Non-negotiable gates (every one must pass before ANY pricing output):
  G1 Freshness    : statement age <= pricing buffer, else bucket=STALE
  G2 Identity     : |Assets - (Liabilities + Equity)| / Assets < 1%, else CORRUPTION
  G3 Cross-check  : |reported_EPS * shares - net| / |net| < 10%, else CORRUPTION
                    (this is what a mixed-unit / wrong-share-count artifact trips)
  G4 Plausibility : 0 < P/E < 500 · 0 < P/B < 50 · debt/assets < 95% ·
                    |net| <= 35% x mcap · revenue <= 5 x mcap (non-financials)
                    a P/B that is 1000x/1e6x off is a units fault -> SCALE
  G5 Known issuer : company with a known sukuk listing but zero booked debt
                    -> "debt lines not scraped" (CORRUPTION)

A failure in any gate yields ok=false; the caller MUST withhold fair values,
zones, IRR and narrative and show the bucket + stated reasons instead.

Monetary inputs are expected in ACTUAL SAR with `shares` the real share count
(the engine's canonical). Market cap may be supplied in SAR (mc_sar) or millions
(mc_m); exactly one is required for the ratio gates.
"""
from typing import Any, Dict, List, Optional
import time

from app.services.rebh_data_guard_service import (
    verify_balance_sheet_identity,
    verify_net_income_plausibility,
)
from app.services.rebh_production_helper_service import evaluate_statement_freshness


# Bucket priority when several gates fail: the most damaging determines the label.
_BUCKET_PRIORITY = ("CORRUPTION", "SCALE", "STALE")


def evaluate_health(
    *,
    ta: Optional[float],
    tl: Optional[float],
    te: Optional[float],
    tot_debt: Optional[float],
    rev: Optional[float],
    net: Optional[float],
    eps_reported: Optional[float],
    shares: Optional[float],
    px: Optional[float],
    mc_sar: Optional[float],
    latest_period: Optional[str],
    is_financial: bool = False,
    has_sukuk: bool = False,
) -> Dict[str, Any]:
    """Pure G1-G5 evaluation on already-extracted figures. No I/O — this is the
    single place the health *rules* live; both the engine and the endpoint feed it."""
    reasons: List[str] = []
    structured: List[Dict[str, str]] = []
    buckets: set = set()

    def _fail(code: str, kind: str, bucket: str, label: str):
        reasons.append(label)
        structured.append({"code": code, "kind": kind, "label": label})
        buckets.add(bucket)

    # --- Presence: a record with no balance sheet / no income statement can't be priced
    has_bs = ta is not None and te is not None
    has_is = net is not None and (rev is not None or eps_reported is not None)
    if not (has_bs and has_is):
        _fail(
            "NO_FILINGS", "empty-statement", "CORRUPTION",
            "قوائم غير مكتملة المصدر (لا ميزانية أو لا قائمة دخل)"
        )

    # --- G1 Freshness
    fresh = evaluate_statement_freshness(latest_period, is_financial_sector=is_financial)
    if not fresh["is_fresh"]:
        _fail(
            "STALE", "stale", "STALE",
            fresh.get("stale_reason") or "قوائم متأخرة — تجاوزت حد الحداثة"
        )

    # --- G2 Accounting identity (relative 1% tolerance)
    if ta and tl is not None and te is not None and ta > 0:
        ident = verify_balance_sheet_identity(ta, tl, te, abs_tol=0.01 * ta)
        if not ident["is_valid"]:
            _fail(
                "BALANCE_IDENTITY", "corruption", "CORRUPTION",
                f"فشل التطابق المحاسبي |أ ≠ ع+م| ({ident['discrepancy']:,.0f} ريال)"
            )

    # --- G3 EPS x shares vs net cross-check (units / share-count coherence).
    # G3 targets a gross UNIT fault (a 1000x file, a share count off by orders of
    # magnitude) — see error.md "خلل وحدات/أسهم". Sub-10x gaps are legitimate
    # reporting-basis noise (reported basic EPS is weighted-average and, for interim
    # columns, often full-year, so it does not equal net_quarter / total_shares).
    # Aramco and Almarco land at ~1.0x; a mis-scaled file lands near 1000x/0.001x,
    # which this band still catches. 1213's real corruption is caught by the P/B gate.
    if eps_reported and shares and net and net != 0:
        ratio = (eps_reported * shares) / net
        if not (0.1 <= ratio <= 10.0):
            # Signature of an issuer filing error: the EPS cell carries the
            # net-income figure itself (ratio then equals the share count to
            # the unit, e.g. 1321's 31,500,000×). Say so plainly instead of
            # the generic units fault, and note that cash dividends — proven
            # independently by the CFF — are NOT withheld by this gate.
            if abs(eps_reported - net) <= 0.01 * abs(net):
                _fail(
                    "EPS_SHARES_CROSSCHECK", "corruption", "CORRUPTION",
                    "سطر ربحية السهم في إفصاح الشركة يعيد رقم صافي الربح نفسه "
                    "(خطأ إدخال من المصدر) — التسعير والنماذج محجوبان، بينما "
                    "التوزيعات النقدية المثبتة في قائمة التدفقات تُعرض مستقلة"
                )
            else:
                _fail(
                    "EPS_SHARES_CROSSCHECK", "corruption", "CORRUPTION",
                    f"EPS×الأسهم لا يوافق الصافي ({ratio:,.2f}×) — خلل وحدات/أسهم"
                )

    # --- Net-income plausibility (<=120% of revenue) — carried from the engine guards
    if net is not None and rev is not None and rev > 0:
        nip = verify_net_income_plausibility(net, rev)
        if not nip["is_plausible"]:
            _fail("NI_IMPLAUSIBLE", "corruption", "CORRUPTION", nip["reason"])

    # --- G4 Plausibility of market ratios (needs price + market cap + equity)
    eps_ttm = (net / shares) if (net is not None and shares and shares > 0) else None
    if px and px > 0 and eps_ttm and eps_ttm > 0:
        pe = px / eps_ttm
        if not (0 < pe < 500):
            _fail("IMPLAUSIBLE_PE", "scale", "SCALE",
                  f"P/E خارج المعقولية ({pe:,.1f}×) — خلل وحدات 🔌")
    if mc_sar and mc_sar > 0 and te and te > 0:
        pb = mc_sar / te
        if not (0 < pb <= 50):
            _fail("IMPLAUSIBLE_PB", "scale", "SCALE",
                  f"P/B خارج المعقولية ({pb:,.1f}×) — خلل وحدات 🔌")
    if not is_financial and ta and ta > 0 and tot_debt is not None:
        if tot_debt / ta >= 0.95:
            _fail("DEBT_ASSETS_HIGH", "corruption", "CORRUPTION",
                  f"الدين/الأصول {tot_debt / ta * 100:.0f}% ≥ 95%")
    if mc_sar and mc_sar > 0:
        if net is not None and abs(net) > 0.35 * mc_sar:
            _fail("NET_VS_MCAP", "corruption", "CORRUPTION",
                  "|صافي| يتجاوز 35% من القيمة السوقية")
        if (not is_financial) and rev is not None and rev > 5 * mc_sar:
            _fail("REV_VS_MCAP", "corruption", "CORRUPTION",
                  "الإيراد يتجاوز 5× القيمة السوقية")

    # --- G5 Known sukuk issuer with zero booked debt
    if has_sukuk and tot_debt == 0:
        _fail("DEBT_NOT_SCRAPED", "corruption", "CORRUPTION",
              "صكوك معروفة والدين = صفر — بنود الدين غير ممسوحة 🔌")

    bucket = "OK"
    for b in _BUCKET_PRIORITY:
        if b in buckets:
            bucket = b
            break

    metrics = {
        "eps_ttm": round(eps_ttm, 4) if eps_ttm is not None else None,
        "pe": round(px / eps_ttm, 2) if (px and eps_ttm and eps_ttm > 0) else None,
        "pb": round(mc_sar / te, 3) if (mc_sar and te and te > 0) else None,
        "days_elapsed": fresh.get("days_elapsed"),
        "buckets": sorted(buckets),
    }

    return {
        "ok": len(reasons) == 0,
        "bucket": bucket,
        "reasons": reasons,
        "structured": structured,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
#  Symbol-level + universe-level wrappers (feed the pure evaluator from disk)
# ---------------------------------------------------------------------------

_UNIVERSE_HEALTH_CACHE: Optional[List[Dict[str, Any]]] = None
_UNIVERSE_HEALTH_TS: float = 0.0
_UNIVERSE_HEALTH_TTL: float = 300.0  # 5 minutes


def _extract_inputs(symbol: str, prices_map: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Load a company's stored file, normalize units to full SAR (idempotent),
    and extract exactly the primitives evaluate_health consumes."""
    from app.services.xbrl_data_service import get_company
    from app.services.rebh_data_guard_service import (
        detect_scale_factor, scale_monetary_items, resolve_total_equity, _pick_basic_eps,
        SHARE_CAPITAL_LABELS,
    )

    company = get_company(symbol)
    if not company:
        return None
    meta = company.meta
    sec = getattr(meta, "sector", "Other") or "Other"
    is_financial = any(k in sec.lower() for k in ["bank", "financial", "insurance"])

    if prices_map is None:
        from app.services.khurafshi_engine_service import _get_latest_prices_map
        prices_map = _get_latest_prices_map()
    row = prices_map.get(str(symbol), {})
    px = row.get("close")
    mc_sar = row.get("market_cap")

    sections = company.sections or {}
    std_bs = sections.get("standardized_balance_sheet")
    std_is = sections.get("standardized_income_statement")

    bs_items = {it.label: it.values for it in (std_bs.items if std_bs else []) if not getattr(it, "is_unmapped", False)}
    is_items = {it.label: it.values for it in (std_is.items if std_is else []) if not getattr(it, "is_unmapped", False)}

    bs_periods = std_bs.periods if std_bs else []
    is_periods = std_is.periods if std_is else []
    latest_bs_p = bs_periods[-1] if bs_periods else None
    latest_is_p = is_periods[-1] if is_periods else None

    # Normalize mixed units to full SAR (skipped when the file is already canonical)
    if getattr(meta, "rounding", None) != "normalized_sar":
        factor, _ = detect_scale_factor(bs_items, is_items, mc_sar, px, latest_bs_p, latest_is_p)
        if factor != 1.0:
            scale_monetary_items(bs_items, factor)
            scale_monetary_items(is_items, factor)

    ta = bs_items.get("Total Assets", {}).get(latest_bs_p)
    tl = bs_items.get("Total Liabilities", {}).get(latest_bs_p)
    te = resolve_total_equity(bs_items, latest_bs_p)
    st_debt = (bs_items.get("Short-term Borrowings & Debt", {}).get(latest_bs_p)
               or bs_items.get("Current Portion of Long-term Debt", {}).get(latest_bs_p) or 0.0)
    lt_debt = (bs_items.get("Long-term Borrowings & Debt", {}).get(latest_bs_p)
               or bs_items.get("Debt securities, term loan, borrowings and sukuk in issue", {}).get(latest_bs_p) or 0.0)
    tot_debt = st_debt + lt_debt

    rev = is_items.get("Revenue / Turnover", {}).get(latest_is_p)
    net = (is_items.get("Net Profit for the Period", {}).get(latest_is_p)
           or is_items.get("Net Profit Attributable to Shareholders of Parent", {}).get(latest_is_p))
    eps_reported = _pick_basic_eps(is_items, latest_is_p)

    shares = (mc_sar / px) if (mc_sar and px and px > 0) else None
    if not shares:
        sc = None
        for lbl in SHARE_CAPITAL_LABELS:
            v = bs_items.get(lbl, {}).get(latest_bs_p)
            if isinstance(v, (int, float)) and v > 0:
                sc = v
                break
        shares = (sc / 10.0) if sc else None

    return {
        "symbol": symbol,
        "name": getattr(meta, "company_name", symbol),
        "en": getattr(meta, "name_en", None) or getattr(meta, "company_name_en", None) or getattr(meta, "company_name", symbol),
        "sec": sec,
        "sec_en": getattr(meta, "sector_en", None) or getattr(meta, "sec_en", None) or sec,
        "ta": ta, "tl": tl, "te": te, "tot_debt": tot_debt,
        "rev": rev, "net": net, "eps_reported": eps_reported,
        "shares": shares, "px": px, "mc_sar": mc_sar,
        "latest_period": latest_is_p or latest_bs_p,
        "is_financial": is_financial,
    }


def assess_health(symbol: str, prices_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load + extract + evaluate. Returns the health contract for one company.
    Adds a light sukuk signal for G5 without failing if the sukuk module is absent."""
    inps = _extract_inputs(symbol, prices_map)
    if inps is None:
        return {"ok": False, "bucket": "CORRUPTION", "symbol": symbol,
                "reasons": ["سجل غير موجود في قاعدة البيانات"],
                "structured": [{"code": "NO_RECORD", "kind": "no-filings", "label": "Record not found"}],
                "metrics": {}}
    has_sukuk = _has_known_sukuk(symbol)
    result = evaluate_health(
        ta=inps["ta"], tl=inps["tl"], te=inps["te"], tot_debt=inps["tot_debt"],
        rev=inps["rev"], net=inps["net"], eps_reported=inps["eps_reported"],
        shares=inps["shares"], px=inps["px"], mc_sar=inps["mc_sar"],
        latest_period=inps["latest_period"], is_financial=inps["is_financial"],
        has_sukuk=has_sukuk,
    )
    result.update({k: inps[k] for k in ("symbol", "name", "en", "sec", "sec_en", "px", "mc_sar")})
    result["mc"] = round(inps["mc_sar"] / 1_000_000, 2) if inps.get("mc_sar") else None
    return result


def _has_known_sukuk(symbol: str) -> bool:
    """True only when the company has a real (non-fallback) sukuk issuance on record."""
    try:
        from app.services.rebh_unified_engine import _get_buildup_sukuk_yield_safe
        info = _get_buildup_sukuk_yield_safe(symbol)
        return bool(info.get("symbol")) and not info.get("is_fallback", True)
    except Exception:
        return False


def assess_universe_health(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Health for every stored company. Cached (5 min) so the quarantine page and
    counters are cheap; break a gate and the company moves buckets on next refresh."""
    global _UNIVERSE_HEALTH_CACHE, _UNIVERSE_HEALTH_TS
    now = time.time()
    if not force_refresh and _UNIVERSE_HEALTH_CACHE is not None and (now - _UNIVERSE_HEALTH_TS) < _UNIVERSE_HEALTH_TTL:
        return _UNIVERSE_HEALTH_CACHE

    from app.services.xbrl_data_service import list_companies
    from app.services.khurafshi_engine_service import _get_latest_prices_map

    prices_map = _get_latest_prices_map()
    out: List[Dict[str, Any]] = []
    for c in list_companies():
        sym = str(c.symbol)
        inps = _extract_inputs(sym, prices_map)
        if inps is None:
            continue
        res = evaluate_health(
            ta=inps["ta"], tl=inps["tl"], te=inps["te"], tot_debt=inps["tot_debt"],
            rev=inps["rev"], net=inps["net"], eps_reported=inps["eps_reported"],
            shares=inps["shares"], px=inps["px"], mc_sar=inps["mc_sar"],
            latest_period=inps["latest_period"], is_financial=inps["is_financial"],
            has_sukuk=False,  # skip per-symbol sukuk I/O in the batch pass
        )
        res.update({k: inps[k] for k in ("symbol", "name", "en", "sec", "sec_en", "px")})
        res["mc"] = round(inps["mc_sar"] / 1_000_000, 2) if inps.get("mc_sar") else None
        out.append(res)

    _UNIVERSE_HEALTH_CACHE = out
    _UNIVERSE_HEALTH_TS = now
    return out
