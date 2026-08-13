"""Этап 2 — договоры, поставщики и суммы по объявлению.

Одна страница `/ru/announce/index/{aid}?tab=contracts` отдаёт по каждому лоту
всё, что нужно для аналитики сделки: номер договора, статус, плановую сумму
лота (= начальная цена), сумму по итогам закупки, фактически исполненную,
поставщика и его БИН/ИИН.

Плановая против итоговой даёт процент снижения. БИН нужен потому, что одно
и то же ТОО в разных закупках пишется по-разному, и считать поставщиков
по строке названия нельзя.
"""

from __future__ import annotations

import logging
import re
import time

from selectolax.parser import HTMLParser

from .client import Portal
from .store import mark_done, pending, upsert

log = logging.getLogger(__name__)

CONTRACT_ID_RE = re.compile(r"cpublic/show/(\d+)")
LOT_TITLE_RE = re.compile(r"Лот\s*№\s*([^\s:]+)\s*:\s*(.*)", re.S)


def _num(s: str | None) -> float | None:
    if not s:
        return None
    s = re.sub(r"[^\d.,-]", "", s.replace("\xa0", "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_contracts(html: str, announce_id: str) -> list[dict]:
    """Разбирает вкладку «Договоры» объявления."""
    tree = HTMLParser(html)
    rows: list[dict] = []
    lot_number = lot_title = None

    for tr in tree.css("table tr"):
        link = tr.css_first("a.lot-links")
        if link:
            m = LOT_TITLE_RE.search(link.text(strip=True))
            if m:
                lot_number, lot_title = m.group(1), m.group(2).strip()
            else:
                lot_number = link.attributes.get("data-id")
                lot_title = link.text(strip=True)
            continue

        tds = tr.css("td")
        if len(tds) != 8:
            continue

        num_cell = tds[0]
        a = num_cell.css_first("a")
        contract_number = (a.text(strip=True) if a else num_cell.text(strip=True)).strip()
        if not contract_number:
            continue
        m = CONTRACT_ID_RE.search(a.attributes.get("href", "")) if a else None

        rows.append(
            {
                "contract_id": m.group(1) if m else None,
                "announce_id": announce_id,
                "lot_number": lot_number,
                "lot_title": lot_title,
                "contract_number": contract_number,
                "contract_status": tds[1].text(strip=True),
                "plan_amount": _num(tds[2].text(strip=True)),
                "contract_amount": _num(tds[3].text(strip=True)),
                "executed_amount": _num(tds[4].text(strip=True)),
                "supplier_name": tds[5].text(strip=True),
                "supplier_bin": tds[6].text(strip=True) or None,
                "winner_status": tds[7].text(strip=True),
            }
        )
    return rows


def enrich(portal: Portal, con, limit: int | None = None, workers: int = 8) -> int:
    """Обходит объявления, по которым этап ещё не отрабатывал.

    Качают потоки, пишет в SQLite только главный — соединение не потокобезопасно.
    Темп регулирует сам Portal: на отказах портала штрафная пауза растёт.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .relevance import announcements

    relevant = set(announcements(con))
    todo = [a for a in pending(con, "contracts") if a in relevant]
    if limit:
        todo = todo[:limit]
    log.info("объявлений к обработке: %s (потоков %s)", len(todo), workers)

    def work(aid):
        return aid, portal.announce(aid, "contracts")

    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, a) for a in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                aid, html = fut.result()
            except Exception as e:  # поток не должен ронять весь прогон
                log.warning("сбой потока: %s", e)
                continue
            if html is None:
                continue
            upsert(con, "contracts", parse_contracts(html, aid))
            # Отмечаем и пустые: «договоров нет» — тоже результат,
            # переспрашивать портал повторно незачем.
            mark_done(con, aid, "contracts")
            done += 1
            if i % 500 == 0:
                con.commit()
                rate = i / max(time.time() - t0, 1)
                left = (len(todo) - i) / max(rate, 0.01) / 60
                log.info(
                    "… %s/%s | %.1f зап/с | осталось ~%.0f мин | ошибок %s штраф %.1fс",
                    i, len(todo), rate, left, portal.errors, portal.penalty,
                )
    con.commit()
    return done
