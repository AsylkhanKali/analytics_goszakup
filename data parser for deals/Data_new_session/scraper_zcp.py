import asyncio
import aiohttp
from bs4 import BeautifulSoup
import sqlite3
import os
import re
import argparse
from typing import List, Dict, Any

DB_PATH = 'data/goszakup.db'
TABLE_NAME = 'contracts_zcp'
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Allowed statuses as requested: Исполнен, Подписан, Действует, Изменен, Частично исполнен
ALLOWED_STATUSES = ['Исполнен', 'Подписан', 'Действует', 'Изменен', 'Частично исполнен']

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    c = conn.cursor()
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            contract_id TEXT,
            contract_number TEXT,
            contract_date TEXT,
            contract_status TEXT,
            customer_name TEXT,
            customer_bin TEXT,
            supplier_name TEXT,
            supplier_bin TEXT,
            lot_title TEXT,
            unit_price REAL,
            quantity REAL,
            contract_amount REAL,
            brand_model TEXT,
            country TEXT,
            manufacturer TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_lots(lots: List[Dict[str, Any]]):
    if not lots:
        return
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    c = conn.cursor()
    c.executemany(f'''
        INSERT INTO {TABLE_NAME} (
            contract_id, contract_number, contract_date, contract_status,
            customer_name, customer_bin, supplier_name, supplier_bin,
            lot_title, unit_price, quantity, contract_amount,
            brand_model, country, manufacturer
        ) VALUES (
            :contract_id, :contract_number, :contract_date, :contract_status,
            :customer_name, :customer_bin, :supplier_name, :supplier_bin,
            :lot_title, :unit_price, :quantity, :contract_amount,
            :brand_model, :country, :manufacturer
        )
    ''', lots)
    conn.commit()
    conn.close()

async def fetch_customer_supplier(session: aiohttp.ClientSession, contract_id: str):
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/customer_n_supplier/{contract_id}"
    for _ in range(5):
        try:
            async with session.get(url, timeout=20) as response:
                if response.status != 200:
                    await asyncio.sleep(5)
                    continue
                html = await response.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"429 on details {contract_id}, sleeping 30s...")
                    await asyncio.sleep(30)
                    continue
                soup = BeautifulSoup(html, 'html.parser')
                tables = soup.find_all('table')
                
                customer_bin, customer_name = "", ""
                supplier_bin, supplier_name = "", ""
                
                if len(tables) > 0:
                    for row in tables[0].find_all('tr'):
                        cols = row.find_all(['th', 'td'])
                        if len(cols) == 2:
                            key = cols[0].text.strip()
                            val = cols[1].text.strip()
                            if 'БИН' in key:
                                customer_bin = val
                            elif 'Наименование заказчика' in key:
                                customer_name = val
                    
                    if len(tables) > 1:
                        for row in tables[1].find_all('tr'):
                            cols = row.find_all(['th', 'td'])
                            if len(cols) == 2:
                                key = cols[0].text.strip()
                                val = cols[1].text.strip()
                                if 'БИН' in key or 'ИИН' in key:
                                    if val: supplier_bin = val
                                elif 'Наименование поставщика' in key:
                                    supplier_name = val
                
                return customer_name, customer_bin, supplier_name, supplier_bin
        except Exception as e:
            await asyncio.sleep(5)
    return None, None, None, None

async def fetch_lots(session: aiohttp.ClientSession, contract_id: str):
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{contract_id}"
    for _ in range(5):
        lots_data = []
        try:
            async with session.get(url, timeout=20) as response:
                if response.status != 200:
                    await asyncio.sleep(5)
                    continue
                html = await response.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"429 on lots {contract_id}, sleeping 30s...")
                    await asyncio.sleep(30)
                    continue
                soup = BeautifulSoup(html, 'html.parser')
                table = soup.find('table')
                if not table:
                    return lots_data
                
                rows = table.find_all('tr')
                for row in rows:
                    cols = row.find_all(['td', 'th'])
                    if len(cols) >= 9 and not cols[0].name == 'th':
                        title = cols[4].text.strip()
                        if title == "Наименование":
                            continue
                        
                        try:
                            quantity = float(cols[5].text.strip().replace(' ', '').replace(',', '.'))
                            unit_price = float(cols[7].text.strip().replace(' ', '').replace(',', '.'))
                            amount = float(cols[8].text.strip().replace(' ', '').replace(',', '.'))
                        except:
                            quantity, unit_price, amount = 0.0, 0.0, 0.0
                            
                        lots_data.append({
                            "lot_title": title,
                            "unit_price": unit_price,
                            "quantity": quantity,
                            "contract_amount": amount,
                            "brand_model": "",
                            "country": "",
                            "manufacturer": ""
                        })
                return lots_data
        except Exception as e:
            await asyncio.sleep(5)
    return []

