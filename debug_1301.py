import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open("output/1301_financials.json", "r", encoding="utf-8") as f:
    d = json.load(f)

items = d['sections']['income_statement']['items']
print("=== Income Statement Items for 1301 ===")
for i in items:
    label = i['label']
    is_hdr = i['is_header']
    # Show Q1 2026 value if available (or any value just to see)
    v2026 = i['values'].get('2026-01_2026-03')
    if v2026 is None:
        # try to get any non-null value
        for v in i['values'].values():
            if v is not None:
                v2026 = f"(other period) {v}"
                break
    print(f"{'[H] ' if is_hdr else '    '}{label:60s} | {v2026}")
