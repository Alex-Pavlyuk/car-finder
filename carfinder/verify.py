"""
Проверка текста объявления на «красные флаги».

Зачем нужен отдельный шаг: фильтры площадок работают по полям, которые заполняет
продавец, и не ловят целые классы мусора —

  * «Auto do sprowadzenia z USA, cena szacowana» — машины в Польше нет,
    цена вымышленная (именно так в выдачу попадали Jeep 2021 и Mazda CX-5 2023
    по 29 999 zł при рынке 150 000+);
  * цессия лизинга, где в цене указан взнос, а не стоимость авто;
  * битые машины, помеченные продавцом как целые;
  * цена нетто по VAT-фактуре.

Всё это видно только в описании. У OLX описание приходит прямо в выдаче (бесплатно),
у otomoto нужен запрос страницы объявления — поэтому результат кешируется в БД
навсегда: каждое объявление проверяется ровно один раз за свою жизнь.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata

from .models import Listing

log = logging.getLogger(__name__)

NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
DESC_RE = re.compile(r'"description":\s*"((?:\\.|[^"\\])*)"')

# (код, человекочитаемая причина, регэксп по тексту без диакритики в нижнем регистре)
RED_FLAGS: list[tuple[str, str, re.Pattern]] = [
    ("to_import", "машины нет в Польше — под заказ",
     re.compile(r"\b(do sprowadzenia|pod zamowienie|na zamowienie|sprowadzimy|"
                r"sprowadzenie na zamowienie|auto z aukcji (usa|kanady)|"
                r"copart|iaai|z aukcji amerykanskich)\b")),
    ("estimated_price", "цена ориентировочная, не окончательная",
     re.compile(r"\b(cena szacowana|cena orientacyjna|cena pogladowa|"
                r"cena jest szacowana|szacowana cena|cena moze ulec zmianie)\b")),
    # \b обязателен: без него «nieuszkodzony» (целый!) попал бы в битые.
    ("damaged", "битая / после ДТП",
     re.compile(r"\b(uszkodzony|uszkodzona|uszkodzone|uszkodzenia|uszkodzenie|"
                r"powypadkowy|powypadkowa|po kolizji|po wypadku|do naprawy|"
                r"na czesci|rozbity|rozbita|szkoda calkowita|"
                r"nie jezdzi|nie odpala|zalany|zalane)\b")),
    ("leasing", "цессия лизинга — в цене взнос, а не авто",
     re.compile(r"\b(cesja|cesji|odstepne|oplata wstepna|wplata wstepna|"
                r"rata leasingowa|rat leasingowych|przejecie leasingu|"
                r"wykup na koniec|pozostalo \d+ rat)\b")),
    ("net_price", "цена нетто — сверху ещё VAT",
     re.compile(r"\b(cena netto|kwota netto|netto \+ vat|do ceny nalezy doliczyc)\b")),
    ("deposit", "в цене указан задаток/аванс",
     re.compile(r"\b(zadatek|zaliczka na poczet)\b")),
    ("reserved", "уже забронирована или продана",
     re.compile(r"\b(zarezerwowany|zarezerwowane|zarezerwowana|rezerwacja|"
                r"sprzedany|sprzedane|juz sprzedany)\b")),
]

# Флаги, по которым объявление выбрасывается совсем: машину нельзя купить
# (повреждена, забронирована, физически отсутствует) либо цена относится не
# к ней. Мягкими остаются только net_price и deposit — салоны часто пишут
# «cena netto» в шаблонном тексте, оставляя в поле цены сумму брутто, и
# отбраковка по ним даёт слишком много ложных срабатываний.
HARD_FLAGS = {"to_import", "estimated_price", "damaged", "leasing", "reserved"}

# Ложное срабатывание: «sprowadzony z USA» — машина уже в Польше, это не флаг.
SAFE_CONTEXT = re.compile(r"\bsprowadzony|sprowadzona|sprowadzone\b")


def _plain(text: str) -> str:
    """Нижний регистр без польской диакритики: uszkodzony/uszkodzoną → одно и то же."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ł", "l").replace("Ł", "L")
    return re.sub(r"\s+", " ", text).lower()


def scan(text: str) -> list[str]:
    """Возвращает коды сработавших флагов."""
    plain = _plain(text)
    hits = []
    for code, _, pattern in RED_FLAGS:
        if pattern.search(plain):
            if code == "to_import" and SAFE_CONTEXT.search(plain) and \
                    not re.search(r"\bdo sprowadzenia\b", plain):
                continue
            hits.append(code)
    return hits


def reason(code: str) -> str:
    for c, label, _ in RED_FLAGS:
        if c == code:
            return label
    return code


def _fetch_description(fetcher, url: str) -> str:
    html = fetcher.get(url)
    match = NEXT_DATA_RE.search(html)
    if not match:
        return ""
    found = DESC_RE.search(match.group(1))
    if not found:
        return ""
    try:
        return json.loads(f'"{found.group(1)}"')
    except json.JSONDecodeError:
        return found.group(1)


def run(listings: list[Listing], store, fetcher, cfg: dict) -> dict:
    """
    Проставляет listing.flags. Порядок проверки:
      1. кеш в БД — объявление уже проверялось;
      2. описание, пришедшее вместе с выдачей (OLX) — бесплатно;
      3. запрос страницы объявления (otomoto) — только для лучших кандидатов
         и не больше verify.max_per_run за прогон.
    """
    conf = cfg.get("verify", {})
    if not conf.get("enabled", True):
        return {"cached": 0, "fetched": 0, "flagged": 0}

    limit = conf.get("max_per_run", 60)
    min_score = conf.get("min_score", 0)

    cached = fetched = flagged = 0
    need_fetch: list[Listing] = []

    for listing in listings:
        known = store.get_verdict(listing.key)
        if known is not None:
            listing.flags = known
            cached += 1
        elif listing.description:
            listing.flags = scan(f"{listing.title} {listing.description}")
            store.save_verdict(listing.key, listing.flags)
        else:
            need_fetch.append(listing)
        flagged += bool(listing.flags)

    # Сначала самые перспективные и самые подозрительные — их важнее проверить.
    need_fetch.sort(key=lambda l: (-(l.suspicious_price is not None), -l.score))
    for listing in need_fetch[:limit]:
        if listing.score < min_score and not listing.suspicious_price:
            continue
        try:
            description = _fetch_description(fetcher, listing.url)
        except Exception as exc:
            log.debug("проверка %s не удалась: %s", listing.url, exc)
            continue
        listing.description = description
        listing.flags = scan(f"{listing.title} {description}")
        store.save_verdict(listing.key, listing.flags)
        fetched += 1
        flagged += bool(listing.flags)

    log.info("проверка описаний: из кеша %s, загружено %s, с флагами %s",
             cached, fetched, flagged)
    return {"cached": cached, "fetched": fetched, "flagged": flagged}
