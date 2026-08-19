import json
import os
from pathlib import Path
import boto3
from botocore.config import Config

from app.schemas.xbrl_financials import CompanyFinancials, CompanyListItem

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


def list_companies() -> list[CompanyListItem]:
    client = _get_r2_client()
    if client:
        try:
            # Try to fetch compiled index from R2
            obj = client.get_object(Bucket=R2_BUCKET_NAME, Key="companies_list.json")
            data = json.loads(obj["Body"].read().decode("utf-8"))
            return [CompanyListItem(**item) for item in data]
        except Exception:
            # Fallback to local index build if R2 read fails
            pass

    # Local fallback
    result: list[CompanyListItem] = []
    for fp in _all_json_files():
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("meta", {})
            all_periods: set[str] = set()
            for sec in data.get("sections", {}).values():
                all_periods.update(sec.get("periods", []))
            result.append(
                CompanyListItem(
                    symbol=meta.get("symbol", fp.stem.split("_")[0]),
                    company_name=meta.get("company_name", "Unknown"),
                    sector=meta.get("sector"),
                    report_end=meta.get("report_end"),
                    periods_count=len(all_periods),
                )
            )
        except Exception:
            continue
    return result


def get_company(symbol: str) -> CompanyFinancials | None:
    client = _get_r2_client()
    if client:
        try:
            key = f"xbrl/{symbol}_financials.json"
            obj = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
            raw = json.loads(obj["Body"].read().decode("utf-8"))
            return CompanyFinancials(**raw)
        except Exception:
            pass  # Fall through to local fallback

    # Local fallback
    fp = OUTPUT_DIR / f"{symbol}_financials.json"
    if not fp.exists():
        return None
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
        return None


def save_company(symbol: str, data: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fp = OUTPUT_DIR / f"{symbol}_financials.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return fp

