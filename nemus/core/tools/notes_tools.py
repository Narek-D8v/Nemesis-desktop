"""Инструменты заметок о пользователе (markdown-файл, ведёт сама Немуся).

Отличие от ``remember_fact``: факты — ключ/значение в SQLite, заметки —
человекочитаемый журнал в файле, который можно открыть и поправить руками.
"""

from __future__ import annotations

from typing import Any

from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class WriteNoteTool(Tool):
    """Записывает заметку о пользователе в её личный файл заметок."""

    name = "write_note"
    description = (
        "Делает заметку о пользователе в твоём личном файле заметок. "
        "Используй по своему усмотрению, когда узнала что-то важное или полезное "
        "для будущих бесед: имя, предпочтения, привычки, планы, события жизни, "
        "договорённости, важные детали проектов. Не переспрашивай разрешения и не "
        "докладывай об этом в ответе — просто сохрани, когда посчитаешь нужным. "
        "Не записывай очевидные и одноразовые мелочи."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Текст заметки: одно-два предложения по-русски.",
            },
        },
        "required": ["text"],
    }
    risk = RiskLevel.SAFE

    #: подставляется фабрикой
    notebook: Any = None

    def execute(self, text: str = "", **_: object) -> ToolResult:
        if self.notebook is None:
            return ToolResult.failure("Заметки не подключены.")
        if not text.strip():
            return ToolResult.failure("Пустая заметка.")
        try:
            line = self.notebook.add(text)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        except OSError as exc:
            return ToolResult.failure(f"Не удалось записать заметку: {exc}")
        return ToolResult.success(f"Заметка сохранена в {self.notebook.path}: {line}")


class ReadNotesTool(Tool):
    """Читает или ищет заметки о пользователе в файле заметок."""

    name = "read_notes"
    description = (
        "Читает твои заметки о пользователе из файла: без параметров — последние "
        "заметки, с query — поиск по слову. Заглядывай сюда, если нужен контекст "
        "о пользователе из прошлых бесед."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Слово для поиска (опционально).",
            },
            "limit": {
                "type": "integer",
                "description": "Сколько последних заметок показать (по умолчанию 20, 0 — все).",
            },
        },
    }
    risk = RiskLevel.SAFE

    notebook: Any = None

    def execute(self, query: str = "", limit: int = 0, **_: object) -> ToolResult:
        if self.notebook is None:
            return ToolResult.failure("Заметки не подключены.")
        if query.strip():
            items = self.notebook.search(query)
            if not items:
                return ToolResult.success(
                    f"По запросу «{query.strip()}» заметок нет."
                )
            header = f"Заметки по запросу «{query.strip()}»:"
        else:
            items = self.notebook.recent(int(limit) or 20)
            if not items:
                return ToolResult.success("Заметок пока нет.")
            header = "Последние заметки:"
        return ToolResult.success(header + "\n" + "\n".join(items))


def notes_tools() -> list[Tool]:
    return [WriteNoteTool(), ReadNotesTool()]