async def process_contract(session: aiohttp.ClientSession, sem: asyncio.Semaphore, contract: dict):
    async with sem:
        if contract['contract_status'] not in ALLOWED_STATUSES:
            return None
            
        c_name, c_bin, s_name, s_bin = await fetch_customer_supplier(session, contract['contract_id'])
        if c_name is None:
            return None
            
        lots = await fetch_lots(session, contract['contract_id'])
        
        results = []
        for lot in lots:
            row = {
                "contract_id": contract['contract_id'],
                "contract_number": contract['contract_number'],
                "contract_date": contract['contract_date'],
                "contract_status": contract['contract_status'],
                "customer_name": c_name,
                "customer_bin": c_bin,
                "supplier_name": s_name,
                "supplier_bin": s_bin,
                "lot_title": lot['lot_title'],
                "unit_price": lot['unit_price'],
                "quantity": lot['quantity'],
                "contract_amount": lot['contract_amount'],
                "brand_model": "",
                "country": "",
                "manufacturer": ""
            }
            results.append(row)
        return results

async def scrape_period(start_date: str, end_date: str):
    init_db()
    
    print(f"Loading existing contract IDs from DB table '{TABLE_NAME}'...")
    conn_db = sqlite3.connect(DB_PATH, timeout=60.0)
    c = conn_db.cursor()
    c.execute(f"SELECT DISTINCT contract_id FROM {TABLE_NAME}")
    existing_ids = set(row[0] for row in c.fetchall())
    conn_db.close()
    print(f"Loaded {len(existing_ids)} existing contracts in '{TABLE_NAME}'.")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    conn = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
        page = 1
        sem = asyncio.Semaphore(10)
        
        while True:
            print(f"Fetching page {page} for ZCP Goods ({start_date} - {end_date})...")
            # ZCP method=3, ref_subject_type=1 (Goods), statuses: 390 (Исполнен), 185 (Подписан), 190 (Действует), 455 (Изменен), 375 (Частично исполнен), count_record=2000
            url = f"https://goszakup.gov.kz/ru/registry/contract?filter%5Bref_subject_type%5D=1&filter%5Bmethod%5D%5B0%5D=3&filter%5Bstatus%5D%5B0%5D=390&filter%5Bstatus%5D%5B1%5D=185&filter%5Bstatus%5D%5B2%5D=190&filter%5Bstatus%5D%5B3%5D=455&filter%5Bstatus%5D%5B4%5D=375&count_record=2000&filter%5Bstart_date_from%5D={start_date}&filter%5Bstart_date_to%5D={end_date}&page={page}"
            
            retry_count = 0
            tables = []
            soup = None
            while retry_count < 100:
                try:
                    async with session.get(url, timeout=30) as resp:
                        if resp.status != 200:
                            print(f"Failed to fetch registry page {page}: HTTP {resp.status}")
                            retry_count += 1
                            await asyncio.sleep(10)
                            continue
                        html = await resp.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        if "429 Too Many Requests" in html or "g-recaptcha" in html:
                            print(f"CAPTCHA / 429 DETECTED on page {page}! Waiting 30 seconds before retrying...")
                            await asyncio.sleep(30)
                            retry_count += 1
                            continue
                        
                        tables = soup.find_all('table')
                        if len(tables) < 2:
                            print(f"No data tables found on page {page}, retrying in 5s... ({retry_count+1}/100).")
                            retry_count += 1
                            await asyncio.sleep(5)
                            continue
                        break
                except Exception as e:
                    print(f"Error fetching page {page}: {e}. Retrying in 15s...")
                    retry_count += 1
                    await asyncio.sleep(15)
            
            if len(tables) < 2:
                raise Exception(f"Failed to fetch page data after 100 retries. Aborting to prevent data loss.")
            
            table = tables[1]
            rows = table.find('tbody').find_all('tr') if table.find('tbody') else []
            if not rows:
                print("No rows in table. End of results.")
                break
                
            contracts_to_process = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 10:
                    contract_id = cols[0].text.strip()
                    
                    a_tag = cols[1].find('a')
                    if not a_tag: continue
                    contract_number = a_tag.text.strip()
                    
                    contract_status = cols[4].text.strip()
                    contract_date = cols[5].text.strip()
                    
                    if contract_status in ALLOWED_STATUSES:
                        if contract_id not in existing_ids:
                            contracts_to_process.append({
                                "contract_id": contract_id,
                                "contract_number": contract_number,
                                "contract_date": contract_date,
                                "contract_status": contract_status
                            })
                            existing_ids.add(contract_id)
                
            if not contracts_to_process:
                print(f"All contracts on page {page} already exist or skipped. Moving to next page...")
            else:
                print(f"Found {len(contracts_to_process)} new ZCP contracts on page {page}. Fetching details...")
                tasks = [process_contract(session, sem, c) for c in contracts_to_process]
                results = await asyncio.gather(*tasks)
                
                all_lots = []
                for res in results:
                    if res:
                        all_lots.extend(res)
                
                if all_lots:
                    save_lots(all_lots)
                    print(f"Saved {len(all_lots)} lots from page {page} to '{TABLE_NAME}'.")
                
            pagination = soup.find('ul', class_='pagination')
            has_next = False
            if pagination:
                next_page_link = pagination.find('a', href=re.compile(rf'page={page+1}'))
                if next_page_link:
                    has_next = True
            
            if not has_next:
                print("No more pages.")
                break
            page += 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    args = parser.parse_args()
    
    asyncio.run(scrape_period(args.start, args.end))
