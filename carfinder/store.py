"""
SQLite-хранилище.

Нужно ровно для трёх вещей:
  * знать, какое объявление появилось впервые (бейдж «НОВОЕ»);
  * помнить историю цены (бейдж «−5 000 zł»);
  * помечать пропавшие объявления, чтобы отчёт не показывал мёртвые ссылки.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import Listing, iso_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key           TEXT PRIMARY KEY,
    dedupe_key    TEXT,
    source        TEXT,
    url           TEXT,
    title         TEXT,
    price         INTEGER,
    first_price   INTEGER,
    score         INTEGER,
    first_seen    TEXT,
    last_seen     TEXT,
    is_gone       INTEGER DEFAULT 0,
    data          TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_dedupe ON listings(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_listings_lastseen ON listings(last_seen);

CREATE TABLE IF NOT EXISTS price_history (
    key   TEXT,
    ts    TEXT,
    price INTEGER
);
CREATE INDEX IF NOT EXISTS idx_price_key ON price_history(key);

-- Вердикт проверки описания. Кешируется навсегда: текст объявления не меняется,
-- а загрузка страницы отдельного объявления — самая дорогая операция в прогоне.
CREATE TABLE IF NOT EXISTS verdicts (
    key   TEXT PRIMARY KEY,
    ts    TEXT,
    flags TEXT
);

-- Координаты городов, накопленные из OLX-объявлений. Применяются к otomoto,
-- который отдаёт только название города (см. geo.py).
CREATE TABLE IF NOT EXISTS cities (
    key  TEXT PRIMARY KEY,
    name TEXT,
    lat  REAL,
    lon  REAL
);

-- Что распознано на фотографии объявления. Ключ — URL картинки: фото не
-- меняется, а одна и та же заглушка салона встречается в разных объявлениях.
CREATE TABLE IF NOT EXISTS photo_verdicts (
    image TEXT PRIMARY KEY,
    ts    TEXT,
    flags TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    ts        TEXT,
    fetched   INTEGER,
    matched   INTEGER,
    new_count INTEGER
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, listing: Listing) -> dict:
        """
        Сохраняет объявление и возвращает его «дельту» относительно прошлого запуска:
        {is_new, price_drop, first_seen, first_price}.
        """
        now = iso_now()
        row = self.conn.execute(
            "SELECT * FROM listings WHERE key = ?", (listing.key,)
        ).fetchone()

        if row is None:
            self.conn.execute(
                "INSERT INTO listings (key, dedupe_key, source, url, title, price, "
                "first_price, score, first_seen, last_seen, is_gone, data) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
                (listing.key, listing.dedupe_key(), listing.source, listing.url,
                 listing.title, listing.price, listing.price, listing.score,
                 now, now, json.dumps(listing.to_dict(), ensure_ascii=False)),
            )
            if listing.price is not None:
                self.conn.execute(
                    "INSERT INTO price_history (key, ts, price) VALUES (?,?,?)",
                    (listing.key, now, listing.price),
                )
            return {"is_new": True, "price_drop": 0,
                    "first_seen": now, "first_price": listing.price}

        price_drop = 0
        if listing.price is not None and row["price"] is not None:
            price_drop = row["price"] - listing.price
        if price_drop != 0:
            self.conn.execute(
                "INSERT INTO price_history (key, ts, price) VALUES (?,?,?)",
                (listing.key, now, listing.price),
            )

        self.conn.execute(
            "UPDATE listings SET price = ?, score = ?, last_seen = ?, is_gone = 0, "
            "title = ?, url = ?, data = ? WHERE key = ?",
            (listing.price, listing.score, now, listing.title, listing.url,
             json.dumps(listing.to_dict(), ensure_ascii=False), listing.key),
        )
        return {
            "is_new": False,
            "price_drop": price_drop,
            "first_seen": row["first_seen"],
            "first_price": row["first_price"],
        }

    def mark_gone(self, stale_after_days: int) -> int:
        cutoff = (datetime.now().astimezone() - timedelta(days=stale_after_days)).isoformat()
        cur = self.conn.execute(
            "UPDATE listings SET is_gone = 1 WHERE last_seen < ? AND is_gone = 0", (cutoff,)
        )
        return cur.rowcount

    def price_history(self, key: str) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT ts, price FROM price_history WHERE key = ? ORDER BY ts", (key,)
        ).fetchall()
        return [(r["ts"], r["price"]) for r in rows]

    def get_verdict(self, key: str) -> list[str] | None:
        row = self.conn.execute(
            "SELECT flags FROM verdicts WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["flags"]) if row else None

    def save_verdict(self, key: str, flags: list[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO verdicts (key, ts, flags) VALUES (?,?,?)",
            (key, iso_now(), json.dumps(flags)),
        )

    def save_city(self, key: str, name: str, lat: float, lon: float) -> bool:
        """Возвращает True, если город был неизвестен."""
        exists = self.conn.execute(
            "SELECT 1 FROM cities WHERE key = ?", (key,)
        ).fetchone()
        if exists:
            return False
        self.conn.execute(
            "INSERT INTO cities (key, name, lat, lon) VALUES (?,?,?,?)",
            (key, name, lat, lon),
        )
        return True

    def all_cities(self) -> dict[str, tuple[float, float]]:
        return {
            r["key"]: (r["lat"], r["lon"])
            for r in self.conn.execute("SELECT key, lat, lon FROM cities")
        }

    def get_photo_verdict(self, image: str) -> list[str] | None:
        row = self.conn.execute(
            "SELECT flags FROM photo_verdicts WHERE image = ?", (image,)
        ).fetchone()
        return json.loads(row["flags"]) if row else None

    def save_photo_verdict(self, image: str, flags: list[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO photo_verdicts (image, ts, flags) VALUES (?,?,?)",
            (image, iso_now(), json.dumps(flags)),
        )

    def record_run(self, fetched: int, matched: int, new_count: int) -> None:
        self.conn.execute(
            "INSERT INTO runs (ts, fetched, matched, new_count) VALUES (?,?,?,?)",
            (iso_now(), fetched, matched, new_count),
        )

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def commit(self) -> None:
        self.conn.commit()
