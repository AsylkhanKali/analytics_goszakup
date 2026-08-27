import asyncio
import aiohttp
from bs4 import BeautifulSoup

async def main():
    url = "https://goszakup.gov.kz/ru/registry/contract?filter%5Bref_subject_type%5D=1&filter%5Bmethod%5D%5B0%5D=2&filter%5Bstatus%5D%5B0%5D=190&filter%5Bstatus%5D%5B1%5D=390&filter%5Bstatus%5D%5B2%5D=185&count_record=2000&filter%5Bstart_date_from%5D=2024-02-01&filter%5Bstart_date_to%5D=2024-02-29&page=1"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    conn = aiohttp.TCPConnector(limit=10) # Matching scraper.py
    async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
        async with session.get(url, timeout=30) as resp:
            print("Status:", resp.status)
            html = await resp.text()
            print("HTML Length:", len(html))
            print(html)

asyncio.run(main())
