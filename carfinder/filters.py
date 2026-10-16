"""Жёсткие фильтры + эвристики для полей, которые продавцы часто не заполняют."""
from __future__ import annotations

from .geo import haversine
from .models import AWD_DRIVES, AWD_TEXT_RE, AUTO_TEXT_RE, LPG_TEXT_RE, Listing


def enrich(listing: Listing, cfg: dict) -> Listing:
    """Дозаполняем пропуски по тексту объявления и считаем расстояние."""
    if listing.drive in AWD_DRIVES:
        listing.awd_source = "param"
    elif not listing.drive and "drive" in listing.guaranteed:
        # otomoto не отдаёт «Napęd» в выдаче, но запрос уже ограничен 4x4.
        listing.drive = "all-wheel"
        listing.awd_source = "query"
    elif not listing.drive and AWD_TEXT_RE.search(listing.haystack):
        # Поле пустое и запрос его не гарантирует, но в названии есть xDrive/quattro/AWD.
        listing.drive = "all-wheel"
        listing.awd_source = "text"

    if not listing.gearbox and AUTO_TEXT_RE.search(listing.haystack):
        listing.gearbox = "automatic"

    if listing.fuel == "petrol" and LPG_TEXT_RE.search(listing.haystack):
        listing.fuel = "petrol-lpg"

    loc = cfg["location"]
    if listing.lat is not None and listing.lon is not None:
        listing.distance_km = round(
            haversine(loc["city_lat"], loc["city_lon"], listing.lat, listing.lon), 1
        )
    return listing


def apply(listing: Listing, cfg: dict) -> Listing:
    """
    Проставляет reject_reasons и near_miss.

    Пусто в reject_reasons -> объявление проходит в основную выдачу.
    """
    cr = cfg["criteria"]
    nm = cfg.get("near_miss") or {}
    reasons: list[str] = []
    soft: list[str] = []

    price_max = cr["price_max"]
    if listing.price is None:
        reasons.append("нет цены")
    elif listing.price > price_max:
        overshoot = (listing.price - price_max) / price_max * 100
        if nm.get("enabled") and overshoot <= nm.get("price_overshoot_pct", 0):
            soft.append(f"цена выше бюджета на {overshoot:.0f}%")
        else:
            reasons.append(f"цена {listing.price:,} > {price_max:,}".replace(",", " "))
    elif cr.get("price_min") and listing.price < cr["price_min"]:
        reasons.append(f"цена ниже {cr['price_min']} — вероятно битая/на запчасти")

    year_from = cr["year_from"]
    if listing.year is None:
        reasons.append("нет года выпуска")
    elif listing.year < year_from:
        slack = nm.get("year_slack", 0) if nm.get("enabled") else 0
        if listing.year >= year_from - slack:
            soft.append(f"{listing.year} г. вместо {year_from}+")
        else:
            reasons.append(f"{listing.year} г. < {year_from}")

    if cr.get("mileage_max") and listing.mileage and listing.mileage > cr["mileage_max"]:
        reasons.append(f"пробег {listing.mileage} км")

    gearboxes = cr.get("gearboxes") or []
    if gearboxes and listing.gearbox and listing.gearbox not in gearboxes:
        reasons.append("не автомат")
    elif gearboxes and not listing.gearbox:
        reasons.append("коробка не указана")

    drives = cr.get("drives") or []
    if drives and listing.drive not in set(drives) | {"all-wheel"}:
        reasons.append("не полный привод" if listing.drive else "привод не указан")

    if cr.get("exclude_damaged") and listing.condition == "damaged":
        reasons.append("повреждён")

    listing.reject_reasons = reasons
    listing.near_miss_reasons = soft
    listing.near_miss = bool(soft) and not reasons
    return listing
