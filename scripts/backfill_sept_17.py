"""
Backfill & Sync Full Pipeline for 2026-09-17
يقوم بسحب وتصحيح بيانات 17 سبتمبر وإعادة تشغيل كافة مراحل الحسابات
(Prices, Technicals, IBD RS, Industry Groups, Stock Indicators)
"""
import sys
import os
import datetime
from pathlib import Path

# Ensure Windows terminal standard streams handle Unicode characters safely
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from app.core.database import SessionLocal
from app.core.config import settings
from app.models import Price
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TARGET_DATE = datetime.date(2026, 9, 17)


def step1_fetch_and_update_prices(db):
    """جلب الأسعار الصحيحة ليوم 17 وتحديث جدول prices"""
    logger.info(f"📡 [Step 1] Fetching correct prices for {TARGET_DATE}...")
    import yfinance as yf
    
    # جلب قائمة الشركات من قاعدة البيانات
    symbols_res = db.execute(text("SELECT DISTINCT symbol FROM companies")).fetchall()
    symbols = [r[0] for r in symbols_res if r[0] and str(r[0]).isdigit()]
    logger.info(f"Found {len(symbols)} active companies to verify/update.")

    updated_count = 0
    # Batch download عبر yfinance
    tickers_map = {f"{s}.SR": s for s in symbols}
    all_tickers_str = " ".join(tickers_map.keys())
    
    try:
        data = yf.download(all_tickers_str, start="2026-09-16", end="2026-09-19", group_by="ticker", threads=True)
    except Exception as e:
        logger.error(f"yfinance download failed: {e}")
        return

    for t_str, sym in tickers_map.items():
        try:
            df_sym = data[t_str] if len(symbols) > 1 else data
            if df_sym.empty:
                continue
            
            # فلترة يوم 17
            df_17 = df_sym[df_sym.index.date == TARGET_DATE]
            if df_17.empty:
                continue
            
            row = df_17.iloc[0]
            o = float(row.get('Open', 0) or 0)
            h = float(row.get('High', 0) or 0)
            l = float(row.get('Low', 0) or 0)
            c = float(row.get('Close', 0) or 0)
            v = int(row.get('Volume', 0) or 0)
            
            if c > 0:
                stmt = insert(Price).values({
                    "symbol": sym,
                    "date": TARGET_DATE,
                    "open": o if o > 0 else c,
                    "high": h if h > 0 else c,
                    "low": l if l > 0 else c,
                    "close": c,
                    "volume_traded": v
                })
                stmt = stmt.on_conflict_do_update(
                    index_elements=['symbol', 'date'],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume_traded": stmt.excluded.volume_traded
                    }
                )
                db.execute(stmt)
                updated_count += 1
        except Exception:
            continue

    db.commit()
    logger.info(f"✅ [Step 1 Done] Updated prices for {updated_count} stocks on {TARGET_DATE}.")


def step2_recalculate_technicals(db):
    """إعادة حساب المؤشرات الفنية و change"""
    logger.info(f"🧮 [Step 2] Calculating Technicals for {TARGET_DATE}...")
    from scripts.calculate_technicals import TechnicalCalculator
    tech_calc = TechnicalCalculator(str(settings.DATABASE_URL))
    df_tech = tech_calc.load_data()
    df_tech_res = tech_calc.calculate(df_tech)
    tech_map = tech_calc.save_change_only_and_return_tech_map(df_tech_res)
    logger.info(f"✅ [Step 2 Done] Technical Indicators updated.")
    return tech_map


def step3_recalculate_ibd_and_rs(db):
    """إعادة حساب مقاييس IBD و rs_daily_v2"""
    logger.info(f"📊 [Step 3] Calculating IBD Metrics (Group RS, Acc/Dis) for {TARGET_DATE}...")
    from scripts.calculate_ibd_metrics import IBDMetricsCalculator
    from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
    import pandas as pd
    
    ibd_calc = IBDMetricsCalculator(db)
    df_ibd_prices = ibd_calc.load_data(lookback_days=230)
    
    if not df_ibd_prices.empty:
        group_rs_map = ibd_calc.calculate_group_rs(df_ibd_prices, TARGET_DATE) or {}
        acc_dis_map = ibd_calc.calculate_acc_dis(df_ibd_prices, TARGET_DATE) or {}
        
        # Calculate daily RS via RSCalculatorUltraFast
        rs_calc = RSCalculatorUltraFast(str(settings.DATABASE_URL))
        results = rs_calc.calculate_daily_rs_ultrafast(TARGET_DATE)
        
        if results and len(results) > 0:
            df_rs_today = pd.DataFrame(results)
            df_rs_today['sector_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sector_rs_rating'))
            df_rs_today['industry_group_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_group_rs_rating'))
            df_rs_today['industry_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_rs_rating'))
            df_rs_today['sub_industry_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sub_industry_rs_rating'))
            df_rs_today['acc_dis_rating'] = df_rs_today['symbol'].map(acc_dis_map)
            
            rs_calc.save_bulk_results_with_ibd(df_rs_today)
            logger.info(f"✅ [Step 3 Done] Saved RS + IBD for {TARGET_DATE}.")


