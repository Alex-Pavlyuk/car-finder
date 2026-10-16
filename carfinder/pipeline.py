"""Один прогон: собрать → обогатить → отфильтровать → проверить → оценить → отчёт."""
from __future__ import annotations

import copy
import logging
import math
import threading
from collections import Counter
from pathlib import Path

from . import (anomaly, filters, geo, market, notify, photo, report, scoring,
               verify)
from .http import Cancelled, Fetcher
from .models import Listing
from .sources import olx, otomoto
from .store import Store

log = logging.getLogger(__name__)

SOURCES = {"otomoto": otomoto, "olx": olx}


def search_config(cfg: dict) -> dict:
    """
    Конфиг для запросов к площадкам — с ослабленными границами.

    Площадки фильтруют по цене и году на своей стороне, поэтому «почти подходящие»
    (чуть дороже бюджета, на год старше) в выдачу вообще не попали бы. Запрашиваем
    с запасом, а разделение на «подходит» / «почти подходит» делаем локально
    по исходным критериям.
    """
    nm = cfg.get("near_miss") or {}
    if not nm.get("enabled"):
        return cfg

    relaxed = copy.deepcopy(cfg)
    cr = relaxed["criteria"]
    cr["price_max"] = int(math.ceil(
        cfg["criteria"]["price_max"] * (1 + nm.get("price_overshoot_pct", 0) / 100)
    ))
    cr["year_from"] = cfg["criteria"]["year_from"] - nm.get("year_slack", 0)
    return relaxed


def collect(cfg: dict, fetcher: Fetcher) -> list[Listing]:
    search_cfg = search_config(cfg)
    listings: list[Listing] = []
    for name, module in SOURCES.items():
        if not cfg["sources"].get(name, {}).get("enabled"):
            continue
        try:
            listings.extend(module.fetch(fetcher, search_cfg))
        except Exception as exc:
            # Падение одной площадки не должно ронять весь прогон.
            log.error("%s: сбор не удался — %s", name, exc)
    return listings


def dedupe(listings: list[Listing]) -> list[Listing]:
    """
    OLX зеркалит большую часть объявлений салонов с otomoto.

    Основной записью берём otomoto: там аккуратные поля (марка, модель, версия,
    страна происхождения), тогда как на OLX заголовок — свободный текст продавца
    вроде «Toyota RAV4 Toyota Rav4». А из OLX-копии подтягиваем то, чего у otomoto
    нет: координаты и полное описание (описание избавляет от отдельного запроса
    страницы объявления при проверке на «красные флаги»).
    """
    best: dict[str, Listing] = {}
    for listing in listings:
        key = listing.dedupe_key()
        current = best.get(key)
        if current is None:
            best[key] = listing
            continue
        primary, secondary = (
            (listing, current) if listing.source == "otomoto" else (current, listing)
        )
        for attr in ("drive", "fuel", "gearbox", "version", "country_origin",
                     "engine_power", "mileage", "description", "lat", "lon"):
            if getattr(primary, attr) in (None, "") and getattr(secondary, attr) not in (None, ""):
                setattr(primary, attr, getattr(secondary, attr))
        best[key] = primary
    return list(best.values())


def run(cfg: dict, data_dir: Path, stop: threading.Event | None = None) -> dict:
    """
    Один полный прогон.

    `stop` — отмена: взведённое событие прерывает прогон на ближайшем сетевом
    вызове через `Cancelled`. Всё, что успело записаться в базу, остаётся —
    транзакция закрывается в `finally`, так что прерванный прогон не портит
    ни базу, ни отчёт.
    """
    rt = cfg.get("runtime", {})
    fetcher = Fetcher(
        timeout=rt.get("timeout_sec", 40),
        retries=rt.get("retries", 3),
        delay=rt.get("request_delay_sec", 1.2),
        stop=stop,
    )

    try:
        raw = collect(cfg, fetcher)
        unique = dedupe(raw)
        log.info("собрано %s объявлений, после склейки дублей %s", len(raw), len(unique))

        for listing in unique:
            filters.enrich(listing, cfg)
            filters.apply(listing, cfg)

        first_run = not (data_dir / "listings.db").exists()
        store = Store(data_dir / "listings.db")
        try:
            # Координаты городов копим из OLX и применяем к otomoto, который
            # отдаёт только название города.
            geo.learn(unique, store)
            geo.locate(unique, store, cfg)

            # Считаем по всей выборке — больше сопоставимых, устойчивее медиана.
            # Порядок важен: детектор аномалий опирается на рыночную оценку.
            market.evaluate(unique, cfg)
            anomaly.flag(unique)
            for listing in unique:
                scoring.score(listing, cfg)

            # Проверяем описания только у тех, кто прошёл жёсткие фильтры.
            candidates = [l for l in unique if not l.reject_reasons]
            verify.run(candidates, store, fetcher, cfg)

            for listing in candidates:
                for code in listing.flags:
                    if code in verify.HARD_FLAGS:
                        listing.reject_reasons.append(verify.reason(code))

            # Распознавание текста на фото — только для того, что уцелело после
            # проверки описаний: незачем расходовать его на уже отброшенное.
            survivors = [l for l in candidates if not l.reject_reasons]
            photo.run(survivors, store, fetcher, cfg)
            for listing in survivors:
                for code in listing.photo_flags:
                    listing.reject_reasons.append(photo.reason(code))

            keep = [l for l in candidates if not l.reject_reasons]
            log.info("прошли фильтры: %s (в т.ч. почти подходят %s)",
                     len(keep), sum(1 for l in keep if l.near_miss))

            rejected = Counter(l.reject_reasons[0] for l in unique if l.reject_reasons)
            for reason_text, count in rejected.most_common(8):
                log.info("  отсеяно %4s — %s", count, reason_text)

            enriched: list[tuple[Listing, dict]] = []
            fresh: list[Listing] = []
            for listing in keep:
                meta = store.upsert(listing)
                if meta["is_new"]:
                    fresh.append(listing)
                meta["history"] = store.price_history(listing.key)
                enriched.append((listing, meta))
            new_count = len(fresh)

            gone = store.mark_gone(rt.get("stale_after_days", 21))
            store.record_run(len(raw), len(keep), new_count)
            store.commit()
        finally:
            store.close()

        # Отчёт и уведомления — уже после сети, но перед ними стоит спросить
        # ещё раз: если отмена пришла на последних запросах, рисовать отчёт
        # и слать уведомления не нужно, приложение закрывается.
        if fetcher.stop.is_set():
            raise Cancelled("прогон прерван перед отчётом")

        out = report.render(data_dir / "report.html", enriched, len(raw), cfg)
        # Уведомление только про новинки и только если это не первый прогон:
        # на пустой базе новыми оказались бы все 500 объявлений.
        if not first_run:
            notify.send(fresh, cfg, out)
    finally:
        fetcher.close()

    return {
        "fetched": len(raw),
        "unique": len(unique),
        "matched": len(keep),
        "new": new_count,
        "near_miss": sum(1 for l in keep if l.near_miss),
        "gone": gone,
        "report": out,
        "top": sorted((l for l in keep if not l.near_miss), key=lambda l: -l.score)[:10],
    }
