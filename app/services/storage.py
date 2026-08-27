import boto3
import httpx
import os
import io
import asyncio
from botocore.exceptions import ClientError

class S3Storage:
    def __init__(self):
        # Ensure .env is loaded before reading S3 config
        from pathlib import Path
        _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        if _env_path.exists():
            from dotenv import load_dotenv
            load_dotenv(_env_path, override=False)

        self.endpoint_url = os.getenv("S3_ENDPOINT")
        self.access_key = os.getenv("S3_ACCESS_KEY")
        self.secret_key = os.getenv("S3_SECRET_KEY")
        self.bucket_name = os.getenv("S3_BUCKET_NAME")
        self.region_name = os.getenv("S3_REGION", "auto")
        self.public_domain = os.getenv("S3_PUBLIC_DOMAIN")
        
        # Log warning if config missing (but don't crash app startup)
        if not (self.endpoint_url and self.access_key and self.secret_key and self.bucket_name):
             print("[INFO] S3 Configuration missing. Storage service will not work.")
        
        try:
            self.s3_client = boto3.client(
                's3',
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region_name
            )
        except Exception as e:
            print(f"❌ Failed to init S3 Client: {e}")
            self.s3_client = None

    async def upload_file_from_url(self, source_url: str, destination_path: str) -> str:
        """
        Downloads file from source_url and uploads to S3 bucket.
        Returns the public URL of the uploaded file.
        """
        if not self.s3_client:
            raise Exception("S3 Client not initialized")

        # 1. Download File (Async)
        async with httpx.AsyncClient(verify=False) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.saudiexchange.sa/wps/portal/saudiexchange/hidden/company-profile-main/",
                "Origin": "https://www.saudiexchange.sa",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            }
            try:
                resp = await client.get(source_url, headers=headers, follow_redirects=True, timeout=60.0)
                if resp.status_code != 200:
                    raise Exception(f"Failed to download from source {source_url}: Status {resp.status_code}")
                file_content = resp.content
                content_type = resp.headers.get('content-type', 'application/octet-stream')
            except Exception as e:
                raise Exception(f"Download Error: {str(e)}")

        # 2. Upload to S3 (Sync running in thread)
        def _upload():
            self.s3_client.upload_fileobj(
                io.BytesIO(file_content),
                self.bucket_name,
                destination_path,
                ExtraArgs={'ContentType': content_type}
            )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _upload)

        # 3. Return Public URL
        if self.public_domain:
            return f"{self.public_domain.rstrip('/')}/{destination_path}"
        
        # Fallback to endpoint construction (might not be publicly accessible directly)
        # Fallback to endpoint construction (might not be publicly accessible directly)
        return f"{self.endpoint_url}/{self.bucket_name}/{destination_path}"

    async def upload_local_file(self, local_path: str, destination_path: str, content_type: str = None) -> str:
        """
        Uploads a local file to S3 bucket.
        Returns the public URL of the uploaded file.
        """
        if not self.s3_client:
            raise Exception("S3 Client not initialized")
            
        print(f"      ⏳ Starting upload for {local_path} -> {destination_path}")
        
        def _upload():
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    if attempt > 0: print(f"      🔄 Retry {attempt+1}/{max_retries}...")
                    
                    with open(local_path, 'rb') as f:
                         args = {}
                         if content_type:
                             args['ContentType'] = content_type
                         self.s3_client.upload_fileobj(f, self.bucket_name, destination_path, ExtraArgs=args)
                    print(f"      ✅ S3 Upload finished internal.")
                    return # Success
                except Exception as e:
                    print(f"      ❌ S3 Upload Error (Attempt {attempt+1}): {e}")
                    if attempt == max_retries - 1:
                        raise e
                    import time
                    time.sleep(2) # Wait a bit before retry
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _upload)
        
        if self.public_domain:
            return f"{self.public_domain.rstrip('/')}/{destination_path}"
        return f"{self.endpoint_url}/{self.bucket_name}/{destination_path}"

# Global instance
storage_service = S3Storage()
