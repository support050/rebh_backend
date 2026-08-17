from typing import List, Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.core.database import get_db
from app.models.economic_indicators import EconomicIndicator, TreasuryYieldCurve, SP500History, EurodollarFutures, CmeFedwatch
from app.schemas.economic_indicators import EconomicIndicatorResponse, TreasuryYieldCurveResponse, SP500HistoryResponse, EurodollarFuturesResponse, CmeFedwatchResponse

router = APIRouter()

from fastapi.responses import JSONResponse
import json
import logging
import threading

from app.core.security import verify_internal_key
from app.core.redis import redis_cache

# Configure logging
logger = logging.getLogger(__name__)

def fetch_sp500_db(db: Session, start_date, end_date, limit):
    query = db.query(SP500History.trade_date, SP500History.close, SP500History.pe_ratio)
    if start_date:
        query = query.filter(SP500History.trade_date >= start_date)
    if end_date:
        query = query.filter(SP500History.trade_date <= end_date)
        
    query = query.order_by(desc(SP500History.trade_date)).limit(limit)
    return [{"trade_date": str(r.trade_date), "close": float(r.close) if r.close else None, "pe_ratio": float(r.pe_ratio) if r.pe_ratio else None} for r in query.all()]

@router.get("/SP500")
async def get_sp500_history(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(500, description="Max number of records"),
    db: Session = Depends(get_db)
):
    cache_key = f"economic:sp500:{limit}:{start_date}:{end_date}"
    cached_data = await redis_cache.get(cache_key)
    if cached_data:
        return JSONResponse(content=cached_data)

    res = await run_in_threadpool(fetch_sp500_db, db, start_date, end_date, limit)
    
    try:
        await redis_cache.set(cache_key, res, expire=3600)  # cache for 1 hour
    except Exception as e:
        logger.warning(f"Failed to cache SP500 data: {e}")
    
    return JSONResponse(content=res)

