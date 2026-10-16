"""Нормализованное представление объявления, единое для всех источников."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# Нормализация топлива: сайты дают разные написания, приводим к otomoto-словарю.
FUEL_MAP = {
    "benzyna": "petrol",
    "petrol": "petrol",
    "benzyna+lpg": "petrol-lpg",
    "petrol-lpg": "petrol-lpg",
    "lpg": "petrol-lpg",
    "benzyna+cng": "petrol-cng",
    "petrol-cng": "petrol-cng",
    "diesel": "diesel",
    "hybryda": "hybrid",
    "hybrid": "hybrid",
    "hybryda plug-in": "plugin-hybrid",
    "plugin-hybrid": "plugin-hybrid",
    "elektryczny": "electric",
    "electric": "electric",
    "etanol": "etanol",
    "wodor": "hidrogen",
    "hidrogen": "hidrogen",
}

FUEL_LABEL_RU = {
    "petrol": "Бензин",
    "petrol-lpg": "Бензин + ГБО",
    "petrol-cng": "Бензин + CNG",
    "diesel": "Дизель",
    "hybrid": "Гибрид",
    "plugin-hybrid": "Гибрид Plug-in",
    "electric": "Электро",
    "etanol": "Этанол",
    "hidrogen": "Водород",
}

DRIVE_LABEL_RU = {
    "all-wheel": "4x4",
    "all-wheel-auto": "4x4 (авто-подключаемый)",
    "all-wheel-lock": "4x4 (подключаемый)",
    "all-wheel-permanent": "4x4 (постоянный)",
    "front-wheel": "Передний",
    "rear-wheel": "Задний",
}

AWD_DRIVES = {"all-wheel", "all-wheel-auto", "all-wheel-lock", "all-wheel-permanent"}

# Признаки полного привода в тексте — для объявлений без заполненного поля «Napęd».
# Без завершающего \b: в реальных названиях за токеном идёт код комплектации
# («xDrive20d», «4Matic+», «AWD-i»), и \b на конце такие случаи не находил.
AWD_TEXT_RE = re.compile(
    r"(?<![a-z0-9])(4x4|4wd|awd|4-?matic|xdrive|quattro|4-?motion|all-?grip|"
    r"all4|4drive|sh-?awd|e-?four|symmetrical|hybrid4|4x4i)",
    re.IGNORECASE,
)

# Признаки автомата в тексте.
AUTO_TEXT_RE = re.compile(
    r"(?<![a-z0-9])(automat\w*|tiptronic|dsg|dct|cvt|e-?cvt|lineartronic|multidrive|"
    r"s-?tronic|steptronic|powershift|g-?tronic|edc|aut\.)",
    re.IGNORECASE,
)

# Признаки ГБО в тексте (OLX-частники часто не заполняют поле).
LPG_TEXT_RE = re.compile(r"\b(lpg|gaz\b|gbo|instalacja\s+gazowa)\b", re.IGNORECASE)


def norm_fuel(value: str | None) -> str | None:
    if not value:
        return None
    return FUEL_MAP.get(value.strip().lower())


@dataclass
class Listing:
    source: str                  # "otomoto" | "olx"
    ext_id: str                  # id внутри источника
    url: str
    title: str
    price: int | None = None
    currency: str = "PLN"
    year: int | None = None
    mileage: int | None = None
    make: str | None = None
    model: str | None = None
    version: str | None = None
    fuel: str | None = None          # нормализованный код
    gearbox: str | None = None       # automatic | manual
    drive: str | None = None         # all-wheel-* | front-wheel | rear-wheel
    engine_power: int | None = None
    engine_capacity: int | None = None
    condition: str | None = None     # notdamaged | damaged
    country_origin: str | None = None
    city: str | None = None
    region: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = None
    is_business: bool | None = None
    seller_name: str | None = None
    image: str | None = None
    created_at: str | None = None    # ISO, когда объявление создано на площадке
    external_url: str | None = None  # для OLX-зеркал объявлений otomoto

    # Заполняется на этапах фильтрации/скоринга
    score: int = 0
    score_parts: dict[str, float] = field(default_factory=dict)
    reject_reasons: list[str] = field(default_factory=list)
    near_miss: bool = False
    near_miss_reasons: list[str] = field(default_factory=list)
    suspicious_price: str | None = None   # причина, если цена аномально низкая
    description: str | None = None        # полный текст объявления, если доступен
    flags: list[str] = field(default_factory=list)  # коды из verify.RED_FLAGS
    photo_flags: list[str] = field(default_factory=list)  # коды из photo.PHOTO_FLAGS

    # Оценка относительно похожих предложений (см. market.py)
    market_expected: int | None = None    # ожидаемая цена, PLN
    market_delta: int | None = None       # >0 — дешевле похожих на столько
    market_basis: str | None = None       # на чём построено сравнение
    awd_source: str | None = None    # "param" | "query" | "text" — откуда узнали про 4x4

    # Критерии, которые источник уже применил на своей стороне.
    # Нужны, потому что otomoto не возвращает поле «Napęd» в выдаче: без этого
    # мы бы отбраковывали объявления, которые сайт уже отфильтровал по 4x4.
    guaranteed: list[str] = field(default_factory=list)

    # ── производные ───────────────────────────────────────────────────────────
    @property
    def key(self) -> str:
        """Стабильный ключ объявления в БД."""
        return f"{self.source}:{self.ext_id}"

    @property
    def name(self) -> str:
        parts = [p for p in (self.make, self.model) if p]
        return " ".join(parts) if parts else self.title

    @property
    def haystack(self) -> str:
        """Весь текст объявления для эвристик по тексту."""
        return " ".join(
            str(p) for p in (self.title, self.version, self.make, self.model) if p
        ).lower()

    def dedupe_key(self) -> str:
        """
        Ключ для склейки дублей между площадками.

        OLX почти всегда зеркалит объявления салонов с otomoto — там есть
        externalUrl, и это самый надёжный признак. Для остальных берём
        отпечаток «марка+модель+год+пробег+цена».
        """
        if self.external_url:
            m = re.search(r"ID([A-Za-z0-9]+)\.html", self.external_url)
            if m:
                return f"oto:{m.group(1)}"
        if self.source == "otomoto":
            m = re.search(r"ID([A-Za-z0-9]+)\.html", self.url)
            if m:
                return f"oto:{m.group(1)}"
        raw = "|".join(
            str(p or "")
            for p in (
                (self.make or "").lower(),
                (self.model or "").lower(),
                self.year,
                round((self.mileage or 0) / 1000),
                self.price,
            )
        )
        return "fp:" + hashlib.sha1(raw.encode()).hexdigest()[:16]

    @property
    def days_listed(self) -> int | None:
        """Сколько дней объявление висит. Долго висящее = переоценено или с проблемой."""
        if not self.created_at:
            return None
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0, (datetime.now().astimezone() - created).days)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_int(value: Any) -> int | None:
    """'182 900 zł' / '57 km' / '1 995 cm³' -> int."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
