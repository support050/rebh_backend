# backend/scrapers/base_scraper.py
"""
Base Scraper class with common functionality for all scrapers.
"""
import asyncio
import re
import json
import os
import httpx
import pandas as pd
from io import BytesIO
from typing import Dict, List, Any, Optional
from datetime import datetime
from playwright.async_api import async_playwright, Page, Locator, Browser, BrowserContext
from abc import ABC, abstractmethod


# --- Configuration ---
SAUDI_EXCHANGE_BASE_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/hidden/company-profile-main/"
    "!ut/p/z1/04_Sj9CPykssy0xPLMnMz0vMAfIjo8ziTR3NDIw8LAz83d2MXA0C3SydAl1c3Q0NvE30I4EKzBEKDMKcTQzMDPxN3H19LAzdTU31w8syU8v1wwkpK8hOMgUA-oskdg!!/"
)
TIMEOUT_MS = 120000

# API Configuration
API_BASE_URL = os.getenv("LUMIVST_API_URL", "http://localhost:8000")
# Prefer INTERNAL_API_KEY (X-Internal-Key); fall back to legacy LUMIVST_API_TOKEN
API_TOKEN = os.getenv("INTERNAL_API_KEY", "") or os.getenv("LUMIVST_API_TOKEN", "")

# Report type mapping
REPORT_TYPE_MAP = {
    "Balance Sheet": "Balance_Sheet",
    "Statement Of Income": "Income_Statement",
    "Statement of Income": "Income_Statement",
    "Cash Flows": "Cash_Flows"
}


