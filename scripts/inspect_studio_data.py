import sys
import json
import urllib.request

def inspect_symbol(sym):
    print("=" * 60)
    print(f"Checking data for symbol: {sym}")
    print("=" * 60)

    # 1. Statements endpoint
    url1 = f"http://127.0.0.1:8000/api/rebh/statements/{sym}"
    try:
        req = urllib.request.Request(url1, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data1 = json.loads(resp.read().decode())
            print("[1] /api/rebh/statements keys:")
            print("   ", list(data1.keys()))
            
            # Check financials
            is_d = data1.get("income_statement", {})
            bs_d = data1.get("bs", data1.get("balance_sheet", {}))
            cf_d = data1.get("cf", {})
            q_d = data1.get("quarters", {})
            print("    IS keys:", list(is_d.keys()))
            print("    BS keys:", list(bs_d.keys()))
            print("    CF keys:", list(cf_d.keys()))
            print("    Quarters periods:", q_d.get("periods", [])[-5:])
            print("    Quarters net:", q_d.get("net", [])[-5:])
    except Exception as e:
        print(f"Error calling {url1}: {e}")

    # 2. Engine endpoint (has radar, factor grades, key stats, fair value)
    url2 = f"http://127.0.0.1:8000/api/engine/{sym}"
    try:
        req = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data2 = json.loads(resp.read().decode())
            print("\n[2] /api/engine keys:")
            print("   ", list(data2.keys()))
            
            summary = {
                "market_cap": data2.get("market_cap") or data2.get("mktcap"),
                "pe": data2.get("pe") or data2.get("pe_ttm"),
                "pb": data2.get("pb"),
                "net_debt": data2.get("net_debt"),
                "factor_grades": data2.get("factor_grades"),
                "radar_score": data2.get("radar_score"),
                "fair_value": data2.get("fair_value") or data2.get("target_price") or data2.get("fair_val"),
                "next_quarter": data2.get("next_quarter") or data2.get("next_quarter_estimate") or data2.get("earnings_forecast"),
                "coverage": data2.get("coverage") or data2.get("interest_coverage"),
                "price": data2.get("price") or data2.get("close")
            }
            print("\n[3] Extracted Right-Rail candidates:")
            for k, v in summary.items():
                print(f"    {k}: {v}")
    except Exception as e:
        print(f"Error calling {url2}: {e}")

if __name__ == "__main__":
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["2222", "1120"]
    for s in symbols:
        inspect_symbol(s)
