# app/core/database.py

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=15,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    },
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Import models so metadata is populated for create_all / Alembic
from app.models.price import Price  # noqa: E402,F401
from app.models.rs_daily import RSDaily  # noqa: E402,F401
from app.models.official_filings import CompanyOfficialFiling  # noqa: E402,F401
from app.models.economic_indicators import EconomicIndicator  # noqa: E402,F401
from app.models.naaim_exposure import NaaimExposure  # noqa: E402,F401
from app.models.market_pulse import MarketPulse  # noqa: E402,F401
from app.models.tasi_settings import TasiSettings  # noqa: E402,F401
from app.models.wallet import WalletPosition, WalletTrade, WalletSetting, WalletWeeklyStudy  # noqa: E402,F401
from app.models.screener_daily_trend import ScreenerDailyTrend  # noqa: E402,F401
from app.models.user_prefs import UserPreference  # noqa: E402,F401
from app.models.eps_estimates import EpsEstimate  # noqa: E402,F401
from app.models.system_config import SystemConfig  # noqa: E402,F401
from app.models.valuation_zones import ValuationZone  # noqa: E402,F401
from app.models.tasi_components import TasiComponent  # noqa: E402,F401
from app.models.user import User  # noqa: E402,F401
from app.models.contact import ContactMessage  # noqa: E402,F401
from app.models.aporia import AporiaAnalytics  # noqa: E402,F401
from app.models.Corporate_actions import CorporateAction  # noqa: E402,F401
from app.models.stock_indicators import StockIndicator  # noqa: E402,F401
from app.models.scraped_reports import Company, FinancialReport  # noqa: E402,F401
from app.models.financials import IncomeStatement, BalanceSheet, CashFlow  # noqa: E402,F401
from app.models.market_reports import (  # noqa: E402,F401
    SubstantialShareholder,
    NetShortPosition,
    ForeignHeadroom,
    ShareBuyback,
    SBLPosition,
    HistoricalReport,
    QFIOwnershipFlow,
)
from app.models.sukuk_bonds import SukukMarketData  # noqa: E402,F401
from app.models.saudi_macro import SaudiEconomicIndicator  # noqa: E402,F401
from app.models.update_status import UpdateStatus  # noqa: E402,F401


def create_tables():
    """
    Dev convenience only. Production schema changes must go through Alembic.
    """
    try:
        Base.metadata.create_all(bind=engine)
        print("[DB] Tables verified/created successfully in PostgreSQL.")
    except Exception as e:
        print(f"[DB ERROR] Error creating tables: {e}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
