"""
Convert companies_parameters_only.json to a long-format CSV:
Columns: Company_Code, File_Name, Parameter_Number, Parameter_Name
"""
import json
from pathlib import Path
import csv

DATA_DIR = Path("d:/Work/LUMIVST/backend/parameters_analysis")
JSON_PATH = DATA_DIR / "companies_parameters_only.json"
CSV_PATH = DATA_DIR / "companies_parameters_long.csv"

with open(JSON_PATH, 'r', encoding='utf-8') as f:
    companies = json.load(f)

with open(CSV_PATH, 'w', newline='', encoding='utf-8') as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(['Company_Code', 'Oldest_File', 'Newest_File', 'Parameter_Number', 'Parameter_Name'])
    for code in sorted(companies.keys()):
        entry = companies[code]
        params = entry.get('combined_parameters', entry.get('parameters', []))
        oldest_file = entry.get('oldest_file_name', '')
        newest_file = entry.get('newest_file_name', entry.get('file_name', ''))
        for i, p in enumerate(params, 1):
            writer.writerow([code, oldest_file, newest_file, i, p])

print(f"Created: {CSV_PATH}")
