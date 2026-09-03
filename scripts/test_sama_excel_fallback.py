"""
Test Script v4: SAMA Excel — Download via Selenium Session Cookies
------------------------------------------------------------------
Fix: SAMA server rejects plain requests.get() (RemoteDisconnected).
Solution: Capture browser cookies from Selenium, pass to requests.Session.

Run:
    ..\venv\Scripts\python.exe scripts\test_sama_excel_fallback.py
"""
import sys, os, requests, pandas as pd, time
from io import BytesIO

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMA_BASE = "https://www.sama.gov.sa"
SAMA_PAGE = f"{SAMA_BASE}/en-US/Statistics/pages/monthlystatistics.aspx"


def get_xlsx_with_session():
    """Discover XLSX URL via Selenium AND download it using the same browser session cookies."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from bs4 import BeautifulSoup

    print("[STEP 1] Opening SAMA page via Selenium (headless)...")
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=opts)
    content = None
    xlsx_url = None

    try:
        driver.get(SAMA_PAGE)
        time.sleep(3)

        # Discover XLSX link
        soup = BeautifulSoup(driver.page_source, "html.parser")
        xlsx_links = [a["href"] for a in soup.find_all("a", href=True) if a["href"].lower().endswith(".xlsx")]
        if not xlsx_links:
            raise RuntimeError("No XLSX link found on SAMA page.")
        path = xlsx_links[0]
        xlsx_url = path if path.startswith("http") else SAMA_BASE + path
        print(f"[OK] XLSX URL: {xlsx_url}")

        # Capture browser cookies & headers
        cookies = driver.get_cookies()
        ua = driver.execute_script("return navigator.userAgent;")
        print(f"[INFO] Captured {len(cookies)} cookies from browser session.")

        # Build requests session with browser cookies
        session = requests.Session()
        session.headers.update({
            "User-Agent": ua,
            "Referer": SAMA_PAGE,
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        print(f"\n[STEP 2] Downloading XLSX via browser-authenticated session...")
        r = session.get(xlsx_url, timeout=60)
        print(f"[OK] HTTP {r.status_code} | {len(r.content):,} bytes")
        r.raise_for_status()
        content = r.content

    finally:
        driver.quit()

    return xlsx_url, content


def parse_sheet_5_6(content):
    """Parse sheet '5-6' which contains SAIBOR and policy rates."""
    print("\n[STEP 3] Parsing sheet '5-6' (SAIBOR + Policy Rates)...")
    xls = pd.ExcelFile(BytesIO(content), engine="openpyxl")

    if "5-6" not in xls.sheet_names:
        print(f"[WARN] Sheet '5-6' not found. Available: {xls.sheet_names[:20]}")
        return {}

    # Row 9 (0-indexed = 8) is the header
    df = pd.read_excel(xls, sheet_name="5-6", header=8)
    print(f"[INFO] Shape: {df.shape}")
    print(f"[INFO] Columns: {list(df.columns)}")

    # Data ends before footnotes ~row 337 (absolute)
    data = df.iloc[:337].dropna(how="all")
    print(f"[INFO] Non-empty data rows: {len(data)}")

    print("\n[Last 5 data rows]:")
    print(data.tail(5).to_string())

    print("\n[All columns with last numeric value in 0.01–30% range]:")
    results = {}
    for col in data.columns:
        series = pd.to_numeric(data[col], errors="coerce").dropna()
        if not series.empty and 0.01 < series.iloc[-1] < 30:
            val = round(float(series.iloc[-1]), 4)
            print(f"  {str(col):<45s} -> {val}")
            col_lower = str(col).lower()
            if "3m saibor" in col_lower or col_lower == "3m saibor":
                results["saibor_3m"] = val
            elif "saibor" in col_lower and "12" in col_lower:
                results["saibor_12m"] = val
            elif "saibor" in col_lower and "26" in col_lower:
                results["saibor_6m"] = val

    return results


def main():
    print("=" * 65)
    print("  SAMA Excel Fallback — Session Cookie Download Test v4")
    print("=" * 65)

    try:
        url, content = get_xlsx_with_session()
    except Exception as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)

    results = parse_sheet_5_6(content)

    print("\n" + "=" * 65)
    print("  FINAL RESULTS")
    print("=" * 65)
    if results:
        for k, v in results.items():
            print(f"  {k}: {v} %")
        print("\n[PASS] Excel fallback fully functional with session cookies.")
    else:
        print("  [INFO] File downloaded and parsed — check column names above.")
        print("  Update column mapping in SAMA&GaStat.py accordingly.")
        print("\n[PARTIAL] Download OK. Review column names printed above.")


if __name__ == "__main__":
    main()
