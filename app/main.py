from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import Annotated
from app.api.routes import stocks, cache, auth, contact, rs, rs_v2, admin, scraper, official_filings, prices, technical_screener, screeners, market_breadth, market_reports, economic_indicators, xbrl_companies, xbrl_financials
from app.api.routes import market_pulse as market_pulse_router
from app.api.routes import rs_line as rs_line_router
from app.api.routes import mansfield_rs as mansfield_rs_router
from app.api.routes import sata as sata_router
from app.api.routes import tasi_settings as tasi_settings_router

# ... (Previous code)

from app.core.redis import redis_cache
from app.core.database import create_tables
from app.api.deps import get_current_admin
from app.models.user import User
from app.services.rs_rating import calculate_all_rs_ratings
import asyncio
import logging
import os
from app.core.config import settings 

try:
    from pythonjsonlogger import jsonlogger

    log_handler = logging.StreamHandler()
    log_handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.handlers = [log_handler]
    root_logger.setLevel(logging.INFO)
except Exception:
    logging.basicConfig(level=logging.INFO)



try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
except Exception:
    logging.getLogger(__name__).warning("OpenTelemetry runtime not initialized")

app = FastAPI(
    title="Saudi Stocks API",
    description="API for Saudi Stock Market data",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None
)

from fastapi.middleware.gzip import GZipMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core.limiter import limiter
from app.core.csrf import CSRFMiddleware

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(CSRFMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_store_for_auth(request, call_next):
    """Never let an auth response be cached.

    These calls set session/refresh cookies and, on failure, describe account
    state. A shared cache or a proxy holding one would hand the next reader
    either live credentials or the fact that an address exists.

    Scoped to /api/auth rather than all of /api: the data endpoints are large
    read-only payloads where caching is the whole point of the Redis layer.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/auth"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response

async def _calculate_and_save_rs(symbols: list):
    """دالة خلفية لحساب RS وحفظه في DB"""
    try:
        rs_data = await calculate_all_rs_ratings(symbols)
        
        if not rs_data:
            print("❌ No RS data calculated")
            return
        
        from app.core.database import get_db
        from app.models.quote import StockQuote
        
        db = next(get_db())
        
        try:
            for symbol, rs_scores in rs_data.items():
                quote = db.query(StockQuote).filter(StockQuote.symbol == symbol).first()
                if quote:
                    for key, value in rs_scores.items():
                        if hasattr(quote, key):
                            setattr(quote, key, value)
            
            db.commit()
            print(f"✅ RS ratings saved to DB for {len(rs_data)} stocks")
            
        except Exception as e:
            db.rollback()
            print(f"❌ Error saving RS to DB: {e}")
        finally:
            db.close()
            
        await redis_cache.delete("tadawul:all:Saudi Arabia")
        
    except Exception as e:
        print(f"❌ Error in background RS calculation: {e}")

# Register routers
# Register routers
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(contact.router, prefix="/api")

# Protected Routers (Require Authentication)
from fastapi import Depends
from app.api.deps import get_current_admin, get_current_user

protected_dependencies = [Depends(get_current_user)]
admin_dependencies = [Depends(get_current_admin)]

app.include_router(stocks.router, dependencies=protected_dependencies)
app.include_router(cache.router, dependencies=admin_dependencies)

app.include_router(rs.router, prefix="/api", dependencies=protected_dependencies)  # RS V1 endpoints
app.include_router(rs_v2.router, prefix="/api", dependencies=protected_dependencies)  # RS V2 endpoints
app.include_router(admin.router, prefix="/api", dependencies=admin_dependencies) # /api/admin/*

# System Map (Runbook) — read-only registry view. Same admin guard, separate
# router so /api/admin/system-map/* can be dropped without touching /api/admin/*.
from app.api.routes import system_map as system_map_router
app.include_router(system_map_router.router, prefix="/api", dependencies=admin_dependencies)  # /api/admin/system-map/*
app.include_router(scraper.router)  # /api/scraper/*
app.include_router(official_filings.router, prefix="/api") # /api/ingest/official-reports & /api/reports/{symbol}

app.include_router(prices.router, prefix="/api") # /api/prices/latest
from app.api.routes import industry_groups
app.include_router(industry_groups.router, prefix="/api/industry-groups", tags=["Industry Groups"])
app.include_router(technical_screener.router, prefix="/api", dependencies=protected_dependencies)  # Technical Screener
app.include_router(screeners.router, prefix="/api")  # Stock Screeners (PUBLIC)

app.include_router(market_breadth.router, prefix="/api")  # /api/market-breadth/*
app.include_router(market_reports.router, prefix="/api/market-reports", tags=["Market Reports"])


app.include_router(economic_indicators.router, prefix="/api/economic-indicators", tags=["Economic Indicators"])
app.include_router(xbrl_companies.router, dependencies=protected_dependencies)
app.include_router(xbrl_financials.router, dependencies=protected_dependencies)
# /api/upload/xbrl (xbrl_upload) was retired 2026-10-29: zero callers anywhere
# (the frontend useUpload chain was dead and its raw fetch carried no CSRF or
# credentials). Workbook ingestion runs through the scripts in backend/scripts
# which share the same xbrl_parser / xbrl_data_service layer.
from app.api.routes import terminal as terminal_router
app.include_router(terminal_router.router, dependencies=protected_dependencies)
app.include_router(market_pulse_router.router, prefix="/api/market-pulse", tags=["Market Pulse"])
app.include_router(rs_line_router.router, tags=["RS Line Indicators"], dependencies=protected_dependencies)
app.include_router(mansfield_rs_router.router, prefix="/api/indicators/mansfield-rs", tags=["Indicators"], dependencies=protected_dependencies)
app.include_router(sata_router.router, prefix="/api/indicators/sata", tags=["SATA & Stage Analysis"], dependencies=protected_dependencies)
app.include_router(tasi_settings_router.router, prefix="/api/tasi-settings", tags=["TASI Settings"])


from app.api.routes import rebh_engine as rebh_engine_router
app.include_router(rebh_engine_router.router)
app.include_router(rebh_engine_router.engine_router)

# ── REBH Market Breadth engine module (DATA snapshot API) ──
from app.api.routes import rebh_breadth as rebh_breadth_router
app.include_router(rebh_breadth_router.router, prefix="/api", dependencies=protected_dependencies)


# ── Wallet / Finance System ──
from app.api.routes.wallet import calculator as wallet_calculator
from app.api.routes.wallet import rbaf as wallet_rbaf
from app.api.routes.wallet import portfolio as wallet_portfolio
from app.api.routes.wallet import tracker as wallet_tracker
from app.api.routes.wallet import weekly as wallet_weekly

app.include_router(wallet_calculator.router, prefix="/api/wallet/calculator", tags=["Wallet - Risk Calculator"], dependencies=protected_dependencies)
app.include_router(wallet_rbaf.router, prefix="/api/wallet/rbaf", tags=["Wallet - RBAF"], dependencies=protected_dependencies)
app.include_router(wallet_portfolio.router, prefix="/api/wallet/portfolio", tags=["Wallet - Portfolio"], dependencies=protected_dependencies)
app.include_router(wallet_tracker.router, prefix="/api/wallet/tracker", tags=["Wallet - Monthly Tracker"], dependencies=protected_dependencies)
app.include_router(wallet_weekly.router, prefix="/api/wallet/weekly", tags=["Wallet - Weekly Study"], dependencies=protected_dependencies)

# ── Valuation System ──
from app.api.routes import valuation as valuation_router
from app.api.routes import valuation_admin_config as valuation_admin_config_router
from app.api.routes import valuation_admin_system as valuation_admin_system_router

app.include_router(valuation_router.router, tags=["Valuation"], dependencies=protected_dependencies)
app.include_router(valuation_admin_config_router.router, tags=["Admin — Valuation Config"], dependencies=admin_dependencies)
app.include_router(valuation_admin_system_router.router, tags=["Admin — System"], dependencies=admin_dependencies)

# Event handlers
@app.on_event("startup")
async def startup_event():
    print("🚀 Starting Saudi Stocks API...")
    

    # Alembic owns the schema (the Procfile and Dockerfile both run
    # `alembic upgrade head`). create_all() beside it hides drift: a missing
    # column or table gets quietly added at boot, so a broken migration ships
    # unnoticed. Off in production via AUTO_CREATE_TABLES=false.
    if settings.AUTO_CREATE_TABLES:
        create_tables()
    else:
        print("ℹ️ create_tables() skipped — Alembic is the schema authority (AUTO_CREATE_TABLES=false)")
    
    redis_connected = await redis_cache.init_redis()
    if not redis_connected:
        print("⚠️ Redis unavailable — authentication will fail-closed (503) until Redis recovers")
    else:
        print("✅ Redis cache initialized successfully")
    
    # ── Start Scheduler (only if ENABLE_SCHEDULER=true) ──
    from app.core.scheduler import start_scheduler
    start_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    from app.core.scheduler import stop_scheduler
    stop_scheduler()
    print("🛑 Saudi Stocks API shutting down...")

@app.get("/")
async def root():
    return {
        "message": "Saudi Stocks API",
        "version": "1.0.0",
        "docs": "/docs"
    }

@app.get("/health")
async def health_check():
    """Liveness probe — safe to keep public.

    Render polls this, and an unauthenticated endpoint is the right place for a
    yes/no answer only. Row counts, importer names and per-source status used to
    be published here: a free map of the data estate and of how stale each
    source is. They live on /health/detail behind the admin guard.
    """
    import datetime

    redis_ok = bool(redis_cache.redis_client)
    return {
        "status": "healthy",
        "redis": "connected" if redis_ok else "disconnected",
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "message": "API is running" + (" with cache" if redis_ok else " without cache"),
    }


@app.get("/health/detail")
async def health_detail(current_admin: Annotated[User, Depends(get_current_admin)]):
    """Diagnostics: importer status + database totals. Admin only."""
    import datetime
    from app.services.rebh_importers_service import get_importers_health_status

    redis_ok = bool(redis_cache.redis_client)
    importers_status = get_importers_health_status()
    overall = importers_status.get("overall_status")
    return {
        "status": "healthy" if overall in ("healthy", "ok") else "degraded",
        "redis": "connected" if redis_ok else "disconnected",
        "importers": importers_status.get("importers"),
        "totals": importers_status.get("database_totals"),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }
