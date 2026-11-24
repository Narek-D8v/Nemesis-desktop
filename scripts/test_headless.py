"""Тест headless-режима: конфиг из env, проверка перед запуском, файл-замок.

Без сети и без реального токена: проверяется то, что легко сломать при
развёртывании на сервере, — разбор ID из переменной окружения, понятные
ошибки конфигурации и запрет двух ботов с одним токеном.

Запуск:  venv\\Scripts\\python.exe scripts\\test_headless.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus import telegram_autostart, telegram_main  # noqa: E402
from nemus.config import Config, TelegramConfig  # noqa: E402
from nemus.core.telegram_bot import TelegramBot  # noqa: E402
from nemus.telegram_main import (  # noqa: E402
    _health_ok,
    _start_bot_with_retry,
    _validate,
    _write_health,
)


def test_allowed_users_from_env() -> None:
    assert TelegramConfig(allowed_users="123, 456").allowed_users == [123, 456]
    assert TelegramConfig(allowed_users="123 456").allowed_users == [123, 456]
    assert TelegramConfig(allowed_users=123).allowed_users == [123]
    assert TelegramConfig(allowed_users=[1, 2]).allowed_users == [1, 2]
    print("ok  allowed_users: строка из env и список из YAML")


def test_validate() -> None:
    cfg = Config()
    assert "выключен" in _validate(cfg)

    cfg.telegram.enabled = True
    assert "токен" in _validate(cfg).lower()

    cfg.telegram.token = "123:abc"
    assert "ID" in _validate(cfg)

    cfg.telegram.allowed_users = [42]
    assert _validate(cfg) == ""
    print("ok  проверка конфигурации: понятные ошибки до запуска")


def test_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "nemus.db")
        first = object.__new__(TelegramBot)
        first._lock_handle = None
        first.config = SimpleNamespace(memory=SimpleNamespace(database=db))

        second = object.__new__(TelegramBot)
        second._lock_handle = None
        second.config = SimpleNamespace(memory=SimpleNamespace(database=db))

        assert first._acquire_lock(), "первый бот должен занять замок"
        assert not second._acquire_lock(), "второй бот не должен стартовать"
        first._release_lock()
        assert second._acquire_lock(), "после остановки замок освобождается"
        second._release_lock()
    print("ok  файл-замок: два процесса с одним токеном не конфликтуют")


def test_health_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "telegram.health"
        assert not _health_ok(path), "отсутствующий heartbeat не должен быть здоровым"
        _write_health(path)
        assert _health_ok(path), "свежий heartbeat должен быть здоровым"
        path.write_text(str(time.time() - 200), encoding="utf-8")
        assert not _health_ok(path), "старый heartbeat не должен быть здоровым"
        path.write_text("не число", encoding="utf-8")
        assert not _health_ok(path), "битый heartbeat не должен быть здоровым"
    print("ok  heartbeat: свежий/старый/битый файл")


def test_bot_health() -> None:
    bot = object.__new__(TelegramBot)
    bot._start_error = ""
    bot._thread = SimpleNamespace(is_alive=lambda: True)
    bot._app = SimpleNamespace(updater=SimpleNamespace(running=True))
    assert bot.healthy, "живой поток и updater — здоров"

    bot._app.updater.running = False
    assert not bot.healthy, "остановленный polling — не здоров"

    bot._app.updater.running = True
    bot._thread = SimpleNamespace(is_alive=lambda: False)
    assert not bot.healthy, "умерший поток — не здоров"

    bot._thread = SimpleNamespace(is_alive=lambda: True)
    bot._start_error = "ошибка старта"
    assert not bot.healthy, "ошибка старта — не здоров"
    print("ok  health: поток, polling и ошибка старта")


def test_start_bot_with_retry() -> None:
    """Временный сбой сети не должен оставлять демон лежать навсегда."""

    class _Stop:
        def __init__(self, limit: int) -> None:
            self.limit = limit
            self.waits = 0

        def is_set(self) -> bool:
            return self.waits >= self.limit

        def wait(self, timeout: float) -> bool:
            self.waits += 1
            return self.is_set()

    class _Bot:
        def __init__(self, succeed_on: int) -> None:
            self.succeed_on = succeed_on
            self.calls = 0
            self.stops = 0

        def start(self, wait: bool = True) -> bool:
            self.calls += 1
            return self.calls >= self.succeed_on

        def stop(self) -> None:
            self.stops += 1

    old_delays = telegram_main.RETRY_DELAYS
    telegram_main.RETRY_DELAYS = (0.0,)
    try:
        ok_bot = _Bot(succeed_on=2)
        assert _start_bot_with_retry(ok_bot, _Stop(10))  # type: ignore[arg-type]
        assert (ok_bot.calls, ok_bot.stops) == (2, 1), "вторая попытка после сбоя"

        bad_bot = _Bot(succeed_on=99)
        assert not _start_bot_with_retry(bad_bot, _Stop(4))  # type: ignore[arg-type]
        assert (bad_bot.calls, bad_bot.stops) == (4, 3), "попытки идут до остановки"
    finally:
        telegram_main.RETRY_DELAYS = old_delays
    print("ok  запуск бота: попытки не заканчиваются, пока процесс не остановят")


def test_ping_detects_dead_api() -> None:
    """Сторож должен видеть, что Bot API не отвечает, а не только поток."""
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready.wait(5)

    class _Api:
        down = False

        async def get_me(self, **kwargs: object) -> object:
            if self.down:
                raise RuntimeError("api.telegram.org недоступен")
            return SimpleNamespace(username="bot")

    api = _Api()
    bot = object.__new__(TelegramBot)
    bot._thread = SimpleNamespace(is_alive=lambda: True)
    bot._app = SimpleNamespace(bot=api)
    bot._loop = loop
    bot.cfg = SimpleNamespace(token="")
    try:
        assert bot.ping(timeout=1), "живой API должен отвечать"
        api.down = True
        assert not bot.ping(timeout=1), "молчащий API должен считаться недоступным"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
    print("ok  ping: недоступный Bot API отличается от живого")


def test_start_resets_old_error() -> None:
    """Старая ошибка не должна мешать успешной повторной попытке."""
    bot = object.__new__(TelegramBot)
    bot._start_error = "старая ошибка"
    bot._thread = None
    bot._app = None
    bot.cfg = SimpleNamespace(enabled=False, token="", allowed_users=[])
    assert not bot.start(), "выключенный бот не запускается"
    assert bot._start_error == "", "ошибка прошлой попытки сброшена"
    print("ok  повторный запуск: старая ошибка старта сбрасывается")


def test_telegram_autostart_commands() -> None:
    """Настройки не должны трогать Task Scheduler без явной команды."""
    original_supported = telegram_autostart.supported
    original_script = telegram_autostart._script_path
    original_shortcut = telegram_autostart._startup_shortcut
    original_run_script = telegram_autostart._run_script
    original_run_schtasks = telegram_autostart._run_schtasks
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    try:
        telegram_autostart.supported = lambda: True
        telegram_autostart._script_path = lambda: Path(__file__)
        telegram_autostart._startup_shortcut = lambda: Path(__file__)
        telegram_autostart._run_script = lambda args: calls.append(args) or Result()
        telegram_autostart._run_schtasks = lambda args: Result()

        assert telegram_autostart.set_enabled(True) == (True, "")
        assert calls == [[]], calls
        assert telegram_autostart.set_enabled(False) == (True, "")
        assert calls[-1] == ["-Uninstall"], calls
        assert telegram_autostart.status() == (True, "")

        # задача есть, а ярлыка в автозагрузке нет — это старый сломанный случай
        telegram_autostart._startup_shortcut = lambda: Path("missing.lnk")
        assert telegram_autostart.status() == (False, "")
        ok, message = telegram_autostart.set_enabled(True)
        assert not ok and "автозапуск не появился" in message, message
        telegram_autostart._startup_shortcut = lambda: Path(__file__)

        Result.returncode = 1
        assert telegram_autostart.status() == (False, ""), "нет задачи — это выключено"
        ok, message = telegram_autostart.set_enabled(True)
        assert not ok and "кодом 1" in message

        telegram_autostart.supported = lambda: False
        assert telegram_autostart.status()[0] is False
        assert telegram_autostart.set_enabled(True)[0] is False
    finally:
        telegram_autostart.supported = original_supported
        telegram_autostart._script_path = original_script
        telegram_autostart._startup_shortcut = original_shortcut
        telegram_autostart._run_script = original_run_script
        telegram_autostart._run_schtasks = original_run_schtasks
    print("ok  автозапуск: установка, удаление и отсутствие задачи")


def test_stdlib_log_bridge() -> None:
    """Ошибки python-telegram-bot не должны теряться без консоли."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "nemus.log"
        token = "123456789:AAFakeTokenValueThatIsLongEnough123"
        code = (
            "from nemus.utils.logger import setup_logging; "
            "import logging, time; "
            f"setup_logging('INFO', {str(log)!r}); "
            f"logging.getLogger('telegram').error("
            f"'POST https://api.telegram.org/bot{token}/getMe'); "
            "time.sleep(0.4)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert log.exists(), "лог-файл не создан"
        text = log.read_text(encoding="utf-8")
        assert "api.telegram.org" in text
        assert token not in text, "токен Telegram утёк в лог"
        assert "bot***" in text, text
    print("ok  логи telegram/httpx идут в файл, токен маскируется")


def test_no_gui_import() -> None:
    code = (
        "import sys; import nemus.telegram_main; "
        "print('PySide6' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("False"), result.stdout
    print("ok  headless-режим не тянет PySide6")


def main() -> int:
    test_allowed_users_from_env()
    test_validate()
    test_lock()
    test_health_file()
    test_bot_health()
    test_start_bot_with_retry()
    test_ping_detects_dead_api()
    test_start_resets_old_error()
    test_telegram_autostart_commands()
    test_stdlib_log_bridge()
    test_no_gui_import()
    print("\nHEADLESS TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
