from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_
from typing import List, Dict, Any
import asyncio
import os
from datetime import datetime

from app.core.database import get_db
from app.core.security import verify_internal_key
from app.services.storage import storage_service
from app.models.official_filings import CompanyOfficialFiling, FilingCategory, FilingPeriod, FileType, FilingLanguage
from app.models.scraped_reports import Company
from app.schemas.official_filings import IngestOfficialFilingsRequest

router = APIRouter()

# --- Helpers ---

def map_category(cat_str: str) -> FilingCategory:
    # Maps scraper strings to Enum
    try:
        return FilingCategory(cat_str)
    except ValueError:
        # Fallback partial matching or return None
        if "financial" in cat_str.lower(): return FilingCategory.FINANCIAL_STATEMENTS
        if "xbrl" in cat_str.lower(): return FilingCategory.XBRL
        if "board" in cat_str.lower(): return FilingCategory.BOARD_REPORT
        if "esg" in cat_str.lower(): return FilingCategory.ESG_REPORT
        raise ValueError(f"Unknown category: {cat_str}")

def map_period(per_str: str) -> FilingPeriod:
    try:
        if not per_str: return FilingPeriod.ANNUAL
        return FilingPeriod(per_str)
    except ValueError:
        return FilingPeriod.ANNUAL # Default

def map_file_type(ft_str: str) -> FileType:
    ft_map = {'pdf': FileType.PDF, 'excel': FileType.EXCEL, 'xls': FileType.EXCEL, 'xlsx': FileType.EXCEL}
    key = ft_str.lower()
    if key in ft_map: return ft_map[key]
    return FileType.OTHER

async def process_ingestion(symbol: str, items: List[Dict[str, Any]], db_session_factory, language: str = 'en'):
    """
    Background task to process items: Download -> S3 -> DB.
    """
    # Create a new session for background task
    db = db_session_factory()
    try:
        # Auto-create company if not exists
        existing_company = db.query(Company).filter(Company.symbol == symbol).first()
        if not existing_company:
            print(f"🏢 Company {symbol} not found. Creating automatically...")
            new_company = Company(symbol=symbol, name_en=f"Company {symbol}")
            db.add(new_company)
            db.commit()
            print(f"✅ Company {symbol} created.")
        
        for item in items:
            source_url = item.get('url')
            local_path = item.get('local_path')
            
            if not source_url and not local_path: continue # Skip if neither exist

            # Loop duplication check removed as it was faulty (missing symbol check)
            # and we trust the pre-filtering done in the route handler.

            try:
                # Prepare data
                year = item.get('year', 'Unknown')
                period = item.get('period', 'Annual')
                category = item.get('category_enum').value
                
                # Determine extension
                local_path = item.get('local_path')
                ext = 'pdf' # Default
                
                if item.get('file_type') == 'pdf':
                    ext = 'pdf'
                elif item.get('file_type') == 'excel':
                    # Check local path for true extension
                    if local_path and local_path.lower().endswith('.xls'):
                        ext = 'xls'
                    else:
                        ext = 'xlsx'
                
                # Sanitize filename
                filename = f"{period}_{category}".replace(" ", "_")
                destination_path = f"{symbol}/{year}/{language}/{filename}.{ext}"
                
                print(f"⬇️ Processing {filename} ({'Local' if local_path else 'Download'})...")
                
                public_url = ""
                if local_path and os.path.exists(local_path):
                     mime = 'application/pdf' if ext == 'pdf' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                     public_url = await storage_service.upload_local_file(local_path, destination_path, mime)
                elif source_url:
                     # Fallback to download
                     public_url = await storage_service.upload_file_from_url(source_url, destination_path)
                else:
                    print(f"⚠️  Skipping {filename}: No local path and no URL")
                    continue
                     
                print(f"✅ Uploaded to {public_url}")

                # Create DB Record
                new_record = CompanyOfficialFiling(
                    company_symbol=symbol,
                    category=item.get('category_enum'),
                    period=item.get('period_enum'),
                    year=int(year) if str(year).isdigit() else 0,
                    published_date=item.get('date_obj'),
                    file_url=public_url,
                    source_url=source_url,
                    file_type=item.get('file_type_enum'),
                    file_size_bytes=0,
                    language=FilingLanguage(language)
                )
                db.add(new_record)
                db.commit()

            except Exception as e:
                print(f"❌ Failed to process report {source_url}: {e}")
                db.rollback()

    except Exception as e:
        print(f"❌ Critical error in ingestion task: {e}")
    finally:
        db.close()


