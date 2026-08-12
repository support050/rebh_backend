from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_admin
from app.core.redis import redis_cache
from app.models.user import User

router = APIRouter(prefix="/cache", tags=["Cache Management"])

# Auth allowlist / OAuth / verification key prefixes — never deleted by cache clears
_PROTECTED_KEY_PREFIXES = (
    "access_token:",
    "access_jti:",
    "refresh_token:",
    "refresh_jti:",
    "session_index:",
    "oauth_state:",
    "oauth_link:",
    "verify_token:",
    "reset_token:",
)


def _is_protected_key(key: str) -> bool:
    return any(key.startswith(p) for p in _PROTECTED_KEY_PREFIXES)


async def _delete_matching(patterns: list) -> int:
    deleted = 0
    for pattern in patterns:
        keys = await redis_cache.keys(pattern)
        for key in keys:
            if _is_protected_key(key):
                continue
            if await redis_cache.delete(key):
                deleted += 1
    return deleted


@router.post("/clear/stocks")
async def clear_stocks_cache(current_admin: User = Depends(get_current_admin)):
    """مسح كاش الأسهم فقط (بدون FLUSHALL)."""
    _ = current_admin
    try:
        deleted = await _delete_matching(["tadawul_stocks:*", "tadawul:*"])
        return {"message": f"✅ تم مسح كاش الأسهم بنجاح ({deleted} مفتاح)", "deleted_count": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في مسح كاش الأسهم: {type(e).__name__}")


@router.post("/clear/financials")
async def clear_financial_cache(
    symbol: str = Query(None, description="رمز سهم واحد أو رموز متعددة مفصولة بفواصل"),
    current_admin: User = Depends(get_current_admin),
):
    """مسح كاش البيانات المالية لرمز أو رموز محددة"""
    _ = current_admin
    try:
        if symbol:
            symbols = [s.strip() for s in symbol.split(",")]
            patterns = [f"financials:*:{sym}:*" for sym in symbols]
            deleted = await _delete_matching(patterns)
            message = f"✅ تم مسح كاش البيانات المالية لـ {len(symbols)} رمز"
        else:
            deleted = await _delete_matching(["financials:*"])
            message = "✅ تم مسح كاش البيانات المالية بالكامل"
        return {"message": message, "deleted_count": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في مسح كاش البيانات المالية: {type(e).__name__}")


@router.get("/status")
async def cache_status(current_admin: User = Depends(get_current_admin)):
    """الحصول على حالة الكاش"""
    _ = current_admin
    try:
        is_connected = redis_cache.redis_client is not None
        if is_connected:
            try:
                await redis_cache.redis_client.ping()
                status = "connected"
            except Exception:
                status = "disconnected"
        else:
            status = "disconnected"

        return {
            "redis_status": status,
            "message": "✅ نظام الكاش يعمل بشكل طبيعي" if status == "connected" else "❌ نظام الكاش غير متاح",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في التحقق من حالة الكاش: {type(e).__name__}")


@router.delete("/clear/symbols")
async def clear_specific_symbols_cache(
    symbols: str = Query(..., description="رموز الأسهم مفصولة بفواصل"),
    current_admin: User = Depends(get_current_admin),
):
    """مسح كاش رموز محددة من Redis"""
    _ = current_admin
    try:
        symbol_list = [s.strip() for s in symbols.split(",")]
        patterns = []
        for symbol in symbol_list:
            clean_sym = "".join(filter(str.isdigit, symbol)).upper()
            if not clean_sym:
                continue
            patterns.append(f"tadawul_stocks:symbol:{clean_sym}*")
            patterns.append(f"financials:*:{clean_sym}:*")
        deleted = await _delete_matching(patterns)
        return {
            "message": f"✅ تم مسح كاش {len(symbol_list)} رمز",
            "cleared_symbols": symbol_list,
            "deleted_count": deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في مسح الكاش: {type(e).__name__}")


@router.get("/stats")
async def cache_stats(current_admin: User = Depends(get_current_admin)):
    """إحصائيات الكاش"""
    _ = current_admin
    try:
        stock_keys = await redis_cache.keys("tadawul_stocks:*")
        financial_keys = await redis_cache.keys("financials:*")
        return {
            "total_stock_keys": len(stock_keys),
            "total_financial_keys": len(financial_keys),
            "sample_stock_keys": stock_keys[:3],
            "sample_financial_keys": financial_keys[:3],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"خطأ في جلب إحصائيات الكاش: {type(e).__name__}")
