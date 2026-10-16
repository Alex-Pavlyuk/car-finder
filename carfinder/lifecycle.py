"""
Единственный экземпляр, реакция на сигналы, следы в системе.

Три вещи, которые для фонового агента без окна решаются только здесь.

1. **Вторая копия.** Запустить приложение дважды слишком легко: двойной клик
   по бандлу, `open CarFinder.app` из терминала, автозапуск при входе. Второй
   значок в строке меню при этом может и не появиться — на MacBook с вырезом
   строка обрезается — и получается процесс, который опрашивает площадки, пишет
   в ту же базу и не убирается ничем, кроме Мониторинга системы. Замок на файле
   в `data/` делает такой запуск невозможным.

   Замок именно `flock`, а не pid-файл: ядро снимает его само, когда процесс
   умирает любым способом, включая `SIGKILL` и падение. Проверять, жив ли
   записанный pid, и подчищать остатки не нужно — не бывает «застрявшего»
   замка. Pid внутрь файла всё равно пишем, но только чтобы было что показать
   человеку.

2. **Сигналы при занятом рун-лупе.** Обычный `signal.signal` в приложении
   с Cocoa бесполезен: Python исполняет обработчик, только когда управление
   возвращается в интерпретатор, а главный поток всё время внутри C-кода
   `AppHelper.runEventLoop` и вернётся оттуда лишь на следующем тике таймера —
   через полчаса. То есть `kill`, выход из системы и перезагрузка не закрывали
   бы приложение вовсе. `set_wakeup_fd` пишет номер сигнала в трубу прямо
   из C-обработчика, а отдельный поток забирает его сразу.

3. **launchd-агент.** Единственное, что переживает выход из приложения. Если он
   когда-то был установлен, «закрыл приложение» не означает «ничего больше не
   работает», поэтому приложение находит его и предлагает убрать.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

LEGACY_AGENT_LABEL = "com.carfinder.watch"
LEGACY_AGENT_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_AGENT_LABEL}.plist"
)


class AlreadyRunning(RuntimeError):
    """Замок держит другой живой процесс."""

    def __init__(self, pid: int | None):
        super().__init__(f"уже запущен, pid {pid}" if pid else "уже запущен")
        self.pid = pid


class InstanceLock:
    """Замок «работает только один экземпляр», снимаемый ядром при смерти процесса."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Занимает замок или бросает `AlreadyRunning` с pid владельца."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(fd)
                raise
            holder = self._read_pid(fd)
            os.close(fd)
            raise AlreadyRunning(holder) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        log.info("замок занят: %s (pid %s)", self.path.name, os.getpid())

    def release(self) -> None:
        """
        Снимает замок. Вызывать не обязательно — ядро сделает это за нас, —
        но так следующий запуск не ждёт, пока система закроет дескриптор.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    @staticmethod
    def _read_pid(fd: int) -> int | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 32).decode("ascii", "ignore").strip()
            return int(raw) if raw.isdigit() else None
        except (OSError, ValueError):
            return None


def on_termination(handler: Callable[[str], None]) -> None:
    """
    Зовёт `handler(имя_сигнала)` на SIGTERM / SIGINT / SIGHUP.

    Работает и когда главный поток сидит в Cocoa-рун-лупе: обработчик сигнала
    в C только пишет байт в трубу, а разбирается с ним отдельный поток.
    Вызывать строго из главного потока — `signal.signal` из других запрещён.
    """
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    signal.set_wakeup_fd(write_fd)

    # Обработчик-пустышка нужен по существу: без него для сигнала остаётся
    # системное действие (мгновенная смерть процесса) и в трубу ничего
    # не пишется — Python пишет туда только для сигналов, которые ведёт сам.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: None)

    def watch() -> None:
        while True:
            data = os.read(read_fd, 1)
            if not data:
                return
            try:
                name = signal.Signals(data[0]).name
            except ValueError:
                name = f"сигнал {data[0]}"
            log.info("получен %s", name)
            handler(name)

    threading.Thread(target=watch, name="signal-watch", daemon=True).start()


# ── следы в системе ───────────────────────────────────────────────────────────
def legacy_agent_installed() -> bool:
    """Установлен ли launchd-агент — единственное, что живёт дольше приложения."""
    return LEGACY_AGENT_PLIST.exists()


def remove_legacy_agent() -> bool:
    """Выгружает и удаляет launchd-агента. Только по явной команде из меню."""
    if not LEGACY_AGENT_PLIST.exists():
        return False
    subprocess.run(["launchctl", "unload", "-w", str(LEGACY_AGENT_PLIST)],
                   check=False, capture_output=True)
    try:
        LEGACY_AGENT_PLIST.unlink()
    except OSError as exc:
        log.error("не удалось удалить %s: %s", LEGACY_AGENT_PLIST, exc)
        return False
    log.info("launchd-агент %s выгружен и удалён", LEGACY_AGENT_LABEL)
    return True
