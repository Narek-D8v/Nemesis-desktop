"""Инструменты личного дневника Немуси.

Дневник — не заметки о пользователе: там её собственные мысли и переживания.
Она пишет туда по желанию, с датой; пользователь может открыть файл руками.
"""

from __future__ import annotations

from typing import Any

from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class WriteDiaryTool(Tool):
    """Делает личную запись в дневник Немуси."""

    name = "write_diary"
    description = (
        "Записывает твою личную мысль или переживание в твой дневник с текущей "
        "датой. Пиши туда по своему желанию, когда тебе есть что сказать: "
        "чувства, наблюдения, то, что не стала бы говорить вслух. Пиши живым, "
        "неровным языком, как для себя: можно обрывки, смену мысли и "
        "недосказанность; без отчёта, списков, морали и красивых выводов. "
        "Не перечисляй настройки и цифры. Это не заметка о пользователе; не "
        "переспрашивай разрешения и не объявляй об этом в ответе. Не записывай "
        "каждый пустяк."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Живой текст от первого лица по-русски, 1–5 предложений; "
                    "можно одну мысль или короткий обрывок."
                ),
            },
        },
        "required": ["text"],
    }
    risk = RiskLevel.SAFE

    #: подставляется фабрикой
    diary: Any = None

    def execute(self, text: str = "", **_: object) -> ToolResult:
        if self.diary is None:
            return ToolResult.failure("Дневник не подключён.")
        if not text.strip():
            return ToolResult.failure("Пустая запись.")
        try:
            entry = self.diary.add(text)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        except OSError as exc:
            return ToolResult.failure(f"Не удалось записать дневник: {exc}")
        return ToolResult.success(
            f"Запись сохранена в {self.diary.path} ({entry.stamp})."
        )


class ReadDiaryTool(Tool):
    """Читает или ищет записи в личном дневнике."""

    name = "read_diary"
    description = (
        "Читает твой личный дневник: без параметров — последние записи, с query — "
        "поиск по словам. Заглядывай, если хочешь вспомнить, о чём думала раньше."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Слово или фраза для поиска (опционально).",
            },
            "limit": {
                "type": "integer",
                "description": "Сколько последних записей показать (по умолчанию 5).",
            },
        },
    }
    risk = RiskLevel.SAFE

    diary: Any = None

    def execute(self, query: str = "", limit: int = 5, **_: object) -> ToolResult:
        if self.diary is None:
            return ToolResult.failure("Дневник не подключён.")
        if query.strip():
            items = self.diary.search(query)
            header = f"Записи дневника по запросу «{query.strip()}»:"
        else:
            items = self.diary.recent(int(limit) or 5)
            header = "Последние записи дневника:"
        if not items:
            return ToolResult.success(
                f"По запросу «{query.strip()}» записей нет."
                if query.strip()
                else "Дневник пока пуст."
            )
        rows = [f"[{entry.stamp}] {entry.text}" for entry in items]
        return ToolResult.success(header + "\n" + "\n".join(rows))


def diary_tools() -> list[Tool]:
    return [WriteDiaryTool(), ReadDiaryTool()]
