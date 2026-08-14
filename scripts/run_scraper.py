#!/usr/bin/env python
# backend/scripts/run_scraper.py
"""
CLI script to run the financial scrapers.

Usage:
    python scripts/run_scraper.py --type history --symbols 4020,4100
    python scripts/run_scraper.py --type reports --symbols 4020,2222
"""
import asyncio
import argparse
import sys
import os

from dotenv import load_dotenv

# Add parent directory to path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)
load_dotenv(os.path.join(backend_dir, ".env"))


async def run_historical_scraper(symbols: list, headless: bool, send_api: bool, save_json: bool):
    """Run the historical data scraper."""
    from scrapers import HistoricalScraper

    print(f"\n🚀 Starting Historical Scraper")
    print(f"   Symbols: {symbols}")
    print(f"   Headless: {headless}, API: {send_api}, JSON: {save_json}\n")

    scraper = HistoricalScraper(
        symbols=symbols,
        headless=headless,
        send_to_api=send_api,
        save_json=save_json
    )
    return await scraper.scrape_all()


async def run_reports_scraper(symbols: list, headless: bool):
    """Run the financial reports links scraper."""
    from scrapers import FinancialReportsScraper

    print(f"\n🚀 Starting Financial Reports Scraper")
    print(f"   Symbols: {symbols}")
    print(f"   Headless: {headless}\n")

    scraper = FinancialReportsScraper(
        symbols=symbols,
        headless=headless
    )
    return await scraper.scrape_all()


def main():
    parser = argparse.ArgumentParser(description='Run financial data scrapers')

    parser.add_argument(
        '--type', '-t',
        choices=['history', 'reports'],
        default='history',
        help='Type of scraper to run'
    )

    parser.add_argument(
        '--symbols', '-s',
        type=str,
        required=True,
        help='Comma-separated list of company symbols (e.g., 4020,4100,4150)'
    )

    parser.add_argument(
        '--headless',
        action='store_true',
        default=False,
        help='Run browser in headless mode'
    )

    parser.add_argument(
        '--no-api',
        action='store_true',
        default=False,
        help='Disable sending data to API'
    )

    parser.add_argument(
        '--no-json',
        action='store_true',
        default=False,
        help='Disable JSON file saving'
    )

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]

    if not symbols:
        print("❌ No symbols provided")
        sys.exit(1)

    send_api = not args.no_api
    save_json = not args.no_json

    if args.type == 'history':
        result = asyncio.run(run_historical_scraper(symbols, args.headless, send_api, save_json))
    elif args.type == 'reports':
        result = asyncio.run(run_reports_scraper(symbols, args.headless))

    print(f"\n✅ Scraping Complete!")
    if result and 'summary' in result:
        print(f"   Summary: {result['summary']}")


if __name__ == "__main__":
    main()
