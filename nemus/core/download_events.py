"""Единая шина прогресса загрузок: файлы, модель, браузер и торрент.

Одно типизированное событие ``DownloadEvent`` используют загрузчики и UI
(Telegram/GUI). Подписчики получают копию списка слушателей вне блокировки,
поэтому медленный или упавший UI-слушатель не задерживает и не обрывает
загрузку.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from nemus.utils.logger import logger

DownloadListener = Callable[["DownloadEvent"], None]


@dataclass(frozen=True)
class DownloadEvent:
    """Прогресс одной загрузки; ``task_id`` разделяет параллельные задачи."""

    kind: str  # file | browser | torrent | model
    phase: str  # start | progress | done | error | cancelled
    name: str
    done: int = 0
    total: int = 0
    path: str = ""
    message: str = ""
    task_id: str = ""

    @property
    def percent(self) -> int | None:
        """Процент или None, если общий размер неизвестен."""
        if self.total <= 0:
            return None
        return max(0, min(100, int(self.done * 100 / self.total)))


#: токен подписки -> слушатель; токен непрозрачен для вызывающего кода
_LISTENERS: dict[str, DownloadListener] = {}
_LISTENERS_LOCK = threading.Lock()


def new_download_id() -> str:
    """Короткий уникальный id задачи для событий и UI."""
    return uuid.uuid4().hex[:12]


def subscribe_download(listener: DownloadListener) -> str:
    """Подписывает слушателя; возвращает токен для ``unsubscribe_download``."""
    token = uuid.uuid4().hex
    with _LISTENERS_LOCK:
        _LISTENERS[token] = listener
    return token


def unsubscribe_download(token: str) -> None:
    """Снимает подписку по токену; неизвестный токен игнорируется."""
    with _LISTENERS_LOCK:
        _LISTENERS.pop(token, None)


def publish_download(event: DownloadEvent) -> None:
    """Рассылает событие всем слушателям, изолируя их ошибки."""
    with _LISTENERS_LOCK:
        listeners = list(_LISTENERS.values())
    for listener in listeners:
        try:
            listener(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Подписчик прогресса загрузки упал: {exc}")


def render_progress_bar(percent: int | None, width: int = 20) -> str:
    """Полоса из ``width`` символов; неизвестный процент — только ``░``."""
    if percent is None:
        return "░" * width
    filled = round(width * max(0, min(100, percent)) / 100)
    return "█" * filled + "░" * (width - filled) + f" {percent}%"


__all__ = [
    "DownloadEvent",
    "new_download_id",
    "publish_download",
    "render_progress_bar",
    "subscribe_download",
    "unsubscribe_download",
]
