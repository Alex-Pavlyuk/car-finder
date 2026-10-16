"""
Детектор аномально низкой цены.

Зачем: фильтр «Nieuszkodzony» на площадках опирается на честность продавца.
Битый или юридически проблемный автомобиль, помеченный как целый, выдаёт себя
ценой — 2021 RAV4 Hybrid за 29 900 zł при ожидаемых 70 000.

Основной сигнал берётся из market.py: цена сильно ниже ожидаемой по своей же
модели. Если сопоставимых по модели не нашлось, работает грубый запасной
критерий — сравнение с медианой по году выпуска.

Такие объявления не выбрасываются (иногда это действительно срочная продажа),
а помечаются флагом и штрафуются в скоринге, чтобы не занимать верх выдачи.
"""
from __future__ import annotations

import logging
from statistics import median

from .models import Listing

log = logging.getLogger(__name__)

MODEL_RATIO = 0.60    # дешевле 60% ожидаемой цены по своей модели
FALLBACK_RATIO = 0.40  # дешевле 40% медианы по году — когда модель не с чем сравнить
MIN_FALLBACK_PEERS = 3


def flag(listings: list[Listing]) -> int:
    """Проставляет listing.suspicious_price. Возвращает число помеченных."""
    priced = [l for l in listings if l.price and l.year]
    by_year: dict[int, list[int]] = {}
    for listing in priced:
        by_year.setdefault(listing.year, []).append(listing.price)

    flagged = 0
    for listing in priced:
        reason = None

        if listing.market_expected:
            if listing.price < listing.market_expected * MODEL_RATIO:
                reason = (f"дешевле ожидаемой по модели "
                          f"({listing.market_expected:,} zł)".replace(",", " "))
        else:
            same_year = [p for p in by_year.get(listing.year, []) if p != listing.price]
            if len(same_year) >= MIN_FALLBACK_PEERS:
                ref = median(same_year)
                if listing.price < ref * FALLBACK_RATIO:
                    reason = (f"дешевле медианы по {listing.year} г. "
                              f"({int(ref):,} zł)".replace(",", " "))

        if reason:
            listing.suspicious_price = reason
            flagged += 1

    if flagged:
        log.info("подозрительно низкая цена: %s объявлений", flagged)
    return flagged
