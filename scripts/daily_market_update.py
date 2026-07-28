import sys
import gc
from pathlib import Path
import csv
import traceback
import json
import logging
import datetime
import pandas as pd
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

# Add parent directory to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.database import SessionLocal 
from app.models.price import Price
# استيراد الخدمات الجديدة
from app.services.daily_detailed_scraper import scrape_daily_details
# ✅ استخدام الـ Calculator النهائي
from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
# 📊 استيراد Reports Scraper
from scrapers.Reports import main as run_reports_scraper
# 📊 استيراد تحديث الـ Market Pulse
from scripts.backfill_market_pulse import main as run_market_pulse_backfill
# 📊 استيراد Daily Financial Indicators
from app.scrapers.daily_financial_indicators_scraper import run_scraper_and_save_to_db as run_financial_indicators_scraper

# إعداد الـ Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# مسار المشروع والملفات
project_root = Path(__file__).resolve().parent.parent

def load_full_hierarchy_mapping():
    """
    تحميل الربط الكامل من ملف new.csv
    """
    mapping = {}
    csv_path = project_root / "new.csv"
    
    try:
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            for _, row in df.iterrows():
                symbol = str(row['Symbol'])
                mapping[symbol] = {
                    "industry_group": row.get('Industry Group'),
                    "sector": row.get('Sector'),
                    "industry": row.get('Industry'),
                    "sub_industry": row.get('Sub-Industry')
                }
            logger.info(f"Loaded {len(mapping)} symbols with full hierarchy from new.csv.")
        else:
            logger.warning("⚠️ new.csv not found at project root.")
    except Exception as e:
        logger.error(f"❌ Error loading hierarchy mapping: {e}")
        
    return mapping

def run_historical_reports_scraper():
    """
    تشغيل سكريبت تجميع التقارير التاريخية من سوق الأسهم السعودي (TASI)
    """
    try:
        logger.info("🏭 Starting Historical Reports Scraper...")
        run_reports_scraper()
        logger.info("✅ Historical Reports Scraper completed successfully!")
        return True
    except Exception as e:
        logger.error(f"⚠️ Historical Reports Scraper encountered an error: {e}")
        logger.error(traceback.format_exc())
        # Don't fail the entire daily update if reports scraper fails
        return False

def update_market_pulse():
    """
    تشغيل سكريبت backfill_market_pulse لحساب إشارات Market Pulse للأيام الجديدة فقط
    """
    try:
        logger.info("📈 Starting Market Pulse Daily Update...")
        run_market_pulse_backfill()
        logger.info("✅ Market Pulse Daily Update completed successfully!")
        return True
    except Exception as e:
        logger.error(f"⚠️ Market Pulse Daily Update encountered an error: {e}")
        logger.error(traceback.format_exc())
        return False

def update_financial_indicators():
    """
    تشغيل سكريبت Daily Financial Indicators لتحديث أوزان السوق ومكررات الربحية
    """
    try:
        logger.info("💰 Starting Daily Financial Indicators Scraper...")
        run_financial_indicators_scraper()
        logger.info("✅ Daily Financial Indicators completed successfully!")
        return True
    except Exception as e:
        logger.error(f"⚠️ Daily Financial Indicators encountered an error: {e}")
        logger.error(traceback.format_exc())
        return False

def update_aporia_analytics():
    """
    تشغيل سكريبت Aporia Saudi Analytics لسحب جدول التحليلات وتحديثه في DB
    """
    try:
        logger.info("📊 Starting Aporia Analytics Scraper...")
        from scripts.Scrape_aporia_saudi import main as run_aporia_scraper
        run_aporia_scraper()
        logger.info("✅ Aporia Analytics completed successfully!")
        return True
    except Exception as e:
        logger.error(f"⚠️ Aporia Analytics encountered an error: {e}")
        logger.error(traceback.format_exc())
        return False

