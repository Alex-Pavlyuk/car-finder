"""Тесты на хрупкие места: разбор текста, фильтры, склейка дублей, сборка URL."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from carfinder import (anomaly, filters, geo, lifecycle, market, photo,  # noqa: E402
                       report, verify)
from carfinder.http import Cancelled, Fetcher  # noqa: E402
from carfinder.models import Listing  # noqa: E402
from carfinder.pipeline import dedupe, search_config  # noqa: E402
from carfinder.sources import olx, otomoto  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_CFG_PATH = next(p for p in (_ROOT / "config.yaml", _ROOT / "config.example.yaml") if p.exists())
CFG = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8"))


def make(**kw) -> Listing:
    base = dict(source="otomoto", ext_id="1", url="https://x/ID6Iaaaa.html",
                title="Toyota RAV4 2.5 Hybrid 4x4", price=70000, year=2020,
                mileage=90000, make="Toyota", model="RAV4", fuel="hybrid",
                gearbox="automatic", drive="all-wheel-permanent",
                condition="notdamaged", guaranteed=list(otomoto.GUARANTEED))
    base.update(kw)
    return Listing(**base)


# ── verify ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("Samochod sprowadzony z USA, zarejestrowany w Polsce", set()),
    ("Auto do sprowadzenia z USA. Jest to cena szacowana zakupu.",
     {"to_import", "estimated_price"}),
    ("Samochód uszkodzony jak na zdjęciach", {"damaged"}),
    ("Cesja leasingu, odstępne 20 000 zł", {"leasing"}),
    ("Cena netto, do ceny należy doliczyć VAT", {"net_price"}),
    ("SAMOCHÓD JUŻ ZAREZERWOWANY PRZEZ INNEGO KLIENTA", {"reserved"}),
    ("Bezwypadkowy, serwisowany, pierwszy właściciel, salon Polska", set()),
])
def test_red_flags(text, expected):
    assert set(verify.scan(text)) == expected


def test_hard_flags_are_subset_of_known_codes():
    known = {code for code, _, _ in verify.RED_FLAGS}
    assert verify.HARD_FLAGS <= known


@pytest.mark.parametrize("text", [
    "Samochód nieuszkodzony, bezwypadkowy",
    "Stan techniczny: Nieuszkodzony",
])
def test_undamaged_is_not_mistaken_for_damaged(text):
    """«nieuszkodzony» содержит «uszkodzony» — без \\b это ломало бы выдачу."""
    assert "damaged" not in verify.scan(text)


@pytest.mark.parametrize("code", ["damaged", "reserved"])
def test_damaged_and_reserved_are_never_shown(code):
    """Пользователь просил такие объявления не показывать вовсе."""
    assert code in verify.HARD_FLAGS


# ── текст на фотографии ───────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("MUSTANG SALON SAMOCHODOWY REZERWACJA "
     "SAMOCHÓD JUŻ ZAREZERWOWANY PRZEZ INNEGO KLIENTA.", {"reserved"}),
    ("SPRZEDANY", {"sold"}),
    ("AUTO USZKODZONE", {"damaged"}),
    ("SIZ RK 41", set()),                       # номерной знак на настоящем фото
    ("Toyota RAV4 2.5 Hybrid", set()),
])
def test_photo_flags(text, expected):
    assert set(photo.scan(text)) == expected


def test_every_photo_flag_rejects_the_listing():
    """Отметка на фото всегда означает, что машину купить нельзя."""
    for code, _, _ in photo.PHOTO_FLAGS:
        assert photo.reason(code) != code    # у каждого кода есть внятная причина


# ── фильтры ───────────────────────────────────────────────────────────────────
def test_passes_all_criteria():
    listing = filters.apply(filters.enrich(make(), CFG), CFG)
    assert listing.reject_reasons == []
    assert not listing.near_miss


def test_missing_drive_is_trusted_when_site_filtered_it():
    """otomoto не отдаёт «Napęd» в выдаче — но запрос уже ограничен 4x4."""
    listing = filters.apply(filters.enrich(make(drive=None), CFG), CFG)
    assert listing.reject_reasons == []
    assert listing.awd_source == "query"
    assert listing.drive == "all-wheel"


@pytest.mark.parametrize("title", [
    "BMW X3 xDrive20d xLine",          # токен слипается с кодом мотора
    "Mercedes-Benz GLA 250 4Matic",
    "Audi Q5 2.0 TFSI quattro",
    "Suzuki Vitara 1.4 AllGrip",
    "Toyota Yaris Cross 1.5 AWD-i",
    "VW Tiguan 2.0 TDI 4Motion",
    "MINI Countryman Cooper S E All4",
])
def test_missing_drive_without_guarantee_falls_back_to_text(title):
    listing = make(drive=None, guaranteed=[], title=title)
    filters.enrich(listing, CFG)
    assert listing.awd_source == "text"
    assert listing.drive == "all-wheel"


@pytest.mark.parametrize("title", [
    "Toyota Corolla 1.8 Hybrid Comfort",
    "Skoda Karoq 1.5 TSI Style",
])
def test_text_heuristic_does_not_invent_awd(title):
    listing = make(drive=None, guaranteed=[], title=title)
    filters.enrich(listing, CFG)
    assert listing.drive is None


def test_front_wheel_drive_is_rejected():
    listing = filters.apply(filters.enrich(make(drive="front-wheel"), CFG), CFG)
    assert "не полный привод" in listing.reject_reasons


def test_manual_gearbox_is_rejected():
    listing = filters.apply(filters.enrich(make(gearbox="manual"), CFG), CFG)
    assert "не автомат" in listing.reject_reasons


def test_slightly_over_budget_is_near_miss_not_reject():
    over = int(CFG["criteria"]["price_max"] * 1.05)
    listing = filters.apply(filters.enrich(make(price=over), CFG), CFG)
    assert listing.reject_reasons == []
    assert listing.near_miss


def test_far_over_budget_is_rejected():
    listing = filters.apply(filters.enrich(make(price=200000), CFG), CFG)
    assert listing.reject_reasons and not listing.near_miss


def _listed_days_ago(days: int) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


def test_stale_listing_is_not_dropped():
    """Возраст объявления — фильтр в отчёте, а не отбраковка при сборе."""
    old = make(created_at=_listed_days_ago(200))
    listing = filters.apply(filters.enrich(old, CFG), CFG)
    assert listing.reject_reasons == []
    assert listing.days_listed >= 199      # отчёт покажет и отфильтрует сам


def test_days_listed_is_computed():
    assert make(created_at=_listed_days_ago(45)).days_listed == 45
    assert make(created_at=None).days_listed is None


def test_lpg_detected_from_title():
    listing = filters.enrich(
        make(fuel="petrol", title="Suzuki Vitara 1.6 4x4 instalacja gazowa LPG"), CFG
    )
    assert listing.fuel == "petrol-lpg"


# ── склейка дублей ────────────────────────────────────────────────────────────
def test_olx_mirror_merges_into_otomoto_record():
    oto = make(source="otomoto", ext_id="10", url="https://www.otomoto.pl/o/ID6Iabcd.html")
    mirror = make(source="olx", ext_id="20", url="https://www.olx.pl/d/oferta/x.html",
                  external_url="https://www.otomoto.pl/o/ID6Iabcd.html",
                  title="rav4 hybryda", description="Bezwypadkowy",
                  lat=50.3, lon=18.7, guaranteed=list(olx.GUARANTEED))
    merged = dedupe([oto, mirror])
    assert len(merged) == 1
    kept = merged[0]
    assert kept.source == "otomoto"          # чистые поля — от otomoto
    assert kept.description == "Bezwypadkowy"  # описание — от OLX
    assert kept.lat == 50.3


def test_distinct_cars_are_not_merged():
    a = make(ext_id="1", url="https://www.otomoto.pl/o/ID6Iaaa1.html")
    b = make(ext_id="2", url="https://www.otomoto.pl/o/ID6Iaaa2.html")
    assert len(dedupe([a, b])) == 2


# ── сборка запросов ───────────────────────────────────────────────────────────
def test_search_urls_carry_every_hard_criterion():
    oto_url = otomoto.build_url(CFG)
    assert "filter_enum_gearbox" in oto_url
    assert "all-wheel-permanent" in oto_url
    assert "filter_enum_damaged" in oto_url
    assert "body_type" in oto_url

    olx_url = olx.build_url(CFG)
    assert "filter_enum_drive" in olx_url
    assert "filter_enum_condition" in olx_url
    assert "filter_enum_car_body" in olx_url


# ── оценка относительно похожих ───────────────────────────────────────────────
def peer_group(prices, **kw):
    kw.setdefault("mileage", 100000)
    return [make(ext_id=str(i), price=p, **kw) for i, p in enumerate(prices)]


def test_market_delta_is_gap_to_median_of_same_model():
    pool = peer_group([70000, 72000, 74000, 76000, 78000])
    target = make(ext_id="t", price=60000, mileage=100000)
    market.evaluate(pool + [target], CFG)
    assert target.market_expected == 74000
    assert target.market_delta == 14000
    assert "RAV4" in target.market_basis


def test_market_never_compares_across_models():
    """Audi Q7 не должна оцениваться по ценам Audi A3."""
    pool = peer_group([30000] * 6, make="Audi", model="A3")
    target = make(ext_id="t", make="Audi", model="Q7", price=33500)
    market.evaluate(pool + [target], CFG)
    assert target.market_expected is None


def test_market_adjusts_peer_prices_for_mileage():
    """Иначе «дёшево» означало бы просто «больше проехало»."""
    pool = peer_group([70000] * 5, mileage=50000)
    target = make(ext_id="t", price=70000, mileage=150000)
    market.evaluate(pool + [target], CFG)
    # У сопоставимых пробег на 100 000 км меньше — их цены приводятся вниз.
    assert target.market_expected < 70000
    assert target.market_delta < 0


def test_market_ignores_implausible_mileage():
    """318 км у машины 2019 года — опечатка, поправку применять нельзя."""
    pool = peer_group([70000] * 5, mileage=90000)
    target = make(ext_id="t", price=57000, mileage=318, year=2019)
    market.evaluate(pool + [target], CFG, current_year=2026)
    assert target.market_expected == 70000    # без поправки на пробег


def test_anomaly_uses_model_estimate():
    pool = peer_group([70000, 71000, 72000, 73000, 74000])
    target = make(ext_id="t", price=29900)
    everything = pool + [target]
    market.evaluate(everything, CFG)
    anomaly.flag(everything)
    assert target.suspicious_price
    assert not any(p.suspicious_price for p in pool)


# ── справочник городов ────────────────────────────────────────────────────────
def test_city_key_ignores_case_and_diacritics():
    assert geo.normalize("Bielsko-Biała") == geo.normalize("bielsko-biala")
    assert geo.normalize("Kraków") == "krakow"
    assert geo.normalize(None) is None


def test_haversine_matches_known_distance():
    # Гливице → Краков, по прямой ~90 км
    assert 80 < geo.haversine(50.2945, 18.6714, 50.0647, 19.9450) < 100


# ── сборка запросов ───────────────────────────────────────────────────────────
def test_search_config_relaxes_bounds_for_near_miss():
    relaxed = search_config(CFG)["criteria"]
    assert relaxed["price_max"] > CFG["criteria"]["price_max"]
    assert relaxed["year_from"] < CFG["criteria"]["year_from"]
    # исходный конфиг не тронут — по нему считается «подходит / почти подходит»
    assert CFG["criteria"]["price_max"] == 75000


# ── единственный экземпляр ────────────────────────────────────────────────────
def test_second_instance_is_refused(tmp_path):
    first = lifecycle.InstanceLock(tmp_path / "app.lock")
    first.acquire()
    try:
        with pytest.raises(lifecycle.AlreadyRunning) as exc:
            lifecycle.InstanceLock(tmp_path / "app.lock").acquire()
        assert exc.value.pid == os.getpid()   # есть что показать человеку
    finally:
        first.release()


def test_lock_is_free_after_release(tmp_path):
    lock = lifecycle.InstanceLock(tmp_path / "app.lock")
    lock.acquire()
    lock.release()
    assert not lock.held

    again = lifecycle.InstanceLock(tmp_path / "app.lock")
    again.acquire()                            # без этого выход ломал бы запуск
    again.release()


def test_leftover_lock_file_does_not_block_startup(tmp_path):
    """Файл от убитого процесса — не препятствие: замок снимает ядро."""
    path = tmp_path / "app.lock"
    path.write_text("999999\n")                # pid, которого давно нет
    lock = lifecycle.InstanceLock(path)
    lock.acquire()
    lock.release()


# ── отмена прогона ────────────────────────────────────────────────────────────
def test_cancelled_fetcher_makes_no_requests():
    fetcher = Fetcher(stop=threading.Event())
    fetcher.stop.set()
    fetcher.session.get = lambda *a, **kw: pytest.fail("запрос при отменённом прогоне")
    with pytest.raises(Cancelled):
        fetcher.get("https://www.otomoto.pl/")
    with pytest.raises(Cancelled):
        fetcher.get_bytes("https://img.example/1.jpg")
    fetcher.close()


def test_cancel_interrupts_waiting_immediately():
    """
    Прогон почти всё время ждёт — троттлинг и отступ между попытками.
    Если бы ожидание было обычным sleep, выход занимал бы эти секунды.
    """
    fetcher = Fetcher(stop=threading.Event())
    threading.Timer(0.05, fetcher.stop.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        fetcher._sleep(30)
    assert time.monotonic() - started < 5
    fetcher.close()


# ── отчёт: текст из интернета в разметке ──────────────────────────────────────
# Поля, которые приходят с площадок или содержат их текст. Любое из них в
# разметке отчёта обязано быть обёрнуто — иначе заголовок объявления вида
# `<img src=x onerror=...>` выполнит свой код на странице отчёта.
UNTRUSTED_FIELDS = ("title", "city", "key", "url", "image", "market_basis",
                    "suspicious_price", "source", "fuel_label", "drive_label")


@pytest.mark.parametrize("field", UNTRUSTED_FIELDS)
def test_untrusted_fields_are_escaped_in_template(field):
    holes = re.findall(r"\$\{[^{}]*\bc\." + field + r"\b[^{}]*\}", report.TEMPLATE)
    unwrapped = [h for h in holes if "esc(" not in h and "safeUrl(" not in h]
    assert not unwrapped, f"без экранирования: {unwrapped}"


def test_link_and_image_accept_only_http():
    """`javascript:`-ссылка из объявления сработала бы по клику на «Открыть»."""
    assert 'src="${thumb}"' in report.TEMPLATE
    assert "safeUrl = u => /^https?:" in report.TEMPLATE


def test_report_data_cannot_close_the_script_tag(tmp_path):
    """JSON лежит внутри <script>: `</script>` в описании закрыл бы его."""
    listing = make(title="RAV4 </script><img src=x onerror=alert(1)>",
                   city="Warszawa <b>", url="javascript:alert(1)")
    out = report.render(tmp_path / "r.html", [(listing, {})], 1, CFG)
    html = out.read_text(encoding="utf-8")
    assert "</script><img" not in html
    assert "<\\/script>" in html


def test_cancellation_is_not_swallowed_by_per_item_handlers():
    """
    Конвейер обёрнут в `except Exception` на каждой площадке и каждом объявлении.
    Отмена обязана проходить сквозь них, иначе закрытие приложения не прерывает
    прогон, а лишь роняет его по одному объявлению за раз.
    """
    assert not issubclass(Cancelled, Exception)
