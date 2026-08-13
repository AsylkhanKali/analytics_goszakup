"""Модуль выкачки ВСЕХ договоров Реестра через https://goszakup.gov.kz/ru/registry/contract:
- Раскрытие «Расширенный поиск»
- Использование параметра count_record=500 (максимальная выдача по 500 элементов на страницу!)
- Точный разбор столбцов таблицы:
  cells[0] = contract_id
  cells[1] = contract_number
  cells[2] = announce_id
  cells[4] = contract_status (Исполнен, Действует)
  cells[6] = contract_amount
  cells[7] = customer_name
  cells[8] = supplier_name
- Защита от зацикливания пагинации на последней странице
- Автоматический переход по страницам и занесение в SQLite БД
- Исключение расторгнутых, незаключенных и неисполненных договоров
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import List, Tuple

from selectolax.parser import HTMLParser

from .auth import save_session
from .store import upsert

log = logging.getLogger(__name__)

LOGIN_URL = "https://goszakup.gov.kz/ru/user/login"
REGISTRY_URL = "https://goszakup.gov.kz/ru/registry/contract"


def _build_months() -> List[Tuple[str, str]]:
    """Генерирует список помесячных интервалов с 01.01.2024 по 31.12.2026."""
    months = []
    # 2024
    for m in range(1, 13):
        days = 29 if m == 2 else (30 if m in (4, 6, 9, 11) else 31)
        months.append((f"01.{m:02d}.2024", f"{days:02d}.{m:02d}.2024"))
    # 2025
    for m in range(1, 13):
        days = 28 if m == 2 else (30 if m in (4, 6, 9, 11) else 31)
        months.append((f"01.{m:02d}.2025", f"{days:02d}.{m:02d}.2025"))
    # 2026 (по 08.2026)
    for m in range(1, 9):
        days = 28 if m == 2 else (30 if m in (4, 6, 9, 11) else 31)
        months.append((f"01.{m:02d}.2026", f"{days:02d}.{m:02d}.2026"))

    return months


def parse_cpublic_page(html: str) -> List[dict]:
    """Парсит реестровую таблицу договоров на странице registry/contract, исключая расторгнутые и неисполненные."""
    tree = HTMLParser(html)
    rows = []
    for tr in tree.css("table tbody tr, table.table tbody tr"):
        cells = tr.css("td")
        if len(cells) < 9:
            continue

        cid = cells[0].text(strip=True)
        c_num = cells[1].text(strip=True)
        aid_raw = cells[2].text(strip=True)
        c_stat = cells[4].text(strip=True) if len(cells) > 4 else "Действует"
        amt_str = cells[6].text(strip=True).replace(" ", "").replace(",", ".") if len(cells) > 6 else "0"
        c_name = cells[7].text(strip=True) if len(cells) > 7 else ""
        s_name = cells[8].text(strip=True) if len(cells) > 8 else ""

        stat_lower = c_stat.lower()
        if "расторгнут" in stat_lower or "не заключен" in stat_lower or "не исполнен" in stat_lower:
            continue

        try:
            amt = float(amt_str)
        except ValueError:
            amt = 0.0

        if c_num and len(c_num) > 5:
            aid = c_num.split("/")[1] if "/" in c_num else aid_raw
            cbin = c_num.split("/")[0] if "/" in c_num else None

            rows.append({
                "contract_id": cid,
                "contract_number": c_num,
                "contract_status": c_stat,
                "customer_name": c_name,
                "customer_bin": cbin,
                "supplier_name": s_name,
                "contract_amount": amt,
                "lot_title": "",
                "announce_id": aid,
            })
    return rows


async def _run_indexer_async(con: sqlite3.Connection):
    """Скрипт пошагового сбора через рабочий URL реестра договоров с 500 записями на страницу."""
    from playwright.async_api import async_playwright

    months = _build_months()
    print(f"🚀 Запуск индексации реестра договоров по {len(months)} месячным интервалам (2024-2026)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print("🔑 Открываю страницу входа на Госзакупки...")
        await page.goto(LOGIN_URL, timeout=60000)
        print("💡 Пожалуйста, авторизуйтесь на сайте в открывшемся окне браузера (ЭЦП или пароль).")
        print("⏳ Ожидание авторизации...")

        for _ in range(120):
            if "login" not in page.url:
                print("✅ Успешная авторизация обнаружена!")
                break
            await asyncio.sleep(2)

        cookies = await context.cookies()
        ci_cookie = next((c["value"] for c in cookies if c["name"] == "ci_session"), None)
        if ci_cookie:
            save_session(ci_cookie, await page.evaluate("navigator.userAgent"))
            print("💾 Сессия ci_session автоматически сохранена!")

        total_saved = 0
        for m_idx, (d_start, d_end) in enumerate(months, 1):
            print(f"\n📅 [{m_idx}/{len(months)}] Запрос за период: {d_start} — {d_end}")
            seen_cids = set()
            page_num = 1

            while True:
                search_url = (
                    f"{REGISTRY_URL}?filter%5Bref_subject_type%5D=1"
                    f"&filter%5Bmethod%5D%5B0%5D=2"
                    f"&filter%5Bstart_date_from%5D={d_start}"
                    f"&filter%5Bstart_date_to%5D={d_end}"
                    f"&count_record=500"
                    f"&page={page_num}"
                )

                try:
                    await page.goto(search_url, timeout=45000)
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(1000)

                    html = await page.content()
                    rows = parse_cpublic_page(html)

                    if not rows:
                        break

                    new_rows = [r for r in rows if r["contract_id"] not in seen_cids]
                    if not new_rows:
                        break

                    page_cids = set(r["contract_id"] for r in new_rows)
                    for cid in page_cids:
                        seen_cids.add(cid)

                    upsert(con, "contracts", new_rows)
                    total_saved += len(new_rows)
                    print(f"  [+] Стр.{page_num}: Добавлено {len(page_cids)} уникальных договоров ({len(new_rows)} лотов | За месяц: {len(seen_cids)} договоров | Итого лотов: {total_saved})")

                    if len(rows) < 500:
                        break

                    page_num += 1
                except Exception as e:
                    log.warning("Ошибка на странице %d периода %s — %s: %s", page_num, d_start, d_end, e)
                    break

        await browser.close()
        print(f"\n🎉 Индексация завершена! Всего сохранено уникальных действующих договоров в базу: {total_saved}")


def index_all_goods_open_competitions(con: sqlite3.Connection):
    """Точка входа для запуска индексатора."""
    asyncio.run(_run_indexer_async(con))
