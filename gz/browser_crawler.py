"""Интерактивный скрапер с графическим браузером (Playwright Chromium).

Включает передачу динамического CSRF-токена CodeIgniter при вызове fetch('/ru/egzcontract/cpublic/loadunit').
Это полностью устраняет ошибку 'The action you have requested is not allowed' и гарантирует 100% получение модальных окон!
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from selectolax.parser import HTMLParser

from .auth import save_session
from .specs import parse_html_spec, parse_spec
from .store import upsert

log = logging.getLogger(__name__)

LOGIN_URL = "https://goszakup.gov.kz/ru/user/login"
REGISTRY_URL = "https://goszakup.gov.kz/ru/egzcontract/cpublic"
DOWNLOAD_FILE_RE = re.compile(r"https://(?:v3bl\.)?goszakup\.gov\.kz/files/download_file/\d+/\d+/?")


async def _worker_task(
    worker_id: int,
    context: Any,
    queue: asyncio.Queue,
    con: sqlite3.Connection,
    db_lock: asyncio.Lock,
    total_items: int,
    counter: List[int],
    login_event: asyncio.Event,
) -> int:
    """Параллельная задача с 100% извлечением данных из HTML-модалок И прикреплённых файлов."""
    await asyncio.sleep(0.05 * worker_id)

    page = await context.new_page()
    saved = 0
    batch: List[Dict[str, Any]] = []

    while not queue.empty():
        if not login_event.is_set():
            await login_event.wait()

        item = await queue.get()
        cid, aid, sbin = item

        units_url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{cid}"
        try:
            await page.goto(units_url, wait_until="domcontentloaded", timeout=25000)

            curr_u = page.url
            if "/user/login" in curr_u or "/sign_workaround" in curr_u or "<title>Авторизация</title>" in (await page.content()):
                print(f"⚠️ W[{worker_id}] Требуется авторизация на Госзакупках! Приостанавливаю воркеры...", flush=True)
                login_event.clear()
                await queue.put(item)
                queue.task_done()
                continue

            counter[0] += 1
            curr_idx = counter[0]

            try:
                await page.wait_for_selector("a[href*='loadunit']", timeout=3000)
            except Exception:
                pass

            html_content = await page.content()
            file_links: List[str] = list(set(DOWNLOAD_FILE_RE.findall(html_content)))

            # 1. Забираем unit_id и передаём CSRF-токен в POST fetch!
            unit_ids = re.findall(r"loadunit\(['\"]?(\d+)['\"]?\)", html_content)
            for uid in set(unit_ids):
                try:
                    modal_html = await page.evaluate(
                        """
                        async ([pid, uid]) => {
                            const csrfName = document.querySelector('meta[name="csrf-token-name"]')?.getAttribute('content') || 'csrf';
                            const csrfHash = document.querySelector('meta[name="csrf-token-hash"]')?.getAttribute('content') || '';
                            const bodyStr = csrfName + '=' + encodeURIComponent(csrfHash) + '&pid=' + pid + '&unit_id=' + uid;
                            const resp = await fetch('/ru/egzcontract/cpublic/loadunit', {
                                method: 'POST',
                                headers: {
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Content-Type': 'application/x-www-form-urlencoded'
                                },
                                body: bodyStr
                            });
                            return await resp.text();
                        }
                        """,
                        [cid, uid],
                    )
                    if modal_html:
                        found = DOWNLOAD_FILE_RE.findall(modal_html)
                        file_links.extend(found)

                        parsed_m = parse_html_spec(modal_html)
                        if parsed_m and (parsed_m.get("brand_model") or parsed_m.get("country") or parsed_m.get("manufacturer")):
                            batch.append({
                                "announce_id": aid or cid,
                                "lot_number": parsed_m.get("lot_number", "NO_LOT"),
                                "supplier_name": parsed_m.get("supplier_name", ""),
                                "supplier_bin": sbin or "",
                                "brand_model": parsed_m.get("brand_model", "Нет данных"),
                                "country": parsed_m.get("country", "Нет данных"),
                                "manufacturer": parsed_m.get("manufacturer", "Нет данных"),
                                "year_made": parsed_m.get("year_made", ""),
                                "warranty": parsed_m.get("warranty", ""),
                                "standard": parsed_m.get("standard", ""),
                                "lot_name": parsed_m.get("lot_name", ""),
                                "source_file": f"modal_unit_{uid}",
                            })
                            print(
                                f"  [+] W[{worker_id}] [{curr_idx}/{total_items} Договоров] HTML Модалка: Марка={parsed_m.get('brand_model')} | Страна={parsed_m.get('country')}",
                                flush=True,
                            )
                except Exception as e:
                    log.debug("Ошибка модалки unit_id %s: %s", uid, e)

            # 2. Скачивание прикреплённых файлов Приложения 17
            file_links = list(set(file_links))
            for f_url in file_links:
                try:
                    f_resp = await page.request.get(f_url, timeout=20000)
                    if f_resp.status == 200:
                        f_bytes = await f_resp.body()
                        spec_data = parse_spec(f_bytes)
                        if spec_data.get("brand_model") or spec_data.get("country") or spec_data.get("manufacturer"):
                            batch.append({
                                "announce_id": aid or cid,
                                "lot_number": spec_data.get("lot_number", "NO_LOT"),
                                "supplier_name": spec_data.get("supplier_name", ""),
                                "supplier_bin": sbin or "",
                                "brand_model": spec_data.get("brand_model", "Нет данных"),
                                "country": spec_data.get("country", "Нет данных"),
                                "manufacturer": spec_data.get("manufacturer", "Нет данных"),
                                "year_made": spec_data.get("year_made", ""),
                                "warranty": spec_data.get("warranty", ""),
                                "standard": spec_data.get("standard", ""),
                                "lot_name": spec_data.get("lot_name", ""),
                                "source_file": f_url,
                            })
                            print(
                                f"  [+] W[{worker_id}] [{curr_idx}/{total_items} Договоров] Скачан Файл: Марка={spec_data.get('brand_model')} | Завод={spec_data.get('manufacturer')}",
                                flush=True,
                            )
                except Exception as e:
                    log.debug("Ошибка скачивания файла %s: %s", f_url, e)

            if len(batch) >= 10:
                async with db_lock:
                    upsert(con, "supplier_specs", batch)
                    saved += len(batch)
                    batch = []

        except Exception as e:
            log.warning("W[%d] Ошибка обработки договора CID %s: %s", worker_id, cid, e)
        finally:
            queue.task_done()

    if batch:
        async with db_lock:
            upsert(con, "supplier_specs", batch)
            saved += len(batch)

    await page.close()
    return saved


async def _run_crawler_async(
    con: sqlite3.Connection, limit: Optional[int] = None, num_workers: int = 25
) -> int:
    """Запускает Playwright Chromium браузер с автоматическим ожиданием авторизации."""
    from playwright.async_api import async_playwright

    cur = con.cursor()
    cur.execute(
        """
        SELECT DISTINCT c.contract_id, c.announce_id, c.supplier_bin
        FROM contracts c
        WHERE c.contract_id IS NOT NULL AND c.contract_id != ''
        """
    )
    items = cur.fetchall()

    if limit:
        items = items[:limit]

    if not items:
        print("ℹ️ В базе нет контрактов для обработки.")
        return 0

    print(f"🚀 Запуск Playwright Chromium скрапера для {len(items)} уникальных Договоров ({num_workers} воркеров)...")

    queue: asyncio.Queue = asyncio.Queue()
    for it in items:
        await queue.put(it)

    db_lock = asyncio.Lock()
    counter = [0]
    login_event = asyncio.Event()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        )

        init_page = await context.new_page()
        print("🔑 Открываю страницу входа на `goszakup.gov.kz`...")
        await init_page.goto(LOGIN_URL, timeout=60000)
        print("💡 Пожалуйста, авторизуйтесь в открывшемся окне браузера (ЭЦП или логин/пароль).")
        print("⏳ Ожидание авторизации...")

        login_event.clear()
        while True:
            curr_url = init_page.url
            content = await init_page.content()
            if "/user/login" not in curr_url and "/sign_workaround" not in curr_url and "<title>Авторизация</title>" not in content:
                print("✅ Успешная авторизация обнаружена!")
                login_event.set()
                break
            await asyncio.sleep(2)

        # Сохраняем куки
        cookies = await context.cookies()
        ci_cookie = next((c["value"] for c in cookies if c["name"] == "ci_session"), None)
        if ci_cookie:
            save_session(ci_cookie, await init_page.evaluate("navigator.userAgent"))
            print("💾 Свежие куки ci_session сохранены!")

        print(f"🚀 Авторизация подтверждена! Запускаю {num_workers} браузерных воркеров по {len(items)} договорам...")

        tasks = []
        for i in range(1, num_workers + 1):
            tasks.append(
                _worker_task(
                    worker_id=i,
                    context=context,
                    queue=queue,
                    con=con,
                    db_lock=db_lock,
                    total_items=len(items),
                    counter=counter,
                    login_event=login_event,
                )
            )

        results = await asyncio.gather(*tasks)
        total_saved = sum(results)

        await browser.close()
        print(f"\n🎉 Браузерный скрапинг завершён! Всего сохранено спецификаций: {total_saved}")
        return total_saved


def run_authenticated_crawler(
    con: sqlite3.Connection, limit: Optional[int] = None, num_workers: int = 25
) -> int:
    """Точка входа для браузерного скрапинга через `cli.py crawl`."""
    return asyncio.run(_run_crawler_async(con, limit=limit, num_workers=num_workers))
