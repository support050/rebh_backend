import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from sqlalchemy import inspect
from dateutil import parser as date_parser

# Ensure Python finds the app package from repository root (backend/app)
BASE_DIR = Path(__file__).resolve().parent.parent  # backend/scripts -> backend
sys.path.insert(0, str(BASE_DIR))

import pandas as pd
from sqlalchemy import update

try:
    # Assuming script is run from project root or backend dir
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from app.core.database import SessionLocal, engine
    from app.models.price import Price
    from app.models.static_stock_info import StaticStockInfo
except ImportError:
    # Fallback if run standalone
    from sqlalchemy import create_engine

from app.core.database import SessionLocal
from app.models.price import Price


VALID_SYMBOLS = {
    "2288", "1010", "1020", "1030", "1050", "1060", "1080", "1111", "1120", "1140", "1150", "1180", "1182", "1183",
    "1201", "1202", "1210", "1211", "1212", "1213", "1214", "1301", "1302", "1303", "1304", "1320", "1321", "1322",
    "1323", "1324", "1810", "1820", "1830", "1831", "1832", "1833", "1834", "1835", "2001", "2010", "2020", "2030",
    "2040", "2050", "2060", "2070", "2080", "2081", "2082", "2083", "2084", "2090", "2100", "2110", "2120", "2130",
    "2140", "2150", "2160", "2170", "2180", "2190", "2200", "2210", "2220", "2222", "2223", "2230", "2240", "2250",
    "2270", "2280", "2281", "2282", "2283", "2284", "2285", "2286", "2287", "2290", "2300", "2310", "2320", "2330",
    "2340", "2350", "2360", "2370", "2380", "2381", "2382", "3002", "3003", "3004", "3005", "3007", "3008", "3010",
    "3020", "3030", "3040", "3050", "3060", "3080", "3090", "3091", "3092", "4001", "4002", "4003", "4004", "4005",
    "4006", "4007", "4008", "4009", "4011", "4012", "4013", "4014", "4015", "4016", "4017", "4018", "4019", "4020",
    "4021", "4030", "4031", "4040", "4050", "4051", "4061", "4070", "4071", "4072", "4080", "4081", "4082", "4083",
    "4084", "4090", "4100", "4110", "4130", "4140", "4141", "4142", "4143", "4144", "4145", "4146", "4147", "4148",
    "4150", "4160", "4161", "4162", "4163", "4164", "4165", "4170", "4180", "4190", "4191", "4192", "4193", "4194",
    "4200", "4210", "4220", "4230", "4240", "4250", "4260", "4261", "4262", "4263", "4264", "4265", "4270", "4280",
    "4290", "4291", "4292", "4300", "4310", "4320", "4321", "4322", "4323", "4324", "4325", "4326", "4327", "4330",
    "4331", "4332", "4333", "4334", "4335", "4336", "4337", "4338", "4339", "4340", "4342", "4344", "4345", "4346",
    "4347", "4348", "4349", "4350", "5110", "6001", "6002", "6004", "6010", "6012", "6013", "6014", "6015", "6016",
    "6017", "6018", "6019", "6020", "6040", "6050", "6060", "6070", "6090", "7010", "7020", "7030", "7040", "7200",
    "7201", "7202", "7203", "7204", "7211", "8010", "8012", "8020", "8030", "8040", "8050", "8060", "8070", "8100",
    "8120", "8150", "8160", "8170", "8180", "8190", "8200", "8210", "8230", "8240", "8250", "8260", "8280", "8300",
    "8310", "8311", "8313"
}


def find_existing_column(columns, candidates):
    for cand in candidates:
        if cand in columns:
            return cand
    return None


def clean_val(val, is_numeric=False):
    """Converts NaN to None and ensures correct types."""
    if pd.isna(val):
        return None
    if is_numeric:
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
    return val



