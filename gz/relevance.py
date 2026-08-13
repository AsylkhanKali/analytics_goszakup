"""Отсев услуг, работ и вне-scope позиций из собранных лотов.

Поиск на портале морфологический, поэтому широкие слова тянут лишнее:
«объектив» совпадает с «объектов» и приводит услуги по обследованию объектов
недвижимости (3404 лота) и техническому освидетельствованию (1819).
Чистим здесь — до дорогих этапов, чтобы не тратить запросы на мусор.

Замер на собранных 89 968 лотах: отсеивается 18 411 (20.5%), остаётся 71 557.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Услуги, работы и категории, прямо исключённые из scope.
EXCLUDE = re.compile(
    r"услуг|работ[аы]\b|работам\b|ремонт|монтаж|обслуживани|строительств|проектн"
    r"|освидетельствован|обследован|изыскател|надзор"
    r"|уголь|нефт|мазут|бензин|дизельн|топлив|ГСМ\b"
    r"|цемент|щебен|арматур|кирпич|бетон|пиломатериал|асфальт"
    r"|питани|продукт|мясо|молок|хлеб|овощ"
    r"|страхован|аренд|обучени|подписк|лицензи|разработк",
    re.I,
)


def apply(con) -> tuple[int, int]:
    """Проставляет lots.relevant. Возвращает (релевантных, всего)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(lots)")}
    if "relevant" not in cols:
        con.execute("ALTER TABLE lots ADD COLUMN relevant INTEGER")
        con.execute("CREATE INDEX IF NOT EXISTS ix_lots_relevant ON lots(relevant)")

    rows = con.execute("SELECT lot_id, lot_name, lot_desc FROM lots").fetchall()
    upd = [
        (0 if EXCLUDE.search(f"{name or ''} {desc or ''}") else 1, lot_id)
        for lot_id, name, desc in rows
    ]
    con.executemany("UPDATE lots SET relevant = ? WHERE lot_id = ?", upd)
    con.commit()

    keep = sum(1 for r, _ in upd if r)
    log.info("релевантных лотов: %s из %s", keep, len(upd))
    return keep, len(upd)


def announcements(con) -> list[str]:
    """Объявления, где есть хотя бы один релевантный лот."""
    cur = con.execute("SELECT DISTINCT announce_id FROM lots WHERE relevant = 1")
    return [r[0] for r in cur]
