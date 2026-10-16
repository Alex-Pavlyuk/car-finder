"""
Источник olx.pl.

Листинг лежит в window.__PRERENDERED_STATE__ — JSON-строка внутри JSON-строки.
Публичный /api/v1/offers доступен, но требует внутренних category_id и всё равно
проходит через тот же CloudFront, так что HTML-страница проще и стабильнее.

Важно: значительная часть автообъявлений на OLX — зеркала otomoto (поле
externalUrl). Склейка дублей делается в pipeline по Listing.dedupe_key().
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from ..models import Listing, norm_fuel, parse_int

log = logging.getLogger(__name__)

BASE = "https://www.olx.pl/motoryzacja/samochody"
STATE_RE = re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:\\.|[^"\\])*")', re.S)
# Критерии, применённые самим сайтом в поисковом запросе (см. build_url).
GUARANTEED = ["price", "year", "mileage", "body_type", "gearbox", "drive", "damaged"]



def build_url(cfg: dict, page: int = 1) -> str:
    loc = cfg["location"]
    cr = cfg["criteria"]
    params: list[tuple[str, str]] = [
        ("search[dist]", str(loc["radius_km"])),
        ("search[filter_float_price:to]", str(cr["price_max"])),
        ("search[filter_float_year:from]", str(cr["year_from"])),
        ("search[order]", "created_at:desc"),
    ]
    if cr.get("price_min"):
        params.append(("search[filter_float_price:from]", str(cr["price_min"])))
    if cr.get("mileage_max"):
        params.append(("search[filter_float_milage:to]", str(cr["mileage_max"])))
    if cr.get("exclude_damaged"):
        params.append(("search[filter_enum_condition][0]", "notdamaged"))
    for i, value in enumerate(cr.get("body_types") or []):
        params.append((f"search[filter_enum_car_body][{i}]", value))
    for i, value in enumerate(cr.get("gearboxes") or []):
        params.append((f"search[filter_enum_transmission][{i}]", value))
    for i, value in enumerate(cr.get("drives") or []):
        params.append((f"search[filter_enum_drive][{i}]", value))
    if page > 1:
        params.append(("page", str(page)))

    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    return f"{BASE}/{loc['city']}/?{query}"


def _state(html: str) -> dict | None:
    match = STATE_RE.search(html)
    if not match:
        return None
    return json.loads(json.loads(match.group(1)))


def _to_listing(ad: dict) -> Listing:
    params = {p["key"]: p for p in ad.get("params", [])}

    def norm(key: str) -> str | None:
        return (params.get(key) or {}).get("normalizedValue")

    def shown(key: str) -> str | None:
        return (params.get(key) or {}).get("value")

    price = (ad.get("price") or {}).get("regularPrice") or {}
    location = ad.get("location") or {}
    geo = ad.get("map") or {}
    photos = ad.get("photos") or []

    return Listing(
        guaranteed=list(GUARANTEED),
        source="olx",
        ext_id=str(ad.get("id")),
        url=ad.get("url") or "",
        title=ad.get("title") or "",
        price=parse_int(price.get("value")),
        currency=price.get("currencyCode") or "PLN",
        year=parse_int(norm("year")),
        mileage=parse_int(norm("milage")),
        make=shown("make") or _make_from_url(ad.get("url") or ""),
        model=shown("model"),
        version=shown("version"),
        fuel=norm_fuel(norm("petrol")),
        gearbox=norm("transmission"),
        drive=norm("drive"),
        engine_power=parse_int(norm("enginepower")),
        engine_capacity=parse_int(norm("enginesize")),
        condition="damaged" if norm("condition") == "damaged" else "notdamaged",
        country_origin=norm("country_origin"),
        city=location.get("cityName"),
        region=location.get("regionName"),
        lat=geo.get("lat"),
        lon=geo.get("lon"),
        is_business=ad.get("isBusiness"),
        seller_name=((ad.get("user") or {}).get("name")),
        image=photos[0] if photos else None,
        created_at=ad.get("createdTime"),
        external_url=ad.get("externalUrl"),
        # OLX отдаёт описание прямо в выдаче — проверка на «красные флаги»
        # для этой площадки не стоит ни одного лишнего запроса.
        description=ad.get("description"),
    )


def _make_from_url(url: str) -> str | None:
    """OLX не всегда отдаёт параметр make; в URL-слаге марка обычно первая."""
    match = re.search(r"/d/oferta/([a-z0-9-]+)", url)
    if not match:
        return None
    return match.group(1).split("-")[0].capitalize() or None


def fetch(fetcher, cfg: dict) -> list[Listing]:
    max_pages = cfg["sources"]["olx"].get("max_pages", 10)
    out: list[Listing] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        url = build_url(cfg, page)
        html = fetcher.get(url, referer=f"{BASE}/")
        state = _state(html)
        if state is None:
            log.warning("olx: не найден __PRERENDERED_STATE__ на стр. %s", page)
            break

        listing = (state.get("listing") or {}).get("listing") or {}
        ads = listing.get("ads") or []
        if not ads:
            break

        if page == 1:
            log.info("olx: всего по фильтру %s объявлений (%s стр.)",
                     listing.get("totalElements"), listing.get("totalPages"))

        fresh = 0
        for ad in ads:
            ad_id = str(ad.get("id") or "")
            if not ad_id or ad_id in seen_ids:
                continue
            seen_ids.add(ad_id)
            out.append(_to_listing(ad))
            fresh += 1

        if fresh == 0 or page >= (listing.get("totalPages") or 1):
            break

    log.info("olx: собрано %s объявлений", len(out))
    return out
