import requests
import json
import sys
import os
from pathlib import Path

# Auto-load .env from backend root
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=False)

# API Configuration
API_URL = "http://localhost:8000/api/ingest/official-reports"
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
# Determine absolute path to data file
script_dir = os.path.dirname(os.path.abspath(__file__))
# Assumes script is in backend/scripts, so we go .. -> data
JSON_FILE_PATH = os.path.join(script_dir, "..", "data", "scrape_financial_reports.json")

def ingest_data(symbol, lang='en'):
    print(f"📂 Reading data from {JSON_FILE_PATH}...")
    
    if not os.path.exists(JSON_FILE_PATH):
        print(f"❌ File {JSON_FILE_PATH} not found. Please run the scraper first.")
        return

    if not INTERNAL_API_KEY:
        print("❌ INTERNAL_API_KEY is not set. Refusing to call ingest without a key.")
        return

    with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
        scraped_data = json.load(f)

    payload = {
        "symbol": str(symbol),
        "language": lang,
        "data": scraped_data
    }
    
    print(f"🚀 Sending data to API for Symbol: {symbol}...")
    
    try:
        response = requests.post(
            API_URL,
            json=payload,
            headers={"X-Internal-Key": INTERNAL_API_KEY},
        )
        response.raise_for_status()
        print("✅ Success! Server Response:")
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
        print("\n⏳ The server is now downloading files in the background. Check your S3 bucket/Database in a few seconds.")
        
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        if e.response is not None:
            print(f"Details: {e.response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("⚠️  Usage: python scripts/ingest_reports.py <COMPANY_SYMBOL>")
        print("Example: python scripts/ingest_reports.py 4322")
    else:
        symbol = sys.argv[1]
        ingest_data(symbol)