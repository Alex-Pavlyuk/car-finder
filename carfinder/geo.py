"""
Справочник координат городов, который агент накапливает сам.

otomoto отдаёт только название города, без координат, — поэтому у части
объявлений не было расстояния, хотя «не дальше часа езды» это ключевой критерий.
OLX же присылает lat/lon в каждом объявлении. Складываем эти координаты в
таблицу `cities` и применяем их к otomoto-объявлениям по имени города.

Справочник растёт сам собой с каждым прогоном и не требует ни внешнего
геокодера, ни сетевых запросов: города в регионе повторяются от прогона
к прогону, так что покрытие быстро выходит на плато.
"""
from __future__ import annotations

import logging
import math
import unicodedata

from .models import Listing

log = logging.getLogger(__name__)


def normalize(city: str | None) -> str | None:
    """«Bielsko-Biała» и «bielsko-biala» — один и тот же ключ."""
    if not city:
        return None
    plain = unicodedata.normalize("NFKD", city.strip().lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return plain.replace("ł", "l").replace(" ", "-") or None


def learn(listings: list[Listing], store) -> int:
    """Запоминает координаты городов из объявлений, где они есть."""
    learned = 0
    for listing in listings:
        key = normalize(listing.city)
        if key and listing.lat is not None and listing.lon is not None:
            if store.save_city(key, listing.city, listing.lat, listing.lon):
                learned += 1
    if learned:
        log.info("справочник городов пополнен: +%s", learned)
    return learned


def locate(listings: list[Listing], store, cfg: dict) -> int:
    """Проставляет lat/lon/distance_km тем, у кого их нет."""
    known = store.all_cities()
    loc = cfg["location"]
    resolved = 0

    for listing in listings:
        if listing.distance_km is not None:
            continue
        if listing.lat is None:
            coords = known.get(normalize(listing.city))
            if coords is None:
                continue
            listing.lat, listing.lon = coords
        listing.distance_km = round(
            haversine(loc["city_lat"], loc["city_lon"], listing.lat, listing.lon), 1
        )
        resolved += 1

    unresolved = sum(1 for l in listings if l.distance_km is None)
    log.info("расстояние: определено дополнительно %s, осталось без координат %s",
             resolved, unresolved)
    return resolved


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
