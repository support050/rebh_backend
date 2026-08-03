import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open("output/1301_financials.json", "r", encoding="utf-8") as f:
    d = json.load(f)

# check standardized_income_statement
if "standardized_income_statement" in d['sections']:
    items = d['sections']['standardized_income_statement']['items']
    print("=== Standardized Income Statement Items for 1301 ===")
    for i in items:
        label = i['label']
        is_hdr = i['is_header']
        v2026 = i['values'].get('2026-01_2026-03')
        if v2026 is None:
            for v in i['values'].values():
                if v is not None:
                    v2026 = f"(other period) {v}"
                    break
        print(f"{'[H] ' if is_hdr else '    '}{label:60s} | {v2026}")
else:
    print("No standardized_income_statement found.")
