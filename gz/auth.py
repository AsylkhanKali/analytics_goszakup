"""Модуль управления авторизованной сессией Goszakup (goszakup.gov.kz).

Хранит cookie ci_session и User-Agent в data/session.json.
Позволяет проверять активность сессии и запрашивать данные под авторизацией.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

SESSION_FILE = Path(__file__).resolve().parent.parent / "data" / "session.json"
BASE_URL = "https://goszakup.gov.kz"


def load_session() -> Optional[Dict[str, str]]:
    """Загружает сохранённую сессию из data/session.json."""
    if not SESSION_FILE.exists():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if data.get("ci_session") and data.get("user_agent"):
            return data
    except Exception as e:
        log.warning("Не удалось прочитать session.json: %s", e)
    return None


def save_session(ci_session: str, user_agent: str) -> None:
    """Сохраняет куки и User-Agent в data/session.json."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ci_session": ci_session.strip(),
        "user_agent": user_agent.strip(),
        "updated_at": datetime.now().isoformat(),
    }
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Сессия успешно сохранена в %s", SESSION_FILE)


def check_session_active(session_data: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Проверяет, жива ли кука ci_session делая тестовый запрос к goszakup.gov.kz."""
    s = session_data or load_session()
    if not s:
        return False, "Сессия не найдена. Сохраните ci_session."

    headers = {"User-Agent": s["user_agent"]}
    cookies = {"ci_session": s["ci_session"]}

    try:
        with httpx.Client(timeout=15.0, headers=headers, cookies=cookies, follow_redirects=True) as client:
            resp = client.get(f"{BASE_URL}/ru/registry/contract")
            if resp.status_code == 200 and "login" not in str(resp.url).lower():
                return True, "Авторизован (200 OK)"
            return False, f"Перенаправление или сбой (код: {resp.status_code})"
    except Exception as e:
        return False, f"Ошибка подключения: {e}"