def update_daily(target_date_str=None):
    """
    1. Scrape Daily Data
    2. Save to DB (with correct Industry Group)
    3. Calculate RS (Incremental)
    """
    # تهيئة الاتصال بقاعدة البيانات
    db = SessionLocal()
    
    try:
        logger.info(f"🚀 Starting Daily Market Update...")
        
        # 0. Set Update Status to Updating
        from sqlalchemy import text
        import datetime as dt_module
        db.execute(text("""
            UPDATE update_status 
            SET is_updating = TRUE, 
                started_at = :now 
            WHERE id = 1
        """), {"now": dt_module.datetime.utcnow()})
        db.commit()
        
        # 0.1 Run Historical Reports Scraper
        run_historical_reports_scraper()
        
        # 0.2 Run Market Pulse Update
        update_market_pulse()
        
        # 0.3 Run Daily Financial Indicators
        update_financial_indicators()

        # 0.4 Run Aporia Saudi Analytics Scraper
        update_aporia_analytics()
        
        # 0.5 Load Mappings
        hierarchy_map = load_full_hierarchy_mapping()
        
        # 1. Determine Date
        if target_date_str:
            market_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
            logger.info(f"📅 User provided custom date: {market_date}")
        else:
            market_date = date.today()
            logger.info(f"📅 Using today's date: {market_date}")

        # 2. Scraping
        logger.info("📡 Scraping daily detailed report...")
        scraped_data = scrape_daily_details(headless=True)
        
        if not scraped_data:
            logger.error("❌ Scraping failed or returned no data.")
            return

        logger.info(f"📊 Scraped {len(scraped_data)} records.")



        # 3. Saving Prices
        success_count = 0
        for item in scraped_data:
            symbol = str(item.get("Symbol"))
            company = item.get("Company")
            
            if not symbol: continue

            # Get Detailed Hierarchy
            h = hierarchy_map.get(symbol, {})
            
            try:
                close_val = float(item.get("Close", 0.0) or 0)

                price_data = {
                    "symbol": symbol,
                    "date": market_date,
                    "open": item.get("Open", 0.0),
                    "high": item.get("Highest", 0.0),
                    "low": item.get("Lowest", 0.0),
                    "close": close_val,
                    # Derive absolute change from close + change% when scraper doesn't return it directly
                    "change": item.get("Change") if item.get("Change") is not None else round(
                        float(item.get("Close", 0) or 0) * float(item.get("Change %", 0) or 0) / 
                        (100.0 + float(item.get("Change %", 0) or 0)), 4
                    ),
                    "change_percent": item.get("Change %", 0.0),
                    "volume_traded": int(item.get("Volume Traded", 0)),
                    "value_traded_sar": float(item.get("Value Traded", 0.0)),
                    "no_of_trades": int(item.get("No. of Trades", 0)),
                    "company_name": company,
                    "industry_group": h.get("industry_group"),
                    "sector": h.get("sector"),
                    "industry": h.get("industry"),
                    "sub_industry": h.get("sub_industry"),
                    "market_cap": float(item.get("Market Cap", 0.0))
                }
                
                # Upsert
                stmt = insert(Price).values(price_data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['symbol', 'date'],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "change": stmt.excluded.change,
                        "change_percent": stmt.excluded.change_percent,
                        "volume_traded": stmt.excluded.volume_traded,
                        "value_traded_sar": stmt.excluded.value_traded_sar,
                        "no_of_trades": stmt.excluded.no_of_trades,
                        "company_name": stmt.excluded.company_name,
                        "industry_group": stmt.excluded.industry_group,
                        "sector": stmt.excluded.sector,
                        "industry": stmt.excluded.industry,
                        "sub_industry": stmt.excluded.sub_industry,
                        "market_cap": stmt.excluded.market_cap
                    }
                )
                db.execute(stmt)
                success_count += 1
            except Exception as row_error:
                logger.warning(f"⚠️ Skipped row {symbol}: {row_error}")
                continue
            
        db.commit()
        logger.info(f"✅ Successfully saved/updated {success_count} price records for {market_date}.")

        # 🧹 Free memory after saving prices
        del scraped_data, hierarchy_map
        gc.collect()
        
        # 4. RS Calculation (Optimized Final) — CALCULATE ONLY, DO NOT SAVE YET
        # -------------------------------------------------------------------
        logger.info(f"🧮 Starting RS Calculation for {market_date} (Daily Single-Day Mode)...")
        from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
        import pandas as pd

        # تعريف defaults أولاً يضمن إن الـ del في النهاية آمن حتى لو حصل exception
        calculator = None
        df_rs_today = None

        try:
            calculator = RSCalculatorUltraFast(str(settings.DATABASE_URL))
            results = calculator.calculate_daily_rs_ultrafast(market_date)
            if results and len(results) > 0:
                df_rs_today = pd.DataFrame(results)
                logger.info(f"✅ Calculated RS for {len(df_rs_today)} stocks (held in memory, not saved yet).")
            else:
                logger.warning(f"⚠️ No RS results found for {market_date}.")
            del results
        except Exception as rs_err:
            logger.error(f"⚠️ RS Calculation failed (will skip RS+IBD save): {rs_err}")

        gc.collect()
            
        # 5. Calculate Technical Indicators — ATOMIC: only save prices.change, keep tech data in memory
        # -------------------------------------------------------------------
        logger.info("🧮 Calculating Technical Indicators (SMAs, 52W High/Low)...")
        from scripts.calculate_technicals import TechnicalCalculator
        tech_calc = TechnicalCalculator(str(settings.DATABASE_URL))
        df_tech = tech_calc.load_data()
        df_tech_res = tech_calc.calculate(df_tech)
        # Save ONLY prices.change, return tech data as dict for later merging
        tech_map = tech_calc.save_change_only_and_return_tech_map(df_tech_res)
        logger.info(f"✅ Technical Indicators calculated (held in memory: {len(tech_map)} stocks).")

        # 🧹 Free memory: release raw technical DataFrames, keep only tech_map dict
        del df_tech, df_tech_res, tech_calc
        gc.collect()

        # 6. Calculate IBD Metrics (Group RS, Acc/Dis) — CALCULATE ONLY
        # -------------------------------------------------------------------
        logger.info("📊 Calculating IBD Metrics (Group RS, Acc/Dis)...")
        from scripts.calculate_ibd_metrics import IBDMetricsCalculator
        
        ibd_calc = IBDMetricsCalculator(db)
        df_ibd_prices = ibd_calc.load_data(lookback_days=230)
        
        # تعريف defaults هنا يضمن إن الـ del بعدين آمن حتى لو حصل exception
        group_rs_map = {}
        acc_dis_map = {}
        
        if not df_ibd_prices.empty:
            group_rs_map = ibd_calc.calculate_group_rs(df_ibd_prices, market_date) or {}
            acc_dis_map = ibd_calc.calculate_acc_dis(df_ibd_prices, market_date) or {}
            logger.info(f"✅ Calculated IBD Metrics: {len(group_rs_map)} group RS, {len(acc_dis_map)} Acc/Dis.")
        else:
            logger.warning("⚠️ No price data found for IBD Metrics.")

        # 🧹 Free memory: release the large IBD prices DataFrame
        del df_ibd_prices, ibd_calc
        gc.collect()

        # 6.5 ATOMIC SAVE: Merge RS + IBD → Save to rs_daily_v2 in ONE shot
        # -------------------------------------------------------------------
        if df_rs_today is not None and len(df_rs_today) > 0 and calculator is not None:
            logger.info("💾 Merging RS + IBD data and saving atomically to rs_daily_v2...")

            # Vectorized merge — بدل iterrows + .at[] على كل سهم
            df_rs_today['sector_rs_rating']         = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sector_rs_rating'))
            df_rs_today['industry_group_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_group_rs_rating'))
            df_rs_today['industry_rs_rating']       = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_rs_rating'))
            df_rs_today['sub_industry_rs_rating']   = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sub_industry_rs_rating'))
            df_rs_today['acc_dis_rating']           = df_rs_today['symbol'].map(acc_dis_map)

            # Now save everything at once — the row is COMPLETE from the start
            calculator.save_bulk_results_with_ibd(df_rs_today)
            logger.info(f"✅ RS + IBD Data saved atomically for {market_date} ({len(df_rs_today)} stocks).")

        # 🧹 Free memory: RS, IBD maps, and calculator no longer needed
        # (كلهم معرّفين بـ defaults فوق، فالـ del آمن دايماً)
        del df_rs_today, group_rs_map, acc_dis_map, calculator
        gc.collect()

        # 7. Calculate Industry Group Metrics
        # -------------------------------------------------------------------
        logger.info("🏭 Calculating Industry Group Metrics (IBD Score, Rank, YTD)...")
        from scripts.calculate_industry_groups import IndustryGroupCalculator
        
        ig_calc = IndustryGroupCalculator(db)
        
        # Step 1: Calculate group index prices
        group_indices = ig_calc.calculate_group_index_prices(market_date)
        
        if group_indices:
            # Step 2: Calculate IBD scores
            group_df = ig_calc.calculate_ibd_group_score(group_indices)
            
            if not group_df.empty:
                # Step 3: Prepare summary with YTD and details
                summary_ig = ig_calc.prepare_summary_data(group_df, market_date)
                
                if not summary_ig.empty:
                    # Step 4: Save to database
                    ig_calc.save(summary_ig, market_date)
                    logger.info(f"✅ Industry Group Metrics Updated ({len(summary_ig)} groups).")
                else:
                    logger.warning("⚠️ No summary data generated for Industry Groups.")
            else:
                logger.warning("⚠️ No IBD scores calculated for Industry Groups.")
        else:
            logger.warning("⚠️ No group indices found for Industry Group Metrics calculation.")

        # 🧹 Free memory: industry group objects no longer needed
        del ig_calc
        gc.collect()
        
        # 8. Calculate and Store Stock Technical Indicators — BATCHED (2 × ~140 stocks)
        # -------------------------------------------------------------------
        logger.info("📈 Calculating and Storing Stock Technical Indicators (Batched Mode)...")
        from scripts.calculate_stock_indicators import calculate_and_store_indicators

        BATCH_SIZE = 140
        all_symbols = list(tech_map.keys())
        total_processed = total_errors = total_successful = 0

        for batch_num, batch_start in enumerate(range(0, len(all_symbols), BATCH_SIZE), start=1):
            batch_symbols = all_symbols[batch_start : batch_start + BATCH_SIZE]
            batch_tech_map = {sym: tech_map[sym] for sym in batch_symbols}

            logger.info(
                f"  ⚙️  Batch {batch_num}: symbols {batch_start + 1}–"
                f"{batch_start + len(batch_symbols)} ({len(batch_symbols)} stocks)..."
            )

            b_processed, b_errors, b_successful = calculate_and_store_indicators(
                db, market_date, tech_map=batch_tech_map
            )
            total_processed  += b_processed
            total_errors     += b_errors
            total_successful += b_successful

            logger.info(
                f"  ✅ Batch {batch_num} done — "
                f"Processed: {b_processed}, Successful: {b_successful}, Errors: {b_errors}"
            )

            # 🧹 Free this batch from memory before loading the next one
            del batch_tech_map, batch_symbols
            gc.collect()

        logger.info(
            f"✅ Stock Indicators Updated — Total: "
            f"Processed: {total_processed}, Successful: {total_successful}, Errors: {total_errors}"
        )

        # 🧹 Free memory: full tech_map no longer needed
        del tech_map, all_symbols
        gc.collect()

        # 8.1 Pre-aggregate Minervini trend counts for this date (fast chart API reads)
        try:
            from app.services.screener_daily_trend_service import update_market_date
            update_market_date(db, market_date)
            logger.info("✅ Screener daily trend row updated for chart API.")
        except Exception as trend_err:
            logger.error(f"⚠️ Screener daily trend update failed: {trend_err}")
        
        # 8.5 Calculate Daily Market Breadth — ATOMIC
        # -------------------------------------------------------------------
        from scripts.update_daily_market_breadth import update_todays_market_breadth
        update_todays_market_breadth(db, market_date)

        # 8.6 Calculate RS Line Metrics (TraderLion scale)
        # -------------------------------------------------------------------
        try:
            logger.info("📈 Calculating RS Line Metrics...")
            from scripts.calculate_rs_line_metrics import calculate_and_store_rs_line_metrics
            calculate_and_store_rs_line_metrics(db, market_date)
            logger.info("✅ RS Line Metrics calculated and stored successfully.")
        except Exception as rs_err:
            logger.error(f"⚠️ RS Line Metrics calculation failed: {rs_err}")

        # 9. Finalize Update Status (Atomic Switch)
        # -------------------------------------------------------------------
        db.execute(text("""
            UPDATE update_status 
            SET latest_ready_date = :market_date, 
                is_updating = FALSE, 
                completed_at = :now 
            WHERE id = 1
        """), {"market_date": market_date, "now": dt_module.datetime.utcnow()})
        db.commit()

        # 10. Invalidate Caches so new data shows up immediately
        # -------------------------------------------------------------------
        try:
            import asyncio
            from app.core.redis import redis_cache
            asyncio.run(redis_cache.flush_all())
            logger.info("🧹 Application caches cleared successfully. New data is now live.")
            
            # --- Re-build NAAIM page metadata ---
            try:
                logger.info("🔄 Re-caching NAAIM metadata after cache wipe...")
                from app.scrapers.naaim_scraper import scrape_naaim
                scrape_naaim(mode="incremental")
            except Exception as naaim_err:
                logger.error(f"⚠️ Failed to restore NAAIM metadata: {naaim_err}")
                
        except Exception as cache_err:
            logger.error(f"⚠️ Failed to invalidate caches: {cache_err}")

        logger.info("🎉 Daily Update Workflow Completed Successfully!")

    except Exception as e:
        logger.error(f"❌ Critical Error in Daily Update: {e}")
        # Release the lock if it fails
        db.execute(text("UPDATE update_status SET is_updating = FALSE WHERE id = 1"))
        db.commit()
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Run Daily Market Update')
    parser.add_argument('--date', type=str, help='Target date in YYYY-MM-DD format (overrides today)')
    
    args = parser.parse_args()
    
    update_daily(args.date)