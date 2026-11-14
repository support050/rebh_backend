"""
Convert companies_parameters_only.json to complete Excel file with all parameters
"""

import json
import pandas as pd
from pathlib import Path
from collections import OrderedDict

# Load JSON
data_dir = Path("d:/Work/LUMIVST/backend/parameters_analysis")
json_path = data_dir / "companies_parameters_only.json"

with open(json_path, 'r', encoding='utf-8') as f:
    companies_data = json.load(f)

# Get all unique parameters across all companies
all_parameters = set()
for company_data in companies_data.values():
    all_parameters.update(company_data['parameters'])
all_parameters = sorted(list(all_parameters))

print(f"Total unique parameters: {len(all_parameters)}")
print(f"Total companies: {len(companies_data)}")

# Create wide format: rows = companies, columns = parameters (X if present)
wide_data = []
for company_code in sorted(companies_data.keys()):
    company_params = set(companies_data[company_code]['parameters'])
    row = {
        'Company_Code': company_code,
        'File_Name': companies_data[company_code]['file_name'],
        'Parameter_Count': companies_data[company_code]['count']
    }
    
    # Add X for each parameter that company has
    for param in all_parameters:
        row[param] = 'X' if param in company_params else ''
    
    wide_data.append(row)

# Create DataFrame and save to Excel
df_wide = pd.DataFrame(wide_data)

excel_path = data_dir / "companies_parameters_complete.xlsx"
df_wide.to_excel(excel_path, sheet_name='Parameters_Comparison', index=False)

print(f"\nCreated Excel file: {excel_path}")
print(f"Rows: {len(df_wide)} companies")
print(f"Columns: {len(df_wide.columns)} (3 info columns + {len(all_parameters)} parameters)")
print("\nFormat: X = Company has this parameter, blank = Company doesn't have it")
