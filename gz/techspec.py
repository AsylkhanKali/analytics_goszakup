"""Этап 3 — техспецификации: скачивание и извлечение полей.

Путь: вкладка «Документация» объявления → id группы документов → модальное окно
со списком файлов → PDF. Id группы меняется по годам (старые объявления — 1262,
«Приложение 12», новые — 3325, «Приложение 13»), поэтому он парсится из разметки,
а не задан константой.

Важное ограничение, установленное замером на 32 объявлениях: публичная
техспецификация — это документ ЗАКАЗЧИКА с требованиями к товару. Марка, страна
происхождения и завод-изготовитель в нём почти никогда не заполнены (это поля,
которые заполняет поставщик в своей заявке, а заявки не публикуются). Поэтому
здесь нет и не может быть догадок: не нашли значение — пишем «Нет данных»
и сохраняем ссылку на исходный PDF для ручной перепроверки.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from selectolax.parser import HTMLParser

from .client import Portal
from .store import NO_DATA, mark_done, pending, upsert

log = logging.getLogger(__name__)

GROUP_RE = re.compile(r"actionModalShowFiles\((\d+)\s*,\s*(\d+)\)")
TECHSPEC_ROW_RE = re.compile(r"ехническая\s+спецификаци", re.I)
FILE_RE = re.compile(r"(https://v3bl\.goszakup\.gov\.kz/files/download_file/\d+/\d+/?)")

# Значение должно идти после двоеточия — иначе поймаем слово из требования
# вида «предоставить изготовителя», а не заполненное поле.
FIELDS = {
    "brand": re.compile(r"(?:марка|товарный\s+знак)\s*[:：]\s*([^\n;,]{2,80})", re.I),
    "model": re.compile(r"модель\s*[:：]\s*([^\n;,]{2,80})", re.I),
    "country": re.compile(r"стран[аы]\s+происхождени\w*\s*[:：]\s*([^\n;,]{2,80})", re.I),
    "manufacturer": re.compile(
        r"(?:завод[-\s]*изготовител\w*|изготовитель|производитель)\s*[:：]\s*([^\n]{2,120})",
        re.I,
    ),
}


def pdf_text(blob: bytes) -> str:
    """PDF → текст. В выборке из 32 документов сканов не было, OCR не нужен."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as f:
        f.write(blob)
        f.flush()
        out = Path(f.name).with_suffix(".txt")
        try:
            subprocess.run(
                ["pdftotext", "-layout", f.name, str(out)],
                check=True, capture_output=True, timeout=90,
            )
            text = out.read_text(encoding="utf-8", errors="ignore")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return ""
        finally:
            out.unlink(missing_ok=True)
    return text


def extract(text: str) -> dict:
    res = {}
    for name, rx in FIELDS.items():
        m = rx.search(text)
        value = re.sub(r"\s+", " ", m.group(1)).strip(" .;:-") if m else ""
        res[name] = value if len(value) > 1 else NO_DATA
    return res


def find_group(html: str) -> str | None:
    """Id группы документов техспецификации из вкладки «Документация»."""
    tree = HTMLParser(html)
    for tr in tree.css("tr"):
        if not TECHSPEC_ROW_RE.search(tr.text()):
            continue
        m = GROUP_RE.search(tr.html or "")
        if m:
            return m.group(2)
    return None


def _one(portal: Portal, aid: str) -> dict:
    row = {"announce_id": aid, "doc_group": None, "file_name": None,
           "file_url": None, "text_chars": 0, "is_scan": 0,
           **{k: NO_DATA for k in FIELDS}}

    docs = portal.announce(aid, "documents")
    group = find_group(docs) if docs else None
    if not group:
        return row
    row["doc_group"] = group

    modal = portal.announce_files(aid, group)
    m = FILE_RE.search(modal or "")
    if not m:
        return row
    row["file_url"] = m.group(1)
    fn = re.search(r">\s*([^<>]+\.(?:pdf|doc|docx|xls|xlsx))\s*<", modal or "", re.I)
    row["file_name"] = fn.group(1).strip() if fn else None

    blob = portal.download(row["file_url"])
    if blob:
        text = pdf_text(blob) if blob[:4] == b"%PDF" else ""
        row["text_chars"] = len(text.strip())
        # Текста нет — значит скан; помечаем, но значения не выдумываем.
        row["is_scan"] = 1 if row["text_chars"] < 200 else 0
        if not row["is_scan"]:
            row.update(extract(text))
    return row


def collect(portal: Portal, con, limit: int | None = None,
            announce_ids: list[str] | None = None, workers: int = 10) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    todo = announce_ids if announce_ids is not None else pending(con, "techspec")
    already = {r[0] for r in con.execute("SELECT announce_id FROM done WHERE stage='techspec'")}
    todo = [a for a in todo if a not in already]
    if limit:
        todo = todo[:limit]
    log.info("техспек к сбору: %s (потоков %s)", len(todo), workers)

    done, batch = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, portal, a) for a in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                row = fut.result()
            except Exception as e:
                log.warning("сбой потока: %s", e)
                continue
            batch.append(row)
            mark_done(con, row["announce_id"], "techspec")
            done += 1
            if len(batch) >= 300:
                upsert(con, "techspecs", batch)
                batch = []
                log.info("… %s/%s техспек, ошибок %s", i, len(todo), portal.errors)
    upsert(con, "techspecs", batch)
    con.commit()
    return done
