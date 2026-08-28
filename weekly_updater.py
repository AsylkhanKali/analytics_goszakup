#!/usr/bin/env python3
"""
Goszakup Weekly Auto-Updater (Production Engine)
------------------------------------------------
1. Automatically finds the latest contract date in the SQLite DB (e.g., June 16).
2. Steps back 2 days (e.g., June 14) to catch any late registrations or timestamp cutoffs.
3. Loads all existing contract IDs into memory for strict deduplication.
4. Generates 5-day intervals up to today with 1-day boundary overlaps.
5. Employs 10k threshold protection with dynamic interval subdivision.
6. Uses Selectolax C-Fast Parser and concurrent sub-requests for high throughput.
7. Logs sync checkpoints to sync_history table and triggers SQLite WAL checkpoints.
"""

import asyncio
import aiohttp
from selectolax.parser import HTMLParser
import sqlite3
import os
import re
import sys
import time
import datetime
import argparse
from typing import List, Dict, Any, Tuple, Optional

# Database auto-resolution
DEFAULT_DB_LOCATIONS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'all_data', 'goszakup_2024_2026_final.db'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data parser for deals', 'Data_new_session', 'data', 'goszakup_2024_2026.db'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'goszakup.db')
]

def resolve_default_db() -> str:
    env_db = os.getenv('GOSZAKUP_DB_PATH')
    if env_db and os.path.exists(env_db):
        return env_db
    candidates = []
    for p in DEFAULT_DB_LOCATIONS:
        if os.path.exists(p):
            try:
                sz = os.path.getsize(p)
                candidates.append((sz, p))
            except Exception:
                pass
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return DEFAULT_DB_LOCATIONS[0]

DB_PATH = resolve_default_db()
TABLE_NAME = 'contracts_lots'
PROGRESS_TABLE = 'intervals_progress'
SYNC_HISTORY_TABLE = 'sync_history'

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
        VALUES (?, ?, ?, 'running', 'Weekly sync in progress')
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

def get_existing_contract_ids(db_path: str = DB_PATH, since_date: Optional[str] = None) -> set:
    for attempt in range(10):
        try:
            conn = get_db_connection(db_path)
            c = conn.cursor()
            if since_date:
                c.execute(f"SELECT DISTINCT contract_id FROM {TABLE_NAME} WHERE contract_date >= ?", (since_date,))
            else:
                c.execute(f"SELECT DISTINCT contract_id FROM {TABLE_NAME}")
            res = set(row[0] for row in c.fetchall())
            conn.close()
            return res
        except Exception:
            time.sleep(1)
    return set()

def parse_total_records_fast(html: str) -> int:
    tree = HTMLParser(html)
    info = tree.css_first('div.dataTables_info')
    if info:
        m = re.search(r'из\s+([\d\s]+)\s+запис', info.text())
        if m:
            return int(m.group(1).replace(' ', ''))
    return 0

async def fetch_customer_supplier_fast(session: aiohttp.ClientSession, contract_id: str) -> Tuple[str, str, str, str]:
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/customer_n_supplier/{contract_id}"
    for attempt in range(50):
        try:
            async with session.get(url, timeout=25) as resp:
                if resp.status == 429:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                
                tree = HTMLParser(html)
                tables = tree.css('table')
                c_bin, c_name = "", ""
                s_bin, s_name = "", ""
                
                if len(tables) > 0:
                    for row in tables[0].css('tr'):
                        cols = row.css('th, td')
                        if len(cols) == 2:
                            k = cols[0].text(strip=True)
                            v = cols[1].text(strip=True)
                            if 'БИН' in k: c_bin = v
                            elif 'Наименование заказчика' in k: c_name = v
                if len(tables) > 1:
                    for row in tables[1].css('tr'):
                        cols = row.css('th, td')
                        if len(cols) == 2:
                            k = cols[0].text(strip=True)
                            v = cols[1].text(strip=True)
                            if 'БИН' in k or 'ИИН' in k:
                                if v: s_bin = v
                            elif 'Наименование поставщика' in k: s_name = v
                return c_name, c_bin, s_name, s_bin
        except Exception:
            await asyncio.sleep(2)
    return "", "", "", ""

