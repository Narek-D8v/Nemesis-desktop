"""Заметки о пользователе: markdown-файл, который ведёт сама Немуся.

Отличие от фактов (``remember_fact`` в SQLite): заметки — это человеческий
журнал в открытом файле ``data/notes.md``. Пользователь может прочитать и
отредактировать его руками; Немуся дописывает заметки по своему усмотрению
и подсовывает свежие в системный промпт, чтобы помнить между сессиями.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

_HEADER = (
    "# Заметки о пользователе\n\n"
    "# Формат: - ГГГГ-ММ-ДД ЧЧ:ММ — заметка\n"
    "# Файл ведёт Немуся; правки руками приветствуются.\n\n"
)


class NoteBook:
    """Потокобезопасное хранение заметок о пользователе в markdown-файле."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, text: str) -> str:
        """Добавляет заметку с текущим временем. Возвращает записанную строку."""
        text = " ".join(str(text).split())
        if not text:
            raise ValueError("Пустая заметка.")
        line = f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} — {text}"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(_HEADER + line + "\n", encoding="utf-8")
            else:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        return line

    def notes(self) -> list[str]:
        """Все заметки (строки-записи) в порядке добавления."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [ln.strip() for ln in lines if ln.strip().startswith("- ")]

    def recent(self, limit: int = 20) -> list[str]:
        """Последние ``limit`` заметок (0 и меньше — все)."""
        items = self.notes()
        return items if limit <= 0 else items[-limit:]

    def search(self, query: str) -> list[str]:
        """Заметки, содержащие подстроку (без учёта регистра)."""
        q = str(query).strip().lower()
        if not q:
            return self.notes()
        return [n for n in self.notes() if q in n.lower()]

    def format_for_prompt(self, limit: int = 20) -> str:
        """Последние заметки текстом для системного промпта (или пусто)."""
        return "\n".join(self.recent(limit))


__all__ = ["NoteBook"]
