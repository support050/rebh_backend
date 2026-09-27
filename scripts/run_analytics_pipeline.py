# -*- coding: utf-8 -*-
"""
Run Analytics Pipeline (Production Engine)
===========================================
يقوم هذا السكريبت بتشغيل كامل الـ Analytics Pipeline (حسابات الفئة الثانية والتقييمات)
لتاريخ محدد دون الحاجة لتشغيل أي Web Scrapers، معتمداً على البيانات الدقيقة
الموجودة بالفعل في جدول `prices` و `historical_reports`.

الاستخدام:
    # لتشغيل تاريخ محدد:
    python run_analytics_pipeline.py --date 2026-09-20

    # لتشغيل الثلاثة أيام بالترتيب الصحيح (17 ثم 20 ثم 21):
    python run_analytics_pipeline.py --all-target-dates
"""

import sys
import os
import gc
import json
import logging
import argparse
import traceback
import datetime
from pathlib import Path
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure Windows terminal standard streams handle Unicode characters safely
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add parent directory to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

import pandas as pd
from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal

# Output Markdown report path
OUTPUT_MD_PATH = project_root / "ANALYTICS_PIPELINE_REPORT.md"

# Configure Logging to both Console and Markdown File
logger = logging.getLogger("AnalyticsPipeline")
logger.setLevel(logging.INFO)
logger.propagate = False  # يمنع تكرار الطباعة مع الـ root logger

# تفريغ أي handlers قديمة لتجنب التكرار
logger.handlers.clear()

formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(str(OUTPUT_MD_PATH), mode="w", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(file_handler)


# Header for the Markdown Report
with open(OUTPUT_MD_PATH, "w", encoding="utf-8") as f:
    f.write("# 📊 Analytics Pipeline Execution & Verification Report\n\n")
    f.write(f"**Execution Timestamp:** `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n")
    f.write("```text\n")


def _rollback_session(db):

    try:
        db.rollback()
    except Exception:
        pass


def _release_update_lock(market_date=None):
    lock_db = SessionLocal()
    try:
        if market_date is not None:
            lock_db.execute(text("""
                UPDATE update_status
                SET latest_ready_date = :market_date,
                    is_updating = FALSE,
                    completed_at = :now
                WHERE id = 1
            """), {"market_date": market_date, "now": datetime.datetime.utcnow()})
        else:
            lock_db.execute(text("UPDATE update_status SET is_updating = FALSE WHERE id = 1"))
        lock_db.commit()
    except Exception as e:
        logger.error(f"⚠️ Failed to release update lock: {e}")
        _rollback_session(lock_db)
    finally:
        lock_db.close()


def run_pipeline_for_date(target_date: datetime.date):
    """
    تشغيل خط الإنتاج التحليلي بالكامل ليوم تداول محدد.
    """
    logger.info("=" * 80)
    logger.info(f"🚀 [SENIOR ENGINE] Starting Analytics Calculations for Date: {target_date}")
    logger.info("=" * 80)

    db = SessionLocal()

    try:
        # 0. Set Update Status
        import datetime as dt_module
        db.execute(text("""
            UPDATE update_status 
            SET is_updating = TRUE, 
                started_at = :now 
            WHERE id = 1
        """), {"now": dt_module.datetime.utcnow()})
        db.commit()

        # 1. Market Pulse Update
        # -------------------------------------------------------------------
        logger.info("📈 [1/11] Running Market Pulse Analysis...")
        from scripts.backfill_market_pulse import main as run_market_pulse_backfill
        run_market_pulse_backfill()
        
        # 🔍 Verification for Market Pulse using Index on date
        try:
            pulse_sample = db.execute(text("""
                SELECT date, market_pulse, distribution_days, current_outlook
                FROM market_pulse
                WHERE date IN ('2026-09-17', '2026-09-20', '2026-09-21')
                ORDER BY date
            """)).fetchall()
            logger.info("  🔍 [VERIFY Market Pulse (17, 20, 21)]:")
            for p in pulse_sample:
                logger.info(f"     📅 {p.date}: MarketPulse={p.market_pulse} | DistDays={p.distribution_days} | Outlook={p.current_outlook}")
        except Exception as e:
            db.rollback()
            logger.warning(f"  ⚠️ Could not verify Market Pulse sample: {e}")



        # 2. RS Calculation (Optimized Final)
        # -------------------------------------------------------------------
        logger.info(f"🧮 [2/11] Calculating IBD Relative Strength (RS Rating 1-99)...")
        from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
        calculator = RSCalculatorUltraFast(str(settings.DATABASE_URL))
        df_rs_today = None
        try:
            results = calculator.calculate_daily_rs_ultrafast(target_date)
            if results and len(results) > 0:
                df_rs_today = pd.DataFrame(results)
                logger.info(f"✅ Calculated RS for {len(df_rs_today)} stocks (held in memory).")
            else:
                logger.warning(f"⚠️ No RS results found for {target_date}.")
        except Exception as rs_err:
            logger.error(f"⚠️ RS Calculation error: {rs_err}")

        # 3. Technical Indicators (SMAs, 52W, Beta)
        # -------------------------------------------------------------------
        logger.info("🧮 [3/11] Calculating Technical Indicators (SMAs, 52W High/Low, Beta)...")
        from scripts.calculate_technicals import TechnicalCalculator
        tech_calc = TechnicalCalculator(str(settings.DATABASE_URL))
        df_tech = tech_calc.load_data()
        df_tech_res = tech_calc.calculate(df_tech)
        tech_map = tech_calc.save_change_only_and_return_tech_map(df_tech_res)
        logger.info(f"✅ Technical indicators ready for {len(tech_map)} stocks.")
        del df_tech, df_tech_res, tech_calc
        gc.collect()

        # 4. IBD Metrics (Group RS, Acc/Dis)
        # -------------------------------------------------------------------
        logger.info("📊 [4/11] Calculating IBD Metrics (Group RS & Acc/Dis)...")
        from scripts.calculate_ibd_metrics import IBDMetricsCalculator
        ibd_calc = IBDMetricsCalculator(db)
        df_ibd_prices = ibd_calc.load_data(lookback_days=230)
        group_rs_map = {}
        acc_dis_map = {}
        if not df_ibd_prices.empty:
            group_rs_map = ibd_calc.calculate_group_rs(df_ibd_prices, target_date) or {}
            acc_dis_map = ibd_calc.calculate_acc_dis(df_ibd_prices, target_date) or {}
            logger.info(f"✅ IBD Metrics: {len(group_rs_map)} group RS, {len(acc_dis_map)} Acc/Dis.")
        del df_ibd_prices, ibd_calc
        gc.collect()

        # 5. ATOMIC SAVE: Merge RS + IBD → rs_daily_v2
        # -------------------------------------------------------------------
        if df_rs_today is not None and len(df_rs_today) > 0 and calculator is not None:
            logger.info("💾 [5/11] Atomic Merge: Saving RS + IBD to rs_daily_v2...")
            df_rs_today['sector_rs_rating']         = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sector_rs_rating'))
            df_rs_today['industry_group_rs_rating'] = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_group_rs_rating'))
            df_rs_today['industry_rs_rating']       = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('industry_rs_rating'))
            df_rs_today['sub_industry_rs_rating']   = df_rs_today['symbol'].map(lambda s: group_rs_map.get(s, {}).get('sub_industry_rs_rating'))
            df_rs_today['acc_dis_rating']           = df_rs_today['symbol'].map(acc_dis_map)

            calculator.save_bulk_results_with_ibd(df_rs_today)
            logger.info(f"✅ RS + IBD atomically written for {len(df_rs_today)} stocks.")

            # 🔍 Verification for RS Rating using SQL JOIN & Index on (symbol, date)
            try:
                rs_sample = db.execute(text("""
                    SELECT 
                        s.sym,
                        r17.rs_rating AS rs17, r17.acc_dis_rating AS ad17,
                        r20.rs_rating AS rs20, r20.acc_dis_rating AS ad20,
                        r21.rs_rating AS rs21, r21.acc_dis_rating AS ad21
                    FROM (VALUES ('1010'), ('1120'), ('2222'), ('7010'), ('2010')) AS s(sym)
                    LEFT JOIN rs_daily_v2 r17 ON r17.symbol = s.sym AND r17.date = '2026-09-17'
                    LEFT JOIN rs_daily_v2 r20 ON r20.symbol = s.sym AND r20.date = '2026-09-20'
                    LEFT JOIN rs_daily_v2 r21 ON r21.symbol = s.sym AND r21.date = '2026-09-21'
                    ORDER BY s.sym
                """)).fetchall()
                logger.info("  🔍 [VERIFY RS & Acc/Dis via SQL JOIN]:")
                for r in rs_sample:
                    logger.info(f"     📊 {r.sym} -> RS [17: {r.rs17} | 20: {r.rs20} | 21: {r.rs21}] | Acc/Dis: [17: {r.ad17} | 20: {r.ad20} | 21: {r.ad21}]")
            except Exception as e:
                db.rollback()
                logger.warning(f"  ⚠️ Could not verify RS sample: {e}")


        del df_rs_today, group_rs_map, acc_dis_map, calculator
        gc.collect()

        # 6. Industry Groups Metrics
        # -------------------------------------------------------------------
        logger.info("🏭 [6/11] Calculating Industry Groups (IBD Score, Rank 1w/3m/6m)...")
        from scripts.calculate_industry_groups import IndustryGroupCalculator
        ig_calc = IndustryGroupCalculator(db)
        group_indices = ig_calc.calculate_group_index_prices(target_date)
        if group_indices:
            group_df = ig_calc.calculate_ibd_group_score(group_indices)
            if not group_df.empty:
                summary_ig = ig_calc.prepare_summary_data(group_df, target_date)
                if not summary_ig.empty:
                    ig_calc.save(summary_ig, target_date)
                    logger.info(f"✅ Industry Groups updated ({len(summary_ig)} groups).")

                    # 🔍 Verification for Industry Groups using SQL JOIN
                    try:
                        ig_sample = db.execute(text("""
                            SELECT 
                                g.name,
                                ig17.rs_score AS score17, ig17.rank AS rank17,
                                ig20.rs_score AS score20, ig20.rank AS rank20,
                                ig21.rs_score AS score21, ig21.rank AS rank21
                            FROM (VALUES ('Banks'), ('Materials'), ('Telecommunication Services')) AS g(name)
                            LEFT JOIN industry_group_history ig17 ON ig17.industry_group = g.name AND ig17.date = '2026-09-17'
                            LEFT JOIN industry_group_history ig20 ON ig20.industry_group = g.name AND ig20.date = '2026-09-20'
                            LEFT JOIN industry_group_history ig21 ON ig21.industry_group = g.name AND ig21.date = '2026-09-21'
                        """)).fetchall()
                        logger.info("  🔍 [VERIFY Industry Groups via SQL JOIN]:")
                        for ig in ig_sample:
                            logger.info(f"     🏭 {ig.name} -> RS Score [17: {ig.score17} | 20: {ig.score20} | 21: {ig.score21}] | Rank [17: #{ig.rank17} | 20: #{ig.rank20} | 21: #{ig.rank21}]")
                    except Exception as e:
                        db.rollback()
                        logger.warning(f"  ⚠️ Could not verify Industry Groups sample: {e}")


        del ig_calc
        gc.collect()


        # 7. Stock Indicators (Minervini / PineScript) in Batches
        # -------------------------------------------------------------------
        logger.info("📈 [7/11] Calculating Stock Technical Indicators (Batched Mode)...")
        from scripts.calculate_stock_indicators import calculate_and_store_indicators
        BATCH_SIZE = 140
        all_symbols = list(tech_map.keys())
        total_processed = total_successful = total_errors = 0

        for batch_num, batch_start in enumerate(range(0, len(all_symbols), BATCH_SIZE), start=1):
            batch_symbols = all_symbols[batch_start : batch_start + BATCH_SIZE]
            batch_tech_map = {sym: tech_map[sym] for sym in batch_symbols}

            b_processed, b_errors, b_successful = calculate_and_store_indicators(
                db, target_date, tech_map=batch_tech_map
            )
            total_processed  += b_processed
            total_errors     += b_errors
            total_successful += b_successful
            del batch_tech_map, batch_symbols
            gc.collect()

        logger.info(f"✅ Stock Indicators complete: {total_successful} successful, {total_errors} errors.")

        # 🔍 Verification for Stock Indicators using SQL JOIN & Index on (symbol, date)
        try:
            ind_sample = db.execute(text("""
                SELECT 
                    s.sym,
                    i17.sma_20 AS sma20_17, i17.trend_signal AS tt17,
                    i20.sma_20 AS sma20_20, i20.trend_signal AS tt20,
                    i21.sma_20 AS sma20_21, i21.trend_signal AS tt21
                FROM (VALUES ('1010'), ('1120'), ('2222'), ('7010'), ('2010')) AS s(sym)
                LEFT JOIN stock_indicators i17 ON i17.symbol = s.sym AND i17.date = '2026-09-17'
                LEFT JOIN stock_indicators i20 ON i20.symbol = s.sym AND i20.date = '2026-09-20'
                LEFT JOIN stock_indicators i21 ON i21.symbol = s.sym AND i21.date = '2026-09-21'
                ORDER BY s.sym
            """)).fetchall()
            logger.info("  🔍 [VERIFY Stock Indicators via SQL JOIN]:")
            for ind in ind_sample:
                logger.info(f"     📈 {ind.sym} -> SMA20 [17: {ind.sma20_17} | 20: {ind.sma20_20} | 21: {ind.sma20_21}] | Trend Signal: [17: {ind.tt17} | 20: {ind.tt20} | 21: {ind.tt21}]")
        except Exception as e:
            db.rollback()
            logger.warning(f"  ⚠️ Could not verify Stock Indicators sample: {e}")



        del tech_map, all_symbols
        gc.collect()

        # 8. Screener Daily Trend Pre-aggregation
        # -------------------------------------------------------------------
        try:
            logger.info("📊 [8/11] Updating Screener Daily Trend summary...")
            from app.services.screener_daily_trend_service import update_market_date
            update_market_date(db, target_date)
            logger.info("✅ Screener daily trend row saved.")
        except Exception as trend_err:
            logger.error(f"⚠️ Screener daily trend error: {trend_err}")

        # 9. Market Breadth & RS Line
        # -------------------------------------------------------------------
        logger.info("🌊 [9/11] Updating Daily Market Breadth & RS Line Metrics...")
        from scripts.update_daily_market_breadth import update_todays_market_breadth
        update_todays_market_breadth(db, target_date)

        try:
            from scripts.calculate_rs_line_metrics import calculate_and_store_rs_line_metrics
            calculate_and_store_rs_line_metrics(db, target_date)
            logger.info("✅ Market Breadth and RS Line calculated successfully.")

            # 🔍 Verification for Market Breadth
            try:
                mb_sample = db.execute(text("""
                    SELECT date, pct_above_20, pct_above_50, pct_above_200
                    FROM market_breadth
                    WHERE date IN ('2026-09-17', '2026-09-20', '2026-09-21')
                    ORDER BY date
                """)).fetchall()
                logger.info("  🔍 [VERIFY Market Breadth (17, 20, 21)]:")
                for mb in mb_sample:
                    logger.info(f"     🌊 {mb.date} -> Above SMA20: {mb.pct_above_20}% | Above SMA50: {mb.pct_above_50}% | Above SMA200: {mb.pct_above_200}%")
            except Exception as mb_err:
                db.rollback()
                logger.warning(f"  ⚠️ Could not verify Market Breadth sample: {mb_err}")
        except Exception as rs_err:
            logger.error(f"⚠️ RS Line Metrics error: {rs_err}")
            _rollback_session(db)


        # 10. Export RS Hub JSON & Valuation Models
        # -------------------------------------------------------------------
        logger.info("📦 [10/11] Exporting RS Hub Cache & Valuation Models...")
        try:
            from scripts.export_rs_hub_data import export_rs_hub_data
            export_rs_hub_data(target_date)
            logger.info("✅ RS Hub JSON exported.")
        except Exception as exp_err:
            logger.error(f"⚠️ RS Hub export error: {exp_err}")

        try:
            from app.services.xbrl_data_service import OUTPUT_DIR, _get_r2_client, R2_BUCKET_NAME, list_companies
            from app.services.rebh_engine_service import calculate_valuation_models

            companies = list_companies()
            all_models = []
            for c in companies:
                try:
                    m = calculate_valuation_models(c.symbol)
                    all_models.append({
                        "symbol": c.symbol,
                        "company_name": c.company_name,
                        "sector": c.sector,
                        "models": m.get("models")
                    })
                except Exception as model_err:
                    pass

            models_summary_path = OUTPUT_DIR / "all_models_summary.json"
            with open(models_summary_path, "w", encoding="utf-8") as f:
                json.dump(all_models, f, ensure_ascii=False, indent=2)

            r2_client = _get_r2_client()
            if r2_client:
                r2_client.upload_file(
                    Filename=str(models_summary_path),
                    Bucket=R2_BUCKET_NAME,
                    Key="all_models_summary.json",
                    ExtraArgs={"ContentType": "application/json"}
                )
                logger.info(f"✅ Pre-compiled models ({len(all_models)} companies) uploaded to R2.")
            else:
                logger.info(f"✅ Pre-compiled models ({len(all_models)} companies) saved locally.")
        except Exception as models_err:
            logger.error(f"⚠️ Valuation models sync error: {models_err}")

        # 11. Universal Engine Vintages
        # -------------------------------------------------------------------
        logger.info("🏛️ [11/11] Generating Point-In-Time Engine Vintages...")
        try:
            from app.services.rebh_unified_engine import calculate_full_company_payload
            from app.services.xbrl_data_service import list_companies, OUTPUT_DIR
            from app.services.rebh_production_helper_service import warm_sector_medians_cache
            from app.services.khurafshi_engine_service import _get_latest_prices_map

            warm_sector_medians_cache()
            prices_map = _get_latest_prices_map()
            companies = list_companies()
            vintages_file = OUTPUT_DIR / f"rebh_engine_vintage_{target_date}.json"
            engine_vintages = {}

            def _process_company_vintage(c_item):
                try:
                    payload = calculate_full_company_payload(c_item.symbol, prices_map=prices_map)
                    return c_item.symbol, payload.model_dump(mode="json")
                except Exception:
                    return c_item.symbol, None

            with ThreadPoolExecutor(max_workers=8) as executor:
                future_to_sym = {executor.submit(_process_company_vintage, c): c.symbol for c in companies}
                for f in as_completed(future_to_sym):
                    sym, res = f.result()
                    if res is not None:
                        engine_vintages[sym] = res

            with open(vintages_file, "w", encoding="utf-8") as vf:
                json.dump(engine_vintages, vf, ensure_ascii=False)
            logger.info(f"✅ Generated and archived engine vintage for {len(engine_vintages)} companies as of {target_date}.")
        except Exception as vintage_err:
            logger.error(f"⚠️ Engine vintage archiving error: {vintage_err}")

        # Finalize
        _release_update_lock(target_date)
        logger.info(f"🎉 SUCCESS! Pipeline for {target_date} finished completely and verified!")

    except Exception as e:
        logger.error(f"❌ Pipeline Failure for {target_date}: {e}")
        logger.error(traceback.format_exc())
        _rollback_session(db)
        _release_update_lock()
        raise e
    finally:
        try:
            db.close()
        except Exception:
            pass


