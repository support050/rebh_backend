# backend/app/api/routes/scraper.py
"""
FastAPI router for scraped financial data integration.
Handles ingestion from Playwright scraper and data retrieval.
"""
from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import desc
from datetime import datetime
from typing import Optional, List
import json

from app.core.database import get_db
from app.core.redis import redis_cache
from app.core.security import verify_internal_key
from app.models.scraped_reports import Company, FinancialReport, PeriodType, ReportType
from app.schemas.scraped_financials import (
    IngestRequest, IngestResponse, BulkIngestRequest, BulkIngestResponse,
    HistoricalFinancialsResponse, FinancialPeriodData,
    FinancialTableResponse, FinancialTableRow,
    CompanyResponse, PeriodTypeEnum, ReportTypeEnum
)

router = APIRouter(prefix="/api/scraper", tags=["Scraper Integration"])

FINANCIALS_CACHE_TTL = 7 * 24 * 60 * 60  # 7 days


# ==================== Ingest Endpoints ====================

@router.post("/ingest", response_model=IngestResponse)
async def ingest_scraped_data(
    request: IngestRequest,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_key),
):
    """
    Receives scraped data from the Playwright script and saves to PostgreSQL.
    Uses UPSERT logic to avoid duplicates based on:
    (company_symbol, period_type, report_type, period_end_date)
    """
    try:
        print(f"📥 Ingesting data for company: {request.company_symbol}")
        
        # 1. Upsert Company
        company_data = {
            "symbol": request.company_symbol,
            "name_en": request.company_name_en,
            "name_ar": request.company_name_ar,
            "sector": request.sector,
            "last_scraped_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        stmt = pg_insert(Company).values(company_data)
        stmt = stmt.on_conflict_do_update(
            index_elements=['symbol'],
            set_={
                'name_en': stmt.excluded.name_en,
                'name_ar': stmt.excluded.name_ar,
                'sector': stmt.excluded.sector,
                'last_scraped_at': stmt.excluded.last_scraped_at,
                'updated_at': stmt.excluded.updated_at
            }
        )
        db.execute(stmt)
        
        # 2. Process each report
        created_count = 0
        updated_count = 0
        
        for report in request.reports:
            report_data = {
                "company_symbol": request.company_symbol,
                "period_type": PeriodType(report.period_type.value),
                "report_type": ReportType(report.report_type.value),
                "period_end_date": report.period_end_date,
                "metrics": report.metrics,
                "raw_data": report.raw_data
            }
            
            # Check if exists
            existing = db.query(FinancialReport).filter(
                FinancialReport.company_symbol == request.company_symbol,
                FinancialReport.report_type == report.report_type.value,
                FinancialReport.period_type == report.period_type.value,
                FinancialReport.period_end_date == report.period_end_date
            ).first()
            
            if existing:
                # Update existing
                existing.metrics = report.metrics
                existing.raw_data = report.raw_data
                existing.updated_at = datetime.utcnow()
                updated_count += 1
            else:
                # Create new
                db.add(FinancialReport(**report_data))
                db.flush()  # Ensure it's visible for duplicate checks within same transaction
                created_count += 1
        
        db.commit()
        
        # 3. Invalidate Redis cache for this symbol
        await redis_cache.delete(f"scraper:financials:{request.company_symbol}")
        
        table_keys = await redis_cache.keys(f"scraper:table:{request.company_symbol}:*")
        for k in table_keys:
            await redis_cache.delete(k)
            
        from app.core.cache_helpers import (
            invalidate_prices_latest,
            invalidate_prices_history,
            invalidate_rs_data,
            invalidate_rs_v2_data,
            invalidate_screener_data,
            invalidate_technical_screener_data,
            invalidate_industry_groups_data
        )
        await invalidate_prices_latest()
        await invalidate_prices_history()
        await invalidate_rs_data()
        await invalidate_rs_v2_data()
        await invalidate_screener_data()
        await invalidate_technical_screener_data()
        await invalidate_industry_groups_data()
        
        print(f"✅ Ingested {len(request.reports)} reports for {request.company_symbol} (Created: {created_count}, Updated: {updated_count})")
        
        return IngestResponse(
            success=True,
            message=f"Successfully processed {len(request.reports)} reports",
            company_symbol=request.company_symbol,
            reports_processed=len(request.reports),
            reports_created=created_count,
            reports_updated=updated_count
        )
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error ingesting data for {request.company_symbol}: {e}")
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")


@router.post("/ingest/bulk", response_model=BulkIngestResponse)
async def bulk_ingest_scraped_data(
    request: BulkIngestRequest,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_key),
):
    """
    Bulk ingestion for multiple companies at once.
    Useful when scraping many companies in a batch.
    """
    _ = _auth
    total_processed = 0
    failed = []
    
    for company_request in request.companies:
        try:
            # Auth already verified for this request; pass True for internal call
            result = await ingest_scraped_data(company_request, db, True)
            total_processed += result.reports_processed
        except Exception as e:
            failed.append(company_request.company_symbol)
            print(f"❌ Failed to ingest {company_request.company_symbol}: {e}")
    
    return BulkIngestResponse(
        success=len(failed) == 0,
        message=f"Processed {len(request.companies)} companies, {len(failed)} failed",
        total_companies=len(request.companies),
        total_reports_processed=total_processed,
        failed_companies=failed
    )