class BaseScraper(ABC):
    """
    Base class for all Saudi Exchange scrapers.
    Provides common functionality for navigation, table parsing, and API integration.
    """
    
    def __init__(
        self, 
        headless: bool = False, 
        api_url: str = API_BASE_URL,
        api_token: str = API_TOKEN,
        save_json: bool = True,
        send_to_api: bool = True,
        upload_excel: bool = True
    ):
        self.headless = headless
        self.api_url = api_url
        self.api_token = api_token
        self.save_json = save_json
        self.send_to_api_enabled = send_to_api
        self.upload_excel_enabled = upload_excel
        
        self.http_client: Optional[httpx.AsyncClient] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        # Output directory
        self.output_dir = os.path.join(os.path.dirname(__file__), "..", "scraped_data")
        os.makedirs(self.output_dir, exist_ok=True)
    
    # ==================== HTTP Client Management ====================
    
    async def init_http_client(self):
        """Initialize HTTP client for API calls."""
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["X-Internal-Key"] = self.api_token
        self.http_client = httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=60.0
        )
    
    async def close_http_client(self):
        """Close HTTP client."""
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
    
    # ==================== Browser Management ====================
    
    async def init_browser(self):
        """Initialize Playwright browser."""
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-http2"]
        )
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (HTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 1000},
            locale="en-US"
        )
        self.page = await self.context.new_page()
    
    async def close_browser(self):
        """Close browser and cleanup."""
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.context = None
            self.page = None
    
    # ==================== Navigation Helpers ====================
    
    async def navigate_to_company(self, symbol: str) -> bool:
        """Navigate to a company's page on Saudi Exchange."""
        try:
            url = f"{SAUDI_EXCHANGE_BASE_URL}?companySymbol={symbol}"
            print(f"  📍 Navigating to {symbol}...")
            await self.page.goto(url, timeout=TIMEOUT_MS)
            await self.page.wait_for_load_state("domcontentloaded")
            await self.page.wait_for_timeout(3000)
            return True
        except Exception as e:
            print(f"  ❌ Navigation error for {symbol}: {e}")
            return False
    
    async def click_tab(self, tab_name: str) -> bool:
        """Click a tab by its text content using JavaScript."""
        return await self.page.evaluate("""(text) => {
            const target = text.toLowerCase();
            const tags = ['li', 'a', 'button', 'div', 'span', 'h2', 'h3', 'h4', 'h5'];
            let best = null;
            let bestScore = -9999;
            
            function getScore(el) {
               let score = 0;
               const txt = (el.innerText || '').toLowerCase().trim();
               if (!txt.includes(target)) return -10000;
               score -= txt.length; 
               const tag = el.tagName.toLowerCase();
               if (['li', 'a', 'button'].includes(tag)) score += 2000;
               else if (['div', 'span'].includes(tag)) score += 500;
               if (txt === target) score += 1000;
               if (el.offsetParent !== null) score += 100;
               return score;
            }
            
            const all = document.querySelectorAll(tags.join(','));
            for (const el of all) {
               if (el.offsetParent === null) continue;
               const s = getScore(el);
               if (s > bestScore) {
                   bestScore = s;
                   best = el;
               }
            }
            
            if (best && bestScore > -5000) {
                best.scrollIntoView();
                best.click();
                return true;
            }
            return false;
        }""", tab_name)
    
    async def click_display_previous_periods(self):
        """Click 'Display Previous Periods' button to show historical data."""
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self.page.wait_for_timeout(500)
        await self.page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('a, button, div, span'))
                .find(b => b.innerText && b.innerText.trim() === 'Display Previous Periods');
            if (el) { el.scrollIntoView(); el.click(); }
        }""")
        await self.page.evaluate("window.scrollTo(0, 0)")
    
    # ==================== Table Parsing ====================
    
    async def parse_html_table(self, table_locator: Locator) -> List[Dict[str, str]]:
        """Parse an HTML table into a list of dictionaries."""
        try:
            rows = table_locator.locator("tr")
            row_count = await rows.count()
            if row_count == 0:
                return []

            # Get headers
            headers = []
            header_row = rows.nth(0)
            th_cells = header_row.locator("th")
            th_count = await th_cells.count()
            
            raw_headers = []
            if th_count > 0:
                for i in range(th_count):
                    text = await th_cells.nth(i).inner_text()
                    raw_headers.append((text or f"col_{i}").strip())
            else:
                td_cells = header_row.locator("td")
                for i in range(await td_cells.count()):
                    raw_headers.append(f"col_{i}")
            
            # Make unique
            headers = []
            seen = {}
            for h in raw_headers:
                if h in seen:
                    seen[h] += 1
                    headers.append(f"{h}__{seen[h]}")
                else:
                    seen[h] = 0
                    headers.append(h)

            # Get data rows
            data = []
            start_row = 1 if th_count > 0 else 0
            
            for i in range(start_row, row_count):
                row = rows.nth(i)
                cells = row.locator("td, th")
                cell_count = await cells.count()
                row_dict = {}
                for j in range(cell_count):
                    key = headers[j] if j < len(headers) else f"col_{j}"
                    val = await cells.nth(j).inner_text()
                    row_dict[key] = (val or "").strip()
                if row_dict:
                    data.append(row_dict)
            return data
        except Exception:
            return []
    
    async def get_visible_tables(self) -> List[List[Dict[str, str]]]:
        """Get content of all visible tables on the page."""
        extracted = []
        tables = await self.page.locator("table").all()
        for table in tables:
            if await table.is_visible():
                data = await self.parse_html_table(table)
                if data:
                    extracted.append(data)
        return extracted
    
    async def table_has_history(self) -> bool:
        """Check if current tables contain historical year headers."""
        tables = await self.get_visible_tables()
        for tbl in tables:
            if not tbl:
                continue
            headers = " ".join(list(tbl[0].keys())).lower()
            if any(y in headers for y in ["2021", "2020", "2019", "2018"]):
                return True
        return False
    
    async def extract_all_tables_with_visibility(self) -> List[Dict[str, Any]]:
        """Extract all tables with their visibility status."""
        extracted_tables = []
        try:
            tables = await self.page.locator("table").all()
            for table in tables:
                is_vis = await table.is_visible()
                table_data = await self.parse_html_table(table)
                if table_data:
                    extracted_tables.append({
                        "content": table_data,
                        "visible": is_vis
                    })
        except Exception as e:
            print(f"  ❌ Error extracting tables: {e}")
        return extracted_tables
    
    def slice_mixed_table(self, table: List[Dict[str, str]], target_section: str) -> List[Dict[str, str]]:
        """
        Slice a combined table to extract a specific section.
        Used when Balance Sheet, Income Statement, and Cash Flows are in one table.
        """
        if not table:
            return []
        
        idx_income = -1
        idx_cash = -1
        
        header_key = list(table[0].keys())[0]
        for i, row in enumerate(table):
            val = row.get(header_key, "").strip()
            if "Statement Of Income" in val or "Statement of Income" in val:
                idx_income = i
            elif "Cash Flows" in val and len(val) < 40:
                idx_cash = i
        
        if idx_income == -1 and idx_cash == -1:
            if target_section == "Balance Sheet":
                return table
            return []

        start_idx = 0
        end_idx = len(table)
        
        if target_section == "Balance Sheet":
            start_idx = 0
            if idx_income != -1:
                end_idx = idx_income
            elif idx_cash != -1:
                end_idx = idx_cash
        elif target_section == "Statement Of Income":
            if idx_income == -1:
                return []
            start_idx = idx_income + 1
            if idx_cash != -1:
                end_idx = idx_cash
        elif target_section == "Cash Flows":
            if idx_cash == -1:
                return []
            start_idx = idx_cash + 1
            end_idx = len(table)
        
        if start_idx >= end_idx:
            return []
        
        return table[start_idx:end_idx]
    
    # ==================== API Integration ====================
    
    async def send_to_api(self, company_data: Dict[str, Any]) -> bool:
        """Send scraped data to the FastAPI ingest endpoint."""
        if not self.send_to_api_enabled or not self.http_client:
            return False
        
        try:
            ingest_payload = self.transform_to_ingest_format(company_data)
            
            if not ingest_payload.get("reports"):
                print(f"    ⚠️ No reports to send for {company_data.get('symbol')}")
                return False
            
            response = await self.http_client.post(
                "/api/scraper/ingest",
                json=ingest_payload
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"    ✅ API: {result.get('message', 'Success')}")
                return True
            else:
                print(f"    ❌ API Error {response.status_code}: {response.text}")
                return False
                
        except httpx.RequestError as e:
            print(f"    ❌ HTTP Error: {e}")
            return False
        except Exception as e:
            print(f"    ❌ Error sending to API: {e}")
            return False
    
    def transform_to_ingest_format(self, company_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform scraped data to the format expected by the ingest API."""
        symbol = company_data.get("symbol", "")
        financial_info = company_data.get("financial_information", {}) or company_data.get("history_information", {})
        
        reports = []
        
        for key, table_data_list in financial_info.items():
            if not table_data_list or not table_data_list[0]:
                continue
            
            table_data = table_data_list[0]
            
            # Determine period type and report name
            period_type = None
            report_name = None
            
            if key.startswith("Annually_"):
                period_type = "Annually"
                report_name = key[9:].replace("_", " ")
            elif key.startswith("Quarterly_"):
                period_type = "Quarterly"
                report_name = key[10:].replace("_", " ")
            else:
                # Try suffix format (Balance_Sheet_Annually)
                parts = key.rsplit("_", 1)
                if len(parts) == 2 and parts[1] in ["Annually", "Quarterly"]:
                    report_name = parts[0].replace("_", " ")
                    period_type = parts[1]
            
            if not period_type or not report_name:
                continue

            report_type = REPORT_TYPE_MAP.get(report_name)
            if not report_type:
                continue
            
            if not table_data:
                continue
            
            headers = list(table_data[0].keys())
            date_columns = [h for h in headers if any(year in h for year in ["2019", "2020", "2021", "2022", "2023", "2024", "2025"])]
            
            for date_col in date_columns:
                period_end_date = self.parse_date_column(date_col)
                if not period_end_date:
                    continue
                
                metrics = {}
                for row in table_data:
                    metric_name = row.get(headers[0], "").strip()
                    if not metric_name:
                        continue
                    value = row.get(date_col, "")
                    if value:
                        metrics[metric_name] = value
                
                if metrics:
                    reports.append({
                        "report_type": report_type,
                        "period_type": period_type,
                        "period_end_date": period_end_date,
                        "metrics": metrics,
                        "raw_data": table_data
                    })
        
        return {
            "company_symbol": symbol,
            "reports": reports
        }
    
    def parse_date_column(self, column_header: str) -> Optional[str]:
        """Parse date from column header like '31 Dec 2023' or '2023-12-31'."""
        try:
            # Remove suffix (e.g., 2025-09-30__1 -> 2025-09-30)
            text = column_header.split("__")[0].strip()
            
            # 1. ISO Format YYYY-MM-DD
            iso_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
            if iso_match:
                return iso_match.group(0)
            
            # 2. Month map
            month_map = {
                "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
                "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
                "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"
            }
            
            for month_name in month_map.keys():
                if month_name in text:
                    parts = text.split()
                    if len(parts) >= 3:
                        day = parts[0].zfill(2)
                        month = month_map.get(parts[1], "12")
                        year = parts[2]
                        return f"{year}-{month}-{day}"
            
            # 3. Year only (Last resort for Annually)
            for year in ["2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"]:
                if year in text:
                    return f"{year}-12-31"
            
            return None
        except Exception:
            return None
    
    # ==================== Excel Export ====================
    
    async def export_to_excel(self, company_data: Dict[str, Any]) -> Optional[bytes]:
        """Export scraped data to Excel format in memory."""
        try:
            symbol = company_data.get("symbol", "")
            financial_info = company_data.get("financial_information", {}) or company_data.get("history_information", {})
            
            if not financial_info:
                return None
            
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                for key, table_data_list in financial_info.items():
                    if not table_data_list or not table_data_list[0]:
                        continue
                    table_data = table_data_list[0]
                    df = pd.DataFrame(table_data)
                    sheet_name = key[:31]
                    df.to_excel(writer, sheet_name=sheet_name, index=False)
            
            output.seek(0)
            return output.getvalue()
            
        except Exception as e:
            print(f"    ❌ Error creating Excel: {e}")
            return None
    
    async def upload_excel(self, symbol: str, excel_bytes: bytes) -> bool:
        """Upload Excel file to the API."""
        if not self.upload_excel_enabled:
            return False
        
        try:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"{symbol}_financials_{timestamp}.xlsx"
            
            files = {
                "file": (filename, excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            }
            data = {
                "company_symbol": symbol,
                "description": f"Financial data scraped on {datetime.utcnow().isoformat()}"
            }
            
            headers = {}
            if self.api_token:
                headers["X-Internal-Key"] = self.api_token
            
            async with httpx.AsyncClient(base_url=self.api_url, headers=headers, timeout=60.0) as client:
                response = await client.post(
                    "/api/scraper/upload-excel",
                    files=files,
                    data=data
                )
            
            if response.status_code == 200:
                print(f"    ✅ Excel uploaded: {filename}")
                return True
            else:
                print(f"    ❌ Excel upload failed: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"    ❌ Error uploading Excel: {e}")
            return False
    
    # ==================== JSON Save ====================
    
    def save_to_json(self, symbol: str, data: Dict[str, Any], subfolder: str = ""):
        """Save scraped data to JSON file."""
        if not self.save_json:
            return
        
        output_path = os.path.join(self.output_dir, subfolder) if subfolder else self.output_dir
        os.makedirs(output_path, exist_ok=True)
        
        filepath = os.path.join(output_path, f"{symbol}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print(f"    📁 JSON saved: {filepath}")
    
    # ==================== Abstract Methods ====================
    
    @abstractmethod
    async def scrape_company(self, symbol: str) -> Dict[str, Any]:
        """Scrape financial data for a single company. Must be implemented by subclasses."""
        pass
    
    @abstractmethod
    async def scrape_all(self) -> None:
        """Scrape all companies. Must be implemented by subclasses."""
        pass
