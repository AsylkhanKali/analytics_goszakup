"""Предметы договора — товарная позиция, а не лот целиком.

Вкладка «Договоры» даёт сумму по договору, но один договор часто содержит
десяток разных товаров. Наименование, количество и цена за единицу из ТЗ
живут именно здесь: `/ru/egzcontract/cpublic/units/{cid}` — таблица позиций,
POST `loadunit` — карточка одной позиции с характеристиками и местом поставки.

Собираем только по открытому конкурсу: это 358 договоров, но 7.4 млрд из
12.3 млрд тенге всей выборки. По остальным способам закупки объём позиций
несоразмерен пользе.
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from .client import Portal
from .store import upsert

log = logging.getLogger(__name__)

# Строка таблицы позиций: #, Ид, П/П, КТРУ, Наименование, Кол-во, Ед., Цена, Сумма
UNIT_COLS = 9

# В таблице позиций «Сумма» идёт с НДС у плательщиков НДС и без — у остальных
# (замер: 191 договор из 358 ровно в 1.12 раза выше суммы договора, 119 — ровно
# в 1.0). Сравнивать цены между поставщиками по такой колонке нельзя, поэтому
# цену без НДС берём из карточки позиции, где она подписана явно.
WANT = {
    "Краткая характеристика (на русском языке)": "short_char",
    "Дополнительная характеристика (на русском языке)": "extra_char",
    "Место поставки, адрес": "delivery",
    "Цена за единицу (без учета НДС)": "price_no_vat",
}
NUM_FIELDS = {"price_no_vat"}


def _num(s):
    s = re.sub(r"[^\d.,]", "", (s or "").replace("\xa0", "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_units(html: str, contract_id: str) -> list[dict]:
    """Таблица позиций договора. Строки без Ид (итоги, шапки) отбрасываем."""
    rows = []
    for tr in HTMLParser(html).css("table tbody tr"):
        tds = tr.css("td")
        if len(tds) != UNIT_COLS:
            continue
        cells = [td.text(strip=True) for td in tds]
        unit_id = cells[1]
        if not unit_id.isdigit():
            continue
        rows.append(
            {
                "unit_id": unit_id,
                "contract_id": contract_id,
                "ktru": cells[3] or None,
                "name": cells[4] or None,
                "qty": _num(cells[5]),
                "unit": cells[6] or None,
                "price_per_unit": _num(cells[7]),
                "amount": _num(cells[8]),
            }
        )
    return rows


def parse_unit_detail(html: str) -> dict:
    """Карточка позиции — та же схема «подпись → значение», что и у договора."""
    out = {}
    for tr in HTMLParser(html).css("tr"):
        cells = [c.text(strip=True) for c in tr.css("td,th")]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        key = WANT.get(re.sub(r"\s+", " ", cells[0]).strip())
        if key:
            value = re.sub(r"\s+", " ", cells[1]).strip()
            out[key] = _num(value) if key in NUM_FIELDS else (value or None)
    return out


def open_tender_contracts(con) -> list[str]:
    cur = con.execute(
        "SELECT DISTINCT ct.contract_id FROM contracts ct "
        "JOIN lots l ON l.announce_id = ct.announce_id AND l.lot_number = ct.lot_number "
        "WHERE l.relevant = 1 AND l.method = 'Открытый конкурс' "
        "AND ct.contract_id IS NOT NULL"
    )
    return [r[0] for r in cur]


def collect(portal: Portal, con, workers: int = 10) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    have = {r[0] for r in con.execute("SELECT DISTINCT contract_id FROM contract_units")}
    todo = [c for c in open_tender_contracts(con) if c not in have]
    log.info("договоров открытого конкурса к разбору: %s", len(todo))

    units: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(portal.contract_units, c): c for c in todo}
        for fut in as_completed(futures):
            cid = futures[fut]
            try:
                html = fut.result()
            except Exception as e:
                log.warning("позиции договора %s: %s", cid, e)
                continue
            if html:
                units.extend(parse_units(html, cid))
    log.info("позиций найдено: %s, добираю характеристики", len(units))

    # Характеристики — отдельным проходом: один запрос на позицию.
    by_id = {u["unit_id"]: u for u in units}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(portal.load_unit, u["contract_id"], u["unit_id"]): u["unit_id"]
            for u in units
        }
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                html = fut.result()
            except Exception as e:
                log.warning("карточка позиции: %s", e)
                continue
            if html:
                by_id[futures[fut]].update(parse_unit_detail(html))
            if i % 500 == 0:
                log.info("… %s/%s позиций", i, len(futures))

    upsert(con, "contract_units", units)
    return len(units)
