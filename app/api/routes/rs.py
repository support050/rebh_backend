from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Optional
from datetime import date
import logging
import os
import json
from pathlib import Path
from pydantic import BaseModel

from app.core.database import get_db
from app.models.rs_daily import RSDaily
from app.models.user_prefs import UserPreference
from app.schemas.rs import RSResponse, RSLatestResponse
from app.core.limiter import limiter
from app.core.cache_helpers import (
    cache_read_through,
    make_rs_latest_key,
    make_rs_history_key,
    make_rs_advanced_key,
)
from app.core.cache_config import CACHE_TTL_SCREENERS, CACHE_TTL_RS_HISTORY
from app.api.deps import get_current_user
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rs", tags=["Relative Strength"])


class UserPrefsUpdate(BaseModel):
    preferences: dict


@router.get("/latest", response_model=RSLatestResponse)
@limiter.limit("20/minute")
async def get_latest_rs(
    request: Request,
    min_rs: Optional[int] = Query(None, ge=0, le=99, description="الحد الأدنى لـ RS Rating"),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db)
):
    """
    الحصول على آخر RS Rating لكل الأسهم مع التقييم السابق.
    Cached with 10-minute TTL.
    """
    cache_key = make_rs_latest_key(min_rs, limit)
    
    async def fetch_rs_latest():
        try:
            dates_row = db.query(RSDaily.date).distinct().order_by(desc(RSDaily.date)).limit(2).all()
            
            if not dates_row:
                return RSLatestResponse(data=[], total_count=0, date=date.today())
            
            latest_date = dates_row[0][0]
            prev_date = dates_row[1][0] if len(dates_row) > 1 else None
            
            prev_ratings = {}
            if prev_date:
                prev_results = db.query(RSDaily.symbol, RSDaily.rs_rating).filter(RSDaily.date == prev_date).all()
                prev_ratings = {r.symbol: r.rs_rating for r in prev_results}
            
            query = db.query(RSDaily).filter(RSDaily.date == latest_date)
            
            if min_rs is not None:
                query = query.filter(RSDaily.rs_rating >= min_rs)
            
            query = query.order_by(desc(RSDaily.rs_rating))
            results = query.limit(limit).all()
            
            for r in results:
                r.prev_rs_rating = prev_ratings.get(r.symbol)
            
            return RSLatestResponse(
                data=results,
                total_count=len(results),
                date=latest_date
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"Error in get_latest_rs: {e}")
            raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}")
    
    result = await cache_read_through(
        cache_key,
        CACHE_TTL_SCREENERS,
        fetch_rs_latest
    )
    return result


@router.get("/latest_hub")
@router.get("/latest_hub/")
@router.get("/latest_hub/")
async def get_latest_hub():
    """
    Returns the unified rs_data.json cached file for the RS Rating Hub.
    Attempts to fetch from R2 (using Redis cache to avoid constant downloading) if configured.
    Falls back to local file system.
    """
    r2_account_id = os.getenv("R2_ACCOUNT_ID")
    r2_access_key = os.getenv("R2_ACCESS_KEY_ID")
    r2_secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    r2_bucket = os.getenv("R2_BUCKET_NAME", "lumivst-xbrl")
    
    # 1. Try fetching from R2 with Redis cache to maximize performance
    if r2_account_id and r2_access_key and r2_secret_key:
        cache_key = "rs_hub:latest_data"
        try:
            from app.core.redis import redis_cache
            # Try to get cached data from Redis
            if redis_cache.is_connected or await redis_cache.ensure_connection():
                cached_data = await redis_cache.get(cache_key)
                if cached_data:
                    return JSONResponse(content=cached_data)
        except Exception as cache_err:
            logger.warning(f"Redis cache fetch failed for RS Hub: {cache_err}")
            
        # Download from R2
        try:
            import boto3
            import json
            from botocore.config import Config
            endpoint_url = f"https://{r2_account_id}.r2.cloudflarestorage.com"
            s3_client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=r2_access_key,
                aws_secret_access_key=r2_secret_key,
                config=Config(signature_version="s3v4"),
            )
            response = s3_client.get_object(Bucket=r2_bucket, Key="rs/rs_data.json")
            data = json.loads(response['Body'].read().decode('utf-8'))
            
            # Cache in Redis for 10 minutes (600 seconds)
            try:
                from app.core.redis import redis_cache
                if redis_cache.is_connected or await redis_cache.ensure_connection():
                    await redis_cache.set(cache_key, data, expire=600)
            except Exception:
                pass
                
            return JSONResponse(content=data)
        except Exception as r2_err:
            logger.error(f"Failed to fetch rs_data.json from R2: {r2_err}. Falling back to local file.")

    # 2. Local Fallback
    json_path = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "static" / "rs_data.json"
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return JSONResponse(content=data)
    else:
        logger.error(f"❌ latest_hub: rs_data.json not found at {json_path}")
        return JSONResponse(content={"stocks": [], "error": f"File not found: {json_path}"}, status_code=200)


