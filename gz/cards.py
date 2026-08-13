"""Карточки договоров — дата заключения, краткое содержание, способ закупки.

Вкладка «Договоры» объявления даёт поставщика и суммы, но не даёт даты
заключения и краткого содержания договора. Оба поля есть в карточке
`/ru/egzcontract/cpublic/show/{id}`, публично, одним запросом.

Краткое содержание — то самое поле, по которому на портале ищут договоры
(под авторизацией это `filter[description]`). Здесь оно достаётся без логина.
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from .client import Portal
from .refs import BASE
from .store import upsert

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_cards (
    contract_id   TEXT PRIMARY KEY,
    signed_date   TEXT,
    description   TEXT,   -- краткое содержание договора
    subject_type  TEXT,   -- вид предмета закупок: Товар / Работа / Услуга
    method_fact   TEXT,
    status        TEXT,
    fin_year      TEXT,
    plan_total    REAL,
    result_total  REAL
);
"""

WANT = {
    "Дата заключения договора": "signed_date",
    "Краткое содержание договора на русском языке": "description",
    "Вид предмета закупок": "subject_type",
    "Фактический способ осуществления закупки": "method_fact",
    "Статус договора": "status",
    "Финансовый год": "fin_year",
    "Общая плановая сумма договора": "plan_total",
    "Общая сумма договора по итогам закупки": "result_total",
}
NUM_FIELDS = {"plan_total", "result_total"}


def _num(s):
    s = re.sub(r"[^\d.,]", "", (s or "").replace("\xa0", "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_card(html: str, contract_id: str) -> dict:
    """Карточка — таблица «подпись → значение»; берём только нужные строки."""
    tree = HTMLParser(html)
    row = {"contract_id": contract_id}
    for tr in tree.css("tr"):
        cells = tr.css("td") or tr.css("th")
        if len(cells) < 2:
            continue
        label = re.sub(r"\s+", " ", cells[0].text(strip=True)).strip()
        key = WANT.get(label)
        if not key:
            continue
        value = re.sub(r"\s+", " ", cells[1].text(strip=True)).strip()
        row[key] = _num(value) if key in NUM_FIELDS else (value or None)
    return row


def collect(portal: Portal, con, workers: int = 10) -> int:
    """Карточки только тех договоров, что реально попали в нашу выборку лотов."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    con.executescript(SCHEMA)
    cur = con.execute(
        "SELECT DISTINCT ct.contract_id FROM contracts ct "
        "JOIN lots l ON l.announce_id = ct.announce_id AND l.lot_number = ct.lot_number "
        "LEFT JOIN contract_cards cc ON cc.contract_id = ct.contract_id "
        "WHERE l.relevant = 1 AND ct.contract_id IS NOT NULL AND cc.contract_id IS NULL"
    )
    todo = [r[0] for r in cur]
    log.info("карточек договоров к сбору: %s", len(todo))

    def work(cid):
        # Без кэша: 56 тыс. карточек по ~40 КБ переполнят диск, а нужны из них
        # только 8 полей — они уходят в БД, ссылка восстановима из contract_id.
        return cid, portal.fetch(f"{BASE}/ru/egzcontract/cpublic/show/{cid}", cache=False)

    done, batch = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, c) for c in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                cid, html = fut.result()
            except Exception as e:
                log.warning("сбой потока: %s", e)
                continue
            if not html:
                continue
            batch.append(parse_card(html, cid))
            done += 1
            if len(batch) >= 500:
                upsert(con, "contract_cards", batch)
                batch = []
                log.info("… %s/%s карточек, ошибок %s", i, len(todo), portal.errors)
    upsert(con, "contract_cards", batch)
    con.commit()
    return done
