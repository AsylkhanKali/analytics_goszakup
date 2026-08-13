"""Протоколы итогов — число участников и их ценовые предложения.

Публичны, авторизация не нужна. Это единственный источник, дающий реальную
конкуренцию по лоту: сколько поставщиков подало заявку и по какой цене.
Отсюда берутся разделы отчёта «где наименьшая конкуренция» и «высокая
стоимость при малом числе участников».

Формат протокола (проверено на реальном документе):

    Лот №                        41880907-ОЛ-ЗЦП1
    Наименование лота            Приобретение спортивных мячей
    Запланированная цена за      21000
    единицу, тенге
    Потенциальными поставщиками представлены следующие ценовые предложения: 8
    1 ИП ДАУРЕН      890921300467   8700    71400   2026-05-21 15:19:42.915
"""

from __future__ import annotations

import logging
import re

from .client import Portal
from .store import mark_done, upsert
from .techspec import pdf_text

log = logging.getLogger(__name__)

FILE_RE = re.compile(r"(https://v3bl\.goszakup\.gov\.kz/files/download_file/\d+/\d+/?)")
LOT_SPLIT_RE = re.compile(r"\n\s*Лот\s*№\s+", re.I)
LOT_NUM_RE = re.compile(r"^\s*([\dА-Яа-яA-Za-z\-]+)")
PLANNED_RE = re.compile(r"Запланированная\s+цена\s+за\s*\n?\s*(?:единицу[^\n]*)?\s*([\d\s.,]+)", re.I)
# Важно не спутать со строкой «...автоматически отклоненные веб-порталом: 0»,
# которая идёт выше и тоже заканчивается на «предложения: N».
COUNT_RE = re.compile(
    r"представлен\w*\s+следующие\s+ценовые\s+предложения\s*:\s*(\d+)", re.I
)
# № | наименование | БИН/ИИН (12 цифр) | цена | сумма | дата
OFFER_RE = re.compile(
    r"^\s*\d+\s+(.+?)\s+(\d{12})\s+([\d\s.,]+?)\s+([\d\s.,]+?)\s+\d{4}-\d{2}-\d{2}",
    re.M,
)

# Протокол открытого конкурса — не PDF, а HTML, и сначала идёт казахская
# версия, следом русская. Разбираем русскую: подписи там однозначные.
# Заявки в конкурсе делятся на поданные, отклонённые и допущенные — это
# более точная мера конкуренции, чем одно число.
LOT_RU_RE = re.compile(r"№\s*лота\s*\n\s*([\w\-]+)\s*\n\s*Наименование\s+лота", re.I)
SUBMITTED_RE = re.compile(
    r"заявк\w*\s+на\s+участие\s+в\s+конкурсе\s*\(?\s*лоте\s*\)?\s*:\s*(\d+)", re.I
)
ADMITTED_RE = re.compile(r"Допущенные\s+заявки[^:\n]*:\s*(\d+)", re.I)
REJECTED_RE = re.compile(r"Отклоненные\s+заявки[^:\n]*:\s*(\d+)", re.I)
BIDDER_RE = re.compile(r"^\s*\d+\s*\n\s*(.+?)\s*\n\s*(\d{12})\s*$", re.M)

SCHEMA = """
CREATE TABLE IF NOT EXISTS protocols (
    announce_id  TEXT,
    lot_number   TEXT,
    participants INTEGER,
    admitted     INTEGER,
    planned_price REAL,
    min_price    REAL,
    max_price    REAL,
    file_url     TEXT,
    PRIMARY KEY (announce_id, lot_number)
);
CREATE TABLE IF NOT EXISTS protocol_offers (
    announce_id  TEXT,
    lot_number   TEXT,
    supplier     TEXT,
    bin          TEXT,
    price        REAL,
    PRIMARY KEY (announce_id, lot_number, bin)
);
"""


