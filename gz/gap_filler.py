"""Модуль молниеносной докачки недостающих договоров с портала по БИН.

Проверяет до 5 страниц (до 10 000 записей) по Заказчику и Поставщику.
Строго фильтрует только ОК и ЗЦП для предметов закупки ТОВАР (ref_subject_type=1).
Сразу вытягивает реальные наименования лотов со страницы units/<contract_id>.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Set, List, Tuple
from selectolax.parser import HTMLParser
from gz.auth import load_session

log = logging.getLogger(__name__)


def _fetch_single_page(url: str, ua: str, cookie: str):
    """Скачивает 1 страницу портала."""
    cmd = ["curl", "-s", "-L", "--max-time", "20", "-H", f"User-Agent: {ua}", "-H", f"Cookie: ci_session={cookie}", url]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=20)
        return res.stdout.decode("utf-8", "ignore")
    except Exception as e:
        log.warning(f"cURL error for {url}: {e}")
        return ""


def _fetch_units_for_contract(cid: str, ua: str, cookie: str) -> Tuple[str, List[Tuple[str, float, float]]]:
    """Скачивает детализированный список лотов по договору."""
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{cid}"
    cmd = ["curl", "-s", "-L", "--max-time", "15", "-H", f"User-Agent: {ua}", "-H", f"Cookie: ci_session={cookie}", url]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        html_str = res.stdout.decode("utf-8", "ignore")
        tree = HTMLParser(html_str)
        tables = tree.css("table")
        if not tables:
            return cid, []

        lots = []
        for tr in tables[0].css("tbody tr"):
            cells = [td.text(strip=True) for td in tr.css("td")]
            if len(cells) >= 8:
                title = cells[4]
                qty_str = cells[5].replace(" ", "").replace(",", ".")
                price_str = cells[7].replace(" ", "").replace(",", ".")
                qty = float(qty_str) if qty_str else 1.0
                price = float(price_str) if price_str else 0.0
                if title:
                    lots.append((title, price, qty))
        return cid, lots
    except Exception as e:
        log.warning(f"Error fetching units for CID {cid}: {e}")
        return cid, []


def sync_bin_contracts_from_portal(con: sqlite3.Connection, bin_code: str) -> Dict[str, int]:
    """Скачивает и вносит в базу все отсутствующие договоры по БИН с портала Госзакупок."""
    try:
        con.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass

    s = load_session()
    ua = s.get("user_agent", "Mozilla/5.0")
    cookie = s.get("ci_session", "")

    cur = con.cursor()

    # Считываем существующие contract_id в базе только для искомого БИН
    existing_cids: Set[str] = set(
        r[0] for r in cur.execute(
            "SELECT contract_id FROM contracts_lots WHERE (supplier_bin = ? OR customer_bin = ?) AND contract_id IS NOT NULL AND contract_id != ''",
            (bin_code, bin_code)
        ).fetchall()
    )

    # Формируем задачи с count_record=2000 для страниц 1..5 с фильтром по дате с 01.01.2024 и ref_subject_type=1 (Товары)
    tasks = []
    for role in ["customer", "supplier"]:
        for page in range(1, 6):
            url = f"https://goszakup.gov.kz/ru/registry/contract?filter%5B{role}%5D={bin_code}&filter%5Bstart_date_from%5D=01.01.2024&filter%5Bref_subject_type%5D=1&count_record=2000&page={page}"
            tasks.append((role, page, url))

    # Выполняем параллельно в 6 потоков
    html_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_to_task = {pool.submit(_fetch_single_page, url, ua, cookie): (role, page) for role, page, url in tasks}
        for future in as_completed(future_to_task):
            role, page = future_to_task[future]
            html_results[(role, page)] = future.result()

    new_contracts_dict = {}
    added_contracts = 0

    # Парсим ответы
    for (role, page), html_text in sorted(html_results.items(), key=lambda x: (x[0][0], x[0][1])):
        if not html_text or "table" not in html_text:
            continue

        tree = HTMLParser(html_text)
        tables = tree.css("table")
        if not tables:
            continue

        # Находим главную таблицу результатов
        main_table = tables[1] if len(tables) > 1 else tables[0]
        trs = main_table.css("tbody tr")
        if not trs:
            continue

        for tr in trs:
            cells = [td.text(strip=True) for td in tr.css("td")]
            if len(cells) < 8:
                continue

            c_id = cells[0]
            c_number = cells[1]
            c_status = cells[4] if len(cells) > 4 else "Действует"
            c_date = cells[5] if len(cells) > 5 else ""
            c_amount_str = cells[6] if len(cells) > 6 else "0"
            c_customer = cells[7] if len(cells) > 7 else ""
            c_supplier = cells[8] if len(cells) > 8 else ""
            c_method = cells[9] if len(cells) > 9 else "ОК"

            # Пропускаем расторгнутые, неисполненные, переданные и старые до 2024 года
            stat_l = c_status.lower()
            if "расторгнут" in stat_l or "не заключен" in stat_l or "не исполнен" in stat_l or "передан" in stat_l:
                continue
            if c_date and c_date < "2024-01-01":
                continue

            # Извлекаем БИНы из имен или из номера договора
            c_bin_match = re.search(r"\b\d{12}\b", c_customer)
            if c_bin_match:
                cust_bin = c_bin_match.group(0)
            elif c_number and len(c_number) >= 12 and c_number[:12].isdigit():
                cust_bin = c_number[:12]
            else:
                cust_bin = bin_code if role == "customer" else ""

            s_bin_match = re.search(r"\b\d{12}\b", c_supplier)
            supp_bin = s_bin_match.group(0) if s_bin_match else (bin_code if role == "supplier" else "")

            if c_id in existing_cids:
                continue

            # Парсим сумму
            amt_cleaned = re.sub(r"[^\d.]", "", c_amount_str.replace(" ", "").replace(",", "."))
            c_amount = float(amt_cleaned) if amt_cleaned else 0.0

            # Формализуем способ закупки (СТРОГО ТОЛЬКО ОК И ЗЦП)
            meth_l = c_method.lower().strip()
            if "ценовых" in meth_l or "зцп" in meth_l:
                meth_code = "ЗЦП"
            elif "открытый конкурс" in meth_l or meth_l == "ок":
                meth_code = "ОК"
            else:
                continue

            new_contracts_dict[c_id] = {
                "c_id": c_id, "c_number": c_number, "c_date": c_date, "c_status": c_status,
                "c_customer": c_customer, "cust_bin": cust_bin,
                "c_supplier": c_supplier, "supp_bin": supp_bin,
                "c_amount": c_amount, "meth_code": meth_code
            }
            existing_cids.add(c_id)
            added_contracts += 1

    # Если есть новые договоры — параллельно скачиваем их настоящие лоты!
    to_insert_rows = []
    if new_contracts_dict:
        log.info(f"Докачка реальных лотов для {len(new_contracts_dict)} новых договоров...")
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(_fetch_units_for_contract, cid, ua, cookie): cid for cid in new_contracts_dict}
            for future in as_completed(futures):
                cid, lots = future.result()
                info = new_contracts_dict[cid]

                if lots:
                    for title, price, qty in lots:
                        lot_amt = round(price * qty, 2)
                        to_insert_rows.append((
                            info["c_id"], info["c_number"], info["c_date"], info["c_status"],
                            info["c_customer"], info["cust_bin"], info["c_supplier"], info["supp_bin"],
                            title, price, qty, lot_amt if lot_amt > 0 else info["c_amount"],
                            info["meth_code"]
                        ))
                else:
                    # Фолбэк если лоты не прогрузились
                    to_insert_rows.append((
                        info["c_id"], info["c_number"], info["c_date"], info["c_status"],
                        info["c_customer"], info["cust_bin"], info["c_supplier"], info["supp_bin"],
                        "Договор с портала (докачка)", info["c_amount"], 1.0, info["c_amount"],
                        info["meth_code"]
                    ))

    # Массовая вставка
    if to_insert_rows:
        cur.executemany(
            """
            INSERT OR IGNORE INTO contracts_lots (
                contract_id, contract_number, contract_date, contract_status,
                customer_name, customer_bin, supplier_name, supplier_bin,
                lot_title, unit_price, quantity, contract_amount,
                brand_model, country, manufacturer, purchase_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?)
            """,
            to_insert_rows,
        )
        con.commit()

    return {"added_contracts": added_contracts, "added_lots": len(to_insert_rows)}