# ==================== Financials Retrieval Endpoints ====================

@router.get("/financials/{symbol}", response_model=HistoricalFinancialsResponse)
async def get_historical_financials(
    symbol: str,
    period_type: Optional[PeriodTypeEnum] = Query(None, description="Filter by period type"),
    db: Session = Depends(get_db)
):
    """
    Returns all historical financial data for a company, ordered by period_end_date.
    Groups data by report type (balance_sheets, income_statements, cash_flows).
    """
    try:
        # Check Redis cache first
        cache_key = f"scraper:financials:{symbol}"
        cached = await redis_cache.get(cache_key)
        if cached:
            return json.loads(cached)
        
        # Get company info
        company = db.query(Company).filter(Company.symbol == symbol).first()
        company_name = company.name_en if company else None
        
        # Build query
        query = db.query(FinancialReport).filter(
            FinancialReport.company_symbol == symbol
        )
        
        if period_type:
            query = query.filter(FinancialReport.period_type == period_type.value)
        
        reports = query.order_by(desc(FinancialReport.period_end_date)).all()
        
        # Group by report type
        balance_sheets = []
        income_statements = []
        cash_flows = []
        
        for report in reports:
            period_data = FinancialPeriodData(
                period_end_date=report.period_end_date,
                period_type=PeriodTypeEnum(report.period_type),
                metrics=report.metrics or {}
            )
            
            if report.report_type == ReportType.Balance_Sheet.value:
                balance_sheets.append(period_data)
            elif report.report_type == ReportType.Income_Statement.value:
                income_statements.append(period_data)
            elif report.report_type == ReportType.Cash_Flows.value:
                cash_flows.append(period_data)
        
        response = HistoricalFinancialsResponse(
            symbol=symbol,
            company_name=company_name,
            balance_sheets=balance_sheets,
            income_statements=income_statements,
            cash_flows=cash_flows
        )
        
        await redis_cache.set(cache_key, response.model_dump_json(), expire=FINANCIALS_CACHE_TTL)
        
        return response
        
    except Exception as e:
        print(f"❌ Error getting financials for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving financials: {str(e)}")


@router.get("/financials/{symbol}/table", response_model=FinancialTableResponse)
async def get_financial_table(
    symbol: str,
    report_type: ReportTypeEnum = Query(..., description="Report type"),
    period_type: PeriodTypeEnum = Query(PeriodTypeEnum.ANNUALLY, description="Period type"),
    limit: int = Query(12, ge=1, le=50, description="Number of periods to return"),
    db: Session = Depends(get_db)
):
    """
    Returns financial data formatted as a table for frontend display.
    Years/Quarters as columns, metrics as rows.
    """
    try:
        # Cache key
        cache_key = f"scraper:table:{symbol}:{report_type.value}:{period_type.value}:{limit}"
        cached = await redis_cache.get(cache_key)
        if cached:
            return json.loads(cached)
        
        # Query reports
        reports = db.query(FinancialReport).filter(
            FinancialReport.company_symbol == symbol,
            FinancialReport.report_type == report_type.value,
            FinancialReport.period_type == period_type.value
        ).order_by(desc(FinancialReport.period_end_date)).limit(limit).all()
        
        if not reports:
            raise HTTPException(status_code=404, detail=f"No {report_type.value} data found for {symbol}")
        
        # Get all unique metric names across all periods
        all_metrics = set()
        for report in reports:
            if report.metrics:
                all_metrics.update(report.metrics.keys())
        
        # Build periods list (column headers)
        periods = [str(r.period_end_date) for r in reports]
        
        # Build rows (metrics with values per period)
        rows = []
        for metric_name in sorted(all_metrics):
            values = {}
            for report in reports:
                period_key = str(report.period_end_date)
                if report.metrics:
                    values[period_key] = report.metrics.get(metric_name, None)
                else:
                    values[period_key] = None
            
            rows.append(FinancialTableRow(
                metric_name=metric_name,
                values=values
            ))
        
        response = FinancialTableResponse(
            symbol=symbol,
            report_type=report_type,
            period_type=period_type,
            periods=periods,
            rows=rows
        )
        
        await redis_cache.set(cache_key, response.model_dump_json(), expire=FINANCIALS_CACHE_TTL)
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting financial table for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving financial table: {str(e)}")


# ==================== Company Endpoints ====================

@router.get("/companies", response_model=List[CompanyResponse])
async def get_all_companies(
    db: Session = Depends(get_db)
):
    """
    Returns list of all companies in the database.
    """
    companies = db.query(Company).order_by(Company.symbol).all()
    return companies


@router.get("/companies/{symbol}", response_model=CompanyResponse)
async def get_company(
    symbol: str,
    db: Session = Depends(get_db)
):
    """
    Returns company information by symbol.
    """
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        raise HTTPException(status_code=404, detail=f"Company {symbol} not found")
    return company
