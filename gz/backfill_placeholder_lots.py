"""Скрипт фонового заполнения реальных наименований лотов для всех затычек `Договор с портала (докачка)`.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from selectolax.parser import HTMLParser
from gz.auth import load_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    con = sqlite3.connect("data/goszakup.db", timeout=60.0)
    cur = con.cursor()

    placeholder_rows = cur.execute(
        """
        SELECT contract_id, contract_number, contract_date, contract_status,
               customer_name, customer_bin, supplier_name, supplier_bin,
               contract_amount, purchase_method
        FROM contracts
        WHERE lot_title = 'Договор с портала (докачка)'
        """
    ).fetchall()

    total = len(placeholder_rows)
    print(f"📦 Найдено {total} договоров с заглушкой 'Договор с портала (докачка)'. Начинаем подгрузку реальных лотов...", flush=True)

    if total == 0:
        print(" Все лоты уже имеют нормальные наименования!")
        return

    s = load_session()
    ua = s.get("user_agent", "Mozilla/5.0")
    cookie = s.get("ci_session", "")

    def fetch_units(row):
        cid = row[0]
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
                    lot_amt = round(qty * price, 2)
                    if title:
                        lots.append((
                            row[0], row[1], row[2], row[3],
                            row[4], row[5], row[6], row[7],
                            title, price, qty, lot_amt if lot_amt > 0 else row[8],
                            row[9]
                        ))
            return cid, lots
        except Exception:
            return cid, []

    t0 = time.time()
    processed = 0
    batch_delete_cids = []
    batch_insert_rows = []

    with ThreadPoolExecutor(max_workers=15) as pool:
        future_to_cid = {pool.submit(fetch_units, r): r[0] for r in placeholder_rows}
        for future in as_completed(future_to_cid):
            cid, lots = future.result()
            processed += 1

            if lots:
                batch_delete_cids.append((cid,))
                batch_insert_rows.extend(lots)

            if processed % 500 == 0 or processed == total:
                print(f"⏳ Обработано {processed}/{total} договоров... (Подгружено {len(batch_insert_rows)} лотов)", flush=True)

    if batch_delete_cids:
        print("💾 Записываем реальные наименования лотов в базу данных...", flush=True)
        cur.executemany(
            "DELETE FROM contracts WHERE contract_id = ? AND lot_title = 'Договор с портала (докачка)'",
            batch_delete_cids
        )
        cur.executemany(
            """
            INSERT INTO contracts (
                contract_id, contract_number, contract_date, contract_status,
                customer_name, customer_bin, supplier_name, supplier_bin,
                lot_title, unit_price, quantity, contract_amount,
                brand_model, country, manufacturer, purchase_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?)
            """,
            batch_insert_rows
        )
        con.commit()

    t1 = time.time()
    print(f"🎉 Готово! Все {total} договоров заменены на реальные {len(batch_insert_rows)} лотов за {(t1-t0):.2f}s!", flush=True)


if __name__ == "__main__":
    main()
