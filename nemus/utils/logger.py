"""Централизованное логирование на базе loguru."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_configured = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Настраивает вывод логов в консоль и (опционально) файл."""
    global _configured
    if _configured:
        return

    logger.remove()
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=level.upper(),
            colorize=True,
            format=(
                "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
                "<cyan>{name}</cyan> — <level>{message}</level>"
            ),
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
            backtrace=True,
            diagnose=False,
        )

    _configured = True


__all__ = ["logger", "setup_logging"]