@router.post("/ingest/official-reports")
async def ingest_official_reports(
    payload: IngestOfficialFilingsRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_key),
):
    """
    Ingest metadata + triggers background file download/upload.
    """
    _ = _auth
    
    products_to_process = []
    
    # Pre-filter: Check what is new
    for category_name, items in payload.data.items():
        try:
            cat_enum = map_category(category_name)
        except ValueError:
            continue # Skip unknown categories
        
        for item in items:
            # Allow if URL exists OR local path exists
            if not item.url and not item.local_path: 
                continue
            
            # Check if exists in DB
            # If URL exists, check by URL
            exists = None
            try:
                if item.url:
                    exists = db.query(CompanyOfficialFiling).filter(
                        CompanyOfficialFiling.source_url == item.url,
                        CompanyOfficialFiling.company_symbol == payload.symbol,
                        CompanyOfficialFiling.language == payload.language  # CRITICAL: Check language here too
                    ).first()
                elif item.local_path:
                    # If no URL, check by Year/Period/Category to avoid duplicates
                    try:
                        per_enum_check = map_period(item.period)
                        cat_enum_check = map_category(category_name)
                    except ValueError as e:
                        print(f"⚠️ Validation skipped for {item.local_path}: {e}")
                        continue

                    exists = db.query(CompanyOfficialFiling).filter(
                        CompanyOfficialFiling.company_symbol == payload.symbol,
                        CompanyOfficialFiling.year == int(item.year) if str(item.year).isdigit() else 0,
                        CompanyOfficialFiling.period == per_enum_check,
                        CompanyOfficialFiling.category == cat_enum_check,
                        CompanyOfficialFiling.language == payload.language  # CRITICAL: Check language too
                    ).first()
            except Exception as e:
                print(f"❌ DB Check Error: {e}")
                continue
            
            if exists:
                continue
                
            # Prepare data for processing
            try:
                per_enum = map_period(item.period)
                ft_enum = map_file_type(item.file_type)
            except Exception as e:
                print(f"❌ Mapping Error for {item}: {e}")
                continue
            
            # Parse date if possible (scraper returns text like '2025-01-01' or similar)
            # You might need a date parser helper here
            date_obj = None 
            # (Skipping robust date parsing for brevity, let's assume NULL or implement simple)
            
            products_to_process.append({
                "url": item.url,
                "category_enum": cat_enum,
                "period_enum": per_enum,
                "file_type_enum": ft_enum,
                "year": item.year,
                "period": item.period,
                "file_type": item.file_type,
                "date_obj": date_obj,
                "local_path": item.local_path
            })
    
    if not products_to_process:
        return {"message": "No new reports to ingest."}

    # Pass a Session Factory or handle session carefully. 
    # FastAPI's Depends(get_db) session is closed after request.
    # We need a new session factory logic. 
    from app.core.database import SessionLocal
    
    background_tasks.add_task(process_ingestion, payload.symbol, products_to_process, SessionLocal, payload.language)
    
    return {"message": f"Queued {len(products_to_process)} reports for background processing."}


@router.get("/reports/{symbol}")
def get_company_reports(symbol: str, db: Session = Depends(get_db)):
    """
    Get official filings grouped by category for frontend.
    """
    reports = db.query(CompanyOfficialFiling).filter(
        CompanyOfficialFiling.company_symbol == symbol
    ).all()
    
    # Structure for frontend: { "Financial Statements": [items...], ... }
    grouped = {
        FilingCategory.FINANCIAL_STATEMENTS.value: [],
        FilingCategory.XBRL.value: [],
        FilingCategory.BOARD_REPORT.value: [],
        FilingCategory.ESG_REPORT.value: []
    }
    
    for r in reports:
        item = {
            "id": r.id,
            "period": r.period.value,
            "year": r.year,
            "file_url": r.file_url,
            "published_date": r.published_date,
            "file_type": r.file_type.value if r.file_type else None,
            "language": r.language.value if r.language else 'en'
        }
        if r.category.value in grouped:
            grouped[r.category.value].append(item)
            
    return grouped


# ═══════════════════════════════════════════════════════
# ══  Admin Endpoints for Reports Management Dashboard ══
# ═══════════════════════════════════════════════════════

from sqlalchemy import func as sql_func, distinct