def import_static_stock_info(file1_path: str, file2_path: str,
                             symbol_column: str = None,
                             approval_column: str = None,
                             purge_amount_column: str = None,
                             marginable_column: str = None) -> dict:
    """Reads two Excel files and updates price static fields in DB."""

    df1 = pd.read_excel(file1_path, engine='openpyxl')
    df2 = pd.read_excel(file2_path, engine='openpyxl')

    # Auto-detect column names
    symbol_column_df1 = find_existing_column(
        df1.columns,
        ['Symbol', 'symbol', 'Symbol Code', 'symbol code', 'الرمز', 'الرقم']
    )

    symbol_column_df2 = find_existing_column(
        df2.columns,
        ['Symbol', 'symbol', 'Symbol Code', 'symbol code', 'الرمز', 'الرقم']
    )

    symbol_column = symbol_column or symbol_column_df1 or symbol_column_df2

    approval_column = approval_column or find_existing_column(
        df1.columns,
        ['الحكم', 'موافقة مع الضوابط', 'approval', 'approval_with_controls']
    )

    purge_amount_column = purge_amount_column or find_existing_column(
        df1.columns,
        ['مبلغ التطهير', 'مبلغ التطهير (SAR)', 'purge_amount', 'Purge Amount']
    )

    marginable_column = marginable_column or find_existing_column(
        df2.columns,
        ['Marginable %', 'Marginable', 'marginable_percent', 'مستوى قابل للتداول', 'MarginablePercent']
    )

    if not symbol_column_df1 or not symbol_column_df2:
        raise ValueError('Symbol column missing in one of input files')

    if not approval_column or not purge_amount_column:
        raise ValueError('file1 must contain approval + purge amount columns')

    if not marginable_column:
        raise ValueError('file2 must contain marginable percent column')

    # Rename columns in df1
    df1 = df1.rename(columns={
        symbol_column_df1: 'symbol',
        approval_column: 'approval_with_controls',
        purge_amount_column: 'purge_amount'
    })

    # Rename columns in df2
    df2 = df2.rename(columns={
        symbol_column_df2: 'symbol',
        marginable_column: 'marginable_percent'
    })

    # Convert symbol columns to string and strip after rename
    df1['symbol'] = df1['symbol'].astype(str).str.strip().str.replace('.0', '', regex=False)
    df2['symbol'] = df2['symbol'].astype(str).str.strip().str.replace('.0', '', regex=False)

    # Filtering: Only keep symbols in VALID_SYMBOLS
    df1 = df1[df1['symbol'].isin(VALID_SYMBOLS)]
    df2 = df2[df2['symbol'].isin(VALID_SYMBOLS)]

    # Deduplication: Keep the first occurrence for each symbol
    df1 = df1.drop_duplicates(subset=['symbol'], keep='first')
    df2 = df2.drop_duplicates(subset=['symbol'], keep='first')

    if 'approval_with_controls' not in df1.columns or 'purge_amount' not in df1.columns:
        raise ValueError('file1 must contain approval_with_controls and purge_amount columns')
    if 'marginable_percent' not in df2.columns:
        raise ValueError('file2 must contain marginable_percent column')

    merged = pd.merge(
        df1[['symbol', 'approval_with_controls', 'purge_amount']],
        df2[['symbol', 'marginable_percent']],
        on='symbol',
        how='outer'
    )

    # Convert marginable_percent from decimal (0.75) to percentage (75)
    # Excel stores 75% as 0.75 internally, pandas reads the raw decimal
    if 'marginable_percent' in merged.columns:
        def convert_marginable(val):
            if pd.isna(val):
                return val
            val = float(val)
            # If value is a decimal fraction (0 < val <= 1), convert to percentage
            if 0 < val <= 1.0:
                return round(val * 100, 2)
            return val
        merged['marginable_percent'] = merged['marginable_percent'].apply(convert_marginable)

    # DEBUG: Print first 15 rows to verify data
    print("\n=== DEBUG: First 15 rows of merged data ===")
    print(merged.head(15).to_string(index=False))
    print("=" * 60)

    db = SessionLocal()
    total_updated = 0
    failures = []

    try:
        total_rows_to_process = len(merged)
        print(f"Starting update for {total_rows_to_process} symbols into StaticStockInfo...")
        
        for i, (_, row) in enumerate(merged.iterrows(), 1):
            symbol = str(row.get('symbol', '')).strip()
            if not symbol or symbol == 'nan':
                continue

            values = {
                'approval_with_controls': clean_val(row.get('approval_with_controls')),
                'purge_amount': clean_val(row.get('purge_amount'), is_numeric=True),
                'marginable_percent': clean_val(row.get('marginable_percent'), is_numeric=True)
            }

            # DEBUG: Print first 5 symbols with their values
            if i <= 5:
                print(f"  DEBUG [{symbol}]: purge={values['purge_amount']}, margin={values['marginable_percent']}, approval={values['approval_with_controls']}")

            if all(v is None for v in values.values()):
                continue

            try:
                # Upsert into StaticStockInfo
                from sqlalchemy.dialects.postgresql import insert
                stmt = insert(StaticStockInfo).values(symbol=symbol, **values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['symbol'],
                    set_={
                        'approval_with_controls': stmt.excluded.approval_with_controls,
                        'purge_amount': stmt.excluded.purge_amount,
                        'marginable_percent': stmt.excluded.marginable_percent
                    }
                )
                db.execute(stmt)
                total_updated += 1
                
                if i % 10 == 0 or i == total_rows_to_process:
                    print(f"Progress: Processed {i}/{total_rows_to_process} symbols. Total rows updated in DB: {total_updated}")
            except Exception as e:
                print(f"Error updating symbol {symbol}: {e}")
                failures.append(symbol)

        db.commit()
        print("Successfully committed changes to database.")

    except Exception as exc:
        db.rollback()
        raise

    finally:
        db.close()

    return {
        'updated_rows': total_updated,
        'failed_symbols': failures,
        'total_symbols_processed': len(merged),
        'run_at': datetime.utcnow().isoformat() + 'Z'
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import static stock info from excel into prices table')
    parser.add_argument('--file1', required=True, help='Path to first Excel file with symbol, approval, purge amount')
    parser.add_argument('--file2', required=True, help='Path to second Excel file with symbol, marginable percent')
    parser.add_argument('--symbol-column', default=None, help='Force column name for symbol in both files')
    parser.add_argument('--approval-column', default=None, help='Force column name for approval in file1')
    parser.add_argument('--purge-column', default=None, help='Force column name for purge amount in file1')
    parser.add_argument('--marginable-column', default=None, help='Force column name for marginable percent in file2')

    args = parser.parse_args()
    result = import_static_stock_info(
        args.file1,
        args.file2,
        symbol_column=args.symbol_column,
        approval_column=args.approval_column,
        purge_amount_column=args.purge_column,
        marginable_column=args.marginable_column
    )

    print('Import result:')
    import json
    print(json.dumps(result, indent=2))