async def fetch_lots_fast(session: aiohttp.ClientSession, contract_id: str) -> List[Dict[str, Any]]:
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{contract_id}"
    for attempt in range(50):
        try:
            async with session.get(url, timeout=25) as resp:
                if resp.status == 429:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                
                tree = HTMLParser(html)
                table = tree.css_first('table')
                if not table:
                    return []
                
                lots = []
                for row in table.css('tr'):
                    cols = row.css('td, th')
                    if len(cols) >= 9 and cols[0].tag != 'th':
                        lot_id = cols[1].text(strip=True)
                        title = cols[4].text(strip=True)
                        if title == "Наименование":
                            continue
                        try:
                            quantity = float(cols[5].text(strip=True).replace(' ', '').replace(',', '.'))
                            unit_price = float(cols[7].text(strip=True).replace(' ', '').replace(',', '.'))
                            amount = float(cols[8].text(strip=True).replace(' ', '').replace(',', '.'))
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
            await asyncio.sleep(2)
    return []

async def process_contract_fast(session: aiohttp.ClientSession, sem: asyncio.Semaphore, contract: dict) -> List[Dict[str, Any]]:
    async with sem:
        cust_supp_task = fetch_customer_supplier_fast(session, contract['contract_id'])
        units_task = fetch_lots_fast(session, contract['contract_id'])
        
        (c_name, c_bin, s_name, s_bin), lots = await asyncio.gather(cust_supp_task, units_task)
        
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

async def fetch_registry_page_fast(session: aiohttp.ClientSession, params: list, page: int) -> Tuple[str, List[Any], Any]:
    params_with_page = list(params) + [('page', str(page))]
    for retry in range(50):
        try:
            async with session.get('https://goszakup.gov.kz/ru/registry/contract', params=params_with_page, timeout=35) as resp:
                if resp.status == 429:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                if resp.status != 200:
                    await asyncio.sleep(5)
                    continue
                html = await resp.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    await asyncio.sleep(RETRY_PAUSE_429)
                    continue
                    
                tree = HTMLParser(html)
                tables = tree.css('table')
                if len(tables) < 2:
                    if "Записей не найдено" in html or "Ничего не найдено" in html or "нет данных" in html.lower():
                        return html, [], tree
                    await asyncio.sleep(3)
                    continue
                return html, tables, tree
        except Exception:
            await asyncio.sleep(8)
    return "", [], None

