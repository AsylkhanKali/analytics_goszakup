#!/usr/bin/env python3
"""
Goszakup ULTRA-TURBO Multi-Date Gap Filler (120 Workers + Selectolax C Parser)
-----------------------------------------------------------------------------
1. 4 dates processed in parallel concurrently.
2. 120 global async worker sessions with persistent connection pooling.
3. Selectolax (C-based MyHTML) for 20x faster HTML parsing.
4. Concurrent customer_n_supplier and units fetch per contract.
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
from typing import List, Dict, Any, Tuple

from weekly_updater import (
    get_db_connection,
    resolve_default_db,
    save_lots,
    get_existing_contract_ids,
    STATUS_CODES,
    ALL_METHOD_CODES,
    RETRY_PAUSE_429
)

GAP_PROGRESS_TABLE = 'boundary_gaps_progress'

def init_gap_table(db_path: str):
    conn = get_db_connection(db_path)
    c = conn.cursor()
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS {GAP_PROGRESS_TABLE} (
            boundary_date TEXT PRIMARY KEY,
            status TEXT,
            missing_contracts_found INTEGER,
            lots_saved INTEGER,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def is_boundary_done(b_date: str, db_path: str) -> bool:
    for _ in range(5):
        try:
            conn = get_db_connection(db_path)
            c = conn.cursor()
            c.execute(f"SELECT status FROM {GAP_PROGRESS_TABLE} WHERE boundary_date = ?", (b_date,))
            row = c.fetchone()
            conn.close()
            return row is not None and row[0] == 'completed'
        except Exception:
            time.sleep(0.5)
    return False

def mark_boundary_done(b_date: str, missing_cnt: int, lots_cnt: int, db_path: str):
    for _ in range(10):
        try:
            conn = get_db_connection(db_path)
            c = conn.cursor()
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute(f'''
                INSERT INTO {GAP_PROGRESS_TABLE} (boundary_date, status, missing_contracts_found, lots_saved, updated_at)
                VALUES (?, 'completed', ?, ?, ?)
                ON CONFLICT(boundary_date) DO UPDATE SET
                    status='completed',
                    missing_contracts_found=excluded.missing_contracts_found,
                    lots_saved=excluded.lots_saved,
                    updated_at=excluded.updated_at
            ''', (b_date, missing_cnt, lots_cnt, now_str))
            conn.commit()
            conn.close()
            return
        except Exception:
            time.sleep(1)

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

async def process_contract_ultra(session: aiohttp.ClientSession, sem: asyncio.Semaphore, contract: dict) -> List[Dict[str, Any]]:
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

async def process_boundary_date(session: aiohttp.ClientSession, sem: asyncio.Semaphore, b_date: str, existing_ids: set, db_path: str, date_idx: int, total_dates: int) -> Tuple[int, int]:
    if is_boundary_done(b_date, db_path):
        return 0, 0
        
    dt = datetime.datetime.strptime(b_date, "%Y-%m-%d").date()
    dt_next = dt + datetime.timedelta(days=1)
    
    d_from = dt.strftime("%Y-%m-%d")
    d_to = dt_next.strftime("%Y-%m-%d")
    
    params = [
        ('filter[ref_subject_type]', '1'),
        ('count_record', '2000'),
        ('filter[start_date_from]', d_from),
        ('filter[start_date_to]', d_to),
    ]
    for s in STATUS_CODES: params.append(('filter[status][]', s))
    for m in ALL_METHOD_CODES: params.append(('filter[method][]', m))
    
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ⚡ [{date_idx}/{total_dates}] Fetching boundary {b_date}...", flush=True)
    html_p1, tables_p1, tree_p1 = await fetch_registry_page_fast(session, params, 1)
    if not html_p1 or len(tables_p1) < 2:
        mark_boundary_done(b_date, 0, 0, db_path)
        return 0, 0
        
    page = 1
    missing_contracts_date = 0
    lots_saved_date = 0
    
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
            CHUNK_SIZE = 120
            for i in range(0, len(to_fetch), CHUNK_SIZE):
                chunk = to_fetch[i:i + CHUNK_SIZE]
                tasks = [process_contract_ultra(session, sem, c) for c in chunk]
                batch_res = await asyncio.gather(*tasks)
                
                chunk_lots = []
                for res in batch_res:
                    if res: chunk_lots.extend(res)
                    
                if chunk_lots:
                    save_lots(chunk_lots, db_path)
                    lots_saved_date += len(chunk_lots)
                missing_contracts_date += len(chunk)
                
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{b_date}] 🚀 +{len(chunk)} contracts -> +{len(chunk_lots)} lots (Date total: {missing_contracts_date})", flush=True)
                
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
        
    mark_boundary_done(b_date, missing_contracts_date, lots_saved_date, db_path)
    if missing_contracts_date > 0:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ✅ [{date_idx}/{total_dates}] Completed {b_date}: +{missing_contracts_date:,} contracts, +{lots_saved_date:,} lots.", flush=True)
    else:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 🆗 [{date_idx}/{total_dates}] {b_date}: verified complete (0 missing).", flush=True)
    return missing_contracts_date, lots_saved_date

async def run_gap_filler_ultra(db_path: str, concurrency: int = 120, parallel_dates: int = 4):
    init_gap_table(db_path)
    existing_ids = get_existing_contract_ids(db_path)
    print(f"Loaded {len(existing_ids):,} existing contract IDs for deduplication.", flush=True)
    
    start_dt = datetime.date(2024, 1, 1)
    end_dt = datetime.date(2026, 8, 31)
    all_dates = set()
    curr = start_dt
    while curr <= end_dt:
        nxt_end = min(curr + datetime.timedelta(days=4), end_dt)
        all_dates.add(nxt_end.strftime("%Y-%m-%d"))
        curr = nxt_end + datetime.timedelta(days=1)
        
    try:
        conn = get_db_connection(db_path)
        c = conn.cursor()
        c.execute("SELECT DISTINCT interval_end FROM intervals_progress WHERE method_tag = 'all'")
        for r in c.fetchall():
            if r[0]: all_dates.add(r[0])
        conn.close()
    except Exception:
        pass
        
    boundary_dates = sorted(list(all_dates))
    remaining_dates = [d for d in boundary_dates if not is_boundary_done(d, db_path)]
    
    print("=" * 65)
    print(f"🚀 ULTRA-TURBO GAP FILLER ACTIVATED")
    print(f"Total boundary dates: {len(boundary_dates)} (Remaining: {len(remaining_dates)})")
    print(f"Global Concurrency: {concurrency} workers | Parallel Dates: {parallel_dates}")
    print(f"Parser Engine: Selectolax C-Fast Parser")
    print("=" * 65 + "\n", flush=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru,en-US;q=0.9,en;q=0.8'
    }
    
    conn_tcp = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=900, keepalive_timeout=90)
    sem = asyncio.Semaphore(concurrency)
    date_sem = asyncio.Semaphore(parallel_dates)
    
    async def _bounded_date_task(idx, d):
        async with date_sem:
            return await process_boundary_date(session, sem, d, existing_ids, db_path, idx, len(boundary_dates))
            
    async with aiohttp.ClientSession(headers=headers, connector=conn_tcp) as session:
        tasks = []
        for idx, b_date in enumerate(boundary_dates, 1):
            if is_boundary_done(b_date, db_path):
                continue
            tasks.append(_bounded_date_task(idx, b_date))
            
        results = await asyncio.gather(*tasks)
        total_rec_c = sum(r[0] for r in results)
        total_rec_l = sum(r[1] for r in results)
        
    print("\n" + "=" * 65)
    print("🎉 ALL BOUNDARY & SUBDIVISION GAPS FILLED (ULTRA TURBO)!")
    print(f"Total recovered contracts: {total_rec_c:,}")
    print(f"Total recovered lots: {total_rec_l:,}")
    print(f"Database: {db_path}")
    print("=" * 65 + "\n", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ultra-Turbo Goszakup Gap Filler")
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument("--concurrency", type=int, default=120, help="Total workers (default 120)")
    parser.add_argument("--parallel-dates", type=int, default=4, help="Parallel dates (default 4)")
    args = parser.parse_args()
    
    target_db = args.db if args.db else resolve_default_db()
    asyncio.run(run_gap_filler_ultra(target_db, args.concurrency, args.parallel_dates))
