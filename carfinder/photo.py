"""
Распознавание текста на фотографии объявления.

Часть салонов не пишет о брони в описании, а закрывает первое фото баннером
«REZERWACJA — SAMOCHÓD JUŻ ZAREZERWOWANY PRZEZ INNEGO KLIENTA». В данных
объявления такой машины не отличить от свободной: цена, пробег и текст обычные,
а картинка у каждого объявления своя, так что и по совпадению URL их не поймать.

Единственный носитель информации — сами пиксели, поэтому превью прогоняется
через распознаватель текста Vision, встроенный в macOS: офлайн, без внешних
сервисов и без tesseract. Результат кешируется по URL картинки — фотография
объявления не меняется, и повторно её обрабатывать незачем.

Если модуль недоступен (не macOS или не установлен pyobjc), распознавание
молча выключается, а остальной конвейер работает как раньше.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from .models import Listing

log = logging.getLogger(__name__)

# (код, причина, регэксп по распознанному тексту без диакритики)
PHOTO_FLAGS: list[tuple[str, str, re.Pattern]] = [
    ("reserved", "на фото отметка о брони",
     re.compile(r"\b(rezerwacja|zarezerwowany|zarezerwowane|zarezerwowana)\b")),
    ("sold", "на фото отметка «продано»",
     re.compile(r"\b(sprzedany|sprzedane|sprzedana|sold)\b")),
    ("damaged", "на фото отметка о повреждении",
     re.compile(r"\b(uszkodzony|uszkodzone|powypadkowy|szkoda calkowita)\b")),
]

_vision = None
_unavailable = False


def _backend():
    """Ленивая загрузка Vision — импорт дорогой, а нужен не всегда."""
    global _vision, _unavailable
    if _vision is not None or _unavailable:
        return _vision
    try:
        import Quartz
        import Vision
        from Foundation import NSData
        _vision = (Vision, Quartz, NSData)
    except Exception as exc:
        log.info("распознавание текста на фото недоступно (%s) — шаг пропускается", exc)
        _unavailable = True
    return _vision


def available() -> bool:
    return _backend() is not None


def read_text(image_bytes: bytes) -> str:
    """Возвращает весь распознанный на картинке текст одной строкой."""
    backend = _backend()
    if backend is None:
        return ""
    Vision, Quartz, NSData = backend

    data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    if source is None:
        return ""
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        return ""

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(0)                       # accurate
    request.setRecognitionLanguages_(["pl-PL", "en-US"])
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, _ = handler.performRequests_error_([request], None)
    if not ok:
        return ""

    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates:
            lines.append(str(candidates[0].string()))
    return " ".join(lines)


def scan(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    plain = plain.replace("ł", "l").replace("Ł", "l").lower()
    return [code for code, _, pattern in PHOTO_FLAGS if pattern.search(plain)]


def reason(code: str) -> str:
    for c, label, _ in PHOTO_FLAGS:
        if c == code:
            return label
    return code


def run(listings: list[Listing], store, fetcher, cfg: dict) -> dict:
    conf = cfg.get("photo") or {}
    if not conf.get("enabled", True) or not available():
        return {"cached": 0, "scanned": 0, "flagged": 0}

    limit = conf.get("max_per_run", 120)
    cached = scanned = flagged = 0
    pending: list[Listing] = []

    for listing in listings:
        if not listing.image:
            continue
        known = store.get_photo_verdict(listing.image)
        if known is None:
            pending.append(listing)
            continue
        cached += 1
        if known:
            listing.photo_flags = known
            flagged += 1

    # Сначала лучшие кандидаты: именно они видны наверху и именно там
    # ошибочно показанная забронированная машина обиднее всего.
    pending.sort(key=lambda l: -l.score)
    for listing in pending[:limit]:
        try:
            found = scan(read_text(fetcher.get_bytes(listing.image)))
        except Exception as exc:
            log.debug("не удалось разобрать фото %s: %s", listing.image, exc)
            continue
        store.save_photo_verdict(listing.image, found)
        scanned += 1
        if found:
            listing.photo_flags = found
            flagged += 1

    skipped = max(0, len(pending) - limit)
    log.info("текст на фото: из кеша %s, распознано %s, с отметками %s%s",
             cached, scanned, flagged,
             f", отложено до следующего прогона {skipped}" if skipped else "")
    return {"cached": cached, "scanned": scanned, "flagged": flagged}