@router.get("/screener/advanced", response_model=RSLatestResponse)
@limiter.limit("10/minute")
async def advanced_screener(
    request: Request,
    min_rs: int = Query(0, ge=0, le=99),
    min_rank_3m: Optional[int] = Query(None, description="Minimum 3 Month Rank"),
    min_rank_6m: Optional[int] = Query(None, description="Minimum 6 Month Rank"),
    sort_by: str = Query("rs_rating", regex="^(rs_rating|rank_3m|rank_6m|rank_12m|return_3m|return_12m)$"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db)
):
    """
    فلترة متقدمة للأسهم بناءً على الرتب والفترات.
    Cached with 10-minute TTL.
    """
    cache_key = make_rs_advanced_key(min_rs, min_rank_3m, min_rank_6m, sort_by, limit)
    
    async def fetch_advanced():
        latest_date_row = db.query(RSDaily.date).order_by(desc(RSDaily.date)).first()
        if not latest_date_row:
            return RSLatestResponse(data=[], total_count=0, date=date.today())
        
        latest_date = latest_date_row[0]
        
        query = db.query(RSDaily).filter(RSDaily.date == latest_date)
        
        if min_rs > 0:
            query = query.filter(RSDaily.rs_rating >= min_rs)
        
        if min_rank_3m is not None:
            query = query.filter(RSDaily.rank_3m >= min_rank_3m)
            
        if min_rank_6m is not None:
            query = query.filter(RSDaily.rank_6m >= min_rank_6m)
        
        if hasattr(RSDaily, sort_by):
            col = getattr(RSDaily, sort_by)
            query = query.order_by(desc(col))
        else:
            query = query.order_by(desc(RSDaily.rs_rating))
            
        results = query.limit(limit).all()
        
        return RSLatestResponse(
            data=results,
            total_count=len(results),
            date=latest_date
        )
    
    result = await cache_read_through(
        cache_key,
        CACHE_TTL_SCREENERS,
        fetch_advanced
    )
    return result


@router.get("/user_preferences")
def get_user_preferences(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if pref:
        return {"preferences": pref.preferences}
    return {"preferences": {}}


@router.post("/user_preferences")
def update_user_preferences(prefs: UserPrefsUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if not pref:
        pref = UserPreference(user_id=current_user.id)
        pref.preferences = prefs.preferences
        db.add(pref)
    else:
        pref.preferences = prefs.preferences
    db.commit()
    return {"status": "success", "preferences": pref.preferences}


# NOTE: parameterized /{symbol} must be registered AFTER all static paths
@router.get("/{symbol}", response_model=List[RSResponse])
@limiter.limit("20/minute")
async def get_rs_history(
    request: Request,
    symbol: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    db: Session = Depends(get_db)
):
    """
    الحصول على تاريخ RS لسهم معين.
    Cached with 30-minute TTL.
    """
    cache_key = make_rs_history_key(
        symbol,
        from_date.isoformat() if from_date else None,
        to_date.isoformat() if to_date else None
    )
    
    async def fetch_rs_history():
        symbol_str = str(symbol).strip()
        
        query = db.query(RSDaily).filter(RSDaily.symbol == symbol_str)
        
        if from_date:
            query = query.filter(RSDaily.date >= from_date)
        if to_date:
            query = query.filter(RSDaily.date <= to_date)
        
        results = query.order_by(RSDaily.date).all()
        
        return results or []
    
    result = await cache_read_through(
        cache_key,
        CACHE_TTL_RS_HISTORY,
        fetch_rs_history
    )
    return result
