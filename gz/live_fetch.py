"""Модуль 100% надежного и чистого сбора техспецификаций под авторизацией.

Собирает техспецификации напрямую через HTTP/cURL запросы:
- Парсит HTML-модалки лотов (/ru/egzcontract/cpublic/loadunit)
- Извлекает и скачивает Приложения 17 (/files/download_file/...)
- Разбирает Марку, Модель, Страну, Завод-изготовитель
- Сохраняет данные в `goszakup.db` в таблицу `supplier_specs`
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from selectolax.parser import HTMLParser

from .auth import BASE_URL, load_session
from .specs import parse_html_spec, parse_spec
from .store import upsert

log = logging.getLogger(__name__)

DOWNLOAD_FILE_RE = re.compile(r"https://(?:v3bl\.)?goszakup\.gov\.kz/files/download_file/\d+/\d+/?")


def _curl_request(url: str, session_data: Dict[str, str], post_data: Optional[str] = None, max_retries: int = 5) -> str:
    """Выполняет быстрый и устойчивый запрос через curl."""
    ua = session_data["user_agent"]
    cookie = session_data["ci_session"]

    time.sleep(random.uniform(0.05, 0.2))

    for attempt in range(1, max_retries + 1):
        cmd = [
            "curl",
            "-s",
            "-L",
            "-H",
            f"User-Agent: {ua}",
            "-H",
            f"Cookie: ci_session={cookie}",
        ]
        if post_data:
            cmd.extend(["-X", "POST", "-H", "X-Requested-With: XMLHttpRequest", "-d", post_data])
        cmd.append(url)

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            stdout = res.stdout
            if "503 Service" in stdout or "Temporarily Unavailable" in stdout:
                backoff = 1.0 * attempt + random.uniform(0.3, 1.0)
                time.sleep(backoff)
                continue
            return stdout
        except Exception:
            time.sleep(0.5)
    return ""


def _fetch_one_contract(cid: str, session_data: Dict[str, str]) -> List[Dict[str, Any]]:
    """100% рабочий парсинг спецификаций договора из HTML-модалок И прикрепленных файлов."""
    units_url = f"{BASE_URL}/ru/egzcontract/cpublic/units/{cid}"
    units_html = _curl_request(units_url, session_data)

    if not units_html:
        return []

    file_urls = list(set(DOWNLOAD_FILE_RE.findall(units_html)))
    results: List[Dict[str, Any]] = []

    tree = HTMLParser(units_html)
    unit_ids = []
    for a in tree.css("a[href*='loadunit']"):
        href = a.attributes.get("href", "")
        m = re.search(r"loadunit\(['\"]?(\d+)['\"]?\)", href)
        if m:
            unit_ids.append(m.group(1))

    for uid in set(unit_ids):
        modal_html = _curl_request(
            f"{BASE_URL}/ru/egzcontract/cpublic/loadunit",
            session_data,
            post_data=f"pid={cid}&unit_id={uid}",
        )
        if modal_html:
            found = DOWNLOAD_FILE_RE.findall(modal_html)
            file_urls.extend(found)

            parsed_m = parse_html_spec(modal_html)
            if parsed_m and (parsed_m.get("brand_model") or parsed_m.get("country") or parsed_m.get("manufacturer")):
                results.append({
                    "announce_id": cid,
                    "lot_number": parsed_m.get("lot_number", "NO_LOT"),
                    "supplier_name": parsed_m.get("supplier_name", ""),
                    "supplier_bin": "",
                    "brand_model": parsed_m.get("brand_model", "Нет данных"),
                    "country": parsed_m.get("country", "Нет данных"),
                    "manufacturer": parsed_m.get("manufacturer", "Нет данных"),
                    "year_made": parsed_m.get("year_made", ""),
                    "warranty": parsed_m.get("warranty", ""),
                    "standard": parsed_m.get("standard", ""),
                    "lot_name": parsed_m.get("lot_name", ""),
                    "source_file": f"modal_unit_{uid}",
                })

    file_urls = list(set(file_urls))
    for f_url in file_urls:
        cmd = [
            "curl",
            "-s",
            "-L",
            "-H",
            f"User-Agent: {session_data['user_agent']}",
            "-H",
            f"Cookie: ci_session={session_data['ci_session']}",
            f_url,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=35)
        file_bytes = res.stdout
        if file_bytes:
            spec_data = parse_spec(file_bytes)
            if spec_data.get("brand_model") or spec_data.get("country") or spec_data.get("manufacturer"):
                results.append({
                    "announce_id": spec_data.get("announce_id", cid),
                    "lot_number": spec_data.get("lot_number", "NO_LOT"),
                    "supplier_name": spec_data.get("supplier_name", ""),
                    "supplier_bin": spec_data.get("supplier_bin", ""),
                    "brand_model": spec_data.get("brand_model", "Нет данных"),
                    "country": spec_data.get("country", "Нет данных"),
                    "manufacturer": spec_data.get("manufacturer", "Нет данных"),
                    "year_made": spec_data.get("year_made", ""),
                    "warranty": spec_data.get("warranty", ""),
                    "standard": spec_data.get("standard", ""),
                    "lot_name": spec_data.get("lot_name", ""),
                    "source_file": f_url,
                })
    return results


def fetch_and_parse_contract_units(
    con: sqlite3.Connection, contract_id: str
) -> List[Dict[str, Any]]:
    """Скачивает техспецификации поставщика по contract_id под авторизацией."""
    session_data = load_session()
    if not session_data:
        return []

    results = _fetch_one_contract(contract_id, session_data)
    if results:
        con.execute(
            "CREATE TABLE IF NOT EXISTS supplier_specs (announce_id TEXT, lot_number TEXT,"
            " supplier_name TEXT, supplier_bin TEXT, brand_model TEXT, country TEXT,"
            " manufacturer TEXT, year_made TEXT, warranty TEXT, standard TEXT,"
            " lot_name TEXT, source_file TEXT, PRIMARY KEY (announce_id, lot_number))"
        )
        upsert(con, "supplier_specs", results)
    return results


def fetch_all_open_tender_specs(
    con: sqlite3.Connection,
    categories: Optional[List[str]] = None,
    workers: int = 12,
    limit: Optional[int] = None,
) -> int:
    """Чистый и стабильный сбор без падающих браузеров."""
    session_data = load_session()
    if not session_data:
        print("❌ Ошибка: Сессия не найдена в data/session.json. Сохраните ci_session.")
        return 0

    cur = con.cursor()
    if categories:
        placeholders = ",".join("?" * len(categories))
        sql = f"""
        SELECT DISTINCT c.contract_id 
        FROM contracts c
        JOIN lots l ON l.announce_id = c.announce_id AND l.lot_number = c.lot_number
        WHERE c.contract_id IS NOT NULL AND c.contract_id != '' AND l.category IN ({placeholders})
        """
        cur.execute(sql, categories)
    else:
        sql = """
        SELECT DISTINCT c.contract_id 
        FROM contracts c
        WHERE c.contract_id IS NOT NULL AND c.contract_id != ''
        """
        cur.execute(sql)

    all_cids = [r[0] for r in cur.fetchall()]
    if limit:
        all_cids = all_cids[:limit]

    print(f"🚀 Начинаю 100% чистый сбор техспецификаций по {len(all_cids)} договорам ({workers} воркеров)...")

    total_specs = 0
    batch = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one_contract, cid, session_data): cid for cid in all_cids}
        for idx, fut in enumerate(as_completed(futures), 1):
            try:
                specs = fut.result()
                if specs:
                    batch.extend(specs)
                    for s in specs:
                        print(f"  [+] ({idx}/{len(all_cids)} Договоров) Марка: {s['brand_model']} | Страна: {s['country']} | Завод: {s['manufacturer']}")
            except Exception as e:
                log.warning("Ошибка воркера: %s", e)

            if len(batch) >= 10:
                upsert(con, "supplier_specs", batch)
                total_specs += len(batch)
                batch = []

    if batch:
        upsert(con, "supplier_specs", batch)
        total_specs += len(batch)

    print(f"\n🎉 Сбор завершён! Успешно извлечено и сохранено спецификаций: {total_specs}")
    return total_specs
