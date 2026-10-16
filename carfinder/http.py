"""
HTTP-слой.

И otomoto, и olx стоят за CloudFront, который отбрасывает запросы по TLS-отпечатку:
обычный `requests`/`curl` получает 403 ещё до применения заголовков. `curl_cffi`
имперсонирует TLS-стек Chrome, и это единственная причина, по которой он тут нужен.
"""
from __future__ import annotations

import logging
import random
import threading
import time

from curl_cffi import requests

log = logging.getLogger(__name__)


class Cancelled(BaseException):
    """
    Прогон прерван снаружи — обычно потому, что приложение закрывают.

    Наследуется от `BaseException`, а не от `Exception`, по той же причине, что
    и `KeyboardInterrupt`: конвейер весь построен на «падение одного объявления
    или одной площадки не роняет прогон», и десяток `except Exception` вокруг
    сетевых вызовов гасил бы отмену, продолжая опрашивать дальше по списку.
    """

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


class Fetcher:
    """
    Сетевой слой одного прогона.

    Прогон — это сотни запросов с троттлингом, то есть большую часть минуты
    процесс просто ждёт. Поэтому именно здесь стоит точка отмены: `stop`
    прерывает ожидание немедленно, и прогон сворачивается на первом же
    запросе, а не через полминуты. Без этого закрытие приложения посреди
    прогона означало бы либо ожидание, либо обрыв на полуслове.
    """

    def __init__(self, timeout: int = 40, retries: int = 3, delay: float = 1.2,
                 stop: threading.Event | None = None):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.stop = stop or threading.Event()
        self.session = requests.Session(impersonate="chrome")
        self._last_request = 0.0

    def close(self) -> None:
        """Закрывает соединения. Один прогон — один Fetcher, держать нечего."""
        try:
            self.session.close()
        except Exception as exc:
            log.debug("не удалось закрыть сессию: %s", exc)

    def get(self, url: str, referer: str | None = None) -> str:
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.text
                last_error = RuntimeError(f"HTTP {resp.status_code}")
                log.warning("%s -> HTTP %s (попытка %s/%s)",
                            url, resp.status_code, attempt, self.retries)
            except Exception as exc:  # сеть, таймаут, TLS
                last_error = exc
                log.warning("%s -> %s (попытка %s/%s)", url, exc, attempt, self.retries)
            self._sleep(min(2 ** attempt, 10) + random.random())

        raise RuntimeError(f"Не удалось загрузить {url}: {last_error}")

    def get_bytes(self, url: str) -> bytes:
        """Загрузка картинки. Отдаётся CDN, троттлинг выдачи тут не нужен."""
        self.check_cancelled()
        resp = self.session.get(url, headers={"Accept": "image/*,*/*;q=0.8"},
                                timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} для {url}")
        return resp.content

    def check_cancelled(self) -> None:
        if self.stop.is_set():
            raise Cancelled("прогон прерван")

    def _throttle(self) -> None:
        self.check_cancelled()
        elapsed = time.monotonic() - self._last_request
        wait = self.delay - elapsed
        if wait > 0:
            self._sleep(wait + random.uniform(0, 0.4))
        self._last_request = time.monotonic()

    def _sleep(self, seconds: float) -> None:
        """Пауза, прерываемая отменой: ждём на событии, а не на `time.sleep`."""
        if self.stop.wait(seconds):
            raise Cancelled("прогон прерван")
