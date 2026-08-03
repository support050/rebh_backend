"""
build_standardized_mapping.py
=============================
1. Iterates over ALL years (files) for ALL companies in data/downloads/
2. Extracts all unique parameters from every single XBRL file.
3. Groups all similar names and automatically links them to Standardized items.
4. Generates a new comprehensive mapping dictionary (PARAM_MAPPING).
"""

import os
from pathlib import Path
import pandas as pd
import re

DATA_DIR = Path("d:/Work/LUMIVST/backend/data/downloads")
OUTPUT_DIR = Path("d:/Work/LUMIVST/backend/parameters_analysis")
OUTPUT_DIR.mkdir(exist_ok=True)

def extract_parameter_names_from_xls(file_path):
    """Extract actual parameter names/labels from Column 0 of the Excel file"""
    try:
        df = pd.read_excel(file_path, sheet_name=0)
        if df.empty or df.shape[1] == 0:
            return []
        col0 = df.iloc[:, 0].dropna()
        names = []
        for val in col0:
            s = str(val).replace('\xa0', ' ').replace('\u200b', '').strip()
            if s and s not in ('Start Date', 'End Date', 'nan', 'None', 'Sheet0'):
                # Clean up known XBRL abstract/line items indicators to help mapping
                s = re.sub(r"\s*\[abstract\]", "", s, flags=re.I)
                s = re.sub(r"\s*\[line items\]", "", s, flags=re.I)
                names.append(s.strip())
        return names
    except Exception:
        return []

def assign_mapping(param_lower):
    """Smart heuristic keyword mapping for Standardized Items"""
    p = param_lower
    
    # Remove bracket codes like [100010] for matching
    if "]" in p and "[" in p:
        p = p.split("]")[-1].strip()

    # --- Income Statement ---
    if "profit (loss) for the period" == p or p == "net profit" or "profit (loss), attributable to equity holders" in p or "profit (loss) for the year" == p:
        return ("IS-160", "Net Profit")
    if p == "gross profit" or p == "gross profit (loss)":
        return ("IS-120", "Gross Profit")
    if "total operating income" == p or p == "revenue" or "total revenue" == p:
        return ("IS-100", "Revenue / Operating Income")
    if "total operating expenses" == p or "cost of sales" in p or "cost of revenue" in p:
        return ("IS-110", "Cost of Sales / Operating Exp")
    if "selling" in p and "marketing" in p and "expenses" in p:
        return ("IS-130", "Selling & Marketing Exp")
    if "general" in p and "administrative" in p and "expenses" in p:
        return ("IS-140", "General & Admin Exp")
    if "profit (loss) from operating activities" == p or p == "operating profit (loss)" or p == "operating profit":
        return ("IS-150", "Operating Profit")
    if "total basic earnings (loss) per share" in p or p == "basic earnings per share" or p == "earnings per share":
        return ("IS-170", "EPS")
        
    # --- Balance Sheet ---
    if p == "total assets":
        return ("BS-100", "Total Assets")
    if p == "total liabilities":
        return ("BS-120", "Total Liabilities")
    if p == "total equity":
        return ("BS-130", "Total Equity")
    if "cash and cash equivalents" in p or "cash and balances" in p:
        return ("BS-110", "Cash & Equivalents")
    if "retained earnings" in p:
        return ("BS-140", "Retained Earnings")
        
    # --- Cash Flow ---
    if "net cash flows from (used in) operating activities" in p or p == "cash flows from operating activities":
        return ("CF-100", "Operating Cash Flow")
    if "net cash flows from (used in) investing activities" in p or p == "cash flows from investing activities":
        return ("CF-110", "Investing Cash Flow")
    if "net cash flows from (used in) financing activities" in p or p == "cash flows from financing activities":
        return ("CF-120", "Financing Cash Flow")
    if "net increase (decrease) in cash" in p or "net change in cash" in p:
        return ("CF-130", "Net Change in Cash")
        
    return None

def main():
    print("=" * 80)
    print("Extracting parameters from ALL companies and ALL years...")
    print("=" * 80)
    
    company_dirs = sorted([d for d in DATA_DIR.iterdir() if d.is_dir()])
    
    all_params_records = []
    unique_params = set()
    
    total_files_processed = 0
    
    for company_dir in company_dirs:
        company_code = company_dir.name
        
        # Skip duplicate _ar folders
        if company_code.endswith("_ar"):
            continue
            
        xls_files = sorted(list(company_dir.glob("*.xls")) + list(company_dir.glob("*.xlsx")))
        
        for f in xls_files:
            cols = extract_parameter_names_from_xls(f)
            total_files_processed += 1
            for param in cols:
                unique_params.add(param)
                all_params_records.append({
                    "Company_Code": company_code,
                    "File_Name": f.name,
                    "Parameter_Name": param
                })
        
        if len(xls_files) > 0:
            print(f"Processed Company {company_code} - {len(xls_files)} files")

    print("\n" + "=" * 80)
    print(f"Extraction Complete!")
    print(f"Total Files Processed: {total_files_processed}")
    print(f"Total Unique Parameters Found: {len(unique_params)}")
    
    # 2. Group all similar names and automatically link them
    print("\nGenerating Standardized Mappings...")
    mapped_params = {}
    unmapped_params = []
    
    for p in unique_params:
        p_clean = p.lower().strip()
        mapping = assign_mapping(p_clean)
        
        if mapping:
            mapped_params[p_clean] = mapping
        else:
            unmapped_params.append(p)
            
    print(f"Successfully Mapped: {len(mapped_params)} unique parameters")
    print(f"Unmapped (Left as Raw): {len(unmapped_params)} unique parameters")
    
    # 3. Save the Dictionary (PARAM_MAPPING) to a Python file
    output_py = OUTPUT_DIR / "generated_xbrl_mapping.py"
    with open(output_py, "w", encoding="utf-8") as f:
        f.write('"""\nAuto-generated mapping dictionary from all companies/years\n"""\n\n')
        f.write("PARAM_MAPPING = {\n")
        for k, v in sorted(mapped_params.items()):
            safe_k = k.replace('"', '\\"')
            f.write(f'    "{safe_k}": ("{v[0]}", "{v[1]}"),\n')
        f.write("}\n")
        
    # Save the detailed extraction to CSV for the user
    df_all = pd.DataFrame(all_params_records)
    csv_out = OUTPUT_DIR / "all_years_parameters_extracted.csv"
    df_all.to_csv(csv_out, index=False, encoding='utf-8-sig')
    
    # Save a clean mapping view to CSV
    df_mapping = pd.DataFrame([
        {"Raw_Parameter": k, "Standardized_Code": v[0], "Standardized_Name": v[1]} 
        for k, v in mapped_params.items()
    ])
    csv_map_out = OUTPUT_DIR / "standardized_mapping_results.csv"
    df_mapping.to_csv(csv_map_out, index=False, encoding='utf-8-sig')
    
    print("\n" + "=" * 80)
    print(f"SUCCESS!")
    print(f"1. Detailed all-years parameters CSV saved to: {csv_out}")
    print(f"2. Mapping results CSV saved to: {csv_map_out}")
    print(f"3. Python Dictionary for xbrl_mapping.py saved to: {output_py}")
    print("=" * 80)

if __name__ == "__main__":
    main()
