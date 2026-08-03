import sys, re
sys.path.append('d:/Work/LUMIVST/backend')
from app.services.xbrl_mapping import PARAM_MAPPING as OLD_MAP
from parameters_analysis.generated_xbrl_mapping import PARAM_MAPPING as NEW_MAP

trans = {
    'IS-160': ('IS-140', 1), # Net Profit
    'IS-120': ('IS-030', 1), # Gross Profit
    'IS-100': ('IS-010', 1), # Revenue
    'IS-110': ('IS-020', -1), # Cost of Sales
    'IS-130': ('IS-040', -1), # Selling
    'IS-140': ('IS-050', -1), # G&A
    'IS-150': ('IS-070', 1), # Operating Profit
    'IS-170': ('IS-160', 1), # EPS
    'BS-100': ('BS-090', 1), # Total Assets
    'BS-120': ('BS-160', 1), # Total Liab
    'BS-130': ('BS-190', 1), # Total Equity
    'BS-110': ('BS-010', 1), # Cash
    'BS-140': ('BS-180', 1), # Retained Earnings
    'CF-100': ('CF-060', 1), # OCF
    'CF-110': ('CF-090', 1), # ICF
    'CF-120': ('CF-130', 1), # FCF
    'CF-130': ('CF-140', 1)  # Change in Cash
}

merged = dict(OLD_MAP)
for k, v in NEW_MAP.items():
    if v[0] in trans:
        merged[k] = trans[v[0]]

lines = []
for k, v in sorted(merged.items()):
    clean_k = k.replace("'", "\\'")
    lines.append(f"    '{clean_k}': ('{v[0]}', {v[1]}),\n")

with open('d:/Work/LUMIVST/backend/app/services/xbrl_mapping.py', 'r', encoding='utf-8') as f:
    content = f.read()

pattern = r'PARAM_MAPPING = \{.*?^\}'
replacement = 'PARAM_MAPPING = {\n' + ''.join(lines) + '}'
new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE | re.DOTALL)

with open('d:/Work/LUMIVST/backend/app/services/xbrl_mapping.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print(f'Successfully merged mappings. Total keys: {len(merged)}')
