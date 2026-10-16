"""
Скоринг: 0..100. Все веса вынесены в config.yaml -> scoring.

Логика намеренно прозрачная и аддитивная — в отчёте показывается разбивка
по слагаемым, чтобы можно было понять, почему объявление оказалось наверху.
"""
from __future__ import annotations

from .models import Listing


def score(listing: Listing, cfg: dict) -> Listing:
    s = cfg["scoring"]
    cr = cfg["criteria"]
    parts: dict[str, float] = {"база": s.get("base", 50)}

    # Тип топлива: гибрид/ГБО — приоритет
    if listing.fuel:
        bonus = (s.get("fuel_bonus") or {}).get(listing.fuel)
        if bonus:
            parts["топливо"] = bonus

    # Марка/модель
    haystack = listing.haystack
    best_label, best_value = None, 0
    for needle, bonus in (s.get("model_bonus") or {}).items():
        if needle.lower() in haystack and bonus > best_value:
            best_label, best_value = needle, bonus
    if best_label:
        parts[f"модель ({best_label})"] = best_value

    # Пробег
    if listing.mileage is not None:
        for threshold, bonus in (s.get("mileage_bonus") or []):
            if listing.mileage <= threshold:
                parts["пробег"] = bonus
                break

    # Год
    if listing.year:
        per_year = s.get("year_bonus_per_year", 0)
        parts["год"] = min((listing.year - cr["year_from"]) * per_year, 15)

    # Запас по бюджету
    if listing.price and cr.get("price_max"):
        margin = max(0.0, (cr["price_max"] - listing.price) / cr["price_max"])
        parts["запас по цене"] = round(margin * s.get("price_margin_weight", 0), 1)

    # Выгода относительно похожих предложений. Учитываем только положительную
    # сторону: выдача обрезана бюджетом сверху, поэтому «дороже рынка»
    # систематически занижено и штрафовать по нему было бы нечестно.
    if listing.market_delta and listing.market_delta > 0 and listing.price:
        share = min(listing.market_delta / listing.price, 0.5)
        parts["дешевле похожих"] = round(share * s.get("market_delta_weight", 0), 1)

    # Частник
    if listing.is_business is False and s.get("private_seller_bonus"):
        parts["частник"] = s["private_seller_bonus"]

    # Близость
    if (
        listing.distance_km is not None
        and listing.distance_km <= s.get("near_city_km", 0)
        and s.get("near_city_bonus")
    ):
        parts["рядом"] = s["near_city_bonus"]

    # Импорт
    if (
        listing.country_origin
        and listing.country_origin.lower() not in ("pl", "polska")
        and s.get("foreign_origin_penalty")
    ):
        parts["импорт"] = -s["foreign_origin_penalty"]

    # Привод определён по тексту, а не по полю — данные менее надёжны
    if listing.awd_source == "text":
        parts["4x4 по тексту"] = -4

    # Аномально низкая цена почти всегда означает скрытую проблему
    if listing.suspicious_price:
        parts["подозрительная цена"] = -s.get("suspicious_price_penalty", 30)

    total = sum(parts.values())
    listing.score_parts = parts
    listing.score = int(max(0, min(100, round(total))))
    return listing
