"""SQLite-хранилище нормализованных данных.

Сырьё (HTML/PDF) лежит отдельно в data/raw и сюда не дублируется — здесь
только разобранные поля плюс ссылка на исходный документ, чтобы любое
извлечённое значение можно было перепроверить вручную.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "goszakup.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    lot_id        TEXT PRIMARY KEY,
    announce_id   TEXT NOT NULL,
    lot_number    TEXT,
    announce_name TEXT,
    lot_name      TEXT,
    lot_desc      TEXT,
    qty           REAL,
    amount        REAL,
    method        TEXT,
    status        TEXT,
    year          TEXT,
    category      TEXT,
    keyword       TEXT
);
CREATE INDEX IF NOT EXISTS ix_lots_announce ON lots(announce_id);
CREATE INDEX IF NOT EXISTS ix_lots_category ON lots(category);

-- Один лот может дать несколько договоров, поэтому ключ составной.
CREATE TABLE IF NOT EXISTS contracts (
    contract_id     TEXT,
    announce_id     TEXT NOT NULL,
    lot_number      TEXT,
    lot_title       TEXT,
    contract_number TEXT,
    contract_status TEXT,
    plan_amount     REAL,   -- плановая сумма лота = начальная цена
    contract_amount REAL,   -- сумма по итогам закупки
    executed_amount REAL,
    supplier_name   TEXT,
    supplier_bin    TEXT,
    winner_status   TEXT,
    PRIMARY KEY (announce_id, lot_number, contract_number)
);
CREATE INDEX IF NOT EXISTS ix_contracts_cid ON contracts(contract_id);
CREATE INDEX IF NOT EXISTS ix_contracts_bin ON contracts(supplier_bin);

CREATE TABLE IF NOT EXISTS contract_units (
    unit_id      TEXT PRIMARY KEY,
    contract_id  TEXT NOT NULL,
    ktru         TEXT,
    name         TEXT,
    qty          REAL,
    unit         TEXT,
    price_per_unit REAL,   -- из таблицы позиций: у плательщиков НДС — с НДС
    price_no_vat REAL,     -- из карточки позиции, подписана явно
    amount       REAL,
    short_char   TEXT,
    extra_char   TEXT,
    customer     TEXT,
    delivery     TEXT,
    doc_url      TEXT
);
CREATE INDEX IF NOT EXISTS ix_units_contract ON contract_units(contract_id);

CREATE TABLE IF NOT EXISTS techspecs (
    announce_id  TEXT PRIMARY KEY,
    doc_group    TEXT,
    file_name    TEXT,
    file_url     TEXT,          -- ссылка на исходник для ручной перепроверки
    text_chars   INTEGER,
    is_scan      INTEGER,       -- 1 = текста нет, нужен OCR
    brand        TEXT,
    model        TEXT,
    country      TEXT,
    manufacturer TEXT
);

-- Объявления, по которым этап обогащения уже отработал (в т.ч. пустые).
CREATE TABLE IF NOT EXISTS done (
    announce_id TEXT,
    stage       TEXT,
    PRIMARY KEY (announce_id, stage)
);
"""

NO_DATA = "Нет данных"


def connect(path: Path = DB) -> sqlite3.Connection:
    err_path = Path("data/setup_error.txt")
    if err_path.exists():
        with open(err_path, "r") as f:
            err_msg = f.read()
        raise Exception(f"DB Setup failed: {err_msg}")
        
    tmp_path = Path("data/goszakup.db.tmp")
    if tmp_path.exists() or not path.exists():
        raise Exception("Установка базы данных... Пожалуйста, подождите 2-3 минуты и обновите страницу.")
        
    con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def upsert(con: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0])
    sql = (
        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})"
    )
    for attempt in range(5):
        try:
            con.executemany(sql, [[r.get(c) for c in cols] for r in rows])
            con.commit()
            return len(rows)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                time.sleep(0.5 * (attempt + 1))
            else:
                raise
    return len(rows)


def mark_done(con: sqlite3.Connection, announce_id: str, stage: str) -> None:
    for attempt in range(5):
        try:
            con.execute("INSERT OR IGNORE INTO done VALUES (?,?)", (announce_id, stage))
            con.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                time.sleep(0.5 * (attempt + 1))
            else:
                raise


def pending(con: sqlite3.Connection, stage: str) -> list[str]:
    """Объявления из lots, по которым этап stage ещё не отрабатывал."""
    cur = con.execute(
        "SELECT DISTINCT l.announce_id FROM lots l "
        "LEFT JOIN done d ON d.announce_id = l.announce_id AND d.stage = ? "
        "WHERE d.announce_id IS NULL",
        (stage,),
    )
    return [r[0] for r in cur]
