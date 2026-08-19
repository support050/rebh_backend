import json
import os
import sys
from pathlib import Path
import boto3
from botocore.config import Config
from tqdm import tqdm

from dotenv import load_dotenv

# Add backend directory to path
backend_dir = Path(__file__).parent.parent
sys.path.append(str(backend_dir))
load_dotenv(backend_dir / ".env")

# Configuration
OUTPUT_DIR = backend_dir / "output"
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "lumivst-xbrl")

def get_r2_client():
    if not R2_ACCOUNT_ID or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
        print("❌ Missing R2 credentials in environment variables.")
        sys.exit(1)
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
    )

def main():
    client = get_r2_client()
    json_files = sorted(OUTPUT_DIR.glob("*_financials.json"))
    
    if not json_files:
        print("❌ No financials JSON files found in output directory.")
        return

    print(f"🔄 Scanning {len(json_files)} files and building index...")
    companies_list = []
    
    # 1. Parse metadata and prepare uploads
    for fp in json_files:
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("meta", {})
            all_periods = set()
            for sec in data.get("sections", {}).values():
                all_periods.update(sec.get("periods", []))
            
            companies_list.append({
                "symbol": meta.get("symbol", fp.stem.split("_")[0]),
                "company_name": meta.get("company_name", "Unknown"),
                "sector": meta.get("sector"),
                "report_end": meta.get("report_end"),
                "periods_count": len(all_periods)
            })
        except Exception as e:
            print(f"⚠️ Error reading metadata from {fp.name}: {e}")

    # Write temporary index locally
    index_path = OUTPUT_DIR / "companies_list.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(companies_list, f, ensure_ascii=False, indent=2)

    # 2. Upload index file
    print(f"☁️ Uploading index 'companies_list.json' to R2 bucket '{R2_BUCKET_NAME}'...")
    try:
        client.upload_file(
            Filename=str(index_path),
            Bucket=R2_BUCKET_NAME,
            Key="companies_list.json",
            ExtraArgs={"ContentType": "application/json"}
        )
        print("✅ Index uploaded successfully.")
    except Exception as e:
        print(f"❌ Failed to upload index: {e}")
        return

    # 3. Upload financials JSON files
    print(f"🚀 Uploading {len(json_files)} financials files to R2...")
    for fp in tqdm(json_files):
        key = f"xbrl/{fp.name}"
        try:
            client.upload_file(
                Filename=str(fp),
                Bucket=R2_BUCKET_NAME,
                Key=key,
                ExtraArgs={"ContentType": "application/json"}
            )
        except Exception as e:
            print(f"\n⚠️ Error uploading {fp.name} to R2: {e}")
            
    print("\n🎉 Upload complete! All files have been successfully synced to Cloudflare R2.")

if __name__ == "__main__":
    main()
