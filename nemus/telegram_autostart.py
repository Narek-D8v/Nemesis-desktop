"""Автозапуск headless-бота Telegram через Планировщик заданий Windows.

Задача ``NemusTelegram`` уже описана в ``scripts/install_telegram_autostart.ps1``:
она запускает ``pythonw -m nemus --telegram`` при входе пользователя и
перезапускает процесс при падении. Этот модуль лишь включает/выключает её из
настроек GUI и умеет честно сказать, включена ли она сейчас.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "NemusTelegram"
SHORTCUT_NAME = "Немуся Telegram.lnk"
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_telegram_autostart.ps1"


def supported() -> bool:
    """Автозапуск реализован только для Windows (Task Scheduler)."""
    return sys.platform == "win32"


def _script_path() -> Path:
    return _SCRIPT


def _startup_dir() -> Path:
    """Папка автозагрузки текущего пользователя (видна в Windows)."""
    raw = os.environ.get("APPDATA")
    base = Path(raw) if raw else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_shortcut() -> Path:
    return _startup_dir() / SHORTCUT_NAME


def _run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    # Консоль Windows отдаёт cp866; скрипт сохранён с BOM, поэтому русский
    # текст ошибок читается, а не превращается в «?????».
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "cp866" if sys.platform == "win32" else "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, **kwargs)


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["schtasks", *args], timeout=15)


def _run_script(extra_args: list[str]) -> subprocess.CompletedProcess[str]:
    """Запускает установочный PowerShell-скрипт с нужными ключами."""
    return _run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_script_path()),
            *extra_args,
        ]
    )


def status() -> tuple[bool, str]:
    """Возвращает (включён ли автозапуск, текст ошибки проверки)."""
    if not supported():
        return False, "Автозапуск доступен только в Windows."
    try:
        result = _run_schtasks(["/Query", "/TN", TASK_NAME])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Не удалось проверить задачу: {exc}"
    # Ненулевой код означает «задачи нет» либо сбой запроса. Для галочки в
    # настройках это в любом случае «выключено»; подробности не показываем,
    # чтобы не пугать пользователя локализованным выводом schtasks.
    if result.returncode != 0:
        return False, ""
    # Старая версия создавала только задачу Планировщика: в «Автозагрузке»
    # её не видно, поэтому без ярлыка автозапуск считается выключенным.
    if not _startup_shortcut().is_file():
        return False, ""
    return True, ""


def is_enabled() -> bool:
    """Включена ли задача автозапуска (False, если проверить не удалось)."""
    return status()[0]


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """Создаёт или удаляет задачу автозапуска. Возвращает (успех, ошибка)."""
    if not supported():
        return False, "Автозапуск доступен только в Windows."
    script = _script_path()
    if not script.is_file():
        return False, f"Не найден установочный скрипт: {script}"
    args = [] if enabled else ["-Uninstall"]
    try:
        result = _run_script(args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Не удалось изменить автозапуск: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"PowerShell завершился с кодом {result.returncode}"
    if enabled:
        ok, check_error = status()
        if not ok:
            return False, (
                "Задача создана, но автозапуск не появился в Windows. "
                "Проверь права пользователя на папку Автозагрузки. "
                + check_error
            ).strip()
    return True, ""
