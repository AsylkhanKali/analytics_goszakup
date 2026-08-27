#!/usr/bin/env python3
"""
Goszakup Weekly Auto-Updater
----------------------------
Automatically detects the latest contract date in the database,
fetches fresh contracts up to today (Goods only: ZCP, OK, OI),
handles the 10k portal limit with dynamic interval subdividing,
saves lots to contracts_lots, creates database checkpoints,
and logs sync history.
"""

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
from typing import List, Dict, Any, Tuple, Optional

# Default database resolution
DEFAULT_DB_LOCATIONS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'all_data', 'goszakup_2024_2026_final.db'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data parser for deals', 'Data_new_session', 'data', 'goszakup_2024_2026.db'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'goszakup.db')
]

def resolve_default_db() -> str:
    env_db = os.getenv('GOSZAKUP_DB_PATH')
    if env_db and os.path.exists(env_db):
        return env_db
    for p in DEFAULT_DB_LOCATIONS:
        if os.path.exists(p):
            return p
    return DEFAULT_DB_LOCATIONS[0]

DB_PATH = resolve_default_db()
TABLE_NAME = 'contracts_lots'
PROGRESS_TABLE = 'intervals_progress'
SYNC_HISTORY_TABLE = 'sync_history'

ALLOWED_STATUS_STRINGS = {
    'Действует', 'Изменен', 'Изменён', 'Исполнен', 'Подписан',
    'Создано доп.соглашение', 'Создано доп. соглашение', 'Создано дополнительное соглашение',
    'Частично исполнен'
}

STATUS_CODES = ['190', '455', '390', '185', '450', '375']
# ЗЦП (3), ОК (2), ОИ (6, 23, 105, 123, 125, 131)
ALL_METHOD_CODES = ['3', '2', '6', '23', '105', '123', '125', '131']
CAP_THRESHOLD = 9500
RETRY_PAUSE_429 = 15

