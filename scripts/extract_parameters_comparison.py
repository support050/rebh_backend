"""
Extract Parameters from XBRL files for all companies and compare them
"""

import os
import sys
import json
import pandas as pd
from pathlib import Path
from collections import defaultdict
import openpyxl

# Data paths
DATA_DIR = Path("d:/Work/LUMIVST/backend/data/downloads")
OUTPUT_DIR = Path("d:/Work/LUMIVST/backend/parameters_analysis")
OUTPUT_DIR.mkdir(exist_ok=True)

def extract_data_sample_from_xls(file_path, num_rows=5):
    """Extract sample data from Excel file with complete information"""
    try:
        xls = pd.ExcelFile(file_path)
        data_structure = {}
        
        for sheet_name in xls.sheet_names[:3]:  # First 3 sheets only
            df = pd.read_excel(file_path, sheet_name=sheet_name)
            data_structure[sheet_name] = {
                "columns": list(df.columns),
                "shape": df.shape,
                "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
                "sample_rows": df.head(num_rows).to_dict('records')
            }
        
        return data_structure
    except Exception as e:
        print(f"Error: {e}")
        return {}

def main():
    print("=" * 80)
    print("Starting parameter extraction from company files")
    print("=" * 80)
    
    companies_data = {}
    company_files_info = []
    
    # Get all company directories
    company_dirs = sorted([d for d in DATA_DIR.iterdir() if d.is_dir()])
    total_companies = len(company_dirs)
    
    print(f"\nTotal companies detected: {total_companies}\n")
    
    for idx, company_dir in enumerate(company_dirs, 1):
        company_code = company_dir.name
        
        # Search for XBRL files
        xls_files = sorted(list(company_dir.glob("*.xls")) + list(company_dir.glob("*.xlsx")), key=lambda p: p.name)
        
        if not xls_files:
            print(f"[{idx:3d}] {company_code}: No XBRL files")
            continue
        
        oldest_file = None
        oldest_struct = None
        for f in xls_files:
            struct = extract_data_sample_from_xls(f)
            if struct:
                oldest_file = f
                oldest_struct = struct
                break
                
        newest_file = None
        newest_struct = None
        for f in reversed(xls_files):
            struct = extract_data_sample_from_xls(f)
            if struct:
                newest_file = f
                newest_struct = struct
                break
                
        if not oldest_file or not newest_file:
            print(f"[{idx:3d}] {company_code}: Unable to read any XBRL files")
            continue
        
        print(f"[{idx:3d}] {company_code}: Oldest={oldest_file.name} | Newest={newest_file.name}")
        
        oldest_cols = oldest_struct.get("Sheet0", {}).get("columns", []) if oldest_struct else []
        newest_cols = newest_struct.get("Sheet0", {}).get("columns", []) if newest_struct else []
        
        combined_cols = sorted(list(set(oldest_cols).union(set(newest_cols))))
        
        companies_data[company_code] = {
            "oldest_file_name": oldest_file.name,
            "oldest_file_path": str(oldest_file),
            "oldest_data_structure": oldest_struct,
            "oldest_parameters": oldest_cols,
            "newest_file_name": newest_file.name,
            "newest_file_path": str(newest_file),
            "newest_data_structure": newest_struct,
            "newest_parameters": newest_cols,
            "combined_parameters": combined_cols,
            "file_count": len(xls_files)
        }
        
        # Store file information
        company_files_info.append({
            "company_code": company_code,
            "oldest_file": oldest_file.name,
            "newest_file": newest_file.name,
            "total_files": len(xls_files)
        })
        
        # Print progress every 50 companies
        if idx % 50 == 0:
            print(f"  Progress: {idx} of {total_companies}\n")
    
    # Save file information
    files_info_path = OUTPUT_DIR / "companies_files_info.json"
    with open(files_info_path, 'w', encoding='utf-8') as f:
        json.dump(company_files_info, f, ensure_ascii=False, indent=2)
    
    print(f"\nSaved file info to: {files_info_path}")
    
    # Save complete data
    companies_data_path = OUTPUT_DIR / "companies_detailed_data.json"
    with open(companies_data_path, 'w', encoding='utf-8') as f:
        json.dump(companies_data, f, ensure_ascii=False, indent=2)
    
    print(f"Saved detailed data to: {companies_data_path}")
    
    # Analyze parameters and sheets
    print("\n" + "=" * 80)
    print("Analyzing parameters and sheets")
    print("=" * 80)
    
    all_sheets = set()
    all_columns_by_sheet = defaultdict(set)
    sheet_column_consistency = defaultdict(list)
    
    for company_code, company_data in companies_data.items():
        for sheet_name, sheet_data in company_data["newest_data_structure"].items():
            all_sheets.add(sheet_name)
            columns = set(sheet_data["columns"])
            all_columns_by_sheet[sheet_name].update(columns)
            sheet_column_consistency[sheet_name].append({
                "company_code": company_code,
                "columns": sheet_data["columns"],
                "column_count": len(sheet_data["columns"])
            })
    
    print(f"\nUnique sheet names: {len(all_sheets)}")
    print(f"Sheets: {sorted(all_sheets)}\n")
    
    # Analyze parameter consistency for each sheet
    analysis_results = {}
    for sheet_name in sorted(all_sheets):
        columns_list = [item["columns"] for item in sheet_column_consistency[sheet_name]]
        column_counts = [item["column_count"] for item in sheet_column_consistency[sheet_name]]
        
        analysis_results[sheet_name] = {
            "total_companies_with_sheet": len(columns_list),
            "column_count_variation": {
                "min": min(column_counts),
                "max": max(column_counts),
                "is_consistent": min(column_counts) == max(column_counts)
            },
            "all_unique_columns": sorted(list(all_columns_by_sheet[sheet_name])),
            "column_count_unique_values": list(set(column_counts)),
            "companies_column_consistency": sheet_column_consistency[sheet_name][:5]
        }
    
    # Save analysis results
    analysis_path = OUTPUT_DIR / "parameters_analysis.json"
    with open(analysis_path, 'w', encoding='utf-8') as f:
        json.dump(analysis_results, f, ensure_ascii=False, indent=2)
    
    print(f"Saved analysis results to: {analysis_path}\n")
    
    # Print summary
    print("\nSummary of Results:")
    print("-" * 80)
    for sheet_name, result in analysis_results.items():
        consistency_status = "CONSISTENT" if result["column_count_variation"]["is_consistent"] else "INCONSISTENT"
        print(f"\nSheet: {sheet_name}")
        print(f"  Companies with sheet: {result['total_companies_with_sheet']}")
        print(f"  Column count: min={result['column_count_variation']['min']}, max={result['column_count_variation']['max']} [{consistency_status}]")
        print(f"  Total unique columns: {len(result['all_unique_columns'])}")
        if not result["column_count_variation"]["is_consistent"]:
            print(f"  Different column count values: {result['column_count_unique_values']}")
    
    print("\n" + "=" * 80)
    print("Extraction completed!")
    print("=" * 80)

if __name__ == "__main__":
    main()