async def scrape_slice_incremental(session: aiohttp.ClientSession, sem: asyncio.Semaphore, start_date: str, end_date: str, methods: list, existing_ids: set, db_path: str = DB_PATH) -> Tuple[int, int]:
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
        
    html_p1, tables_p1, tree_p1 = await fetch_registry_page_fast(session, params, 1)
    if not html_p1 or len(tables_p1) < 2:
        return 0, 0
        
    total_records = parse_total_records_fast(html_p1)
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Query {start_date}..{end_date}: Portal reports {total_records:,} contracts.", flush=True)
    
    # 10k Cap Protection: subdivide recursively if >= CAP_THRESHOLD
    if total_records >= CAP_THRESHOLD:
        days_diff = (end_dt - start_dt).days
        if days_diff > 1:
            mid_days = days_diff // 2
            mid_dt = start_dt + datetime.timedelta(days=mid_days)
            
            s1_start, s1_end = start_dt.strftime("%Y-%m-%d"), mid_dt.strftime("%Y-%m-%d")
            s2_start, s2_end = mid_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
            
            print(f"⚡ [CAP PROTECTION] Subdividing [{start_date}..{end_date}] -> [{s1_start}..{s1_end}] and [{s2_start}..{s2_end}]", flush=True)
            c1, l1 = await scrape_slice_incremental(session, sem, s1_start, s1_end, methods, existing_ids, db_path)
            c2, l2 = await scrape_slice_incremental(session, sem, s2_start, s2_end, methods, existing_ids, db_path)
            return c1 + c2, l1 + l2
        else:
            if len(methods) > 1:
                print(f"⚡ [CAP PROTECTION] Subdividing {start_date} by individual procurement methods...", flush=True)
                total_c_sub, total_l_sub = 0, 0
                for m_code in methods:
                    c_m, l_m = await scrape_slice_incremental(session, sem, start_date, end_date, [m_code], existing_ids, db_path)
                    total_c_sub += c_m
                    total_l_sub += l_m
                return total_c_sub, total_l_sub

    page = 1
    total_new_contracts = 0
    total_new_lots = 0
    
    while True:
        if page == 1:
            tables = tables_p1
            tree = tree_p1
        else:
            _, tables, tree = await fetch_registry_page_fast(session, params, page)
            
        if len(tables) < 2:
            break
            
        table = tables[1]
        tbody = table.css_first('tbody')
        rows = tbody.css('tr') if tbody else []
        if not rows:
            break
            
        to_fetch = []
        for r in rows:
            cols = r.css('td')
            if len(cols) >= 10:
                cid = cols[0].text(strip=True)
                # DEDUPLICATION: Only process contract if NOT already in DB
                if cid not in existing_ids:
                    c_num_tag = cols[1].css_first('a')
                    c_num = c_num_tag.text(strip=True) if c_num_tag else cols[1].text(strip=True)
                    try:
                        amt = float(cols[6].text(strip=True).replace(' ', '').replace(',', '.'))
                    except Exception:
                        amt = 0.0
                    to_fetch.append({
                        'contract_id': cid,
                        'contract_number': c_num,
                        'purchase_number': cols[2].text(strip=True),
                        'contract_type': cols[3].text(strip=True),
                        'contract_status': cols[4].text(strip=True),
                        'contract_date': cols[5].text(strip=True),
                        'amount_raw': amt,
                        'customer_reg': cols[7].text(strip=True),
                        'supplier_reg': cols[8].text(strip=True),
                        'purchase_method': cols[9].text(strip=True),
                    })
                    existing_ids.add(cid)
                    
        if to_fetch:
            CHUNK_SIZE = 100
            for i in range(0, len(to_fetch), CHUNK_SIZE):
                chunk = to_fetch[i:i + CHUNK_SIZE]
                tasks = [process_contract_fast(session, sem, c) for c in chunk]
                batch_res = await asyncio.gather(*tasks)
                
                chunk_lots = []
                for res in batch_res:
                    if res: chunk_lots.extend(res)
                    
                if chunk_lots:
                    save_lots(chunk_lots, db_path)
                    total_new_lots += len(chunk_lots)
                total_new_contracts += len(chunk)
                
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{start_date}..{end_date}] Page {page} [{min(i + CHUNK_SIZE, len(to_fetch))}/{len(to_fetch)}]: +{len(chunk_lots)} lots (Slice total: {total_new_lots})", flush=True)
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{start_date}..{end_date}] Page {page}: all {len(rows)} contracts already exist in DB.", flush=True)
            
        pagination = tree.css_first('ul.pagination')
        has_next = False
        if pagination:
            for a_tag in pagination.css('a'):
                href = a_tag.attributes.get('href', '')
                if f'page={page+1}' in href:
                    has_next = True
                    break
        if not has_next:
            break
        page += 1
        
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] <<< Completed slice {start_date}..{end_date}: +{total_new_contracts:,} contracts, +{total_new_lots:,} lots added.\n", flush=True)
    return total_new_contracts, total_new_lots

