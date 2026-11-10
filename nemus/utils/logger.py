"""Централизованное логирование на базе loguru.

Все сообщения (включая трейсбеки) проходят через ``redact_secrets``: тексты
ошибок сетевых библиотек содержат заголовки запросов с API-ключами, и в лог
они не должны попадать. loguru подставляет трейсбек в ``{exception}`` уже
после фильтров, поэтому исключение рендерится в фильтре, маскируется и
подкладывается в сообщение — иначе маскировать было бы нечего.
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from loguru import logger

from nemus.utils.redact import redact_secrets

_configured = False


class _InterceptHandler(logging.Handler):
    """Переводит логи сторонних библиотек (telegram, httpx) в loguru.

    Без этого в ``pythonw`` у python-telegram-bot нет консоли, и ошибки
    polling'а/обработчиков уходили в никуда: в файл попадали только наши
    сообщения.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        try:
            # Без depth: неверная глубина стека уронила бы сам обработчик,
            # и сообщение снова потерялось бы — важнее доставить его.
            logger.opt(exception=record.exc_info).log(level, record.getMessage())
        except Exception:  # noqa: BLE001
            pass


def _bridge_stdlib(level: str) -> None:
    handler = _InterceptHandler()
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # HTTP-клиенты на INFO пишут каждый запрос (в URL Telegram — токен),
    # поэтому им достаточно предупреждений и ошибок.
    levels = {
        "telegram": level.upper(),
        "httpx": "WARNING",
        "httpcore": "WARNING",
        "urllib3": "WARNING",
        "asyncio": "WARNING",
    }
    for name, lib_level in levels.items():
        lib = logging.getLogger(name)
        lib.handlers = [handler]
        lib.propagate = False
        lib.setLevel(lib_level)


_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{name}</cyan> — <level>{message}</level>"
)


def _redact_record(record: dict[str, Any]) -> bool:
    """Маскирует секреты в сообщении и в трейсбеке исключения.

    Фильтр вызывается до форматирования, а трейсбек логурю рендерит из
    ``record['exception']`` позже, поэтому исключение раскрывается здесь
    заранее: рендерим, маскируем, кладём в сообщение и гасим ``exception``,
    чтобы второй синк не напечатал его без маскировки.
    """
    try:
        record["message"] = redact_secrets(str(record["message"]))
        exc = record.get("exception")
        if exc:
            exc_type, exc_value, exc_tb = exc
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            record["message"] = f"{record['message']}\n{redact_secrets(text)}"
            record["exception"] = None
    except Exception:  # noqa: BLE001
        # логирование не должно падать ни при каких раскладах
        pass
    return True


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Настраивает вывод логов в консоль и (опционально) в файл."""
    global _configured
    if _configured:
        return

    logger.remove()
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=level.upper(),
            colorize=True,
            filter=_redact_record,
            format=_CONSOLE_FORMAT,
            backtrace=False,
            diagnose=False,
        )

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(path),
            level=level.upper(),
            rotation="5 MB",
            retention=5,
            encoding="utf-8",
            enqueue=True,
            filter=_redact_record,
            backtrace=True,
            diagnose=False,
        )

    _bridge_stdlib(level)
    _configured = True


# Пока setup_logging не вызван (скрипты, импорт модуля), loguru пишет в stderr
# своим обработчиком с diagnose=True — он печатает локальные переменные
# фреймов, то есть заголовки запросов с API-ключами. Ставим свой обработчик
# вместо него, чтобы маскирование действовало всегда.
if sys.stderr is not None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        colorize=True,
        filter=_redact_record,
        format=_CONSOLE_FORMAT,
        backtrace=True,
        diagnose=False,
    )


__all__ = ["logger", "setup_logging"]
