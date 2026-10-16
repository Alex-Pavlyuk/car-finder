"""
Оценка цены относительно похожих предложений.

Главный вопрос покупателя — не «подходит ли машина под фильтры», а «нормальная
ли это цена». Считаем ожидаемую цену по сопоставимым объявлениям из той же
выдачи и показываем отклонение: «−12 300 zł дешевле похожих».

Сопоставимые — только **та же марка и модель**. Сравнение по марке пробовали,
и оно врёт: Audi Q7 попадала в одну группу с A3, а «весь рынок того же года»
смешивает Dacia Duster с BMW X5. Если сопоставимых мало, расширяется окно лет
(±1 → ±3), а не круг моделей. Оценка получается у ~80% выдачи — это честнее,
чем 100% мусорных оценок.

Цены сопоставимых приводятся к пробегу оцениваемой машины, иначе «дёшево»
означало бы просто «больше проехало».

Ограничение: выдача обрезана бюджетом сверху, поэтому дорогой хвост рынка в
выборку не попадает. «Дешевле похожих» надёжно, «дороже похожих» систематически
занижено — поэтому в скоринге используется только первая половина шкалы.
"""
from __future__ import annotations

import logging
from statistics import median

from .models import Listing

log = logging.getLogger(__name__)

MIN_PEERS = 4                # меньше — оценка не имеет смысла
YEAR_WINDOWS = (1, 2, 3)     # расширяем окно лет, но не круг моделей
MIN_PLAUSIBLE_MILEAGE = 1000  # «318 км» у машины 2019 года — опечатка продавца
MAX_MILEAGE_ADJUST = 0.35    # поправка на пробег не больше 35% цены


def _trustworthy_mileage(listing: Listing, current_year: int) -> bool:
    """Отсекает явно недостоверный пробег, который ломает поправку."""
    if listing.mileage is None:
        return False
    if listing.mileage < MIN_PLAUSIBLE_MILEAGE and (listing.year or current_year) < current_year - 1:
        return False
    return True


def _peers(listing: Listing, by_model: dict[tuple[str, str], list[Listing]]) -> tuple[list[Listing], int]:
    group = by_model.get(((listing.make or "").lower(), (listing.model or "").lower()), [])
    for window in YEAR_WINDOWS:
        peers = [
            o for o in group
            if o is not listing and abs((o.year or 0) - (listing.year or 0)) <= window
        ]
        if len(peers) >= MIN_PEERS:
            return peers, window
    return [], 0


def evaluate(listings: list[Listing], cfg: dict, current_year: int | None = None) -> int:
    """Проставляет market_expected / market_delta / market_basis."""
    conf = cfg.get("market") or {}
    if not conf.get("enabled", True):
        return 0

    from datetime import datetime
    current_year = current_year or datetime.now().year
    per_km = conf.get("price_per_km", 0.12)

    pool = [l for l in listings if l.price and l.year and l.make and l.model]
    by_model: dict[tuple[str, str], list[Listing]] = {}
    for listing in pool:
        by_model.setdefault((listing.make.lower(), listing.model.lower()), []).append(listing)

    evaluated = 0
    for listing in pool:
        peers, window = _peers(listing, by_model)
        if not peers:
            continue

        own_mileage_ok = _trustworthy_mileage(listing, current_year)
        adjusted = []
        for peer in peers:
            price = float(peer.price)
            if own_mileage_ok and _trustworthy_mileage(peer, current_year):
                # Если у сопоставимого пробег меньше, его цена «завышена»
                # относительно нашей машины — и наоборот.
                shift = (listing.mileage - peer.mileage) * per_km
                cap = peer.price * MAX_MILEAGE_ADJUST
                price -= max(-cap, min(cap, shift))
            adjusted.append(price)

        expected = int(round(median(adjusted)))
        listing.market_expected = expected
        listing.market_delta = expected - listing.price
        years = "" if window == 1 else f" ±{window} г."
        listing.market_basis = f"{listing.make} {listing.model}{years}, {len(peers)} шт."
        evaluated += 1

    below = sum(1 for l in pool if (l.market_delta or 0) > 0)
    log.info("оценка цены: посчитано %s из %s, дешевле похожих %s",
             evaluated, len(pool), below)
    return evaluated
