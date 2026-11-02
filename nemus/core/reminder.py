"""Отложенные напоминания: планировщик внутри процесса приложения.

Напоминание ставится инструментом ``remind`` и срабатывает через
``threading.Timer`` в отдельном потоке; результат уходит в колбэк
``on_fire`` (в GUI — реплика в чате и озвучка). Таймеры демонические,
поэтому процесс не ждёт их на выходе.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from nemus.utils.logger import logger

#: максимальный горизонт напоминания — дальше смысла мало (таймер переживёт
#: только сессию, состояние в БД не сохраняется)
MAX_DELAY = timedelta(days=30)


@dataclass
class _Reminder:
    id: str
    at: datetime
    text: str
    timer: threading.Timer = field(repr=False, default=None)  # type: ignore[assignment]


class ReminderScheduler:
    """Потокобезопасный список отложенных напоминаний."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _Reminder] = {}
        #: колбэк срабатывания: получает текст напоминания
        self.on_fire: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ api
    def add(self, at: datetime, text: str) -> tuple[str, datetime]:
        """Ставит напоминание. Возвращает (id, фактическое время)."""
        import uuid

        now = datetime.now()
        if at <= now:
            raise ValueError("Время напоминания уже прошло.")
        if at - now > MAX_DELAY:
            raise ValueError(f"Слишком далеко: максимум {MAX_DELAY.days} дней вперёд.")

        reminder_id = uuid.uuid4().hex[:8]
        delay = (at - now).total_seconds()
        timer = threading.Timer(delay, self._fire, args=(reminder_id,))
        timer.daemon = True
        item = _Reminder(id=reminder_id, at=at, text=text, timer=timer)
        with self._lock:
            self._items[reminder_id] = item
        timer.start()
        logger.info(f"Напоминание {reminder_id} на {at:%Y-%m-%d %H:%M:%S}: {text[:60]}")
        return reminder_id, at

    def pending(self) -> list[dict[str, Any]]:
        """Список активных напоминаний (время, текст, id)."""
        with self._lock:
            items = sorted(self._items.values(), key=lambda i: i.at)
        return [
            {"id": i.id, "at": i.at.isoformat(timespec="minutes"), "text": i.text}
            for i in items
        ]

    def cancel(self, reminder_id: str) -> bool:
        """Отменяет напоминание. True, если оно существовало."""
        with self._lock:
            item = self._items.pop(reminder_id, None)
        if item is None:
            return False
        item.timer.cancel()
        logger.info(f"Напоминание {reminder_id} отменено")
        return True

    def clear(self) -> None:
        """Отменяет все напоминания (выход из приложения)."""
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for item in items:
            item.timer.cancel()

    # -------------------------------------------------------------- internal
    def _fire(self, reminder_id: str) -> None:
        with self._lock:
            item = self._items.pop(reminder_id, None)
        if item is None:
            return
        logger.info(f"Напоминание {reminder_id} сработало: {item.text[:60]}")
        if self.on_fire is None:
            return
        try:
            self.on_fire(item.text)
        except Exception:  # noqa: BLE001 — колбэк GUI не должен ронять поток
            logger.exception("Сбой обработки напоминания")


__all__ = ["ReminderScheduler"]
