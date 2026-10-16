"""
Приложение в строке меню macOS.

Для фонового агента это удобнее окна: значок всегда на виду, рядом с ним —
счётчик новых объявлений с прошлого просмотра, а прогон запускается по клику
или сам, по таймеру. Заменяет собой launchd-агента: пока приложение запущено,
отдельный планировщик не нужен.

Три особенности, без которых приложение выглядит сломанным:

1. `LSUIElement` в нашем Info.plist ни на что не влияет. PyObjC для доступа
   к GUI перезапускает процесс через системный `Python.app`, и Dock смотрит
   уже в его Info.plist — отсюда иконка-ракета в Dock вместо значка в строке
   меню. Убирается только программно, через политику активации Accessory.
   У этого же перезапуска есть следствие пострашнее — значок не появляется
   совсем, если процесс запущен через `exec` из бандла; почему так и как
   лечится, написано в `scripts/build_app.sh` над генерацией launcher'а.

2. Cocoa запрещает трогать интерфейс не из главного потока. Прогон идёт минуту
   и обязан быть фоновым, поэтому поток считает, а результат передаётся
   в главный поток через `AppHelper.callAfter`. Прямое присваивание
   `self.title` из потока приводит к тому, что значок просто не появляется.

3. «Закрыл» для фонового приложения означает ровно одно: значок исчез. Дальше
   процесс либо умер, либо продолжает опрашивать площадки уже невидимо, и
   отличить одно от другого человек не может. Поэтому выход тут не один вызов
   `terminate:`, а последовательность: погасить таймеры, отменить идущий
   прогон, снять замок и — если через `GRACE_SECONDS` процесс всё ещё жив —
   выйти принудительно. Подробности про замок и сигналы — в `lifecycle`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import traceback
from datetime import datetime

import rumps
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                    NSScreen)
from PyObjCTools import AppHelper

from . import lifecycle, notify
from .cli import DATA, ROOT, load_config
from .http import Cancelled
from .lifecycle import AlreadyRunning, InstanceLock

log = logging.getLogger(__name__)

IDLE_ICON = "🚗"
BUSY_ICON = "🔄"
FAIL_ICON = "⚠️"
PAUSED_MARK = "⏸"

# Сколько ждать мягкого завершения, прежде чем выйти принудительно. Прогон
# отменяется на ближайшем сетевом вызове, так что этого хватает с запасом.
GRACE_SECONDS = 3.0

# Через сколько после старта проверять, попал ли значок в строку меню.
STATUS_CHECK_DELAY = 3.0

class CarFinderApp(rumps.App):
    def __init__(self, lock: InstanceLock):
        super().__init__("CarFinder", title=IDLE_ICON, quit_button=None)
        self.config_path = ROOT / "config.yaml"
        self.report = DATA / "report.html"
        self.lock = lock
        self._busy = threading.Lock()
        self._stopping = threading.Event()
        self._force_timer: threading.Timer | None = None
        self._state = "idle"
        self._unseen = 0

        self.item_status = rumps.MenuItem("Запускается…")
        self.item_open = rumps.MenuItem("Открыть отчёт", callback=self.open_report)
        self.item_run = rumps.MenuItem("Обновить сейчас", callback=self.run_now)
        self.item_auto = rumps.MenuItem("Обновлять автоматически", callback=self.toggle_auto)
        self.item_config = rumps.MenuItem("Изменить критерии…", callback=self.open_config)
        self.item_folder = rumps.MenuItem("Показать файлы…", callback=self.open_folder)
        # Пункт есть только когда есть что убирать: launchd-агент — единственное,
        # что продолжает работать после выхода из приложения.
        self.item_agent = rumps.MenuItem("Отключить фоновый launchd-агент",
                                         callback=self.remove_agent)

        self.menu = [
            self.item_status,
            None,
            self.item_open,
            self.item_run,
            None,
            self.item_auto,
            self.item_config,
            self.item_folder,
            None,
            rumps.MenuItem("Выход", callback=self.quit_app),
        ]
        self._sync_agent_item()

        if self.report.exists():
            when = datetime.fromtimestamp(self.report.stat().st_mtime)
            self.item_status.title = f"Отчёт от {when:%d.%m %H:%M}"

        interval = self._interval_minutes()
        self.timer = rumps.Timer(self.on_tick, interval * 60)
        self.item_auto.state = 1
        self.item_auto.title = f"Обновлять автоматически ({interval} мин)"
        self.timer.start()

        # Первый прогон — не здесь: на этом этапе строка меню ещё не создана,
        # и обновления значка из потока просто потерялись бы. Ждём, пока
        # приложение поднимется, и стартуем разово. Заметьте, что rumps-таймер
        # срабатывает сразу же, а не через переданный интервал: NSTimer
        # создаётся с датой срабатывания «сейчас», и 3 — это лишь период
        # повторов, который мы всё равно гасим первым же вызовом.
        self._boot_timer = rumps.Timer(self._boot, 3)
        self._boot_timer.start()

    # ── настройки ─────────────────────────────────────────────────────────────
    def _config(self) -> dict:
        try:
            return load_config(self.config_path)
        except Exception as exc:
            log.error("не удалось прочитать config.yaml: %s", exc)
            return {}

    def _interval_minutes(self) -> int:
        return int((self._config().get("app") or {}).get("interval_minutes", 30))

    # ── действия меню ─────────────────────────────────────────────────────────
    def open_report(self, _):
        if not self.report.exists():
            rumps.notification("Car Finder", "Отчёта пока нет",
                               "Нажмите «Обновить сейчас»")
            return
        log.info("открываю отчёт в браузере")
        subprocess.run(["open", str(self.report)], check=False)
        self._unseen = 0          # отчёт просмотрен — счётчик можно гасить
        self._refresh_title()

    def open_config(self, _):
        subprocess.run(["open", "-t", str(self.config_path)], check=False)

    def open_folder(self, _):
        subprocess.run(["open", str(DATA)], check=False)

    def toggle_auto(self, sender):
        sender.state = 0 if sender.state else 1
        if sender.state:
            interval = self._interval_minutes()
            self.timer.interval = interval * 60
            self.timer.start()
            sender.title = f"Обновлять автоматически ({interval} мин)"
        else:
            self.timer.stop()
            sender.title = "Обновлять автоматически"
        self._refresh_title()

    def remove_agent(self, _):
        if lifecycle.remove_legacy_agent():
            rumps.notification("Car Finder", "Фоновый launchd-агент отключён",
                               "Прогоны идут только пока запущено приложение")
        self._sync_agent_item()

    def _sync_agent_item(self) -> None:
        installed = lifecycle.legacy_agent_installed()
        present = self.item_agent.title in self.menu
        if installed and not present:
            self.menu.insert_after(self.item_folder.title, self.item_agent)
        elif not installed and present:
            del self.menu[self.item_agent.title]

    # ── завершение ────────────────────────────────────────────────────────────
    def quit_app(self, _):
        self._shutdown("меню «Выход»")

    def shutdown_from_signal(self, name: str) -> None:
        """
        Завершение по сигналу: `kill`, выход из системы, перезагрузка.

        Приходит из потока-наблюдателя, поэтому сама работа делается в главном:
        останавливать rumps-таймеры и звать `terminate:` из чужого потока
        нельзя. Страховку взводим сразу и здесь — если рун-луп по какой-то
        причине не крутится, `callAfter` не выполнится никогда.
        """
        self._stopping.set()
        self._arm_force_exit()
        AppHelper.callAfter(self._shutdown, name)

    def _shutdown(self, reason: str) -> None:
        log.info("завершение работы: %s", reason)
        self._stopping.set()          # идущий прогон свернётся на ближайшем запросе
        for timer in (self.timer, getattr(self, "_boot_timer", None)):
            try:
                if timer is not None:
                    timer.stop()
            except Exception as exc:
                log.debug("таймер уже остановлен: %s", exc)
        self._arm_force_exit()
        self.lock.release()
        rumps.quit_application()

    def _arm_force_exit(self) -> None:
        """
        Страховка от «значок исчез, а процесс остался».

        `terminate:` может не довести дело до конца: фоновый поток внутри
        сетевого вызова или распознавания фото, зависший рун-луп. Для человека
        приложение в этот момент уже закрыто — значка нет, — поэтому лучше
        выйти жёстко, чем оставить невидимый процесс опрашивать площадки.
        Данные при этом не страдают: запись в SQLite идёт транзакцией, а
        отчёт до неё либо дописан целиком, либо не начат.
        """
        if self._force_timer is not None:
            return
        timer = threading.Timer(GRACE_SECONDS, self._force_exit)
        timer.name = "force-exit"
        timer.daemon = True
        timer.start()
        self._force_timer = timer

    def _force_exit(self) -> None:
        log.warning("процесс не завершился за %s с — выходим принудительно",
                    GRACE_SECONDS)
        self.lock.release()
        logging.shutdown()
        os._exit(0)

    def _boot(self, timer):
        timer.stop()
        # Проверка значка — с задержкой: раскладка строки меню происходит уже
        # после того, как значок создан, и сразу после старта его координаты
        # нулевые даже когда всё в порядке.
        AppHelper.callLater(STATUS_CHECK_DELAY, self._report_status_item)
        # Показываем, что приложение действительно поднялось: у него нет окна,
        # и без этого запуск неотличим от «ничего не произошло».
        rumps.notification(
            "Car Finder запущен",
            "Значок 🚗 — в строке меню, справа вверху",
            "Обновляю выдачу…",
        )
        if lifecycle.legacy_agent_installed():
            log.warning("установлен launchd-агент %s — он работает и после выхода "
                        "из приложения", lifecycle.LEGACY_AGENT_LABEL)
            rumps.notification(
                "Car Finder", "Найден фоновый launchd-агент",
                "Он опрашивает площадки и после выхода. Отключить — в меню значка",
            )
        if self.report.exists():
            self.open_report(None)
        self.run_now(None)

    def _report_status_item(self) -> None:
        """
        Пишет в лог, добрался ли значок до строки меню — и если нет, то почему.

        «Приложение запустилось, но ничего не видно» имеет две разные причины:
        значок не создан (ошибка в коде) либо создан, но не помещается — на
        MacBook с вырезом строка меню короче, чем кажется, и лишние значки
        просто не рисуются. Отличает их положение значка на экране: у
        непоместившегося оно так и остаётся нулевым.

        Мерить нужно с задержкой (см. `_boot`): сразу после создания значок ещё
        не разложен, и его координаты нулевые независимо от того, поместился он
        или нет. Ни `isVisible`, ни список окон оконного сервера тут не помощь:
        первый отражает наше же пожелание показать значок, а второй без
        разрешения на запись экрана пуст даже для собственных окон процесса.
        """
        try:
            item = getattr(self._nsapp, "nsstatusitem", None)
            if item is None:
                log.error("значок в строке меню НЕ создан — ошибка при старте")
                return

            button = item.button()
            window = button.window() if button else None
            frame = window.frame() if window is not None else None
            screen = NSScreen.mainScreen()
            screen_width = screen.frame().size.width if screen is not None else 0.0

            placed = frame is not None and frame.size.width > 0 and frame.origin.x > 0
            log.info("значок: заголовок %r, %s, экран %.0f px — %s",
                     str(button.title()) if button else "?",
                     f"x={frame.origin.x:.0f} y={frame.origin.y:.0f} "
                     f"w={frame.size.width:.0f}" if frame is not None else "нет окна",
                     screen_width,
                     "на месте" if placed else "НЕ РАЗМЕЩЁН")
            if placed:
                return

            log.error("значок не поместился в строку меню — macOS его не рисует")
            rumps.notification(
                "Car Finder работает, но без значка",
                "Значок не поместился в строку меню",
                "Уберите из неё лишнее: ⌘ + перетащить значок за пределы строки",
            )
        except Exception as exc:
            log.warning("не удалось проверить значок: %s", exc)

    def on_tick(self, _):
        self.run_now(None)

    def run_now(self, _):
        if self._busy.locked() or self._stopping.is_set():
            return                 # прогон уже идёт (или мы закрываемся)
        threading.Thread(target=self._run, daemon=True).start()

    # ── сам прогон ────────────────────────────────────────────────────────────
    def _run(self) -> None:
        """Выполняется в фоновом потоке. Интерфейс отсюда трогать нельзя."""
        if not self._busy.acquire(blocking=False):
            return
        AppHelper.callAfter(self._ui_busy)
        try:
            from .pipeline import run
            result = run(self._config(), DATA, stop=self._stopping)
            AppHelper.callAfter(self._ui_done, result)
        except Cancelled:
            # Приложение закрывается — интерфейса, который стоило бы обновить,
            # через мгновение уже не будет.
            log.info("прогон прерван, приложение закрывается")
        except Exception as exc:
            log.error("прогон упал: %s\n%s", exc, traceback.format_exc())
            AppHelper.callAfter(self._ui_failed, str(exc))
        finally:
            self._busy.release()

    # ── обновления интерфейса: только главный поток ───────────────────────────
    def _refresh_title(self) -> None:
        """
        Значок — единственный индикатор состояния: окна нет.

        🚗 — работает, 🔄 — идёт прогон, ⚠️ — последний прогон упал, ⏸ —
        автообновление выключено вручную. Число рядом — сколько новых
        объявлений появилось с тех пор, как отчёт открывали в последний раз.
        """
        if self._state == "busy":
            self.title = BUSY_ICON
            return
        parts = [FAIL_ICON if self._state == "failed" else IDLE_ICON]
        if self._unseen:
            parts.append(str(self._unseen))
        if not self.item_auto.state:
            parts.append(PAUSED_MARK)
        self.title = " ".join(parts)

    def _ui_busy(self) -> None:
        self._state = "busy"
        self._refresh_title()
        self.item_run.title = "Обновляется…"

    def _ui_done(self, result: dict) -> None:
        self._state = "idle"
        self._unseen += result["new"]
        self._refresh_title()
        self.item_status.title = (
            f"{result['matched'] - result['near_miss']} подходит · "
            f"{result['new']} новых · {datetime.now():%H:%M}"
        )
        self.item_run.title = "Обновить сейчас"

    def _ui_failed(self, message: str) -> None:
        self._state = "failed"
        self._refresh_title()
        self.item_status.title = f"Ошибка: {message[:60]}"
        self.item_run.title = "Обновить сейчас"


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        filename=str(DATA / "app.log"),
    )

    lock = InstanceLock(DATA / "app.lock")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        # Второй экземпляр не поднимаем: делать он будет то же самое, а вот
        # значок его может и не поместиться в строку меню — получился бы
        # невидимый процесс, который не закрыть. Вместо этого ведём себя так,
        # как и ожидается от повторного двойного клика по бандлу: открываем
        # отчёт. Именно этим путём повторный запуск и идёт — Launch Services
        # наш процесс не отслеживает, см. scripts/build_app.sh.
        log.warning("запуск отменён — %s", exc)
        notify.banner("Car Finder уже запущен",
                      "Значок 🚗 — в строке меню, справа вверху")
        report = DATA / "report.html"
        if report.exists():
            subprocess.run(["open", str(report)], check=False)
        return 0

    # Без этого приложение висит в Dock иконкой-ракетой системного Python.app.
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )

    application = CarFinderApp(lock)
    # Строго из главного потока и до старта рун-лупа.
    lifecycle.on_termination(application.shutdown_from_signal)
    try:
        application.run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
