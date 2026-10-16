"""
Уведомления macOS о новых стоящих объявлениях.

Без них агент пассивен: он обновляет отчёт в фоне, но узнать об этом можно,
только открыв файл. Уведомление приходит только на действительно новые
объявления с рейтингом выше порога — иначе оно быстро превращается в шум,
который перестают замечать.

Клик по уведомлению открывает отчёт.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from .models import Listing

log = logging.getLogger(__name__)


def _osascript(script: str) -> None:
    subprocess.run(["osascript", "-e", script], check=False,
                   capture_output=True, timeout=10)


def banner(title: str, text: str, subtitle: str | None = None) -> None:
    """
    Разовое уведомление в обход rumps.

    Нужно для сообщений до старта приложения — например «уже запущен»:
    `rumps.notification` требует поднятого NSApplication, которого в этот
    момент ещё нет.
    """
    if not shutil.which("osascript"):
        return
    script = f"display notification {_quote(text)} with title {_quote(title)}"
    if subtitle:
        script += f" subtitle {_quote(subtitle)}"
    _osascript(script)


def send(new_listings: list[Listing], cfg: dict, report_path) -> int:
    conf = cfg.get("notify") or {}
    if not conf.get("enabled") or not new_listings:
        return 0
    if not shutil.which("osascript"):
        log.debug("osascript недоступен — уведомления пропущены")
        return 0

    min_score = conf.get("min_score", 70)
    max_items = conf.get("max_per_run", 5)

    worthy = sorted(
        (l for l in new_listings if l.score >= min_score and not l.near_miss),
        key=lambda l: -l.score,
    )
    if not worthy:
        return 0

    for listing in worthy[:max_items]:
        price = f"{listing.price:,}".replace(",", " ") if listing.price else "?"
        bits = [f"{price} zł", f"{listing.year} г."]
        if listing.mileage is not None:
            bits.append(f"{listing.mileage // 1000} тыс. км")
        if listing.city:
            bits.append(listing.city)
        if listing.market_delta and listing.market_delta > 0:
            bits.append(f"−{listing.market_delta // 1000} тыс. к похожим")

        _osascript(
            'display notification {} with title {} subtitle {}'.format(
                _quote(" · ".join(bits)),
                _quote(listing.title[:60]),
                _quote(f"Car Finder · рейтинг {listing.score}"),
            )
        )

    extra = len(worthy) - max_items
    if extra > 0:
        _osascript(
            'display notification {} with title {}'.format(
                _quote(f"…и ещё {extra} — смотрите отчёт"), _quote("Car Finder")
            )
        )

    log.info("отправлено уведомлений: %s", min(len(worthy), max_items))
    return min(len(worthy), max_items)


def _quote(text: str) -> str:
    """Строковый литерал AppleScript."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
