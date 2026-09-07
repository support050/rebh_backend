
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from app.core.database import Base
import app.models.user
import app.models.contact
import app.models.industry_group
import app.models.price
import app.models.financials
import app.models.financial_metrics
import app.models.official_filings
import app.models.rs_daily
import app.models.scraped_reports
import app.models.market_reports
import app.models.sukuk_bonds
import app.models.saudi_macro
import app.models.stock_indicators
import app.models.economic_indicators
import app.models.naaim_exposure
import app.models.market_pulse
import app.models.tasi_settings
import app.models.wallet
import app.models.screener_daily_trend
import app.models.user_prefs
import app.models.eps_estimates
import app.models.system_config
import app.models.valuation_zones
import app.models.tasi_components
import app.models.aporia
import app.models.Corporate_actions
import app.models.update_status

config = context.config
from app.core.config import settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
fileConfig(config.config_file_name)
target_metadata = Base.metadata

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()
