"""HTTP-клиент к порталу госзакупок.

Проверено разведкой: публичные реестры отдают 200 обычному HTTP-клиенту,
браузер и сессия не нужны. Портал медленный (~1 с на страницу, ~12 с на
страницу в 2000 записей), поэтому запросы строго последовательные.

Всё сырьё кладётся на диск: повторный парсинг возможен без похода на портал.
Кэш же обеспечивает возобновляемость — прерванный прогон продолжается с места
обрыва, уже скачанное не перезапрашивается.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path

import httpx

from .refs import BASE

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


class Portal:
    """Потокобезопасен. Скорость регулируется сама: на отказах портала
    штрафная пауза растёт, на успехах — затухает. Так прогон идёт быстро,
    но при первых признаках недовольства сервера сам сбавляет темп."""

    def __init__(self, delay: float = 0.7, timeout: float = 90.0, retries: int = 4):
        self.delay = delay
        self.retries = retries
        self.client = httpx.Client(
            headers={
                "User-Agent": UA,
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self.penalty = 0.0
        self._lock = threading.Lock()
        RAW.mkdir(parents=True, exist_ok=True)

    def _slow_down(self):
        with self._lock:
            self.errors += 1
            self.penalty = min(self.penalty * 2 + 1.0, 30.0)
            return self.penalty

    def _speed_up(self):
        with self._lock:
            if self.penalty:
                self.penalty = max(0.0, self.penalty * 0.85 - 0.05)

    # --- кэш ------------------------------------------------------------
    def _path(self, method: str, url: str, params, data, suffix: str) -> Path:
        key = json.dumps([method, url, params, data], sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256(key.encode()).hexdigest()[:24]
        return RAW / h[:2] / f"{h}{suffix}"

    # --- запрос ---------------------------------------------------------
    def disk_free_gb(self) -> float:
        return shutil.disk_usage(RAW).free / 1e9

    def fetch(
        self,
        url: str,
        *,
        params=None,
        data=None,
        suffix: str = ".html",
        binary: bool = False,
        cache: bool = True,
    ) -> bytes | str | None:
        """Возвращает тело ответа. None — если не удалось.

        `cache=False` — не класть на диск. Так качаются PDF: они занимают
        основной объём, а нужен из них только извлечённый текст; ссылка на
        исходник всё равно хранится в базе для ручной перепроверки.
        """
        method = "POST" if data is not None else "GET"
        if not cache:
            return self._request(method, url, params, data)

        path = self._path(method, url, params, data, suffix)
        if path.exists():
            with self._lock:
                self.hits += 1
            blob = path.read_bytes()
            return blob if binary else blob.decode("utf-8", "ignore")

        blob = self._request(method, url, params, data)
        if blob is None:
            return None

        with self._lock:
            self.misses += 1
            # Диск почти полон — работаем дальше, но перестаём кэшировать.
            # Прогон важнее возможности перепарсить из кэша.
            if self.misses % 200 == 0:
                self._low_disk = self.disk_free_gb() < 1.5
        if getattr(self, "_low_disk", False):
            return blob if binary else blob.decode("utf-8", "ignore")

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.part")
        tmp.write_bytes(blob)
        tmp.replace(path)  # атомарно: незавершённый файл не станет «кэшем»
        return blob if binary else blob.decode("utf-8", "ignore")

    def _request(self, method, url, params, data) -> bytes | None:
        for attempt in range(self.retries):
            try:
                time.sleep(self.delay + self.penalty)
                headers = {"X-Requested-With": "XMLHttpRequest"} if data is not None else {}
                r = self.client.request(method, url, params=params, data=data, headers=headers)
                if r.status_code == 200:
                    # Портал умеет отдавать ошибку как 200 + JSON — проверяем тело.
                    body = r.content
                    if body[:1] == b"{" and b'"status":"error"' in body[:400]:
                        log.warning("портал вернул ошибку в теле 200: %s", url)
                        return None
                    self._speed_up()
                    return body
                if r.status_code in (403, 429, 500, 502, 503, 504):
                    p = self._slow_down()
                    log.warning("HTTP %s, штраф %.1f с — %s", r.status_code, p, url)
                    time.sleep(2 ** attempt)
                    continue
                log.warning("HTTP %s на %s — пропуск", r.status_code, url)
                return None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                p = self._slow_down()
                log.warning("%s, штраф %.1f с — %s", type(e).__name__, p, url)
                time.sleep(2 ** attempt)
        log.error("не удалось получить %s после %s попыток", url, self.retries)
        return None

    # --- удобные обёртки ------------------------------------------------
    def lots(self, params):
        return self.fetch(f"{BASE}/ru/search/lots", params=params)

    def announce(self, aid: int | str, tab: str):
        return self.fetch(f"{BASE}/ru/announce/index/{aid}", params={"tab": tab})

    def announce_files(self, aid: int | str, group: int | str):
        return self.fetch(f"{BASE}/ru/announce/actionAjaxModalShowFiles/{aid}/{group}")

    def contract_units(self, cid: int | str):
        return self.fetch(f"{BASE}/ru/egzcontract/cpublic/units/{cid}")

    def load_unit(self, cid: int | str, unit_id: int | str):
        return self.fetch(
            f"{BASE}/ru/egzcontract/cpublic/loadunit",
            data={"pid": str(cid), "unit_id": str(unit_id)},
        )

    def download(self, url: str) -> bytes | None:
        """PDF на диск не кладём — см. fetch(cache=False)."""
        return self.fetch(url, suffix=".bin", binary=True, cache=False)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
