"""Модуль и CLI-скрипт создания резервных копий базы данных `data/goszakup.db`.

Сохраняет снимки базы в папку `data/backups/` с меткой времени.
Поддерживает мгновенный фоллбэк к любой предыдущей версии.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path("data/goszakup.db")
BACKUP_DIR = Path("data/backups")


def create_backup(label: str = "manual") -> Path:
    """Создаёт безопасную копию базы данных с помощью SQLite online backup API."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"База данных {DB_PATH} не найдена!")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"goszakup_{timestamp}_{label}.db"
    backup_path = BACKUP_DIR / backup_filename

    # Используем надежный sqlite3 backup API для атомарного снимка
    source_con = sqlite3.connect(DB_PATH)
    dest_con = sqlite3.connect(backup_path)

    try:
        source_con.backup(dest_con)
        log.info(f"Создан бэкап: {backup_path}")
        print(f"✅ Создан бэкап базы данных: {backup_path}")
    finally:
        source_con.close()
        dest_con.close()

    return backup_path


def list_backups() -> list[Path]:
    """Возвращает список всех имеющихся бэкапов, отсортированных от свежих к старым."""
    if not BACKUP_DIR.exists():
        return []
    backups = sorted(BACKUP_DIR.glob("goszakup_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return backups


def restore_backup(backup_path: Path | str) -> bool:
    """Восстанавливает базу данных из указанного бэкапа."""
    target_backup = Path(backup_path)
    if not target_backup.exists():
        print(f"❌ Файл бэкапа {target_backup} не существует!")
        return False

    # Создаём предохранительную копию перед откатом
    print("⏳ Создаем автоматический дамп текущей базы перед откатом...")
    create_backup(label="pre_restore_safety")

    # Копируем файл бэкапа поверх основной базы
    shutil.copy2(target_backup, DB_PATH)
    print(f"✅ База данных успешно восстановлена из: {target_backup.name}")
    return True


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        if len(sys.argv) > 2:
            restore_backup(sys.argv[2])
        else:
            backups = list_backups()
            if not backups:
                print("Нет доступных бэкапов!")
            else:
                print("Доступные бэкапы:")
                for i, b in enumerate(backups, 1):
                    size_mb = b.stat().st_size / (1024 * 1024)
                    print(f"  {i}. {b.name} ({size_mb:.1f} MB)")
    else:
        label = sys.argv[1] if len(sys.argv) > 1 else "checkpoint"
        create_backup(label=label)
