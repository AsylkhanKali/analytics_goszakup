#!/usr/bin/env python3
"""Автономный прогон оставшихся этапов: карточки → техспеки → протоколы → отчёт.

Ждёт завершения этапа enrich, если тот ещё идёт, затем отрабатывает без участия
человека. Все этапы возобновляемы: повторный запуск не перезапрашивает
скачанное, поэтому скрипт можно перезапускать после любого обрыва.

Техспеки и протоколы собираются не по всем 47 тысячам объявлений, а только
по тем, чьи договоры реально попали в выборку, — так решил владелец.
"""

import logging
import subprocess
import sys
import time

from gz.cards import collect as collect_cards
from gz.client import Portal
from gz.protocols import collect as collect_protocols
from gz.store import connect
from gz.techspec import collect as collect_techspecs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("run_all")


def wait_for_enrich():
    while subprocess.run(["pgrep", "-f", "cli.py enrich"], capture_output=True).returncode == 0:
        log.info("этап enrich ещё идёт, жду…")
        time.sleep(60)


def matched_announcements(con) -> list[str]:
    """Объявления, где наш релевантный лот привёл к договору."""
    cur = con.execute(
        "SELECT DISTINCT l.announce_id FROM lots l "
        "JOIN contracts ct ON ct.announce_id = l.announce_id "
        "                 AND ct.lot_number = l.lot_number "
        "WHERE l.relevant = 1"
    )
    return [r[0] for r in cur]


def main():
    wait_for_enrich()
    con = connect()
    with Portal(delay=0.1) as portal:
        ids = matched_announcements(con)
        log.info("объявлений с состоявшимися договорами: %s", len(ids))

        log.info("=== карточки договоров ===")
        collect_cards(portal, con, workers=10)

        log.info("=== техспецификации ===")
        collect_techspecs(portal, con, announce_ids=ids, workers=10)

        log.info("=== протоколы итогов ===")
        collect_protocols(portal, con, ids, workers=10)

        log.info("ошибок за прогон: %s, кэш hits=%s misses=%s",
                 portal.errors, portal.hits, portal.misses)

    log.info("=== отчёт ===")
    from gz.analyze import report

    path = report(con)
    log.info("ГОТОВО: %s", path)


if __name__ == "__main__":
    main()
