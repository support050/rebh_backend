import asyncio
import json
import os
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Any
from playwright.async_api import async_playwright, Page, Locator

# Auto-load .env from backend root so INTERNAL_API_KEY is available
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=False)

# --- Configuration ---
TIMEOUT_MS = 120000

# --- Language Configuration ---
LANG_CONFIG = {
    'en': {
        'locale': 'en-US',
        'tab_name': 'Financials',
        'section_header': 'FINANCIAL STATEMENTS AND REPORTS',
        'section_header_fallback': 'Financial Statements',
        'valid_sections': ['Financial Statements', 'XBRL', 'Board Report', 'ESG Report'],
        'valid_periods': ['Annual', 'Q1', 'Q2', 'Q3', 'Q4'],
        'default_section': 'General Reports',
    },
    'ar': {
        'locale': 'ar-SA',
        'tab_name': 'البيانات المالية',
        'section_header': 'القوائم المالية والتقارير',
        'section_header_fallback': 'القوائم المالية',
        'valid_sections': ['القوائم المالية', 'لغة التقارير المرنة', 'تقرير مجلس الإدارة', 'تقرير الممارسات البيئية والإجتماعية وحوكمة الشركات', 'تقرير الممارسات البيئية والاجتماعية وحوكمة الشركات'],
        'valid_periods': ['سنوي', 'الربع الأول', 'الربع الثاني', 'الربع الثالث', 'الربع الرابع'],
        'default_section': 'تقارير عامة',
    }
}

# Mapping Arabic section names to English (for storage consistency)
AR_SECTION_TO_EN = {
    'القوائم المالية': 'Financial Statements',
    'لغة التقارير المرنة': 'XBRL',
    'تقرير مجلس الإدارة': 'Board Report',
    'تقرير الممارسات البيئية والإجتماعية وحوكمة الشركات': 'ESG Report',
    'تقرير الممارسات البيئية والاجتماعية وحوكمة الشركات': 'ESG Report',
}

# Mapping Arabic periods to English (for storage consistency)
AR_PERIOD_TO_EN = {
    'سنوي': 'Annual',
    'الربع الأول': 'Q1',
    'الربع الثاني': 'Q2',
    'الربع الثالث': 'Q3',
    'الربع الرابع': 'Q4',
}