@router.get("/reports/admin/summary")
def get_admin_reports_summary(
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_key),
):
    """
    Returns a summary of all companies with filing counts, grouped by language.
    Used by the Admin Reports Management dashboard.
    """
    _ = _auth
    # Subquery: count filings per symbol, language
    results = (
        db.query(
            CompanyOfficialFiling.company_symbol,
            CompanyOfficialFiling.language,
            sql_func.count(CompanyOfficialFiling.id).label("count"),
            sql_func.max(CompanyOfficialFiling.created_at).label("last_updated"),
        )
        .group_by(CompanyOfficialFiling.company_symbol, CompanyOfficialFiling.language)
        .order_by(CompanyOfficialFiling.company_symbol)
        .all()
    )

    # Build structured response
    companies: Dict[str, Any] = {}
    for row in results:
        sym = row.company_symbol
        if sym not in companies:
            companies[sym] = {
                "symbol": sym,
                "en_count": 0,
                "ar_count": 0,
                "total_count": 0,
                "last_updated": None,
            }
        lang_val = row.language.value if hasattr(row.language, 'value') else str(row.language)
        if lang_val == 'en':
            companies[sym]["en_count"] = row.count
        else:
            companies[sym]["ar_count"] = row.count
        companies[sym]["total_count"] += row.count

        # Track the most recent update
        if row.last_updated:
            ts = row.last_updated.isoformat() if row.last_updated else None
            if companies[sym]["last_updated"] is None or (ts and ts > companies[sym]["last_updated"]):
                companies[sym]["last_updated"] = ts

    return {
        "total_companies": len(companies),
        "total_filings": sum(c["total_count"] for c in companies.values()),
        "companies": list(companies.values()),
    }


@router.get("/reports/admin/{symbol}/details")
def get_admin_company_details(
    symbol: str,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_key),
):
    """
    Returns detailed filing records for a specific company symbol.
    """
    _ = _auth
    filings = (
        db.query(CompanyOfficialFiling)
        .filter(CompanyOfficialFiling.company_symbol == symbol)
        .order_by(CompanyOfficialFiling.year.desc(), CompanyOfficialFiling.category, CompanyOfficialFiling.period)
        .all()
    )

    items = []
    for f in filings:
        items.append({
            "id": f.id,
            "category": f.category.value,
            "period": f.period.value,
            "year": f.year,
            "file_url": f.file_url,
            "source_url": f.source_url,
            "file_type": f.file_type.value if f.file_type else None,
            "language": f.language.value if f.language else 'en',
            "created_at": f.created_at.isoformat() if f.created_at else None,
        })

    return {
        "symbol": symbol,
        "total": len(items),
        "filings": items,
    }


@router.delete("/reports/admin/{symbol}/{filing_id}")
def delete_single_filing(
    symbol: str,
    filing_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_key),
):
    """
    Delete a single filing record from DB (and its R2 file).
    """
    _ = _auth
    filing = (
        db.query(CompanyOfficialFiling)
        .filter(CompanyOfficialFiling.id == filing_id, CompanyOfficialFiling.company_symbol == symbol)
        .first()
    )
    if not filing:
        raise HTTPException(status_code=404, detail="Filing not found")

    # Try to delete from R2
    try:
        if filing.file_url:
            # Extract the S3 key from the public URL
            # e.g. https://pub-xxx.r2.dev/4322/2024/en/Annual_Financial_Statements.pdf → 4322/2024/en/Annual_Financial_Statements.pdf
            url = filing.file_url
            # Find the key after the domain
            parts = url.split("/")
            # The key is everything after the domain (3rd slash onwards)
            if len(parts) > 3:
                s3_key = "/".join(parts[3:])
                import boto3
                s3_endpoint = os.getenv("S3_ENDPOINT")
                s3_access = os.getenv("S3_ACCESS_KEY")
                s3_secret = os.getenv("S3_SECRET_KEY")
                s3_bucket = os.getenv("S3_BUCKET_NAME")
                if s3_endpoint and s3_access and s3_secret and s3_bucket:
                    s3 = boto3.client('s3',
                        endpoint_url=s3_endpoint,
                        aws_access_key_id=s3_access,
                        aws_secret_access_key=s3_secret
                    )
                    s3.delete_object(Bucket=s3_bucket, Key=s3_key)
                    print(f"🗑️ Deleted R2 object: {s3_key}")
    except Exception as e:
        print(f"⚠️ R2 delete failed (non-critical): {e}")

    # Delete DB record
    db.delete(filing)
    db.commit()

    return {"message": f"Filing {filing_id} deleted for {symbol}"}
