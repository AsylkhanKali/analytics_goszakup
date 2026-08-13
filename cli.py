#!/usr/bin/env python3
"""Точка входа: этапы сбора и аналитики.

    .venv/bin/python cli.py fetch_specs --workers 12    извлечь ВСЕ марки, заводы и файлы (без ошибок 503)
    .venv/bin/python cli.py index                        скачать ВСЕ договоры (Товар + Конкурс 2024-2026) из Реестра
    .venv/bin/python cli.py bot                          запустить Telegram-бот
"""

import argparse
import logging
import sys

from gz.client import Portal
from gz.store import connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "stage",
        choices=["index", "crawl", "bot", "report", "auth", "discover", "enrich", "cards", "units", "specs", "protocols", "techspec", "fetch_specs", "sync"],
    )
    ap.add_argument("--categories", nargs="*", help="спорт медицина техника")
    ap.add_argument("--years", nargs="*")
    ap.add_argument("--limit", type=int, help="ограничить число объявлений (пилот)")
    ap.add_argument("--shard", type=str, default="1/1", help="номер воркера и всего воркеров, напр: 1/3")
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    con = connect()

    if args.stage == "fetch_specs":
        from gz.live_fetch import fetch_all_open_tender_specs

        fetch_all_open_tender_specs(con, workers=args.workers, limit=args.limit)
        return

    if args.stage == "index":
        from gz.cpublic_indexer import index_all_goods_open_competitions

        index_all_goods_open_competitions(con)
        return

    if args.stage == "crawl":
        from gz.browser_crawler import run_authenticated_crawler

        run_authenticated_crawler(con, limit=args.limit, num_workers=args.workers)
        return

    if args.stage == "auth":
        from gz.auth import interactive_auth_cli

        interactive_auth_cli()
        return

    if args.stage == "bot":
        from bot import run_bot

        run_bot()
        return

    if args.stage == "report":
        from gz.analyze import report

        report(con)
        return

    with Portal(delay=args.delay) as portal:
        if args.stage in ("sync", "discover"):
            from gz.discover import discover
            from gz.enrich import enrich
            from gz.relevance import apply

            n = discover(portal, con, args.categories, args.years)
            apply(con)
            n_enrich = enrich(portal, con, limit=args.limit, workers=args.workers)
            logging.info("Синхронизация завершена: подтянуто лотов: %s, договоров: %s", n, n_enrich)
        elif args.stage == "enrich":
            from gz.enrich import enrich
            from gz.relevance import apply

            apply(con)
            n = enrich(portal, con, limit=args.limit, workers=args.workers)
            logging.info("обработано объявлений: %s", n)
        elif args.stage == "cards":
            from gz.cards import collect

            n = collect(portal, con, workers=args.workers)
            logging.info("собрано карточек: %s", n)
        elif args.stage == "units":
            from gz.units import collect

            n = collect(portal, con, workers=args.workers)
            logging.info("собрано предметов договора: %s", n)
        elif args.stage == "specs":
            from pathlib import Path

            from gz.specs import ingest

            total, matched = ingest(con, Path("data/techspec_manual"))
            logging.info("техспецификаций разобрано: %s, привязано к лотам: %s", total, matched)
        elif args.stage == "protocols":
            from gz.protocols import collect

            cur = con.execute(
                "SELECT DISTINCT announce_id FROM lots "
                "WHERE relevant = 1 AND method = 'Открытый конкурс'"
            )
            n = collect(portal, con, [r[0] for r in cur], workers=args.workers)
            logging.info("обработано объявлений: %s", n)
        elif args.stage == "techspec":
            from gz.techspec import collect

            n = collect(portal, con, limit=args.limit)
            logging.info("обработано техспек: %s", n)
        logging.info("кэш: hits=%s misses=%s", portal.hits, portal.misses)


if __name__ == "__main__":
    main()