class FinancialReportsScraper:
    def __init__(self, symbol: str, headless: bool = False, lang: str = 'en'):
        self.symbol = symbol
        self.headless = headless
        self.lang = lang
        self.lang_config = LANG_CONFIG[lang]
        
        # Both EN and AR use the same base URL — language is controlled by browser locale (en-US / ar-SA)
        BASE_URL = "https://www.saudiexchange.sa/wps/portal/saudiexchange/hidden/company-profile-main/"
        self.base_url = f"{BASE_URL}?companySymbol={symbol}"
        
        # Download directory - separate by language
        script_dir = os.path.dirname(os.path.abspath(__file__))
        lang_suffix = f"_{lang}" if lang != 'en' else ""
        self.download_base_dir = os.path.join(script_dir, "..", "data", "downloads", f"{symbol}{lang_suffix}")
        os.makedirs(self.download_base_dir, exist_ok=True)
        
        self.context = None

    async def scrape(self) -> Dict[str, Any]:
        """Main method to scrape only Financial Statements and Reports."""
        reports_data = {}
        
        async with async_playwright() as p:
            # Launch browser
            browser = await p.chromium.launch(headless=self.headless, args=["--disable-http2"])
            self.context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (HTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1600, "height": 1000},
                locale=self.lang_config['locale'],
                accept_downloads=True
            )
            page = await self.context.new_page()
            
            try:
                print(f"Language: {self.lang} ({self.lang_config['locale']})")
                print("Using homepage search as the primary navigation path...")

                home_url = "https://www.saudiexchange.sa/wps/portal/saudiexchange/home/"
                tab_name = self.lang_config['tab_name']
                print(f"Processing '{tab_name}' Tab...")

                # Open the homepage and use the site search flow directly.
                for attempt in range(3):
                    try:
                        await page.goto(home_url, timeout=TIMEOUT_MS)
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(2000)
                        break
                    except Exception as nav_e:
                        print(f"  -> Homepage navigation attempt {attempt+1} failed: {nav_e}")
                        if attempt == 2:
                            print("  -> Max retries reached. Returning empty data.")
                            await browser.close()
                            return reports_data
                        await asyncio.sleep(5)

                # Try to find and fill the search input
                await page.evaluate(f"""(sym) => {{
                    const inputs = document.querySelectorAll('input[type="text"], input[type="search"]');
                    for(let i of inputs) {{
                        if((i.placeholder && i.placeholder.toLowerCase().includes('search')) || 
                           (i.id && i.id.toLowerCase().includes('search')) ||
                           (i.placeholder && i.placeholder.includes('بحث'))) {{
                            i.value = sym;
                            i.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            i.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            i.focus();
                            break;
                        }}
                    }}
                }}""", self.symbol)

                await page.keyboard.press("Enter")
                await page.wait_for_timeout(4000)

                # Try to click the company link in the search results
                await page.evaluate(f"""(sym) => {{
                    const links = document.querySelectorAll('a');
                    for(let l of links) {{
                        const txt = l.innerText.trim();
                        if(txt.includes(sym) || (l.href && l.href.includes(sym))) {{
                            if(txt.length < 50) {{
                                l.click();
                                return;
                            }}
                        }}
                    }}
                }}""", self.symbol)

                await page.wait_for_timeout(5000)
                await page.wait_for_load_state("domcontentloaded")

                tab_found = await self._click_tab(page, tab_name)

                if tab_found:
                    await page.wait_for_timeout(3000) 
                    
                    # Target Section
                    section_header = self.lang_config['section_header']
                    print(f"\n--- Scraping '{section_header}' ---")
                    reports_data = await self._scrape_statements_and_reports(page)
                    
                else:
                    print(f"Could not find '{tab_name}' tab even after search fallback.")
                    # DEBUG: Print all potential tab candidates
                    print("  -> DEBUG: Listing all potential tabs/links on page:")
                    try:
                        candidates = await page.evaluate(r"""() => {
                            const els = document.querySelectorAll('ul.nav li, .tab, h2, h3, a');
                            return Array.from(els).map(e => e.innerText.trim()).filter(t => t.length > 0 && t.length < 50);
                        }""")
                        print(f"  -> Found candidates: {candidates[:20]}...")
                    except Exception as e:
                        print(f"  -> Debug extract failed: {e}")
            
            except Exception as e:
                print(f"An error occurred during scraping: {e}")
            finally:
                await browser.close()
                
        return reports_data

    # --- Helper methods ---

    async def _switch_to_arabic(self, page: Page) -> bool:
        """Switch the Saudi Exchange website to Arabic."""
        try:
            # Try clicking the Arabic language link/button
            result = await page.evaluate("""() => {
                // Look for Arabic language switcher
                const links = document.querySelectorAll('a, button, span');
                for (const el of links) {
                    const text = (el.innerText || '').trim();
                    const href = (el.href || '').toLowerCase();
                    // Common patterns for Arabic switch
                    if (text === 'العربية' || text === 'عربي' || text === 'AR' || 
                        href.includes('/ar/') || href.includes('locale=ar')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            if result:
                await page.wait_for_load_state("domcontentloaded")
                return True
            return False
        except Exception as e:
            print(f"  -> Error switching language: {e}")
            return False

    async def _click_tab(self, page: Page, tab_name: str) -> bool:
        """Helper to find and click a tab."""
        return await self._js_click_tab(page, tab_name)

    async def _js_click_tab(self, page: Page, text: str) -> bool:
        """Robust JS click using scoring strategy."""
        return await page.evaluate(f"""(text) => {{
            const target = text.toLowerCase();
            const tags = ['li', 'a', 'button', 'div', 'span', 'h2', 'h3', 'h4', 'h5'];
            let best = null;
            let bestScore = -9999;
            
            function getScore(el) {{
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
            }}
            
            const all = document.querySelectorAll(tags.join(','));
            for (const el of all) {{
               if (el.offsetParent === null) continue;
               const s = getScore(el);
               if (s > bestScore) {{
                   bestScore = s;
                   best = el;
               }}
            }}
            
            if (best && bestScore > -5000) {{
                best.scrollIntoView();
                best.click();
                return true;
            }}
            return false;
        }}""", text)

    async def _download_file(self, url: str, section: str, year: str, period: str, f_type: str) -> str:
        """
        Downloads a file by injecting a click on the home page context.
        This ensures correct Cookies/Origin and forces 'download' attribute to bypass PDF viewer.
        """
        try:
            if not url: return None
            
            # Construct filename
            ext = 'pdf'
            low_url = url.lower()
            if f_type == 'excel' or '.xls' in low_url: 
                ext = 'xlsx' if '.xlsx' in low_url else 'xls'
            elif '.pdf' in low_url: 
                ext = 'pdf'
            
            # Normalize section/period to English for consistent filenames
            if self.lang == 'ar':
                section = AR_SECTION_TO_EN.get(section, section)
                period = AR_PERIOD_TO_EN.get(period, period)
            
            safe_period = period.replace(" ", "_").replace("/", "-")
            safe_section = section.replace(" ", "_").replace("/", "-")
            filename = f"{year}_{safe_section}_{safe_period}.{ext}"
            
            file_path = os.path.join(self.download_base_dir, filename)
            
            if os.path.exists(file_path) and os.path.getsize(file_path) > 1000:
                 return file_path
            
            print(f"      ⬇️ Downloading {filename}...")
            
            # Retry download logic
            for attempt in range(2):
                page = await self.context.new_page()
                try:
                    # 1. Go to the domain root to establish context (Cookies/Origin)
                    await page.goto("https://www.saudiexchange.sa/wps/portal/saudiexchange/home", 
                                  wait_until="domcontentloaded", 
                                  timeout=90000)
                    
                    # 2. Setup Listener
                    async with page.expect_download(timeout=120000) as download_info:
                        # 3. Inject JS to force download
                        await page.evaluate(f"""(url) => {{
                            const a = document.createElement('a');
                            a.href = url;
                            a.setAttribute('download', 'file'); 
                            a.target = '_self'; 
                            document.body.appendChild(a);
                            a.click();
                        }}""", url)

                    download = await download_info.value
                    await download.save_as(file_path)
                    
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 100:
                        return file_path
                    else:
                        print(f"      ⚠️ Attempt {attempt+1}: File too small or missing.")
                        
                except Exception as e:
                    print(f"      ⚠️ Attempt {attempt+1} failed: {e}")
                    if attempt == 1: # Last attempt
                        print(f"      ❌ Final Download Failure for {filename}")
                        return None
                    await asyncio.sleep(2) # Wait before retry
                finally:
                    await page.close()
            
            return None

        except Exception as e:
            print(f"      ❌ General download error: {e}")
            return None

    async def _scrape_statements_and_reports(self, page: Page) -> Dict[str, Any]:
        """
        Scrapes 'FINANCIAL STATEMENTS AND REPORTS'.
        """
        reports_data = {}
        
        section_header = self.lang_config['section_header']
        section_fallback = self.lang_config['section_header_fallback']
        valid_sections = self.lang_config['valid_sections']
        valid_periods = self.lang_config['valid_periods']
        default_section = self.lang_config['default_section']
        
        print(f"\n  -> Preparing view for '{section_header}'...")
        
        try:
            # Try specific text
            clicked = await self._js_click_tab(page, section_header)
            if not clicked:
                 clicked = await self._js_click_tab(page, section_fallback)
            
            if not clicked:
                print(f"  -> Failed to click '{section_header}' tab.")
                return reports_data
            
            await page.wait_for_timeout(5000)

            # Locate the main table
            table_locator = page.locator(".tableStyle table")
            try:
                await table_locator.first.wait_for(state="visible", timeout=10000)
            except: pass

            if await table_locator.count() == 0:
                print("  -> Main table (.tableStyle table) not found in DOM!")
                return reports_data
            
            print("  -> Found main table. Parsing rows sequentially...")

            # Pass config to JS for parsing
            valid_sections_json = json.dumps(valid_sections, ensure_ascii=False)
            valid_periods_json = json.dumps(valid_periods, ensure_ascii=False)

            # Capture ALL links
            raw_data = await page.evaluate(f"""() => {{
                const rows = Array.from(document.querySelectorAll('.tableStyle table tr'));
                const results = {{}};
                let currentSection = '{default_section}';
                let columnYears = {{}}; 
                
                const validSections = {valid_sections_json};
                const validPeriods = {valid_periods_json};
                
                rows.forEach((row, rowIndex) => {{
                    const text = row.innerText.trim();
                    const firstCell = row.querySelector('td');
                    let rowLabel = ''; 
                    if (firstCell) rowLabel = firstCell.innerText.trim();

                    // --- 1. Detect Section Headers ---
                    if (validSections.includes(text) || validSections.includes(rowLabel)) {{
                         const sectionName = validSections.includes(text) ? text : rowLabel;
                         currentSection = sectionName;
                         if (!results[currentSection]) results[currentSection] = [];
                         if (text === sectionName) return; 
                    }} 
                    else if (validPeriods.includes(rowLabel)) {{}}
                    else if (rowLabel !== '') {{
                        const potentialYears = Array.from(row.querySelectorAll('th, td')).map(c => c.innerText.trim());
                        const hasYears = potentialYears.some(t => /^\\d{{4}}$/.test(t));
                        if (!hasYears) return; 
                    }}

                    // --- 2. Detect Years ---
                    const ths = Array.from(row.querySelectorAll('th, td'));
                    const potentialYears = ths.map(cell => cell.innerText.trim());
                    const hasYears = potentialYears.some(t => /^\\d{{4}}$/.test(t));
                    
                    if (hasYears) {{
                        columnYears = {{}};
                        ths.forEach((cell, index) => {{
                            const txt = cell.innerText.trim();
                            if (/^\\d{{4}}$/.test(txt)) {{
                                columnYears[index] = txt;
                            }}
                        }});
                        return; 
                    }}

                    if (!results[currentSection]) results[currentSection] = [];

                    // --- 3. Process Cells ---
                    const cells = Array.from(row.querySelectorAll('td'));
                    cells.forEach((cell, colIndex) => {{
                         let year = columnYears[colIndex] || "Unknown";
                         let finalPeriod = rowLabel || "Annual";

                         const anchors = Array.from(cell.querySelectorAll('a'));
                         
                         if (anchors.length > 0) {{
                             anchors.forEach(a => {{
                                 const href = a.href;
                                 if (!href || href.includes('javascript') || href === '#') return;
                                 results[currentSection].push({{
                                     url: href,
                                     row_label: finalPeriod,
                                     year: year,          
                                     text: a.innerText.trim() 
                                 }});
                             }});
                         }} else {{
                             if (year !== "Unknown" && colIndex > 0) {{
                                 const cellText = cell.innerText.trim();
                                 results[currentSection].push({{
                                     url: null,
                                     row_label: finalPeriod,
                                     year: year,
                                     text: cellText || "-"
                                 }});
                             }}
                         }}
                    }});
                }});
                return results;
            }}""")
            
            for section, items in raw_data.items():
                if not items: continue
                
                # Normalize section name to English for consistent storage
                en_section = section
                if self.lang == 'ar':
                    en_section = AR_SECTION_TO_EN.get(section, section)
                
                clean_items = []
                print(f"      -> Processing {len(items)} items for {section} (-> {en_section})...")
                
                for item in items:
                    url = item.get('url') 
                    lower_url = (url or "").lower()
                    
                    if en_section == "XBRL":
                        if url and not (".xls" in lower_url): continue
                            
                    f_type = 'none'
                    if url:
                        if '.pdf' in lower_url: f_type = 'pdf'
                        elif '.xls' in lower_url: f_type = 'excel'
                        else: f_type = 'other'
                    
                    period = item.get('row_label', '')
                    year = item.get('year', '')
                    
                    # Normalize period to English for consistent storage
                    en_period = period
                    if self.lang == 'ar':
                        en_period = AR_PERIOD_TO_EN.get(period, period)
                    
                    # --- DOWNLOAD FILE ---
                    local_path = None
                    if url:
                        local_path = await self._download_file(url, section, year, period, f_type)
                    # ---------------------

                    clean_items.append({
                        "url": url,
                        "local_path": local_path,
                        "file_type": f_type,
                        "period": en_period,  # Always store English period
                        "year": year,
                        "published_date": item.get('text', '')
                    })
                
                # Use English section name as key for consistency
                reports_data[en_section] = clean_items
                print(f"      -> Saved {len(clean_items)} items for {en_section}")

        except Exception as e:
            print(f"  -> Error processing statements section: {e}")
            
        return reports_data

async def main():
    # Setup argument parser to accept multiple symbols
    parser = argparse.ArgumentParser(description="Scrape, Generate JSON, and Ingest Financial Reports.")
    parser.add_argument('symbols', nargs='*', help="List of company symbols to process (e.g. 2250 4322)")
    parser.add_argument('--symbol', help="Single symbol (legacy support)")
    parser.add_argument('--lang', default='en', choices=['en', 'ar'], help="Language: 'en' for English, 'ar' for Arabic (default: en)")
    
    args = parser.parse_args()
    
    # Determine list of symbols
    raw_symbols = []
    if args.symbols:
        raw_symbols.extend(args.symbols)
    if args.symbol:
        raw_symbols.append(args.symbol)

    # Debug print to verify input symbols
    print(f"DEBUG: raw_symbols before processing: {raw_symbols}")

    if not raw_symbols:
        print("❌ Error: No symbol(s) provided. Please specify at least one symbol using --symbol or positional argument.")
        return

    # Process symbols to handle commas (e.g., "2284,2200")
    target_symbols = []
    invalid_symbols = []
    for item in raw_symbols:
        # Split by comma if present, strip whitespace
        cleaned = [s.strip() for s in item.split(',') if s.strip()]
        for sym in cleaned:
            if sym.isdigit():
                target_symbols.append(sym)
            else:
                invalid_symbols.append(sym)
    if invalid_symbols:
        print(f"⚠️ Warning: Ignored invalid symbols: {invalid_symbols}")

    lang = args.lang

    # Import helper scripts dynamically
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        scripts_dir = os.path.abspath(os.path.join(current_dir, "..", "scripts"))
        if scripts_dir not in sys.path:
            sys.path.append(scripts_dir)
            
        from generate_json_from_local import generate_json_from_local
        from ingest_reports import ingest_data
    except ImportError as e:
        print(f"❌ Error importing helper scripts: {e}")
        print("Make sure 'generate_json_from_local.py' and 'ingest_reports.py' exist in backend/scripts/")
        return

    lang_label = "العربية" if lang == 'ar' else "English"
    print(f"🚀 Starting automation for {len(target_symbols)} symbols: {target_symbols} (Language: {lang_label})")

    for idx, sym in enumerate(target_symbols):
        print(f"\n{'='*50}")
        print(f"🔄 [{idx+1}/{len(target_symbols)}] Processing Symbol: {sym} ({lang_label})")
        print(f"{'='*50}")
        
        # --- Step 1: Scrape & Download ---
        print(f"📡 1. Starting Scraper for {sym} ({lang_label})...")
        scraper = FinancialReportsScraper(symbol=str(sym), headless=False, lang=lang)
        await scraper.scrape()
        
        # --- Step 2: Generate JSON from Local Files ---
        print(f"\n📝 2. Generating JSON from downloaded files for {sym}...")
        try:
            lang_suffix = f"_{lang}" if lang != 'en' else ""
            generate_json_from_local(str(sym), folder_suffix=lang_suffix)
        except Exception as e:
            print(f"❌ Failed to generate JSON for {sym}: {e}")
            continue
            
        # --- Step 3: Ingest Reports ---
        print(f"\n📨 3. Ingesting data for {sym} ({lang_label})...")
        try:
            ingest_data(str(sym), lang)
        except Exception as e:
            print(f"❌ Failed to ingest data for {sym}: {e}")
            
        # --- Step 4: Wait ---
        if idx < len(target_symbols) - 1:
            print(f"\n⏳ Waiting 5 seconds before next symbol...")
            await asyncio.sleep(5)

    print(f"\n✅ All {len(target_symbols)} symbols processed successfully ({lang_label}).")

if __name__ == "__main__":
    asyncio.run(main())