def get_db_connection(db_path: str = DB_PATH):
    for attempt in range(10):
        try:
            conn = sqlite3.connect(db_path, timeout=120.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA wal_autocheckpoint = 1000;")
            conn.execute("PRAGMA busy_timeout = 60000;")
            return conn
        except Exception as e:
            time.sleep(2)
    raise Exception(f"Failed to connect to SQLite DB at {db_path} after 10 retries.")

def init_tables(db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = get_db_connection(db_path)
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
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS {SYNC_HISTORY_TABLE} (
            sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            completed_at TEXT,
            from_date TEXT,
            to_date TEXT,
            new_contracts_count INTEGER,
            new_lots_count INTEGER,
            status TEXT,
            details TEXT
        )
    ''')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_cid ON {TABLE_NAME}(contract_id);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_date ON {TABLE_NAME}(contract_date);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_method ON {TABLE_NAME}(purchase_method);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_cbin ON {TABLE_NAME}(customer_bin);')
    c.execute(f'CREATE INDEX IF NOT EXISTS idx_contracts_sbin ON {TABLE_NAME}(supplier_bin);')
    conn.commit()
    conn.close()

def get_latest_contract_date(db_path: str = DB_PATH) -> Optional[datetime.date]:
    conn = get_db_connection(db_path)
    c = conn.cursor()
    c.execute(f"SELECT MAX(contract_date) FROM {TABLE_NAME}")
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.datetime.strptime(row[0][:10], "%Y-%m-%d").date()
        except Exception:
            pass
    return None

def record_sync_start(from_date: str, to_date: str, db_path: str = DB_PATH) -> int:
    conn = get_db_connection(db_path)
    c = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(f'''
        INSERT INTO {SYNC_HISTORY_TABLE} (started_at, from_date, to_date, status, details)
        VALUES (?, ?, ?, 'running', 'Sync in progress')
    ''', (now_str, from_date, to_date))
    sync_id = c.lastrowid
    conn.commit()
    conn.close()
    return sync_id

def record_sync_complete(sync_id: int, new_contracts: int, new_lots: int, status: str = 'success', details: str = 'Completed successfully', db_path: str = DB_PATH):
    conn = get_db_connection(db_path)
    c = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(f'''
        UPDATE {SYNC_HISTORY_TABLE}
        SET completed_at=?, new_contracts_count=?, new_lots_count=?, status=?, details=?
        WHERE sync_id=?
    ''', (now_str, new_contracts, new_lots, status, details, sync_id))
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass
    conn.close()

def get_sync_status(db_path: str = DB_PATH) -> Dict[str, Any]:
    init_tables(db_path)
    conn = get_db_connection(db_path)
    c = conn.cursor()
    c.execute(f"SELECT count(DISTINCT contract_id), count(*) FROM {TABLE_NAME}")
    c_cnt, l_cnt = c.fetchone()
    last_date = get_latest_contract_date(db_path)
    
    c.execute(f"SELECT sync_id, started_at, completed_at, from_date, to_date, new_contracts_count, new_lots_count, status, details FROM {SYNC_HISTORY_TABLE} ORDER BY sync_id DESC LIMIT 10")
    history = []
    for r in c.fetchall():
        history.append({
            "sync_id": r[0],
            "started_at": r[1],
            "completed_at": r[2],
            "from_date": r[3],
            "to_date": r[4],
            "new_contracts": r[5],
            "new_lots": r[6],
            "status": r[7],
            "details": r[8]
        })
    conn.close()
    
    is_running = any(h['status'] == 'running' for h in history[:1])
    return {
        "db_path": db_path,
        "total_contracts": c_cnt,
        "total_lots": l_cnt,
        "latest_contract_date": str(last_date) if last_date else None,
        "is_sync_running": is_running,
        "recent_syncs": history
    }

def save_lots(lots: List[Dict[str, Any]], db_path: str = DB_PATH):
    if not lots:
        return
    for attempt in range(10):
        try:
            conn = get_db_connection(db_path)
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
            time.sleep(2)

def update_progress(start_date: str, end_date: str, method_tag: str, status: str, contracts_count: int, lots_count: int, db_path: str = DB_PATH):
    for attempt in range(10):
        try:
            conn = get_db_connection(db_path)
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
        except Exception:
            time.sleep(2)

def is_interval_completed(start_date: str, end_date: str, method_tag: str = 'all', db_path: str = DB_PATH) -> bool:
    for attempt in range(10):
        try:
            conn = get_db_connection(db_path)
            c = conn.cursor()
            c.execute(f"SELECT status FROM {PROGRESS_TABLE} WHERE interval_start=? AND interval_end=? AND method_tag=?", (start_date, end_date, method_tag))
            row = c.fetchone()
            conn.close()
            return row is not None and row[0] == 'completed'
        except Exception:
            time.sleep(1)
    return False

def get_existing_contract_ids(db_path: str = DB_PATH) -> set:
    for attempt in range(10):
        try:
            conn = get_db_connection(db_path)
            c = conn.cursor()
            c.execute(f"SELECT DISTINCT contract_id FROM {TABLE_NAME}")
            res = set(row[0] for row in c.fetchall())
            conn.close()
            return res
        except Exception:
            time.sleep(1)
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
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(4)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
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
                            k, v = cols[0].text.strip(), cols[1].text.strip()
                            if 'БИН' in k: c_bin = v
                            elif 'Наименование заказчика' in k: c_name = v
                if len(tables) > 1:
                    for row in tables[1].find_all('tr'):
                        cols = row.find_all(['th', 'td'])
                        if len(cols) == 2:
                            k, v = cols[0].text.strip(), cols[1].text.strip()
                            if 'БИН' in k or 'ИИН' in k:
                                if v: s_bin = v
                            elif 'Наименование поставщика' in k: s_name = v
                return c_name, c_bin, s_name, s_bin
        except Exception:
            await asyncio.sleep(4)
    return "", "", "", ""

async def fetch_lots(session: aiohttp.ClientSession, contract_id: str) -> List[Dict[str, Any]]:
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{contract_id}"
    for attempt in range(100):
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 429:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(4)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
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
        except Exception:
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
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(10)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                    
                soup = BeautifulSoup(html, 'html.parser')
                tables = soup.find_all('table')
                if len(tables) < 2:
                    if "Записей не найдено" in html or "Ничего не найдено" in html or "нет данных" in html.lower():
                        return html, [], soup
                    await asyncio.sleep(5)
                    continue
                return html, tables, soup
        except Exception:
            await asyncio.sleep(15)
    return "", [], None

async def scrape_slice(session: aiohttp.ClientSession, sem: asyncio.Semaphore, start_date: str, end_date: str, methods: list, existing_ids: set, method_tag: str = 'all', db_path: str = DB_PATH) -> Tuple[int, int]:
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    
    params = [
        ('filter[ref_subject_type]', '1'),
        ('count_record', '2000'),
        ('filter[start_date_from]', start_date),
        ('filter[start_date_to]', end_date),
    ]
    for s in STATUS_CODES: params.append(('filter[status][]', s))
    for m in methods: params.append(('filter[method][]', m))
        
    html_p1, tables_p1, soup_p1 = await fetch_registry_page(session, params, 1)
    if not html_p1 or len(tables_p1) < 2:
        update_progress(start_date, end_date, method_tag, 'completed', 0, 0, db_path)
        return 0, 0
        
    total_records = parse_total_records(html_p1)
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Query {start_date}..{end_date} (tag: {method_tag}): Portal reports {total_records} contracts.", flush=True)
    
    if total_records >= CAP_THRESHOLD:
        days_diff = (end_dt - start_dt).days
        if days_diff > 0:
            mid_days = days_diff // 2
            mid_dt = start_dt + datetime.timedelta(days=mid_days)
            mid_next_dt = mid_dt + datetime.timedelta(days=1)
            
            s1_start, s1_end = start_dt.strftime("%Y-%m-%d"), mid_dt.strftime("%Y-%m-%d")
            s2_start, s2_end = mid_next_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
            
            c1, l1 = await scrape_slice(session, sem, s1_start, s1_end, methods, existing_ids, method_tag, db_path)
            c2, l2 = await scrape_slice(session, sem, s2_start, s2_end, methods, existing_ids, method_tag, db_path)
            
            update_progress(start_date, end_date, method_tag, 'completed', c1 + c2, l1 + l2, db_path)
            return c1 + c2, l1 + l2
        else:
            if len(methods) > 1:
                total_c_sub, total_l_sub = 0, 0
                for m_code in methods:
                    tag_m = f"m_{m_code}"
                    c_m, l_m = await scrape_slice(session, sem, start_date, end_date, [m_code], existing_ids, tag_m, db_path)
                    total_c_sub += c_m
                    total_l_sub += l_m
                update_progress(start_date, end_date, method_tag, 'completed', total_c_sub, total_l_sub, db_path)
                return total_c_sub, total_l_sub

    update_progress(start_date, end_date, method_tag, 'in_progress', 0, 0, db_path)
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
                    save_lots(chunk_lots, db_path)
                    total_lots_slice += len(chunk_lots)
                total_contracts_slice += len(chunk)
                
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{start_date}..{end_date}] Page {page} [{min(i + CHUNK_SIZE, len(contracts_to_fetch))}/{len(contracts_to_fetch)}]: +{len(chunk_lots)} lots (slice total: {total_lots_slice})", flush=True)
                
        pagination = soup.find('ul', class_='pagination')
        has_next = False
        if pagination:
            next_link = pagination.find('a', href=re.compile(rf'page={page+1}'))
            if next_link:
                has_next = True
        if not has_next:
            break
        page += 1
        
    update_progress(start_date, end_date, method_tag, 'completed', total_contracts_slice, total_lots_slice, db_path)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] <<< Completed slice {start_date}..{end_date}: {total_contracts_slice} contracts, {total_lots_slice} lots saved.\n", flush=True)
    return total_contracts_slice, total_lots_slice

async def run_weekly_update(db_path: str = DB_PATH, concurrency: int = 30, force_start: Optional[str] = None, force_end: Optional[str] = None) -> Dict[str, Any]:
    init_tables(db_path)
    
    today = datetime.date.today()
    if force_end:
        end_date = datetime.datetime.strptime(force_end, "%Y-%m-%d").date()
    else:
        end_date = today

    if force_start:
        start_date = datetime.datetime.strptime(force_start, "%Y-%m-%d").date()
    else:
        last_date = get_latest_contract_date(db_path)
        if last_date:
            # Overlap by 1 day to catch any late contracts created on that date
            start_date = last_date - datetime.timedelta(days=1)
        else:
            start_date = datetime.date(2024, 1, 1)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    print(f"\n=======================================================")
    print(f"🚀 GOSZAKUP WEEKLY AUTO-UPDATE")
    print(f"Database: {db_path}")
    print(f"Target Period: {start_str} -> {end_str}")
    print(f"Concurrency: {concurrency} workers | 429 Backoff: {RETRY_PAUSE_429}s")
    print(f"=======================================================\n", flush=True)
    
    if start_date >= end_date:
        print(f"✅ Database is already up to date (last date in DB: {start_date}, today: {end_date}). No update needed.")
        return {
            "status": "up_to_date",
            "from_date": start_str,
            "to_date": end_str,
            "new_contracts": 0,
            "new_lots": 0,
            "message": "Database is already up to date"
        }

    sync_id = record_sync_start(start_str, end_str, db_path)
    existing_ids = get_existing_contract_ids(db_path)
    print(f"Loaded {len(existing_ids):,} existing contract IDs from DB for deduplication.", flush=True)
    
    intervals = []
    curr = start_date
    while curr <= end_date:
        nxt_end = min(curr + datetime.timedelta(days=4), end_date)
        intervals.append((curr.strftime("%Y-%m-%d"), nxt_end.strftime("%Y-%m-%d")))
        curr = nxt_end + datetime.timedelta(days=1)
        
    print(f"Generated {len(intervals)} intervals of 5 days.", flush=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru,en-US;q=0.9,en;q=0.8'
    }
    
    conn_tcp = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(concurrency)
    
    total_new_contracts = 0
    total_new_lots = 0
    
    try:
        async with aiohttp.ClientSession(headers=headers, connector=conn_tcp) as session:
            for idx, (i_start, i_end) in enumerate(intervals, 1):
                print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Processing interval {idx}/{len(intervals)}: {i_start} -> {i_end}")
                c_cnt, l_cnt = await scrape_slice(session, sem, i_start, i_end, ALL_METHOD_CODES, existing_ids, 'all', db_path)
                total_new_contracts += c_cnt
                total_new_lots += l_cnt
                
        record_sync_complete(sync_id, total_new_contracts, total_new_lots, 'success', f"Synced {total_new_contracts} contracts, {total_new_lots} lots", db_path)
        print(f"\n🎉 Weekly update completed successfully! +{total_new_contracts:,} contracts, +{total_new_lots:,} lots added.")
        return {
            "status": "success",
            "sync_id": sync_id,
            "from_date": start_str,
            "to_date": end_str,
            "new_contracts": total_new_contracts,
            "new_lots": total_new_lots
        }
    except Exception as e:
        record_sync_complete(sync_id, total_new_contracts, total_new_lots, 'error', str(e), db_path)
        print(f"❌ Error during weekly update: {e}")
        return {
            "status": "error",
            "sync_id": sync_id,
            "from_date": start_str,
            "to_date": end_str,
            "error": str(e),
            "new_contracts": total_new_contracts,
            "new_lots": total_new_lots
        }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Goszakup Weekly Auto-Updater")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--concurrency", type=int, default=30, help="Number of concurrent sessions (default 30)")
    parser.add_argument("--start", type=str, default=None, help="Force specific start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="Force specific end date (YYYY-MM-DD)")
    parser.add_argument("--check-only", action="store_true", help="Only check latest date without updating")
    args = parser.parse_args()
    
    if args.check_only:
        status = get_sync_status(args.db)
        print(f"Database: {status['db_path']}")
        print(f"Total Contracts: {status['total_contracts']:,}")
        print(f"Total Lots: {status['total_lots']:,}")
        print(f"Latest Contract Date: {status['latest_contract_date']}")
        print(f"Sync Currently Running: {status['is_sync_running']}")
        sys.exit(0)
        
    asyncio.run(run_weekly_update(db_path=args.db, concurrency=args.concurrency, force_start=args.start, force_end=args.end))
