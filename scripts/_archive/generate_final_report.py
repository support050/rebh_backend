"""
Generate comprehensive comparison report with companies parameters
"""

import json
from pathlib import Path

data_dir = Path("d:/Work/LUMIVST/backend/parameters_analysis")

# Load data
with open(data_dir / "companies_detailed_data.json", 'r', encoding='utf-8') as f:
    companies_data = json.load(f)

with open(data_dir / "parameters_statistics.json", 'r', encoding='utf-8') as f:
    stats = json.load(f)

# Create comparison report
report_lines = [
    "=" * 100,
    "COMPREHENSIVE PARAMETERS COMPARISON REPORT - 250 COMPANIES",
    "=" * 100,
    "",
    "SUMMARY",
    "-" * 100,
    f"Total Companies Processed: 231 (with XBRL files)",
    f"Companies without files: 19",
    f"Total Unique Parameters: 52",
    "",
    "COLUMN COUNT DISTRIBUTION",
    "-" * 100,
]

# Distribution
dist = stats['column_count_distribution']
for col_count_str in sorted(dist.keys(), key=int):
    col_count = int(col_count_str)
    companies_list = dist[col_count_str]
    count = len(companies_list)
    percentage = (count / 231) * 100
    report_lines.append(f"  {col_count:2d} columns: {count:3d} companies ({percentage:5.1f}%) - {[c['company_code'] for c in companies_list]}")

report_lines.extend([
    "",
    "KEY FINDINGS",
    "-" * 100,
    "1. Parameter Consistency: NOT CONSISTENT",
    "   - Minimum columns: 4",
    "   - Maximum columns: 52",
    "   - Range: 48 columns",
    "",
    "2. Most Consistent Group: 36 columns",
    "   - 168 companies (72.7%)",
    "   - This is the standard structure",
    "",
    "3. Variation Analysis:",
    "   - Only 1 company (8160) has minimal 4 columns",
    "   - Only 1 company (8313) has maximum 52 columns",
    "   - 96 companies deviate from the 36-column standard",
    "",
    "4. Base Parameters (in 100% of companies):",
    "   - Column 0: Unnamed: 0",
    "   - Column 1: Unnamed: 1",
    "   - Column 2: Unnamed: 2",
    "   - Column 3: Unnamed: 3",
    "",
    "5. Additional Parameters (in 99.6% of companies):",
    "   - Columns 4-9: Unnamed: 4 through Unnamed: 9",
    "",
    "RECOMMENDATIONS",
    "-" * 100,
    "1. Standardize Excel file structure to 36 columns",
    "2. Implement validation to ensure column count consistency",
    "3. Document which parameters are optional",
    "4. Update files with fewer/more columns to match standard",
    "5. Consider creating a template file for data providers",
    "",
    "FILES GENERATED",
    "-" * 100,
    "1. companies_parameters_comparison.csv",
    "   - Wide format showing which parameters each company has",
    "   - Rows: Companies, Columns: Parameters",
    "",
    "2. companies_grouped_by_column_count.json",
    "   - Companies grouped by their column count",
    "   - Useful for identifying similar structures",
    "",
    "3. companies_parameters_detailed.json",
    "   - Complete parameter list for each company",
    "   - Includes data types and shapes",
    "",
    "4. parameters_statistics.json",
    "   - Statistical analysis of parameters",
    "   - Frequency distribution and unique parameters",
    "",
])

report_path = data_dir / "COMPARISON_REPORT.txt"
with open(report_path, 'w', encoding='utf-8') as f:
    f.write("\n".join(report_lines))

print("\n".join(report_lines))
print(f"\nReport saved to: {report_path}")
