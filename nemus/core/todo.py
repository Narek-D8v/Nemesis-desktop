"""План задачи, который агент показывает пользователю через /todo."""

from __future__ import annotations

import json
import threading
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Callable

#: сколько пунктов храним: длинный план не должен раздувать промпт и БД
MAX_ITEMS = 30
MAX_TITLE = 240
STATUSES = ("pending", "in_progress", "completed")

_STATUS_ALIASES = {
    "pending": "pending",
    "todo": "pending",
    "waiting": "pending",
    "ожидает": "pending",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "active": "in_progress",
    "doing": "in_progress",
    "в работе": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "готово": "completed",
    "выполнено": "completed",
}


@dataclass
class TodoItem:
    """Один пункт плана."""

    title: str
    status: str = "pending"


#: текущий план доступен инструменту todo_write в потоке агента
current_todo: ContextVar["TodoList | None"] = ContextVar(
    "nemus_current_todo", default=None
)


class TodoList:
    """Потокобезопасный список пунктов с человекочитаемым выводом."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._items: list[TodoItem] = []
        self._lock = threading.RLock()
        self._on_change = on_change

    @property
    def items(self) -> list[TodoItem]:
        with self._lock:
            return [TodoItem(item.title, item.status) for item in self._items]

    def replace(self, raw: Any) -> list[TodoItem]:
        """Заменяет план целиком; понимает список строк и список объектов."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raise ValueError("План должен быть списком пунктов.") from None
        if isinstance(raw, dict):
            raw = raw.get("items")
        if not isinstance(raw, list):
            raise ValueError("Нужен список пунктов плана.")
        items: list[TodoItem] = []
        in_progress = False
        for entry in raw:
            if isinstance(entry, str):
                title, status = entry, "pending"
            elif isinstance(entry, dict):
                title = str(entry.get("title") or entry.get("text") or "").strip()
                status = str(entry.get("status") or "pending").strip().lower()
            else:
                continue
            title = title.strip()
            if not title:
                continue
            status = _STATUS_ALIASES.get(status, "pending")
            if status == "in_progress":
                # одновременно в работе только один пункт — иначе прогресс
                # в /todo выглядит противоречиво
                if in_progress:
                    status = "pending"
                in_progress = True
            items.append(TodoItem(title[:MAX_TITLE], status))
            if len(items) >= MAX_ITEMS:
                break
        if not items:
            raise ValueError("В плане нет ни одного пункта.")
        with self._lock:
            self._items = items
        self._changed()
        return self.items

    def load(self, raw: str | None) -> None:
        """Загружает сохранённый JSON; битые данные не ломают чат."""
        if not raw:
            with self._lock:
                self._items = []
            return
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            data = []
        items: list[TodoItem] = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title") or "").strip()
                status = str(entry.get("status") or "pending").strip().lower()
                if title:
                    items.append(
                        TodoItem(title[:MAX_TITLE], _STATUS_ALIASES.get(status, "pending"))
                    )
        with self._lock:
            self._items = items[:MAX_ITEMS]

    def dump(self) -> str:
        with self._lock:
            return json.dumps(
                [asdict(item) for item in self._items], ensure_ascii=False
            )

    def clear(self) -> None:
        with self._lock:
            self._items = []
        self._changed()

    def render(self) -> str:
        """Текст для /todo и результата инструмента."""
        items = self.items
        if not items:
            return (
                "📋 Плана пока нет.\n\n"
                "Для средней или длинной задачи я составлю его сама и буду "
                "обновлять по ходу работы."
            )
        done = sum(1 for item in items if item.status == "completed")
        total = len(items)
        if done == total:
            header = f"✅ План выполнен: {done}/{total}"
        else:
            header = f"📋 План: {done}/{total} готово"
        icons = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
        lines = [header, ""]
        for index, item in enumerate(items, 1):
            mark = icons.get(item.status, "⬜")
            lines.append(f"{mark} {index}. {item.title}")
        return "\n".join(lines)

    def _changed(self) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            # сохранение плана не должно ломать выполнение задачи
            pass


__all__ = [
    "MAX_ITEMS",
    "STATUSES",
    "TodoItem",
    "TodoList",
    "current_todo",
]
