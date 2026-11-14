"""
Convert companies_parameters_only.json to Excel file
"""

import json
import pandas as pd
from pathlib import Path

# Load JSON
data_dir = Path("d:/Work/LUMIVST/backend/parameters_analysis")
json_path = data_dir / "companies_parameters_only.json"

with open(json_path, 'r', encoding='utf-8') as f:
    companies_data = json.load(f)

# Create Excel with two sheets:
# Sheet 1: Summary (Company, File Name, Parameter Count)
# Sheet 2: Full Parameters List

excel_path = data_dir / "companies_parameters_only.xlsx"

with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
    
    # Sheet 1: Summary
    summary_rows = []
    for company_code in sorted(companies_data.keys()):
        data = companies_data[company_code]
        summary_rows.append({
            'Company_Code': company_code,
            'File_Name': data['file_name'],
            'Parameter_Count': data['count']
        })
    
    df_summary = pd.DataFrame(summary_rows)
    df_summary.to_excel(writer, sheet_name='Summary', index=False)
    
    # Sheet 2: Full Parameters List
    full_rows = []
    for company_code in sorted(companies_data.keys()):
        data = companies_data[company_code]
        for param_idx, param in enumerate(data['parameters'], 1):
            full_rows.append({
                'Company_Code': company_code,
                'Parameter_Number': param_idx,
                'Parameter_Name': param
            })
    
    df_full = pd.DataFrame(full_rows)
    df_full.to_excel(writer, sheet_name='All_Parameters', index=False)

print(f"Created Excel file: {excel_path}")
print(f"\nSheet 1 (Summary): Company codes with parameter counts")
print(f"Sheet 2 (All_Parameters): All parameters per company")
