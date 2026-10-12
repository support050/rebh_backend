import json
import os
import logging
from pathlib import Path
import boto3
from botocore.config import Config

from app.schemas.xbrl_financials import CompanyFinancials, CompanyListItem

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent.parent / "output"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(_DEFAULT_OUTPUT)))
if not OUTPUT_DIR.exists() and _DEFAULT_OUTPUT.exists():
    OUTPUT_DIR = _DEFAULT_OUTPUT

# R2 Configuration
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "lumivst-xbrl")

def _get_r2_client():
    if not R2_ACCOUNT_ID or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
        return None
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
    )


def _all_json_files() -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []
    return sorted(OUTPUT_DIR.glob("*_financials.json"))


def _load_compiled_index() -> list[CompanyListItem]:
    """Bulk metadata source: local companies_list.json, else the R2 copy.

    The compiled index is fast and is the ONLY source that lists companies whose
    financials live remotely (not on this disk). It is a snapshot though, and can
    lag the real set of available financials — list_companies() treats it as one
    input, never the whole truth."""
    local_index = OUTPUT_DIR / "companies_list.json"
    if local_index.exists():
        try:
            with open(local_index, encoding="utf-8") as f:
                return [CompanyListItem(**item) for item in json.load(f)]
        except Exception:
            pass
    client = _get_r2_client()
    if client:
        try:
            obj = client.get_object(Bucket=R2_BUCKET_NAME, Key="companies_list.json")
            data = json.loads(obj["Body"].read().decode("utf-8"))
            return [CompanyListItem(**item) for item in data]
        except Exception:
            pass
    return []


def _item_from_file(fp: Path) -> CompanyListItem | None:
    """Build one list entry straight from a *_financials.json on disk."""
    try:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    meta = data.get("meta", {})
    all_periods: set[str] = set()
    for sec in data.get("sections", {}).values():
        all_periods.update(sec.get("periods", []))
    return CompanyListItem(
        symbol=meta.get("symbol", fp.name[: -len("_financials.json")]),
        company_name=meta.get("company_name", "Unknown"),
        name_en=meta.get("name_en") or meta.get("company_name") or None,
        sector=meta.get("sector"),
        report_end=meta.get("report_end"),
        periods_count=len(all_periods),
    )


def list_companies() -> list[CompanyListItem]:
    """Valuation universe = every company whose financial data actually exists.

    The compiled companies_list.json index goes stale: on 2026-10-07 it held 238
    symbols while 257 *_financials.json were on disk, so 19 fully-valuatable
    companies vanished from every valuation page purely because nobody re-ran the
    index builder. Membership is therefore taken from the ACTUAL artifacts —
    local financial files unioned with the compiled index (which also carries the
    R2-only names) — not from the frozen index alone, and never invented for a
    symbol that has no financials (those cannot be valued, so they stay out). A
    new company joins the universe the moment its *_financials.json appears,
    ahead of the next index rebuild."""
    by_symbol: dict[str, CompanyListItem] = {}

    # 1. Compiled index (local then R2) — cheap bulk source, knows remote-only names.
    for item in _load_compiled_index():
        by_symbol[str(item.symbol)] = item

    # 2. Local files — ground truth for what is valuatable right now. Only files
    #    missing from the index are parsed, so a healthy index costs one directory
    #    listing, not hundreds of JSON reads.
    index_syms = set(by_symbol)
    admitted = 0
    for fp in _all_json_files():
        sym = fp.name[: -len("_financials.json")]
        if sym in index_syms:
            continue
        item = _item_from_file(fp)
        if item is not None:
            by_symbol[str(item.symbol)] = item
            admitted += 1
    if admitted:
        logger.warning(
            "⚠️ STALE INDEX: %d company(s) had local financials but were missing "
            "from companies_list.json — admitted to the universe anyway. Re-run "
            "scripts/upload_to_r2.py to refresh the index.", admitted,
        )

    if by_symbol:
        return sorted(by_symbol.values(), key=lambda c: str(c.symbol))

    # 3. No index and no readable files — empty universe (callers already guard).
    return []


def get_company(symbol: str) -> CompanyFinancials | None:
    # 1. Local fallback first (fastest)
    fp = OUTPUT_DIR / f"{symbol}_financials.json"
    if fp.exists():
        try:
            with open(fp, encoding="utf-8") as f:
                raw = json.load(f)
            if "meta" not in raw:
                raw["meta"] = {}
            if not raw["meta"].get("symbol"):
                raw["meta"]["symbol"] = symbol
            if not raw["meta"].get("company_name"):
                raw["meta"]["company_name"] = symbol
            return CompanyFinancials(**raw)
        except Exception:
            pass

    # 2. Remote R2 fallback
    client = _get_r2_client()
    if client:
        try:
            key = f"xbrl/{symbol}_financials.json"
            obj = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
            raw = json.loads(obj["Body"].read().decode("utf-8"))
            return CompanyFinancials(**raw)
        except Exception:
            pass

    return None


def save_company(symbol: str, data: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fp = OUTPUT_DIR / f"{symbol}_financials.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return fp

