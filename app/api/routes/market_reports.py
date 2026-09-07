from typing import List, Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
import threading
import logging

from app.core.security import verify_internal_key
from scripts.update_market_reports import main as update_market_reports_main

logger = logging.getLogger(__name__)

from app.core.database import get_db
from app.core.cache_helpers import cache_read_through
from datetime import timedelta
from app.models.market_reports import (
    SubstantialShareholder,
    NetShortPosition,
    ForeignHeadroom,
    ShareBuyback,
    SBLPosition,
    HistoricalReport,
    QFIOwnershipFlow,
)
from app.schemas.market_reports import (
    SubstantialShareholderResponse,
    NetShortPositionResponse,
    ForeignHeadroomResponse,
    ShareBuybackResponse,
    SBLPositionResponse,
    HistoricalReportResponse,
    QFIOwnershipFlowResponse,
)

router = APIRouter()

@router.get("/substantial-shareholders", response_model=List[SubstantialShareholderResponse])
def get_substantial_shareholders(
    report_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(SubstantialShareholder)
    if report_date:
        query = query.filter(SubstantialShareholder.report_date == report_date)
    else:
        latest = db.query(SubstantialShareholder.report_date).order_by(desc(SubstantialShareholder.report_date)).first()
        if latest:
            query = query.filter(SubstantialShareholder.report_date == latest[0])
            
    return query.order_by(SubstantialShareholder.company_name).all()

@router.get("/net-short-positions", response_model=List[NetShortPositionResponse])
def get_net_short_positions(
    report_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(NetShortPosition)
    if report_date:
        query = query.filter(NetShortPosition.report_date == report_date)
    else:
        latest = db.query(NetShortPosition.report_date).order_by(desc(NetShortPosition.report_date)).first()
        if latest:
            query = query.filter(NetShortPosition.report_date == latest[0])
            
    return query.order_by(NetShortPosition.symbol).all()

@router.get("/foreign-headroom", response_model=List[ForeignHeadroomResponse])
def get_foreign_headroom(
    report_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(ForeignHeadroom)
    if report_date:
        query = query.filter(ForeignHeadroom.report_date == report_date)
    else:
        latest = db.query(ForeignHeadroom.report_date).order_by(desc(ForeignHeadroom.report_date)).first()
        if latest:
            query = query.filter(ForeignHeadroom.report_date == latest[0])
            
    return query.order_by(ForeignHeadroom.symbol).all()

@router.get("/share-buybacks", response_model=List[ShareBuybackResponse])
def get_share_buybacks(
    report_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(ShareBuyback)
    if report_date:
        query = query.filter(ShareBuyback.report_date == report_date)
    else:
        latest = db.query(ShareBuyback.report_date).order_by(desc(ShareBuyback.report_date)).first()
        if latest:
            query = query.filter(ShareBuyback.report_date == latest[0])
            
    return query.order_by(ShareBuyback.symbol).all()

@router.get("/sbl-positions", response_model=List[SBLPositionResponse])
def get_sbl_positions(
    report_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(SBLPosition)
    if report_date:
        query = query.filter(SBLPosition.report_date == report_date)
    else:
        latest = db.query(SBLPosition.report_date).order_by(desc(SBLPosition.report_date)).first()
        if latest:
            query = query.filter(SBLPosition.report_date == latest[0])
            
    return query.order_by(SBLPosition.symbol).all()

@router.get("/qfi-flows", response_model=List[QFIOwnershipFlowResponse])
def get_qfi_flows(
    report_date: Optional[date] = None,
    symbol: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(QFIOwnershipFlow)
    if report_date:
        query = query.filter(QFIOwnershipFlow.report_date == report_date)
    else:
        latest = db.query(QFIOwnershipFlow.report_date).order_by(desc(QFIOwnershipFlow.report_date)).first()
        if latest:
            query = query.filter(QFIOwnershipFlow.report_date == latest[0])
            
    if symbol:
        query = query.filter((QFIOwnershipFlow.symbol == symbol) | (QFIOwnershipFlow.normalized_symbol == symbol))
        
    return query.order_by(desc(QFIOwnershipFlow.qfi_holding_percent)).all()

@router.get("/historical-reports", response_model=List[HistoricalReportResponse])
def get_historical_reports(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(HistoricalReport)
    if start_date:
        query = query.filter(HistoricalReport.report_date >= start_date)
    if end_date:
        query = query.filter(HistoricalReport.report_date <= end_date)
            
    return query.order_by(desc(HistoricalReport.report_date)).all()

def _period_min_date(period: str) -> Optional[date]:
    today = date.today()
    period = period.upper()
    if period == "5D": return today - timedelta(days=7)
    if period == "1M": return today - timedelta(days=30)
    if period == "6M": return today - timedelta(days=180)
    if period == "1Y": return today - timedelta(days=365)
    if period == "5Y": return today - timedelta(days=365 * 5)
    if period == "10Y": return today - timedelta(days=365 * 10)
    return None

@router.get("/historical-reports/tasi-chart")
async def get_tasi_chart(
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
    db: Session = Depends(get_db)
):
    try:
        cache_key = f"market_reports:tasi_chart:period:ALL"
        async def fetch():
            query = db.query(
                HistoricalReport.report_date,
                HistoricalReport.close_price
            ).order_by(HistoricalReport.report_date.asc())
            
            results = query.all()
            return [
                {"time": row[0].isoformat(), "value": float(row[1].replace(',', '')) if row[1] else 0}
                for row in results
            ]
            
        data = await cache_read_through(cache_key, 86400, fetch)
        
        if period.upper() != "ALL":
            target_date = _period_min_date(period)
            if target_date:
                target_date_str = target_date.isoformat()
                return [d for d in data if d["time"] >= target_date_str]
        return data
    except Exception as e:
        logger.error(f"Error loading TASI chart data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/scrape")
def trigger_scrape_market_reports(_: bool = Depends(verify_internal_key)):
    def scrape_with_error_handling():
        try:
            logger.info("Starting market reports scraping via API trigger")
            update_market_reports_main()
            logger.info("Market reports scraping completed successfully")
        except Exception as e:
            logger.error(f"Market reports scraping failed: {e}")
            
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "Market reports scraping started in background"}


@router.get("/scrape/daily-financial-indicators")
def trigger_scrape_daily_financial_indicators(_: bool = Depends(verify_internal_key)):
    def scrape_with_error_handling():
        try:
            logger.info("Starting Daily Financial Indicators scraping via API trigger")
            from app.scrapers.daily_financial_indicators_scraper import run_scraper_and_save_to_db
            run_scraper_and_save_to_db()
            logger.info("Daily Financial Indicators scraping completed successfully")
        except Exception as e:
            logger.error(f"Daily Financial Indicators scraping failed: {e}")

    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "Daily Financial Indicators scraping started in background"}