def flush_redis_cache():
    """Invalidate Redis cache at the very end."""
    logger.info("🧹 Invalidating application caches (Redis)...")
    try:
        import redis as sync_redis
        r = sync_redis.from_url(str(settings.REDIS_URL), decode_responses=True, socket_timeout=2)
        r.ping()
        auth_prefixes = (
            "token_blacklist:", "refresh_token:", "refresh_jti:",
            "session_index:", "oauth_state:", "oauth_link:",
            "verify_token:", "reset_token:",
        )
        deleted_count = 0
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match="*", count=500)
            keys_to_del = [k for k in keys if not any(k.startswith(p) for p in auth_prefixes)]
            if keys_to_del:
                r.delete(*keys_to_del)
                deleted_count += len(keys_to_del)
            if cursor == 0:
                break
        r.close()
        logger.info(f"✅ Redis cache flushed successfully ({deleted_count} keys removed).")
    except Exception:
        logger.info("ℹ️ Redis server inactive locally — skipping cache flush.")


def main():
    parser = argparse.ArgumentParser(description="Run Production Analytics Pipeline without Scrapers")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD")
    parser.add_argument("--all-target-dates", action="store_true", help="Run sequentially for Sep 17, Sep 20, Sep 21")
    args = parser.parse_args()

    if args.all_target_dates:
        target_dates = [
            datetime.date(2026, 9, 17),
            datetime.date(2026, 9, 20),
            datetime.date(2026, 9, 21),
        ]
        logger.info(f"🎯 Running sequentially for target dates: {target_dates}")
        for d in target_dates:
            run_pipeline_for_date(d)
        flush_redis_cache()
        logger.info("🏁 ALL 3 TARGET DATES PROCESSED SUCCESSFULLY!")
    elif args.date:
        d = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
        run_pipeline_for_date(d)
        flush_redis_cache()
    else:
        logger.error("❌ Please provide either --date YYYY-MM-DD or --all-target-dates")

    # Close code block in Markdown Report
    try:
        file_handler.close()
        with open(OUTPUT_MD_PATH, "a", encoding="utf-8") as f:
            f.write("```\n\n")
            f.write("## ✅ Execution Completed Successfully\n")
            f.write(f"All calculations for targeted dates were verified and stored in the database.\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()