@router.get("/scrape/SP500")
def trigger_scrape_sp500(_: bool = Depends(verify_internal_key)):
    from app.scrapers.sp500_scraper import scrape_sp500
    
    def scrape_with_error_handling():
        try:
            scrape_sp500()
            logger.info("SP500 scraping completed successfully")
        except Exception as e:
            logger.error(f"SP500 scraping failed: {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "Scraping started for S&P 500 in background"}

def fetch_yield_curve_db(db: Session, start_date, end_date, limit):
    query = db.query(TreasuryYieldCurve)
    if start_date:
        query = query.filter(TreasuryYieldCurve.report_date >= start_date)
    if end_date:
        query = query.filter(TreasuryYieldCurve.report_date <= end_date)
        
    query = query.order_by(desc(TreasuryYieldCurve.report_date)).limit(limit)
    res = []
    for r in query.all():
        res.append({
            "report_date": str(r.report_date),
            "month_1": float(r.month_1) if r.month_1 is not None else None,
            "month_1_5": float(r.month_1_5) if r.month_1_5 is not None else None,
            "month_2": float(r.month_2) if r.month_2 is not None else None,
            "month_3": float(r.month_3) if r.month_3 is not None else None,
            "month_4": float(r.month_4) if r.month_4 is not None else None,
            "month_6": float(r.month_6) if r.month_6 is not None else None,
            "year_1": float(r.year_1) if r.year_1 is not None else None,
            "year_2": float(r.year_2) if r.year_2 is not None else None,
            "year_3": float(r.year_3) if r.year_3 is not None else None,
            "year_5": float(r.year_5) if r.year_5 is not None else None,
            "year_7": float(r.year_7) if r.year_7 is not None else None,
            "year_10": float(r.year_10) if r.year_10 is not None else None,
            "year_20": float(r.year_20) if r.year_20 is not None else None,
            "year_30": float(r.year_30) if r.year_30 is not None else None
        })
    return res

@router.get("/yield-curve")
async def get_yield_curve(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(500, description="Max number of records"),
    db: Session = Depends(get_db)
):
    cache_key = f"economic:yieldcurve:{limit}:{start_date}:{end_date}"
    cached_data = await redis_cache.get(cache_key)
    if cached_data:
        return JSONResponse(content=cached_data)

    res = await run_in_threadpool(fetch_yield_curve_db, db, start_date, end_date, limit)
        
    try:
        await redis_cache.set(cache_key, res, expire=3600)  # cache for 1 hour
    except Exception as e:
        logger.warning(f"Failed to cache yield curve data: {e}")
    
    return JSONResponse(content=res)

@router.get("/scrape/yield-curve")
def trigger_scrape_yield_curve(_: bool = Depends(verify_internal_key)):
    from app.scrapers.treasury_gov_scraper import scrape_treasury_gov
    
    def scrape_with_error_handling():
        try:
            scrape_treasury_gov(mode="incremental")
            logger.info("Treasury yield curve scraping completed successfully (Treasury.gov)")
        except Exception as e:
            logger.error(f"Treasury yield curve scraping failed: {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "Scraping started for US Treasury Yield Curve (Treasury.gov) in background"}

@router.get("/scrape/treasury-gov")
def trigger_scrape_treasury_gov(mode: str = Query("incremental", description="incremental | backfill_recent | backfill"), _: bool = Depends(verify_internal_key)):
    from app.scrapers.treasury_gov_scraper import scrape_treasury_gov
    
    def scrape_with_error_handling():
        try:
            scrape_treasury_gov(mode)
            logger.info(f"Treasury.gov CSV scraping completed successfully (mode={mode})")
        except Exception as e:
            logger.error(f"Treasury.gov CSV scraping failed (mode={mode}): {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": f"Treasury.gov CSV scraper started (mode={mode}). This fetches all maturities and fills missing days."}

@router.get("/clear-cache")
async def clear_yield_cache():
    """Clear all cached economic indicator data from Redis."""
    keys = await redis_cache.keys("economic:*")
    count = 0
    for key in keys:
        await redis_cache.delete(key)
        count += 1
    return {"message": f"Cleared {count} cached keys."}

# ──────────────────── Eurodollar Futures (Investing.com) ────────────────────

@router.get("/scrape/eurodollar-futures")
def trigger_scrape_eurodollar(_: bool = Depends(verify_internal_key)):
    from app.scrapers.eurodollar_scraper import scrape_eurodollar_futures
    
    def scrape_with_error_handling():
        try:
            scrape_eurodollar_futures(save_db=True, force=True)
            logger.info("Eurodollar futures scraping completed successfully")
        except Exception as e:
            logger.error(f"Eurodollar futures scraping failed: {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "Scraping started for Eurodollar Futures (Investing.com) in background"}

@router.get("/scrape/sp500-pe")
def trigger_scrape_sp500_pe(_: bool = Depends(verify_internal_key)):
    from app.scrapers.sp500_pe_scraper import scrape_sp500_pe
    
    def scrape_with_error_handling():
        try:
            scrape_sp500_pe()
            logger.info("S&P 500 PE Ratio scraping completed successfully")
        except Exception as e:
            logger.error(f"S&P 500 PE Ratio scraping failed: {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "S&P 500 PE Ratio scraper started in background"}

# ──────────────────── CME FedWatch ────────────────────

@router.get("/scrape/cme-fedwatch")
def trigger_scrape_cmefedwatch(_: bool = Depends(verify_internal_key)):
    from app.scrapers.cmefedwatch_scraper import scrape_cme_fedwatch
    
    def scrape_with_error_handling():
        try:
            scrape_cme_fedwatch(save_db=True, force=True)
            logger.info("CME FedWatch scraping completed successfully")
        except Exception as e:
            logger.error(f"CME FedWatch scraping failed: {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "CME FedWatch scraper started in background"}


@router.get("/cme-fedwatch/latest", response_model=List[CmeFedwatchResponse])
def get_cme_fedwatch_latest(db: Session = Depends(get_db)):
    """Get the most recent snapshot of CME FedWatch probabilities."""
    from app.schemas.economic_indicators import CmeFedwatchResponse
    from app.models.economic_indicators import CmeFedwatch
    
    latest_date = db.query(CmeFedwatch.scrape_date).order_by(desc(CmeFedwatch.scrape_date)).first()
    if not latest_date:
        return []
    
    return db.query(
        CmeFedwatch.id,
        CmeFedwatch.scrape_date,
        CmeFedwatch.meeting_date,
        CmeFedwatch.rate_range,
        CmeFedwatch.probability
    ).filter(
        CmeFedwatch.scrape_date == latest_date[0]
    ).order_by(CmeFedwatch.meeting_date, CmeFedwatch.rate_range).all()

@router.get("/scrape/{indicator_code}")
def trigger_scrape_specific(indicator_code: str, mode: str = Query("update", description="update or historical"), _: bool = Depends(verify_internal_key)):
    def scrape_with_error_handling(scraper_func, *args):
        try:
            scraper_func(*args)
            logger.info(f"Scraping for {indicator_code} completed successfully")
        except Exception as e:
            logger.error(f"Scraping for {indicator_code} failed: {e}")
    
    if indicator_code.upper() == "SP500_PE":
        from app.scrapers.gurufocus_scraper import scrape_gurufocus_indicator
        
        url = "https://www.gurufocus.com/economic_indicators/57/sp-500-pe-ratio"
        
        guru_mode = "incremental" if mode == "update" else "full"
        max_pages = 3 if guru_mode == "incremental" else None
        
        thread = threading.Thread(target=scrape_with_error_handling, args=(scrape_gurufocus_indicator, url, indicator_code.upper(), guru_mode, max_pages), daemon=True)
        thread.start()
        return {"message": f"GuruFocus Scraping started for {indicator_code} in background (mode={guru_mode})"}
    
    if indicator_code.upper() == "SP500_EY":
        from app.scrapers.gurufocus_scraper import scrape_gurufocus_indicator
        
        url = "https://www.gurufocus.com/economic_indicators/151/sp-500-earnings-yield"
        
        # mode == "update" → incremental (3 pages), mode == "historical" → full (all pages)
        guru_mode = "incremental" if mode == "update" else "full"
        max_pages = 3 if guru_mode == "incremental" else None
        
        thread = threading.Thread(target=scrape_with_error_handling, args=(scrape_gurufocus_indicator, url, indicator_code.upper(), guru_mode, max_pages), daemon=True)
        thread.start()
        return {"message": f"GuruFocus Scraping started for {indicator_code} in background (mode={guru_mode})"}
        
    from app.scrapers.fred_scraper import scrape_fred_indicator
    thread = threading.Thread(target=scrape_with_error_handling, args=(scrape_fred_indicator, indicator_code.upper()), daemon=True)
    thread.start()
    return {"message": f"Scraping started for {indicator_code} in background"}

@router.get("/scrape")
def trigger_scrape_all(_: bool = Depends(verify_internal_key)):
    from app.scrapers.fred_scraper import scrape_all_fred
    
    def scrape_with_error_handling():
        try:
            scrape_all_fred()
            logger.info("All FRED indicators scraping completed successfully")
        except Exception as e:
            logger.error(f"All FRED indicators scraping failed: {e}")
    
    thread = threading.Thread(target=scrape_with_error_handling, daemon=True)
    thread.start()
    return {"message": "Scraping started for all indicators in background"}

# ──────────────────── Eurodollar Futures ────────────────────

@router.get("/eurodollar-futures/latest", response_model=List[EurodollarFuturesResponse])
def get_eurodollar_futures_latest(db: Session = Depends(get_db)):
    """Get the most recent snapshot of all Eurodollar futures contracts."""
    latest_date = db.query(func.max(EurodollarFutures.scrape_date)).scalar()
    if not latest_date:
        return []
    return db.query(EurodollarFutures).filter(EurodollarFutures.scrape_date == latest_date).all()


@router.get("/eurodollar-futures/history/{contract}", response_model=List[EurodollarFuturesResponse])
def get_eurodollar_futures_history(
    contract: str,
    limit: int = Query(365, description="Max number of daily records"),
    db: Session = Depends(get_db)
):
    """Get historical data for a specific Eurodollar futures contract."""
    rows = db.query(EurodollarFutures).filter(
        EurodollarFutures.contract == contract
    ).order_by(desc(EurodollarFutures.scrape_date)).limit(limit).all()
    return rows


@router.get("/eurodollar-futures/dates")
def get_eurodollar_futures_dates(db: Session = Depends(get_db)):
    """Get all available scrape dates for Eurodollar futures."""
    dates = db.query(EurodollarFutures.scrape_date).distinct().order_by(desc(EurodollarFutures.scrape_date)).all()
    return [d[0] for d in dates]


@router.get("/{indicator_code}", response_model=List[EconomicIndicatorResponse])
def get_economic_indicator(
    indicator_code: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(500, description="Max number of records"),
    db: Session = Depends(get_db)
):
    query = db.query(EconomicIndicator).filter(EconomicIndicator.indicator_code == indicator_code.upper())
    
    if start_date:
        query = query.filter(EconomicIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(EconomicIndicator.report_date <= end_date)
        
    # Order by newest first
    query = query.order_by(desc(EconomicIndicator.report_date)).limit(limit)
    
    return query.all()