def _num(s):
    s = re.sub(r"[^\d.,]", "", (s or "").replace("\xa0", "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse(text: str, announce_id: str, file_url: str):
    """Разбирает текст протокола на лоты и ценовые предложения."""
    lots, offers = [], []
    for chunk in LOT_SPLIT_RE.split(text)[1:]:
        m = LOT_NUM_RE.match(chunk)
        if not m:
            continue
        lot_number = m.group(1)

        rows = OFFER_RE.findall(chunk)
        prices = [p for p in (_num(r[2]) for r in rows) if p]
        cnt = COUNT_RE.search(chunk)
        planned = PLANNED_RE.search(chunk)

        lots.append({
            "announce_id": announce_id,
            "lot_number": lot_number,
            # Явное число из текста надёжнее разбора строк, но если строк
            # распознано больше — значит явное число не про этот лот.
            "participants": max(
                int(cnt.group(1)) if cnt else 0, len({r[1] for r in rows})
            ) or None,
            "planned_price": _num(planned.group(1)) if planned else None,
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "file_url": file_url,
        })
        for name, bin_, price, _total in rows:
            offers.append({
                "announce_id": announce_id,
                "lot_number": lot_number,
                "supplier": re.sub(r"\s+", " ", name).strip()[:200],
                "bin": bin_,
                "price": _num(price),
            })
    return lots, offers


def parse_html(text: str, announce_id: str, file_url: str):
    """Протокол итогов открытого конкурса (HTML). Русская часть документа."""
    lots, offers = [], []
    marks = list(LOT_RU_RE.finditer(text))
    for i, m in enumerate(marks):
        chunk = text[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        lot_number = m.group(1)

        sub = SUBMITTED_RE.search(chunk)
        adm = ADMITTED_RE.search(chunk)
        rej = REJECTED_RE.search(chunk)
        submitted = int(sub.group(1)) if sub else None
        admitted = int(adm.group(1)) if adm else None
        if submitted is None and adm and rej:
            submitted = int(adm.group(1)) + int(rej.group(1))

        lots.append({
            "announce_id": announce_id,
            "lot_number": lot_number,
            "participants": submitted,
            "admitted": admitted,
            "planned_price": None,
            "min_price": None,
            "max_price": None,
            "file_url": file_url,
        })
        for name, bin_ in BIDDER_RE.findall(chunk):
            offers.append({
                "announce_id": announce_id,
                "lot_number": lot_number,
                "supplier": re.sub(r"\s+", " ", name).strip()[:200],
                "bin": bin_,
                "price": None,
            })
    return lots, offers


def html_text(blob: bytes) -> str:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(blob.decode("utf-8", "ignore"))
    for node in tree.css("script,style"):
        node.decompose()
    return re.sub(r"\n{2,}", "\n", tree.text(separator="\n", strip=True))


def collect(portal: Portal, con, announce_ids: list[str], workers: int = 10) -> int:
    """Тянет протоколы по заданному списку объявлений."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    con.executescript(SCHEMA)
    already = {r[0] for r in con.execute("SELECT announce_id FROM done WHERE stage='protocol'")}
    todo = [a for a in announce_ids if a not in already]
    log.info("протоколов к сбору: %s", len(todo))

    def work(aid):
        """У конкурса протоколов несколько (вскрытие, допуск, итоги) — берём тот,
        из которого разбор дал число участников."""
        html = portal.announce(aid, "protocols")
        if not html:
            return aid, None
        best = None
        for url in dict.fromkeys(FILE_RE.findall(html)):
            blob = portal.download(url)
            if not blob:
                continue
            if blob[:4] == b"%PDF":
                lots, offers = parse(pdf_text(blob), aid, url)
            else:
                lots, offers = parse_html(html_text(blob), aid, url)
            if any(lot.get("participants") for lot in lots):
                return aid, (lots, offers)
            best = best or (lots, offers)
        return aid, best

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, a) for a in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                aid, parsed = fut.result()
            except Exception as e:
                log.warning("сбой потока: %s", e)
                continue
            if parsed:
                lots, offers = parsed
                upsert(con, "protocols", lots)
                upsert(con, "protocol_offers", offers)
            mark_done(con, aid, "protocol")
            done += 1
            if i % 300 == 0:
                con.commit()
                log.info("… %s/%s протоколов, ошибок %s", i, len(todo), portal.errors)
    con.commit()
    return done
