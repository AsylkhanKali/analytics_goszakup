"""Этап 1 — поиск лотов по ключевым словам.

Поиск лотов (`/ru/search/lots`, filter[name]) ищет по наименованию И описанию
лота, поэтому даёт на два порядка больше товарных позиций, чем поиск по
реестру договоров (замер: «мяч» — 10000+ против 165). Это основной канал
обнаружения товаров; договор и поставщик добираются на этапе 2.

Выдача жёстко обрезана 10 000 записями на набор фильтров, поэтому нарезаем
по ключевому слову × году, а если слайс всё равно упирается в потолок —
дополнительно по способу закупки.
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from . import refs
from .client import Portal
from .store import upsert

log = logging.getLogger(__name__)

TOTAL_RE = re.compile(r"из\s+([\d\s ]+)\s+записей")
ANNOUNCE_RE = re.compile(r"/ru/announce/index/(\d+)")
LOT_RE = re.compile(r"/ru/subpriceoffer/index/(\d+)/(\d+)")


def _num(s: str | None) -> float | None:
    if not s:
        return None
    s = re.sub(r"[^\d.,-]", "", s.replace("\xa0", "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _total(html: str) -> int:
    m = TOTAL_RE.search(html)
    return int(re.sub(r"\D", "", m.group(1))) if m else 0


def parse_lots(html: str, category: str, keyword: str, year: str) -> list[dict]:
    """Разбирает таблицу результатов поиска лотов."""
    tree = HTMLParser(html)
    tables = tree.css("table")
    if not tables:
        return []
    rows = []
    for tr in tables[-1].css("tbody tr"):
        tds = tr.css("td")
        if len(tds) < 7:
            continue

        announce_a = tds[1].css_first("a[href*='/ru/announce/index/']")
        lot_a = tds[2].css_first("a[href*='/ru/subpriceoffer/']")
        if not announce_a or not lot_a:
            continue

        m_a = ANNOUNCE_RE.search(announce_a.attributes.get("href", ""))
        m_l = LOT_RE.search(lot_a.attributes.get("href", ""))
        if not m_a or not m_l:
            continue

        # Заказчик лежит в <small> под ссылкой на объявление.
        customer = ""
        small = tds[1].css_first("small")
        if small:
            customer = small.text(strip=True).replace("Заказчик:", "").strip()

        # Описание лота — <small> в третьей ячейке (часто пустой).
        desc = ""
        d_small = tds[2].css_first("small")
        if d_small:
            t = d_small.text(strip=True)
            if t and t != "История":
                desc = t

        rows.append(
            {
                "lot_id": m_l.group(2),
                "announce_id": m_a.group(1),
                "lot_number": tds[0].text(strip=True),
                "announce_name": announce_a.text(strip=True),
                "lot_name": lot_a.text(strip=True),
                "lot_desc": f"{desc}\nЗаказчик: {customer}".strip(),
                "qty": _num(tds[3].text(strip=True)),
                "amount": _num(tds[4].text(strip=True)),
                "method": tds[5].text(strip=True),
                "status": tds[6].text(strip=True),
                "year": year,
                "category": category,
                "keyword": keyword,
            }
        )
    return rows


def _query(portal: Portal, keyword, year, method=None) -> tuple[list[dict], int]:
    """Одна нарезка: тянет все страницы, возвращает (строки, объявленный total)."""
    params = [
        ("filter[name]", keyword),
        ("filter[year]", year),
        ("filter[status][]", refs.LOT_STATUS_DONE),
        ("count_record", str(refs.PAGE_SIZE)),
    ]
    if method:
        params.append(("filter[method][]", method))

    out, total, page = [], None, 1
    while True:
        html = portal.lots(params + [("page", str(page))])
        if not html:
            break
        if total is None:
            total = _total(html)
        rows = parse_lots(html, "", keyword, year)
        if not rows:
            break
        out.extend(rows)
        if len(out) >= (total or 0) or len(rows) < refs.PAGE_SIZE:
            break
        page += 1
    return out, total or 0


def discover(portal: Portal, con, categories=None, years=None, limit_keywords=None) -> int:
    """Собирает лоты по словарю ключевых слов. Возвращает число сохранённых строк."""
    categories = categories or list(refs.KEYWORDS)
    years = years or refs.YEARS
    saved = 0

    for category in categories:
        kws = refs.KEYWORDS[category]
        if limit_keywords:
            kws = kws[:limit_keywords]
        for keyword in kws:
            for year in years:
                rows, total = _query(portal, keyword, year)

                if total >= refs.RESULT_CAP:
                    # Слайс упёрся в потолок — дробим по способу закупки.
                    log.warning(
                        "«%s» %s: потолок %s, дроблю по способам", keyword, year, total
                    )
                    rows = []
                    for method in refs.METHODS:
                        part, part_total = _query(portal, keyword, year, method)
                        if part_total >= refs.RESULT_CAP:
                            log.error(
                                "«%s» %s способ %s всё ещё %s — данные усечены",
                                keyword, year, method, part_total,
                            )
                        rows.extend(part)

                for r in rows:
                    r["category"] = category
                saved += upsert(con, "lots", rows)
                log.info(
                    "%s | %-28s | %s | найдено %s (total %s)",
                    category, keyword, year, len(rows), total,
                )
    return saved
