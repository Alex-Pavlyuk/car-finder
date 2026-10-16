"""CLI: python -m carfinder <run|watch|report|urls>"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_config(path: Path) -> dict:
    """
    Читает конфиг; если `config.yaml` ещё не создан — берёт `config.example.yaml`.

    Личные критерии (город, бюджет, модели) живут в `config.yaml`, которого в
    репозитории нет; пример с нейтральными значениями — есть всегда.
    """
    if not path.exists() and path.name == "config.yaml":
        example = path.with_name("config.example.yaml")
        if example.exists():
            logging.warning("config.yaml не найден — использую config.example.yaml; "
                            "скопируйте его в config.yaml и впишите свои критерии")
            path = example
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)


def cmd_run(args, cfg: dict) -> int:
    from .pipeline import run

    result = run(cfg, DATA)
    print()
    print(f"  собрано .......... {result['fetched']}")
    print(f"  уникальных ....... {result['unique']}")
    print(f"  подходит ......... {result['matched']} "
          f"(из них почти-подходят: {result['near_miss']})")
    print(f"  новых ............ {result['new']}")
    print(f"  отчёт ............ {result['report']}")
    if result["top"]:
        print("\n  Топ-5:")
        for listing in result["top"][:5]:
            price = f"{listing.price:,}".replace(",", " ") if listing.price else "—"
            print(f"   {listing.score:3}  {listing.year}  {price:>8} zł  "
                  f"{listing.title[:52]}")
    if args.open:
        subprocess.run(["open", str(result["report"])], check=False)
    return 0


def cmd_watch(args, cfg: dict) -> int:
    """
    Наблюдение в терминале — та же роль, что у приложения, только без значка.

    Поэтому и замок тот же: два наблюдателя (или наблюдатель вместе
    с `CarFinder.app`) опрашивали бы площадки вдвое чаще без всякой пользы.
    И то же завершение: по Ctrl+C или `kill` прогон отменяется сразу, а не
    после текущего запроса, и процесс не остаётся висеть.
    """
    from .http import Cancelled
    from .lifecycle import AlreadyRunning, InstanceLock, on_termination
    from .pipeline import run

    DATA.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(DATA / "app.lock")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        logging.error("уже работает другой экземпляр (%s) — второй не нужен", exc)
        return 1

    stop = threading.Event()
    on_termination(lambda name: stop.set())

    interval = args.interval * 60
    logging.info("режим наблюдения: каждые %s мин. Ctrl+C для выхода.", args.interval)
    try:
        while not stop.is_set():
            try:
                result = run(cfg, DATA, stop=stop)
                logging.info("готово: подходит %s, новых %s → %s",
                             result["matched"], result["new"], result["report"])
            except (Cancelled, KeyboardInterrupt):
                break
            except Exception as exc:
                logging.error("прогон упал: %s", exc)
            if stop.wait(interval):
                break
    finally:
        lock.release()
    logging.info("наблюдение остановлено")
    return 0


def cmd_urls(args, cfg: dict) -> int:
    """Печатает поисковые URL — удобно проверить фильтры руками в браузере."""
    from .sources import olx, otomoto

    print("otomoto:", otomoto.build_url(cfg))
    print()
    print("olx    :", olx.build_url(cfg))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carfinder", description="Поиск авто на otomoto.pl и olx.pl")
    parser.add_argument("-c", "--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="один прогон + отчёт")
    p_run.add_argument("--open", action="store_true", help="открыть отчёт в браузере")
    p_run.set_defaults(func=cmd_run)

    p_watch = sub.add_parser("watch", help="крутиться в фоне и обновлять отчёт")
    p_watch.add_argument("--interval", type=int, default=30, help="минут между прогонами")
    p_watch.set_defaults(func=cmd_watch)

    sub.add_parser("urls", help="показать поисковые URL").set_defaults(func=cmd_urls)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