async def run_weekly_update(db_path: str = DB_PATH, concurrency: int = 60, force_start: Optional[str] = None, force_end: Optional[str] = None, overlap_days: int = 8) -> Dict[str, Any]:
    init_tables(db_path)
    
    today = datetime.date.today()
    if force_end:
        end_date = datetime.datetime.strptime(force_end, "%Y-%m-%d").date()
    else:
        # Step forward 1 day so filter[start_date_to] includes all contracts signed today
        end_date = today + datetime.timedelta(days=1)

    if force_start:
        start_date = datetime.datetime.strptime(force_start, "%Y-%m-%d").date()
    else:
        last_date = get_latest_contract_date(db_path)
        if last_date:
            # Step back by overlap_days (default 2 days) to catch late additions on the boundary
            start_date = max(datetime.date(2024, 1, 1), last_date - datetime.timedelta(days=overlap_days))
        else:
            start_date = datetime.date(2024, 1, 1)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    print("=" * 65)
    print(f"🚀 GOSZAKUP WEEKLY INCREMENTAL AUTO-UPDATE")
    print(f"Target Database: {db_path}")
    print(f"Latest Date in DB: {get_latest_contract_date(db_path)}")
    print(f"Scan Window: {start_str} (including -{overlap_days}d overlap) -> {end_str}")
    print(f"Concurrency: {concurrency} workers | 429 Pause: {RETRY_PAUSE_429}s")
    print("=" * 65 + "\n", flush=True)
    
    if start_date >= end_date and (end_date - start_date).days == 0:
        print(f"✅ Database is already up to date ({start_date}). No update needed.")
        return {
            "status": "up_to_date",
            "from_date": start_str,
            "to_date": end_str,
            "new_contracts": 0,
            "new_lots": 0,
            "message": "Database is already up to date"
        }

    sync_id = record_sync_start(start_str, end_str, db_path)
    existing_ids = get_existing_contract_ids(db_path, since_date=start_str)
    print(f"Loaded {len(existing_ids):,} contract IDs (since {start_str}) into memory for fast deduplication.", flush=True)
    
    # Generate 5-day intervals with 1-day boundary overlap (curr = nxt_end)
    intervals = []
    curr = start_date
    while curr < end_date:
        nxt_end = min(curr + datetime.timedelta(days=5), end_date)
        intervals.append((curr.strftime("%Y-%m-%d"), nxt_end.strftime("%Y-%m-%d")))
        curr = nxt_end  # Boundary overlap
        if curr >= end_date:
            break
            
    if not intervals:
        intervals.append((start_str, end_str))
        
    print(f"Generated {len(intervals)} intervals with boundary overlap protection.", flush=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru,en-US;q=0.9,en;q=0.8'
    }
    
    conn_tcp = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=600, keepalive_timeout=75)
    sem = asyncio.Semaphore(concurrency)
    
    total_new_contracts = 0
    total_new_lots = 0
    
    try:
        async with aiohttp.ClientSession(headers=headers, connector=conn_tcp) as session:
            for idx, (i_start, i_end) in enumerate(intervals, 1):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Processing interval {idx}/{len(intervals)}: {i_start} -> {i_end}")
                c_cnt, l_cnt = await scrape_slice_incremental(session, sem, i_start, i_end, ALL_METHOD_CODES, existing_ids, db_path)
                total_new_contracts += c_cnt
                total_new_lots += l_cnt
                
        record_sync_complete(sync_id, total_new_contracts, total_new_lots, 'success', f"Synced +{total_new_contracts:,} contracts, +{total_new_lots:,} lots", db_path)
        print("\n" + "=" * 65)
        print(f"🎉 WEEKLY UPDATE COMPLETED SUCCESSFULLY!")
        print(f"Total new unique contracts added: +{total_new_contracts:,}")
        print(f"Total new lots added: +{total_new_lots:,}")
        print(f"Database: {db_path}")
        print("=" * 65 + "\n", flush=True)
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
        print(f"❌ Error during weekly update: {e}", flush=True)
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
    parser = argparse.ArgumentParser(description="Goszakup Weekly Incremental Updater")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--concurrency", type=int, default=60, help="Concurrency (default 60)")
    parser.add_argument("--start", type=str, default=None, help="Force specific start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="Force specific end date (YYYY-MM-DD)")
    parser.add_argument("--overlap-days", type=int, default=8, help="Days to step back from latest date (default 8)")
    parser.add_argument("--check-only", action="store_true", help="Only check status without running update")
    args = parser.parse_args()
    
    if args.check_only:
        status = get_sync_status(args.db)
        print(f"Database: {status['db_path']}")
        print(f"Total Unique Contracts: {status['total_contracts']:,}")
        print(f"Total Lots: {status['total_lots']:,}")
        print(f"Latest Contract Date: {status['latest_contract_date']}")
        print(f"Sync Currently Running: {status['is_sync_running']}")
        if status['recent_syncs']:
            print("\nRecent Sync History:")
            for s in status['recent_syncs'][:5]:
                print(f" - [{s['started_at']} -> {s['completed_at']}] {s['from_date']}..{s['to_date']}: +{s['new_contracts']} contracts, +{s['new_lots']} lots ({s['status']})")
        sys.exit(0)
        
    asyncio.run(run_weekly_update(db_path=args.db, concurrency=args.concurrency, force_start=args.start, force_end=args.end, overlap_days=args.overlap_days))
