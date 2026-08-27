import asyncio
import aiohttp
from bs4 import BeautifulSoup
import sqlite3
import os
import re
import sys
import time
import datetime
import argparse
from typing import List, Dict, Any, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'goszakup_2024_2026.db')
TABLE_NAME = 'contracts_lots'
PROGRESS_TABLE = 'intervals_progress'

ALLOWED_STATUS_STRINGS = {
    'Действует', 'Изменен', 'Изменён', 'Исполнен', 'Подписан',
    'Создано доп.соглашение', 'Создано доп. соглашение', 'Создано дополнительное соглашение',
    'Частично исполнен'
}

STATUS_CODES = ['190', '455', '390', '185', '450', '375']
# ЗЦП (3), ОК (2), ОИ (6, 23, 105, 123, 125, 131)
ALL_METHOD_CODES = ['3', '2', '6', '23', '105', '123', '125', '131']
CAP_THRESHOLD = 9500  # Portal caps queries at 10,000. If >= 9,500 we subdivide to prevent losing data.
RETRY_PAUSE_429 = 15  # 15 seconds backoff on 429 / Captcha

def get_db_connection():
    for attempt in range(10):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=120.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA wal_autocheckpoint = 1000;")
            conn.execute("PRAGMA busy_timeout = 60000;")
            return conn
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] DB connection retry {attempt+1}/10: {e}")
            time.sleep(2)
    raise Exception("Failed to connect to SQLite DB after 10 retries.")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            contract_id TEXT,
            contract_number TEXT,
            purchase_number TEXT,
            contract_type TEXT,
            contract_date TEXT,
            contract_status TEXT,
            purchase_method TEXT,
            customer_name TEXT,
            customer_bin TEXT,
            supplier_name TEXT,
            supplier_bin TEXT,
            lot_id TEXT,
            lot_title TEXT,
            unit_price REAL,
            quantity REAL,
            contract_amount REAL,
            brand_model TEXT,
            country TEXT,
            manufacturer TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (
            interval_start TEXT,
            interval_end TEXT,
            method_tag TEXT,
            status TEXT,
            contracts_count INTEGER,
            lots_count INTEGER,
            updated_at TEXT,
            PRIMARY KEY (interval_start, interval_end, method_tag)
        )
    ''')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_cid ON {TABLE_NAME}(contract_id);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_date ON {TABLE_NAME}(contract_date);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_method ON {TABLE_NAME}(purchase_method);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_cbin ON {TABLE_NAME}(customer_bin);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_sbin ON {TABLE_NAME}(supplier_bin);')
    conn.commit()
    conn.close()

def save_lots(lots: List[Dict[str, Any]]):
    if not lots:
        return
    for attempt in range(10):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.executemany(f'''
                INSERT INTO {TABLE_NAME} (
                    contract_id, contract_number, purchase_number, contract_type,
                    contract_date, contract_status, purchase_method,
                    customer_name, customer_bin, supplier_name, supplier_bin,
                    lot_id, lot_title, unit_price, quantity, contract_amount,
                    brand_model, country, manufacturer
                ) VALUES (
                    :contract_id, :contract_number, :purchase_number, :contract_type,
                    :contract_date, :contract_status, :purchase_method,
                    :customer_name, :customer_bin, :supplier_name, :supplier_bin,
                    :lot_id, :lot_title, :unit_price, :quantity, :contract_amount,
                    :brand_model, :country, :manufacturer
                )
            ''', lots)
            conn.commit()
            conn.close()
            return
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] save_lots retry {attempt+1}/10: {e}")
            time.sleep(3)

def update_progress(start_date: str, end_date: str, method_tag: str, status: str, contracts_count: int, lots_count: int):
    for attempt in range(10):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute(f'''
                INSERT INTO {PROGRESS_TABLE} (interval_start, interval_end, method_tag, status, contracts_count, lots_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interval_start, interval_end, method_tag) DO UPDATE SET
                    status=excluded.status,
                    contracts_count=contracts_count + excluded.contracts_count,
                    lots_count=lots_count + excluded.lots_count,
                    updated_at=excluded.updated_at
            ''', (start_date, end_date, method_tag, status, contracts_count, lots_count, now_str))
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass
            conn.close()
            return
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] update_progress retry {attempt+1}/10: {e}")
            time.sleep(3)

def is_interval_completed(start_date: str, end_date: str, method_tag: str = 'all') -> bool:
    for attempt in range(10):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute(f"SELECT status FROM {PROGRESS_TABLE} WHERE interval_start=? AND interval_end=? AND method_tag=?", (start_date, end_date, method_tag))
            row = c.fetchone()
            conn.close()
            return row is not None and row[0] == 'completed'
        except Exception as e:
            time.sleep(1)
    return False

def get_existing_contract_ids() -> set:
    for attempt in range(10):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute(f"SELECT DISTINCT contract_id FROM {TABLE_NAME}")
            res = set(row[0] for row in c.fetchall())
            conn.close()
            return res
        except Exception as e:
            time.sleep(2)
    return set()

def parse_total_records(html: str) -> int:
    soup = BeautifulSoup(html, 'html.parser')
    info = soup.find('div', class_='dataTables_info')
    if info:
        m = re.search(r'из\s+([\d\s]+)\s+запис', info.text)
        if m:
            return int(m.group(1).replace(' ', ''))
    return 0

async def fetch_customer_supplier(session: aiohttp.ClientSession, contract_id: str) -> Tuple[str, str, str, str]:
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/customer_n_supplier/{contract_id}"
    for attempt in range(100):
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 429:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 429 on customer_n_supplier {contract_id}, sleeping {RETRY_PAUSE_429}s...", flush=True)
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(4)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Captcha/429 on customer_n_supplier {contract_id}, sleeping {RETRY_PAUSE_429}s...", flush=True)
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                
                soup = BeautifulSoup(html, 'html.parser')
                tables = soup.find_all('table')
                c_bin, c_name = "", ""
                s_bin, s_name = "", ""
                
                if len(tables) > 0:
                    for row in tables[0].find_all('tr'):
                        cols = row.find_all(['th', 'td'])
                        if len(cols) == 2:
                            k = cols[0].text.strip()
                            v = cols[1].text.strip()
                            if 'БИН' in k:
                                c_bin = v
                            elif 'Наименование заказчика' in k:
                                c_name = v
                if len(tables) > 1:
                    for row in tables[1].find_all('tr'):
                        cols = row.find_all(['th', 'td'])
                        if len(cols) == 2:
                            k = cols[0].text.strip()
                            v = cols[1].text.strip()
                            if 'БИН' in k or 'ИИН' in k:
                                if v: s_bin = v
                            elif 'Наименование поставщика' in k:
                                s_name = v
                return c_name, c_bin, s_name, s_bin
        except Exception as e:
            await asyncio.sleep(4)
    return "", "", "", ""

async def fetch_lots(session: aiohttp.ClientSession, contract_id: str) -> List[Dict[str, Any]]:
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{contract_id}"
    for attempt in range(100):
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 429:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 429 on units {contract_id}, sleeping {RETRY_PAUSE_429}s...", flush=True)
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(4)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Captcha/429 on units {contract_id}, sleeping {RETRY_PAUSE_429}s...", flush=True)
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                
                soup = BeautifulSoup(html, 'html.parser')
                table = soup.find('table')
                if not table:
                    return []
                
                lots = []
                for row in table.find_all('tr'):
                    cols = row.find_all(['td', 'th'])
                    if len(cols) >= 9 and cols[0].name != 'th':
                        lot_id = cols[1].text.strip()
                        title = cols[4].text.strip()
                        if title == "Наименование":
                            continue
                        try:
                            quantity = float(cols[5].text.strip().replace(' ', '').replace(',', '.'))
                            unit_price = float(cols[7].text.strip().replace(' ', '').replace(',', '.'))
                            amount = float(cols[8].text.strip().replace(' ', '').replace(',', '.'))
                        except Exception:
                            quantity, unit_price, amount = 0.0, 0.0, 0.0
                        lots.append({
                            'lot_id': lot_id,
                            'lot_title': title,
                            'unit_price': unit_price,
                            'quantity': quantity,
                            'contract_amount': amount
                        })
                return lots
        except Exception as e:
            await asyncio.sleep(4)
    return []

async def process_contract(session: aiohttp.ClientSession, sem: asyncio.Semaphore, contract: dict) -> List[Dict[str, Any]]:
    async with sem:
        c_name, c_bin, s_name, s_bin = await fetch_customer_supplier(session, contract['contract_id'])
        lots = await fetch_lots(session, contract['contract_id'])
        
        cust_name = c_name if c_name else contract.get('customer_reg', '')
        supp_name = s_name if s_name else contract.get('supplier_reg', '')
        
        if not lots:
            lots = [{
                'lot_id': '',
                'lot_title': 'Товары по договору (без детализации позиций)',
                'unit_price': contract.get('amount_raw', 0.0),
                'quantity': 1.0,
                'contract_amount': contract.get('amount_raw', 0.0)
            }]
            
        results = []
        for lot in lots:
            row = {
                'contract_id': contract['contract_id'],
                'contract_number': contract['contract_number'],
                'purchase_number': contract['purchase_number'],
                'contract_type': contract['contract_type'],
                'contract_date': contract['contract_date'],
                'contract_status': contract['contract_status'],
                'purchase_method': contract['purchase_method'],
                'customer_name': cust_name,
                'customer_bin': c_bin,
                'supplier_name': supp_name,
                'supplier_bin': s_bin,
                'lot_id': lot['lot_id'],
                'lot_title': lot['lot_title'],
                'unit_price': lot['unit_price'],
                'quantity': lot['quantity'],
                'contract_amount': lot['contract_amount'],
                'brand_model': '',
                'country': '',
                'manufacturer': ''
            }
            results.append(row)
        return results

async def fetch_registry_page(session: aiohttp.ClientSession, params: list, page: int) -> Tuple[str, List[Any], BeautifulSoup]:
    params_with_page = list(params) + [('page', str(page))]
    for retry in range(100):
        try:
            async with session.get('https://goszakup.gov.kz/ru/registry/contract', params=params_with_page, timeout=40) as resp:
                if resp.status == 429:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 429 on registry page {page}, sleeping {RETRY_PAUSE_429}s...", flush=True)
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    print(f"Failed registry page {page}: HTTP {resp.status}, retry {retry+1}/100 in 10s...", flush=True)
                    await asyncio.sleep(10)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Captcha/429 on registry page {page}, sleeping {RETRY_PAUSE_429}s...", flush=True)
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                    
                soup = BeautifulSoup(html, 'html.parser')
                tables = soup.find_all('table')
                if len(tables) < 2:
                    if "Записей не найдено" in html or "Ничего не найдено" in html or "нет данных" in html.lower():
                        return html, [], soup
                    print(f"No data tables found on page {page}, retrying in 5s... ({retry+1}/100)", flush=True)
                    await asyncio.sleep(5)
                    continue
                return html, tables, soup
        except Exception as e:
            print(f"Error fetching registry page {page}: {e}. Retrying in 15s...", flush=True)
            await asyncio.sleep(15)
    return "", [], None

async def scrape_slice(session: aiohttp.ClientSession, sem: asyncio.Semaphore, start_date: str, end_date: str, methods: list, existing_ids: set, method_tag: str = 'all') -> Tuple[int, int]:
    if is_interval_completed(start_date, end_date, method_tag):
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Slice {start_date}..{end_date} (tag: {method_tag}) already completed. Skipping.", flush=True)
        return 0, 0
        
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    
    # Build base params
    params = [
        ('filter[ref_subject_type]', '1'),
        ('count_record', '2000'),
        ('filter[start_date_from]', start_date),
        ('filter[start_date_to]', end_date),
    ]
    for s in STATUS_CODES:
        params.append(('filter[status][]', s))
    for m in methods:
        params.append(('filter[method][]', m))
        
    # Fetch page 1 first to check total records count
    html_p1, tables_p1, soup_p1 = await fetch_registry_page(session, params, 1)
    if not html_p1 or len(tables_p1) < 2:
        update_progress(start_date, end_date, method_tag, 'completed', 0, 0)
        return 0, 0
        
    total_records = parse_total_records(html_p1)
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Query {start_date}..{end_date} (tag: {method_tag}): Portal reports {total_records} total contracts found.", flush=True)
    
    # Check if total records >= CAP_THRESHOLD (10,000 cap risk)
    if total_records >= CAP_THRESHOLD:
        days_diff = (end_dt - start_dt).days
        if days_diff > 0:
            mid_days = days_diff // 2
            mid_dt = start_dt + datetime.timedelta(days=mid_days)
            mid_next_dt = mid_dt + datetime.timedelta(days=1)
            
            s1_start, s1_end = start_dt.strftime("%Y-%m-%d"), mid_dt.strftime("%Y-%m-%d")
            s2_start, s2_end = mid_next_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
            
            print(f"\n⚡ [CAP 10,000 PROTECTION] Total records ({total_records}) >= {CAP_THRESHOLD} for {start_date}..{end_date}!", flush=True)
            print(f"⚡ Subdividing into 2 sub-intervals: [{s1_start}..{s1_end}] and [{s2_start}..{s2_end}] to ensure NO contracts are missed.\n", flush=True)
            
            c1, l1 = await scrape_slice(session, sem, s1_start, s1_end, methods, existing_ids, method_tag)
            c2, l2 = await scrape_slice(session, sem, s2_start, s2_end, methods, existing_ids, method_tag)
            
            update_progress(start_date, end_date, method_tag, 'completed', c1 + c2, l1 + l2)
            return c1 + c2, l1 + l2
        else:
            if len(methods) > 1:
                print(f"\n⚡ [CAP 10,000 PROTECTION] Single day {start_date} has {total_records} records! Subdividing by individual procurement methods...\n", flush=True)
                total_c_sub, total_l_sub = 0, 0
                for m_code in methods:
                    tag_m = f"m_{m_code}"
                    c_m, l_m = await scrape_slice(session, sem, start_date, end_date, [m_code], existing_ids, tag_m)
                    total_c_sub += c_m
                    total_l_sub += l_m
                update_progress(start_date, end_date, method_tag, 'completed', total_c_sub, total_l_sub)
                return total_c_sub, total_l_sub

    update_progress(start_date, end_date, method_tag, 'in_progress', 0, 0)
    page = 1
    total_contracts_slice = 0
    total_lots_slice = 0
    
    while True:
        if page == 1:
            tables = tables_p1
            soup = soup_p1
        else:
            _, tables, soup = await fetch_registry_page(session, params, page)
            
        if len(tables) < 2:
            break
            
        table = tables[1]
        tbody = table.find('tbody')
        rows = tbody.find_all('tr') if tbody else []
        if not rows:
            break
            
        contracts_to_fetch = []
        for r in rows:
            cols = r.find_all('td')
            if len(cols) >= 10:
                cid = cols[0].text.strip()
                c_num_tag = cols[1].find('a')
                c_num = c_num_tag.text.strip() if c_num_tag else cols[1].text.strip()
                purch_num = cols[2].text.strip()
                c_type = cols[3].text.strip()
                c_status = cols[4].text.strip()
                c_date = cols[5].text.strip()
                
                try:
                    amt_raw = float(cols[6].text.strip().replace(' ', '').replace(',', '.'))
                except Exception:
                    amt_raw = 0.0
                    
                cust_reg = cols[7].text.strip()
                supp_reg = cols[8].text.strip()
                purch_method = cols[9].text.strip()
                
                if cid not in existing_ids:
                    contracts_to_fetch.append({
                        'contract_id': cid,
                        'contract_number': c_num,
                        'purchase_number': purch_num,
                        'contract_type': c_type,
                        'contract_status': c_status,
                        'contract_date': c_date,
                        'amount_raw': amt_raw,
                        'customer_reg': cust_reg,
                        'supplier_reg': supp_reg,
                        'purchase_method': purch_method,
                    })
                    existing_ids.add(cid)
                    
        if contracts_to_fetch:
            CHUNK_SIZE = 50
            for i in range(0, len(contracts_to_fetch), CHUNK_SIZE):
                chunk = contracts_to_fetch[i:i + CHUNK_SIZE]
                tasks = [process_contract(session, sem, c) for c in chunk]
                batch_results = await asyncio.gather(*tasks)
                
                chunk_lots = []
                for res in batch_results:
                    if res:
                        chunk_lots.extend(res)
                        
                if chunk_lots:
                    save_lots(chunk_lots)
                    total_lots_slice += len(chunk_lots)
                total_contracts_slice += len(chunk)
                
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{start_date}..{end_date} {method_tag}] Page {page} [{min(i + CHUNK_SIZE, len(contracts_to_fetch))}/{len(contracts_to_fetch)}]: +{len(chunk_lots)} lots (slice total: {total_lots_slice})", flush=True)
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{start_date}..{end_date} {method_tag}] Page {page}: all {len(rows)} contracts already exist in DB.", flush=True)
            
        pagination = soup.find('ul', class_='pagination')
        has_next = False
        if pagination:
            next_link = pagination.find('a', href=re.compile(rf'page={page+1}'))
            if next_link:
                has_next = True
        if not has_next:
            break
        page += 1
        
    update_progress(start_date, end_date, method_tag, 'completed', total_contracts_slice, total_lots_slice)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] <<< Completed slice {start_date}..{end_date} (tag: {method_tag}): {total_contracts_slice} contracts, {total_lots_slice} lots saved.\n", flush=True)
    return total_contracts_slice, total_lots_slice

async def main_scraper(start_date_str: str, end_date_str: str, concurrency: int = 30):
    init_db()
    existing_ids = get_existing_contract_ids()
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Initialized DB at '{DB_PATH}'. Existing contracts in DB: {len(existing_ids)}", flush=True)
    
    start_dt = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
    
    intervals = []
    curr = start_dt
    while curr <= end_dt:
        nxt_end = min(curr + datetime.timedelta(days=4), end_dt)
        intervals.append((curr.strftime("%Y-%m-%d"), nxt_end.strftime("%Y-%m-%d")))
        curr = nxt_end + datetime.timedelta(days=1)
        
    print(f"Total base 5-day intervals: {len(intervals)} (from {start_date_str} to {end_date_str}) with auto-subdivision if >= {CAP_THRESHOLD} records", flush=True)
    print(f"Running with CONCURRENCY = {concurrency} workers and 429 BACKOFF = {RETRY_PAUSE_429}s", flush=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru,en-US;q=0.9,en;q=0.8'
    }
    
    conn_tcp = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(concurrency)
    
    total_new_contracts = 0
    total_new_lots = 0
    
    async with aiohttp.ClientSession(headers=headers, connector=conn_tcp) as session:
        for idx, (i_start, i_end) in enumerate(intervals, 1):
            if is_interval_completed(i_start, i_end, 'all'):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Interval {idx}/{len(intervals)} ({i_start}..{i_end}) already completed. Skipping.", flush=True)
                continue
                
            print(f"\n=======================================================")
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Interval {idx}/{len(intervals)}: {i_start} -> {i_end}")
            print(f"=======================================================", flush=True)
            
            c_cnt, l_cnt = await scrape_slice(session, sem, i_start, i_end, ALL_METHOD_CODES, existing_ids, 'all')
            total_new_contracts += c_cnt
            total_new_lots += l_cnt
            
    print(f"\n=======================================================")
    print(f"ALL INTERVALS COMPLETED SUCCESSFULLY!")
    print(f"Total new contracts scraped: {total_new_contracts}")
    print(f"Total new lots saved: {total_new_lots}")
    print(f"Database location: {DB_PATH}")
    print(f"=======================================================\n", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Goszakup full scraper (ZCP, OK, OI) with 10k cap protection 2024-2026")
    parser.add_argument("--start", type=str, default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--concurrency", type=int, default=30, help="Number of concurrent sessions (default 30)")
    args = parser.parse_args()
    
    asyncio.run(main_scraper(args.start, args.end, args.concurrency))
