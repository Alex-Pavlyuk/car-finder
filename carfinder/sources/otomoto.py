"""
Источник otomoto.pl.

Страница выдачи — Next.js; весь листинг лежит в <script id="__NEXT_DATA__">
внутри urqlState (кэш GraphQL-запроса advertSearch). Отдельный запрос к GraphQL
не нужен: HTML уже содержит готовый JSON с 32 объявлениями на страницу.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from ..models import Listing, norm_fuel, parse_int

log = logging.getLogger(__name__)

BASE = "https://www.otomoto.pl/osobowe"
NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# Критерии, применённые самим сайтом в поисковом запросе (см. build_url).
GUARANTEED = ["price", "year", "mileage", "body_type", "gearbox", "drive", "damaged"]



def build_url(cfg: dict, page: int = 1) -> str:
    loc = cfg["location"]
    cr = cfg["criteria"]
    params: list[tuple[str, str]] = [
        ("search[dist]", str(loc["radius_km"])),
        ("search[filter_float_price:to]", str(cr["price_max"])),
        ("search[filter_float_year:from]", str(cr["year_from"])),
        ("search[order]", "created_at_first:desc"),
    ]
    if cr.get("price_min"):
        params.append(("search[filter_float_price:from]", str(cr["price_min"])))
    if cr.get("mileage_max"):
        params.append(("search[filter_float_mileage:to]", str(cr["mileage_max"])))
    if cr.get("exclude_damaged"):
        # Признак повреждения в выдаче не приходит вообще — отсекаем на стороне сайта,
        # иначе битые лоты всплывают наверх как «дешёвые».
        params.append(("search[filter_enum_damaged]", "0"))
    for i, value in enumerate(cr.get("body_types") or []):
        params.append((f"search[filter_enum_body_type][{i}]", value))
    for i, value in enumerate(cr.get("drives") or []):
        params.append((f"search[filter_enum_transmission][{i}]", value))

    gearboxes = cr.get("gearboxes") or []
    if len(gearboxes) == 1:
        params.append(("search[filter_enum_gearbox]", gearboxes[0]))
    else:
        for i, value in enumerate(gearboxes):
            params.append((f"search[filter_enum_gearbox][{i}]", value))

    if page > 1:
        params.append(("page", str(page)))

    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    return f"{BASE}/{loc['city']}?{query}"


def _advert_search(html: str) -> dict | None:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    data = json.loads(match.group(1))
    urql = data.get("props", {}).get("pageProps", {}).get("urqlState", {}) or {}
    for entry in urql.values():
        raw = entry.get("data") if isinstance(entry, dict) else None
        if not raw:
            continue
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict) and "advertSearch" in parsed:
            return parsed["advertSearch"]
    return None


def _to_listing(node: dict) -> Listing:
    params = {p["key"]: p for p in node.get("parameters", [])}

    def val(key: str) -> str | None:
        return (params.get(key) or {}).get("value")

    def display(key: str) -> str | None:
        return (params.get(key) or {}).get("displayValue")

    location = node.get("location") or {}
    price_amount = ((node.get("price") or {}).get("amount") or {})
    seller = node.get("sellerLink") or {}
    thumb = node.get("thumbnail") or {}

    return Listing(
        guaranteed=list(GUARANTEED),
        source="otomoto",
        ext_id=str(node.get("id")),
        url=node.get("url") or "",
        title=node.get("title") or "",
        price=parse_int(price_amount.get("value")),
        currency=price_amount.get("currencyCode") or "PLN",
        year=parse_int(val("year")),
        mileage=parse_int(val("mileage")),
        make=display("make"),
        model=display("model"),
        version=display("version"),
        fuel=norm_fuel(val("fuel_type")),
        gearbox=val("gearbox"),
        drive=val("transmission"),
        engine_power=parse_int(val("engine_power")),
        engine_capacity=parse_int(val("engine_capacity")),
        condition="damaged" if val("damaged") == "damaged" else "notdamaged",
        country_origin=val("country_origin"),
        city=(location.get("city") or {}).get("name"),
        region=(location.get("region") or {}).get("name"),
        is_business=bool(seller.get("id")),
        seller_name=seller.get("name"),
        image=thumb.get("x2") or thumb.get("x1"),
        created_at=node.get("createdAt"),
    )


def fetch(fetcher, cfg: dict) -> list[Listing]:
    max_pages = cfg["sources"]["otomoto"].get("max_pages", 10)
    out: list[Listing] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        url = build_url(cfg, page)
        html = fetcher.get(url, referer=f"{BASE}/{cfg['location']['city']}")
        advert = _advert_search(html)
        if advert is None:
            log.warning("otomoto: не найден advertSearch на стр. %s — вёрстка изменилась?", page)
            break

        edges = advert.get("edges") or []
        if not edges:
            break

        if page == 1:
            log.info("otomoto: всего по фильтру %s объявлений", advert.get("totalCount"))

        fresh = 0
        for edge in edges:
            node = edge.get("node") or {}
            if not node.get("id") or node.get("id") in seen_ids:
                continue
            seen_ids.add(node["id"])
            out.append(_to_listing(node))
            fresh += 1

        total = advert.get("totalCount") or 0
        if fresh == 0 or len(out) >= total:
            break

    log.info("otomoto: собрано %s объявлений", len(out))
    return out
