"""
Daily Market Update - CALCULATIONS ONLY (NO SCRAPING)
تشغيل جميع الحسابات بدون سحب البيانات (السكرابينج)
"""
import sys
import logging
import datetime
from pathlib import Path
from datetime import date
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.database import SessionLocal
from sqlalchemy import text

# إعداد الـ Logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_calculations_only(target_date_str=None):
    """
    تشغيل جميع الحسابات للتاريخ المحدد بدون سحب البيانات
    - RS Calculation
    - Technical Indicators
    - IBD Metrics
    - Industry Group Metrics
    - Stock Technical Indicators
    """
    db = SessionLocal()
    
    try:
        logger.info("=" * 70)
        logger.info("🚀 STARTING CALCULATIONS-ONLY MODE (NO SCRAPING)")
        logger.info("=" * 70)
        
        # تحديد التاريخ
        if target_date_str:
            market_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
            logger.info(f"📅 Target date: {market_date}")
        else:
            market_date = date.today()
            logger.info(f"📅 Using today's date: {market_date}")
        
        # التحقق من وجود البيانات الأساسية
        logger.info(f"\n🔍 VERIFYING base price data exists for {market_date}...")
        
        result = db.execute(text("""
            SELECT COUNT(*) as cnt FROM prices WHERE date = :target_date
        """), {"target_date": market_date})
        
        price_count = result.fetchone()[0]
        
        if price_count == 0:
            logger.error(f"❌ ERROR: No price data found for {market_date}!")
            logger.error("⚠️  You MUST run scraping first!")
            return False
        
        logger.info(f"✅ Found {price_count} price records for {market_date}")
        
        # PHASE 2: RS CALCULATION
        # =====================================================================
        logger.info("\n" + "=" * 70)
        logger.info("PHASE 2/6: RS CALCULATION (In Memory)")
        logger.info("=" * 70)
        
        try:
            from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
            import pandas as pd
            
            logger.info("🧮 Calculating RS (Vectorized)...")
            calculator = RSCalculatorUltraFast(str(settings.DATABASE_URL))
            results = calculator.calculate_daily_rs_ultrafast(market_date)
            df_rs_today = None
            
            if results and len(results) > 0:
                df_rs_today = pd.DataFrame(results)
                logger.info(f"✅ Calculated RS for {len(df_rs_today)} stocks (held in memory).")
            else:
                logger.warning(f"⚠️ No RS results for {market_date}")
                
        except Exception as e:
            logger.error(f"❌ RS Calculation Error: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # PHASE 3: TECHNICAL INDICATORS
        # =====================================================================
        logger.info("\n" + "=" * 70)
        logger.info("PHASE 3/6: TECHNICAL INDICATORS")
        logger.info("=" * 70)
        
        try:
            from scripts.calculate_technicals import TechnicalCalculator
            
            logger.info("🧮 Calculating Technical Indicators (SMA, EMA, 52W, etc)...")
            tech_calc = TechnicalCalculator(str(settings.DATABASE_URL))
            df_tech = tech_calc.load_data()
            df_tech_res = tech_calc.calculate(df_tech)
            tech_map = tech_calc.save_change_only_and_return_tech_map(df_tech_res)
            logger.info(f"✅ Technical Indicators Complete (held in memory: {len(tech_map)} stocks).")
            
        except Exception as e:
            logger.error(f"❌ Technical Indicators Error: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # PHASE 4: IBD METRICS (RS Ratings & Acc/Dis)
        # =====================================================================
        logger.info("\n" + "=" * 70)
        logger.info("PHASE 4/6: IBD METRICS (Group RS, Acc/Dis)")
        logger.info("=" * 70)
        
        try:
            from scripts.calculate_ibd_metrics import IBDMetricsCalculator
            
            logger.info("🧮 Calculating Group RS and Acc/Dis ratings...")
            ibd_calc = IBDMetricsCalculator(db)
            df_ibd_prices = ibd_calc.load_data(lookback_days=230)
            
            group_rs_map = {}
            acc_dis_map = {}
            
            if not df_ibd_prices.empty:
                group_rs_map = ibd_calc.calculate_group_rs(df_ibd_prices, market_date) or {}
                acc_dis_map = ibd_calc.calculate_acc_dis(df_ibd_prices, market_date) or {}
                logger.info(f"✅ Calculated IBD Metrics: {len(group_rs_map)} group RS, {len(acc_dis_map)} Acc/Dis.")
            else:
                logger.warning("⚠️ No price data for IBD calculation")
                
        except Exception as e:
            logger.error(f"❌ IBD Metrics Error: {e}")
            import traceback
            traceback.print_exc()
            return False
            
        # PHASE 4.5: MERGE RS & IBD AND SAVE ATOMICALLY
        # =====================================================================
        logger.info("\n" + "=" * 70)
        logger.info("PHASE 4.5/6: MERGE RS + IBD AND SAVE")
        logger.info("=" * 70)
        try:
            if df_rs_today is not None and not df_rs_today.empty:
                logger.info("💾 Merging RS + IBD data and saving atomically to rs_daily_v2...")
                
                for idx, row in df_rs_today.iterrows():
                    sym = row.get('symbol')
                    if sym:
                        grp = group_rs_map.get(sym, {})
                        df_rs_today.at[idx, 'sector_rs_rating'] = grp.get('sector_rs_rating')
                        df_rs_today.at[idx, 'industry_group_rs_rating'] = grp.get('industry_group_rs_rating')
                        df_rs_today.at[idx, 'industry_rs_rating'] = grp.get('industry_rs_rating')
                        df_rs_today.at[idx, 'sub_industry_rs_rating'] = grp.get('sub_industry_rs_rating')
                        df_rs_today.at[idx, 'acc_dis_rating'] = acc_dis_map.get(sym)
                
                calculator.save_bulk_results_with_ibd(df_rs_today)
                logger.info(f"✅ RS + IBD Data saved atomically for {market_date} ({len(df_rs_today)} stocks).")
            else:
                logger.warning("⚠️ No RS dataframe to merge and save.")
        except Exception as e:
            logger.error(f"❌ Merging RS/IBD Error: {e}")
            import traceback
            traceback.print_exc()
            return False

        
        # PHASE 5: INDUSTRY GROUP METRICS
        # =====================================================================
        logger.info("\n" + "=" * 70)
        logger.info("PHASE 5/6: INDUSTRY GROUP METRICS")
        logger.info("=" * 70)
        
        try:
            from scripts.calculate_industry_groups import IndustryGroupCalculator
            
            logger.info("🧹 Deleting old Industry Group records for this date...")
            delete_ig = text("DELETE FROM industry_group_history WHERE date = :target_date")
            result = db.execute(delete_ig, {"target_date": market_date})
            deleted_count = result.rowcount
            db.commit()
            logger.info(f"✅ Deleted {deleted_count} old records")
            
            logger.info("🧮 Calculating Industry Group Metrics...")
            ig_calc = IndustryGroupCalculator(db)
            
            group_indices = ig_calc.calculate_group_index_prices(market_date)
            
            if group_indices:
                group_df = ig_calc.calculate_ibd_group_score(group_indices)
                
                if not group_df.empty:
                    summary_ig = ig_calc.prepare_summary_data(group_df, market_date)
                    
                    if not summary_ig.empty:
                        ig_calc.save(summary_ig, market_date)
                        logger.info(f"✅ Industry Group Metrics Complete ({len(summary_ig)} groups)")
                    else:
                        logger.warning("⚠️ No summary data generated")
                else:
                    logger.warning("⚠️ No IBD scores calculated")
            else:
                logger.warning("⚠️ No group indices found")
                
        except Exception as e:
            logger.error(f"❌ Industry Group Error: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # PHASE 6: STOCK TECHNICAL INDICATORS
        # =====================================================================
        logger.info("\n" + "=" * 70)
        logger.info("PHASE 6/6: STOCK TECHNICAL INDICATORS & MARKET BREADTH")
        logger.info("=" * 70)
        
        try:
            from scripts.calculate_stock_indicators import calculate_and_store_indicators
            
            logger.info("🧮 Calculating and storing Stock Technical Indicators...")
            processed, errors, successful = calculate_and_store_indicators(db, market_date, tech_map=tech_map)
            logger.info(f"✅ Stock Indicators Complete (Processed: {processed}, Successful: {successful}, Errors: {errors})")
            
            # Calculate Daily Market Breadth
            logger.info("🧮 Calculating Market Breadth...")
            from scripts.update_daily_market_breadth import update_todays_market_breadth
            update_todays_market_breadth(db, market_date)
            logger.info("✅ Market Breadth Updated")
            
        except Exception as e:
            logger.error(f"❌ Stock Indicators / Market Breadth Error: {e}")
            import traceback
            traceback.print_exc()
            return False
            
        # 6.5 Update Status (عشان الموقع يقرأ التاريخ الجديد)
        # -------------------------------------------------------------------
        try:
            db.execute(text("""
                UPDATE update_status 
                SET latest_ready_date = :market_date, 
                    is_updating = FALSE, 
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """), {"market_date": market_date})
            db.commit()
            logger.info(f"✅ Update Status updated to {market_date}")
        except Exception as e:
            logger.error(f"⚠️ Failed to update update_status: {e}")
            db.rollback()
            

        # 7. Invalidate Caches so new data shows up immediately
        # -------------------------------------------------------------------
        try:
            import asyncio
            from app.core.cache_helpers import invalidate_all_caches
            asyncio.run(invalidate_all_caches())
            logger.info("🧹 Application caches cleared successfully. New data is now live.")
            
                
        except Exception as cache_err:
            logger.warning(f"⚠️ Failed to invalidate caches: {cache_err}")
        
        logger.info("\n" + "=" * 70)
        logger.info("🎉 ALL CALCULATIONS COMPLETED SUCCESSFULLY!")
        logger.info("=" * 70)
        logger.info(f"✅ All data for {market_date} has been:")
        logger.info(f"   - Validated")
        logger.info(f"   - Old records deleted")
        logger.info(f"   - New records calculated and saved")
        logger.info("=" * 70)
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Critical Error: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Run Daily Calculations Only (No Scraping)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python daily_calculations_only.py                    # Use today's date
  python daily_calculations_only.py --date 2026-03-02  # Use specific date
        """
    )
    parser.add_argument('--date', type=str, help='Target date in YYYY-MM-DD format')
    
    args = parser.parse_args()
    
    success = run_calculations_only(args.date)
    exit(0 if success else 1)
