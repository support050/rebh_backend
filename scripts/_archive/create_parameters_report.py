"""
Create a comprehensive comparison report of parameters across all companies
"""

import json
import csv
from pathlib import Path
from collections import defaultdict

# Load the detailed data
data_dir = Path("d:/Work/LUMIVST/backend/parameters_analysis")
companies_data_path = data_dir / "companies_detailed_data.json"

with open(companies_data_path, 'r', encoding='utf-8') as f:
    companies_data = json.load(f)

# Create comprehensive reports
print("Creating comprehensive comparison reports...")

# Report 1: All parameters by company (wide format for easy comparison)
output_csv_path = data_dir / "companies_parameters_comparison.csv"

# Collect all unique columns
all_columns = set()
for company_code, company_data in companies_data.items():
    all_columns.update(company_data.get("combined_parameters", []))

all_columns = sorted(list(all_columns))

# Write CSV with wide format (companies as rows, columns as columns)
with open(output_csv_path, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    
    # Header
    header = ["Company_Code", "Oldest_File", "Newest_File", "Oldest_Param_Count", "Newest_Param_Count", "Combined_Param_Count"] + all_columns
    writer.writerow(header)
    
    # Data rows
    for company_code in sorted(companies_data.keys()):
        company_data = companies_data[company_code]
        oldest_cols = set(company_data.get("oldest_parameters", []))
        newest_cols = set(company_data.get("newest_parameters", []))
        combined_cols = set(company_data.get("combined_parameters", []))
        
        row = [
            company_code,
            company_data.get("oldest_file_name", ""),
            company_data.get("newest_file_name", ""),
            len(oldest_cols),
            len(newest_cols),
            len(combined_cols)
        ]
        
        # Add presence of each column (X for present in newest, O for oldest, B for both)
        for col in all_columns:
            in_old = col in oldest_cols
            in_new = col in newest_cols
            if in_old and in_new:
                row.append("B") # Both
            elif in_new:
                row.append("N") # Newest only
            elif in_old:
                row.append("O") # Oldest only
            else:
                row.append("")
        
        writer.writerow(row)

print(f"Saved parameters comparison CSV: {output_csv_path}")

# Save companies_parameters_only.json for simple exporters
params_only_path = data_dir / "companies_parameters_only.json"
params_only_data = {}
for company_code in sorted(companies_data.keys()):
    company_data = companies_data[company_code]
    oldest_p = set(company_data.get("oldest_parameters", []))
    newest_p = set(company_data.get("newest_parameters", []))
    
    params_only_data[company_code] = {
        "oldest_file_name": company_data.get("oldest_file_name", ""),
        "newest_file_name": company_data.get("newest_file_name", ""),
        "oldest_parameters": company_data.get("oldest_parameters", []),
        "newest_parameters": company_data.get("newest_parameters", []),
        "combined_parameters": company_data.get("combined_parameters", []),
        "new_parameters": sorted(list(newest_p - oldest_p)),
        "removed_parameters": sorted(list(oldest_p - newest_p)),
        "file_name": company_data.get("newest_file_name", ""),
        "parameters": company_data.get("combined_parameters", []),
        "count": len(company_data.get("combined_parameters", []))
    }

with open(params_only_path, 'w', encoding='utf-8') as f:
    json.dump(params_only_data, f, ensure_ascii=False, indent=2)

print(f"Saved parameters only JSON: {params_only_path}")

# Report 3: Detailed parameters list by company
detailed_report_path = data_dir / "companies_parameters_detailed.json"
detailed_output = {}
for company_code in sorted(companies_data.keys()):
    company_data = companies_data[company_code]
    
    detailed_output[company_code] = {
        "oldest_file_name": company_data.get("oldest_file_name", ""),
        "newest_file_name": company_data.get("newest_file_name", ""),
        "oldest_parameters": company_data.get("oldest_parameters", []),
        "newest_parameters": company_data.get("newest_parameters", []),
        "combined_parameters": company_data.get("combined_parameters", []),
        "file_count": company_data.get("file_count", 1)
    }

with open(detailed_report_path, 'w', encoding='utf-8') as f:
    json.dump(detailed_output, f, ensure_ascii=False, indent=2)

print(f"Saved detailed parameters report: {detailed_report_path}")

# Report 4: Statistics summary
stats_report_path = data_dir / "parameters_statistics.json"
column_frequency = defaultdict(int)
for company_code, company_data in companies_data.items():
    for col in company_data.get("combined_parameters", []):
        column_frequency[col] += 1

stats_output = {
    "total_companies_analyzed": len(companies_data),
    "total_unique_parameters": len(all_columns),
    "parameter_frequency": {
        "most_common": sorted(column_frequency.items(), key=lambda x: x[1], reverse=True)[:20],
        "least_common": sorted(column_frequency.items(), key=lambda x: x[1])[:20]
    },
    "all_parameters": sorted(all_columns)
}

with open(stats_report_path, 'w', encoding='utf-8') as f:
    json.dump(stats_output, f, ensure_ascii=False, indent=2)

print(f"Saved statistics report: {stats_report_path}")

print("\n" + "=" * 80)
print("All reports generated successfully!")
print("=" * 80)
print(f"\nReports location: {data_dir}")
print("\nGenerated files:")
print(f"  1. companies_parameters_comparison.csv - Easy comparison of all parameters")
print(f"  2. companies_grouped_by_column_count.json - Companies grouped by column count")
print(f"  3. companies_parameters_detailed.json - Detailed parameters per company")
print(f"  4. parameters_statistics.json - Statistics and frequency analysis")
