"""Скрипт 100% заполнения отсутствующих БИН/ИИН Поставщиков и Заказчиков в базе данных.
"""

from __future__ import annotations

import logging
import re
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

    # Находим все уникльные contract_id с пустым supplier_bin
    empty_cids = [
        r[0] for r in cur.execute(
            "SELECT DISTINCT contract_id FROM contracts WHERE (supplier_bin IS NULL OR supplier_bin = '') AND contract_id IS NOT NULL AND contract_id != ''"
        ).fetchall()
    ]

    total = len(empty_cids)
    print(f"📦 Найдено {total} уникальных договоров с пустым БИН Поставщика. Начинаем подгрузку БИН/ИИН...", flush=True)

    if total == 0:
        print(" Все БИНы уже заполнены!", flush=True)
        return

    s = load_session()
    ua = s.get("user_agent", "Mozilla/5.0")
    cookie = s.get("ci_session", "")

    def fetch_bins(cid: str):
        url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/customer_n_supplier/{cid}"
        cmd = ["curl", "-s", "-L", "--max-time", "15", "-H", f"User-Agent: {ua}", "-H", f"Cookie: ci_session={cookie}", url]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=15)
            html_str = res.stdout.decode("utf-8", "ignore")
            tree = HTMLParser(html_str)
            tables = tree.css("table")

            supp_bin = ""
            cust_bin = ""

            if len(tables) > 0:
                # Table 0: Customer
                for tr in tables[0].css("tr"):
                    cells = [td.text(strip=True) for td in tr.css("th, td")]
                    if len(cells) >= 2 and (cells[0] == "БИН" or cells[0] == "ИИН"):
                        val = cells[1].strip()
                        if len(val) == 12 and val.isdigit():
                            cust_bin = val
                            break

            if len(tables) > 1:
                # Table 1: Supplier
                for tr in tables[1].css("tr"):
                    cells = [td.text(strip=True) for td in tr.css("th, td")]
                    if len(cells) >= 2 and (cells[0] == "БИН" or cells[0] == "ИИН"):
                        val = cells[1].strip()
                        if len(val) == 12 and val.isdigit():
                            supp_bin = val
                            break

            return cid, supp_bin, cust_bin
        except Exception:
            return cid, "", ""

    t0 = time.time()
    processed = 0
    updates_supp = []
    updates_cust = []

    with ThreadPoolExecutor(max_workers=15) as pool:
        future_to_cid = {pool.submit(fetch_bins, cid): cid for cid in empty_cids}
        for future in as_completed(future_to_cid):
            cid, s_bin, c_bin = future.result()
            processed += 1

            if s_bin:
                updates_supp.append((s_bin, cid))
            if c_bin:
                updates_cust.append((c_bin, cid))

            if processed % 500 == 0 or processed == total:
                print(f"⏳ Обработано {processed}/{total} договоров... (Найдено {len(updates_supp)} БИНов поставщиков)", flush=True)

    if updates_supp:
        print("💾 Обновляем БИНы поставщиков в базе данных...", flush=True)
        cur.executemany(
            "UPDATE contracts SET supplier_bin = ? WHERE contract_id = ? AND (supplier_bin IS NULL OR supplier_bin = '')",
            updates_supp,
        )

    if updates_cust:
        cur.executemany(
            "UPDATE contracts SET customer_bin = ? WHERE contract_id = ? AND (customer_bin IS NULL OR customer_bin = '')",
            updates_cust,
        )

    con.commit()
    t1 = time.time()
    print(f"🎉 Успешно! Обновлены БИНы для {len(updates_supp)} договоров за {(t1-t0):.2f}s!", flush=True)


if __name__ == "__main__":
    main()
