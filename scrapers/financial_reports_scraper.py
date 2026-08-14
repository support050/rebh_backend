# backend/scrapers/financial_reports_scraper.py
"""
Financial Reports Scraper.
Scrapes links to Financial Statements, XBRL, Board Reports, and ESG Reports from Saudi Exchange.
This extracts report LINKS (PDF, Excel, etc.) - not the financial data itself.
"""
import asyncio
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright

from .base_scraper import BaseScraper


class FinancialReportsScraper(BaseScraper):
    """
    Scraper for extracting financial report links from Saudi Exchange.
    Extracts: Financial Statements, XBRL, Board Reports, ESG Reports
    """
    
    def __init__(self, symbols: List[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.symbols = symbols or []
    
    async def scrape_report_links(self) -> Dict[str, Any]:
        """
        Scrape Financial Statements and Reports links.
        Returns categorized links (PDF, Excel, XBRL) for each report type.
        """
        reports_data = {}
        
        print("    → Preparing view for FINANCIAL STATEMENTS AND REPORTS...")
        
        try:
            # Try to click the tab
            clicked = await self.click_tab("FINANCIAL STATEMENTS AND REPORTS")
            if not clicked:
                clicked = await self.click_tab("Financial Statements")
            
            if not clicked:
                print("    → Failed to click 'FINANCIAL STATEMENTS AND REPORTS' tab.")
                return reports_data
            
            await self.page.wait_for_timeout(5000)

            # Check for table
            table_locator = self.page.locator(".tableStyle table")
            try:
                await table_locator.first.wait_for(state="visible", timeout=10000)
            except:
                pass

            if await table_locator.count() == 0:
                print("    → Main table (.tableStyle table) not found in DOM!")
                return reports_data
            
            print("    → Found main table. Parsing rows sequentially...")

            # Capture ALL links using JavaScript
            raw_data = await self.page.evaluate("""() => {
                const rows = Array.from(document.querySelectorAll('.tableStyle table tr'));
                const results = {};
                let currentSection = 'General Reports';
                
                rows.forEach(row => {
                    const text = row.innerText.trim();
                    
                    // Check if this row is a section header
                    if (text === 'Financial Statements' || text === 'XBRL' || text === 'Board Report' || text === 'ESG Report') {
                        currentSection = text;
                        results[currentSection] = [];
                        return; 
                    }
                    
                    // Also check for th elements as section headers
                    const th = row.querySelector('th');
                    if (th) {
                        const thText = th.innerText.trim();
                        if (['Financial Statements', 'XBRL', 'Board Report', 'ESG Report'].includes(thText)) {
                            currentSection = thText;
                            results[currentSection] = [];
                            return;
                        }
                    }
                    
                    if (!results[currentSection]) results[currentSection] = [];

                    // Extract all anchor links from the row
                    const anchors = Array.from(row.querySelectorAll('a'));
                    anchors.forEach(a => {
                        const href = a.href;
                        if (!href || href.includes('javascript') || href === '#') return;
                        
                        let context = '';
                        const firstCell = row.querySelector('td');
                        if (firstCell) context = firstCell.innerText.trim();
                        
                        results[currentSection].push({
                            url: href,
                            context: context,
                            text: a.innerText.trim()
                        });
                    });
                });
                return results;
            }""")
            
            # Process and categorize the links
            for section, items in raw_data.items():
                if not items:
                    continue
                clean_items = []
                for item in items:
                    url = item['url']
                    lower_url = url.lower()
                    
                    # Determine file type
                    if '.pdf' in lower_url:
                        file_type = 'pdf'
                    elif '.xls' in lower_url or '.xlsx' in lower_url:
                        file_type = 'excel'
                    elif 'xbrl' in lower_url:
                        file_type = 'xbrl'
                    else:
                        file_type = 'other'
                    
                    clean_items.append({
                        "url": url,
                        "file_type": file_type,
                        "context": item['context'],
                        "row_info": item['text']
                    })
                
                reports_data[section] = clean_items
                print(f"      → Found {len(clean_items)} items for {section}")

        except Exception as e:
            print(f"    → Error processing statements section: {e}")
            
        return reports_data

    async def scrape_company(self, symbol: str) -> Dict[str, Any]:
        """
        Scrape financial report links for a single company.
        
        Args:
            symbol: Company symbol
            
        Returns:
            Dictionary containing report links categorized by type
        """
        print(f"\n{'='*60}")
        print(f"Processing Report Links for: {symbol}")
        print(f"{'='*60}")
        
        company_reports = {
            "symbol": symbol,
            "report_links": {}
        }
        
        try:
            if not await self.navigate_to_company(symbol):
                company_reports["error"] = "Failed to navigate to company page"
                return company_reports
            
            print("  📊 Processing Financials Tab...")
            if await self.click_tab("Financials"):
                await self.page.wait_for_timeout(2000)
                company_reports["report_links"] = await self.scrape_report_links()
            else:
                print("  ❌ Could not find 'Financials' tab.")
                company_reports["error"] = "Financials tab not found"
                
        except Exception as e:
            print(f"  ❌ Error scraping reports for {symbol}: {e}")
            company_reports["error"] = str(e)
            
        return company_reports

    async def send_report_links_to_api(self, company_data: Dict[str, Any]) -> bool:
        """
        Send scraped report links to a dedicated API endpoint.
        Note: This requires a separate API endpoint for report links.
        """
        # This would require a separate endpoint like /api/scraper/report-links
        # For now, we just save to JSON
        return False

    async def scrape_all(self) -> Dict[str, Any]:
        """
        Scrape report links for all companies in the symbols list.
        
        Returns:
            Dictionary with summary of scraping results
        """
        if not self.symbols:
            raise ValueError("Symbols list is empty")
        
        print(f"\n{'='*60}")
        print(f"Starting Financial Reports Links Scraper")
        print(f"Companies to scrape: {len(self.symbols)}")
        print(f"{'='*60}")
        
        try:
            await self.init_browser()
            
            successful = 0
            failed = 0
            results = {}
            
            try:
                for i, symbol in enumerate(self.symbols, 1):
                    print(f"\n[{i}/{len(self.symbols)}] Processing {symbol}...")
                    
                    try:
                        reports_data = await self.scrape_company(symbol)
                        results[symbol] = reports_data
                        
                        if "error" not in reports_data:
                            successful += 1
                            # Save JSON
                            self.save_to_json(symbol, reports_data, "report_links")
                        else:
                            failed += 1
                        
                        # Delay between companies
                        await asyncio.sleep(3)
                        
                    except Exception as e:
                        failed += 1
                        print(f"  ❌ Error processing company {symbol}: {e}")
                        results[symbol] = {"symbol": symbol, "error": str(e)}
                
            finally:
                await self.close_browser()
        except Exception as e:
            print(f"Failed to run scraper: {e}")
            raise e
        
        print(f"\n{'='*60}")
        print(f"Report Links Scraping Complete!")
        print(f"{'='*60}")
        print(f"✅ Scraped Successfully: {successful}")
        print(f"❌ Failed: {failed}")
        print(f"{'='*60}")
        
        return {
            "summary": {
                "total": len(self.symbols),
                "successful": successful,
                "failed": failed
            },
            "results": results
        }


async def main():
    """Example usage."""
    # Scrape report links for a subset of companies
    test_symbols = ["4020", "2222"]  # Al Rajhi, Aramco
    
    scraper = FinancialReportsScraper(
        symbols=test_symbols,
        headless=False
    )
    
    results = await scraper.scrape_all()
    print(f"\nSummary: {results['summary']}")


if __name__ == "__main__":
    asyncio.run(main())