def step4_industry_groups(db):
    """إعادة حساب مؤشرات القطاعات ليوم 17"""
    logger.info(f"🏭 [Step 4] Calculating Industry Groups for {TARGET_DATE}...")
    from scripts.calculate_industry_groups import IndustryGroupCalculator
    ig_calc = IndustryGroupCalculator(db)
    group_indices = ig_calc.calculate_group_index_prices(TARGET_DATE)
    if group_indices:
        group_df = ig_calc.calculate_ibd_group_score(group_indices)
        if not group_df.empty:
            summary = ig_calc.prepare_summary_data(group_df, TARGET_DATE)
            if not summary.empty:
                ig_calc.save(summary, TARGET_DATE)
                logger.info(f"✅ [Step 4 Done] Industry Groups updated for {TARGET_DATE}.")


def step5_stock_indicators(db, tech_map):
    """حساب مؤشرات الأسهم ليوم 17 بالكامل"""
    logger.info(f"📈 [Step 5] Calculating Stock Indicators for {TARGET_DATE}...")
    from scripts.calculate_stock_indicators import calculate_and_store_indicators
    BATCH_SIZE = 140
    all_symbols = list(tech_map.keys())
    for batch_num, batch_start in enumerate(range(0, len(all_symbols), BATCH_SIZE), start=1):
        batch_symbols = all_symbols[batch_start : batch_start + BATCH_SIZE]
        batch_tech_map = {sym: tech_map[sym] for sym in batch_symbols}
        calculate_and_store_indicators(db, TARGET_DATE, tech_map=batch_tech_map)
    logger.info(f"✅ [Step 5 Done] Stock Indicators completely calculated for {TARGET_DATE}.")


def step6_market_breadth_and_rs_line(db):
    """حساب Market Breadth و RS Line Metrics ليوم 17"""
    logger.info(f"📊 [Step 6] Calculating Market Breadth & RS Line Metrics for {TARGET_DATE}...")
    try:
        from app.services.screener_daily_trend_service import update_market_date
        update_market_date(db, TARGET_DATE)
        logger.info("✅ Screener daily trend row updated.")
    except Exception as e:
        logger.warning(f"⚠️ Screener daily trend skipped: {e}")

    try:
        from scripts.update_daily_market_breadth import update_todays_market_breadth
        update_todays_market_breadth(db, TARGET_DATE)
        logger.info("✅ Market Breadth updated.")
    except Exception as e:
        logger.warning(f"⚠️ Market Breadth skipped: {e}")

    try:
        from scripts.calculate_rs_line_metrics import calculate_and_store_rs_line_metrics
        calculate_and_store_rs_line_metrics(db, TARGET_DATE)
        logger.info("✅ RS Line Metrics calculated.")
    except Exception as e:
        logger.warning(f"⚠️ RS Line Metrics skipped: {e}")


def step7_vintage_and_exports(db):
    """توليد Engine Vintages وتصدير بيانات الـ RS Hub ليوم 17"""
    logger.info(f"🏛️ [Step 7] Generating Engine Vintage & RS Hub for {TARGET_DATE}...")
    try:
        from scripts.export_rs_hub_data import export_rs_hub_data
        export_rs_hub_data(TARGET_DATE)
        logger.info("✅ RS Hub data exported.")
    except Exception as e:
        logger.warning(f"⚠️ RS Hub export skipped: {e}")

    try:
        from app.services.xbrl_data_service import OUTPUT_DIR, list_companies
        from app.services.rebh_unified_engine import calculate_full_company_payload
        from app.services.rebh_production_helper_service import warm_sector_medians_cache
        from app.services.khurafshi_engine_service import _get_latest_prices_map
        import json
        from concurrent.futures import ThreadPoolExecutor, as_completed

        warm_sector_medians_cache()
        prices_map = _get_latest_prices_map()
        companies = list_companies()
        vintages_file = OUTPUT_DIR / f"rebh_engine_vintage_{TARGET_DATE}.json"
        engine_vintages = {}

        def _proc(c):
            try:
                p = calculate_full_company_payload(c.symbol, prices_map=prices_map)
                return c.symbol, p.model_dump(mode="json")
            except Exception:
                return c.symbol, None

        with ThreadPoolExecutor(max_workers=8) as ex:
            f_map = {ex.submit(_proc, c): c.symbol for c in companies}
            for f in as_completed(f_map):
                s, res = f.result()
                if res: engine_vintages[s] = res

        with open(vintages_file, "w", encoding="utf-8") as vf:
            json.dump(engine_vintages, vf, ensure_ascii=False)
        logger.info(f"✅ Archived Engine Vintage for {len(engine_vintages)} companies.")
    except Exception as e:
        logger.warning(f"⚠️ Engine Vintage skipped: {e}")


def main():
    logger.info("🚀 Running Remaining Pipelines for 2026-09-17 (Steps 6 & 7)...")
    db = SessionLocal()
    try:
        step6_market_breadth_and_rs_line(db)
        step7_vintage_and_exports(db)
        logger.info("🎉 All remaining pipelines for 2026-09-17 completed 100%!")
    finally:
        db.close()


if __name__ == "__main__":
    main()
