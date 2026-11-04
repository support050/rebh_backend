import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime


# Required windows (actual trading days)
PERIODS = {
    "12m": 252,
    "9m": 189,
    "6m": 126,
    "3m": 63,
    "1m": 21,
    "2w": 10,
    "1w": 5
}

async def fetch_all_stocks_time_series(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    import asyncio
    
    results = {}
    batch_size = 20  # only 20 symbols per batch
    
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        tasks = [fetch_time_series(symbol) for symbol in batch]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        for symbol, response in zip(batch, responses):
            if not isinstance(response, Exception) and response is not None:
                results[symbol] = response
        
        # Wait 2 seconds between batches
        if i + batch_size < len(symbols):
            print(f"⏳ Waiting 2 seconds before next batch...")
            await asyncio.sleep(2)
    
    return results

def calculate_period_change(df: pd.DataFrame, days: int) -> Optional[float]:
    """Compute the Change% for a single window"""
    try:
        if df is None or len(df) < 2:
            return None
        
        n = min(days, len(df))
        recent_data = df.tail(n)
        
        if len(recent_data) < 2:
            return None
            
        old_price = recent_data.iloc[0]["close"]
        new_price = recent_data.iloc[-1]["close"]
        
        if pd.isna(old_price) or pd.isna(new_price) or old_price == 0:
            return None
            
        change_percent = ((new_price - old_price) / old_price) * 100
        return change_percent
        
    except Exception as e:
        print(f"❌ Error calculating {days}d change: {str(e)}")
        return None

def percentrank_inc(data: List[float], value: float) -> float:
    """
    🎯 PERCENTRANK.INC exactly as implemented in Excel
    = (count(values < v) + 0.5 * count(values == v)) / (total_count - 1)
    """
    if len(data) < 2:
        return 0.0
    
    arr = np.array(data)
    less_than = np.sum(arr < value)
    equal_to = np.sum(arr == value)
    rank = (less_than + (0.5 * equal_to)) / (len(data) - 1)
    
    return rank

def calculate_rs_for_period(all_data: Dict[str, pd.DataFrame], period_days: int, period_name: str = "") -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[float]]]:
    """
    Compute RS Rating and Change% for every symbol over one window
    Returns: (rs_scores_dict, change_scores_dict)
    """
    try:
        # 1️⃣ Change% per symbol
        changes = {}
        for symbol, df in all_data.items():
            change = calculate_period_change(df, period_days)
            if change is not None:
                changes[symbol] = change
        
        if not changes:
            empty_result = {symbol: None for symbol in all_data.keys()}
            return empty_result, empty_result
        
        # 2️⃣ List of the change values
        change_values = list(changes.values())
        n = len(change_values)
        
        if n < 2:
            empty_result = {symbol: None for symbol in all_data.keys()}
            return empty_result, empty_result
        
        # 3️⃣ PERCENTRANK.INC and RS Score per symbol
        rs_scores = {}
        for symbol, change in changes.items():
            percent_rank = percentrank_inc(change_values, change)
            score = round(percent_rank * 100)
            score = min(score, 99)
            rs_scores[symbol] = score
            
            # 🎯 Verbose trace for the first 3 symbols (sanity check)
            if period_name and symbol in ["1010", "1020", "1030"]:
                print(f"📊 {symbol} [{period_name}]: change={change:.2f}% | rank={percent_rank:.4f} | score={score}")
        
        # 4️⃣ Fill the missing values
        for symbol in all_data.keys():
            if symbol not in rs_scores:
                rs_scores[symbol] = None
                changes[symbol] = None
                
        return rs_scores, changes
        
    except Exception as e:
        print(f"❌ Error calculating RS for period {period_days}: {str(e)}")
        empty_result = {symbol: None for symbol in all_data.keys()}
        return empty_result, empty_result

async def calculate_all_rs_ratings(symbols: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Compute RS Ratings and Change% across every window
    """
    print(f"📊 Fetching time series data for {len(symbols)} symbols...")
    all_data = await fetch_all_stocks_time_series(symbols)
    
    if not all_data:
        print("❌ No data fetched")
        return {}
    
    print(f"✅ Fetched data for {len(all_data)} symbols")
    
    # RS and Change per window
    rs_results = {}
    change_results = {}
    
    for period_name, days in PERIODS.items():
        print(f"\n📈 Computing RS and Change for {period_name} ({days} days)...")
        rs_scores, change_scores = calculate_rs_for_period(all_data, days, period_name)
        rs_results[period_name] = rs_scores
        change_results[period_name] = change_scores
    
    # Shape the final result
    final_results = {}
    for symbol in symbols:
        final_results[symbol] = {}
        for period_name in PERIODS.keys():
            final_results[symbol][f"rs_{period_name}"] = rs_results.get(period_name, {}).get(symbol)
            final_results[symbol][f"change_{period_name}"] = change_results.get(period_name, {}).get(symbol)
    
    print(f"\n✅ Computed RS and Change for {len(final_results)} symbols")
    return final_results