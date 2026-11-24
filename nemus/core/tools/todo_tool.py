"""Инструмент плана задачи: агент сам пишет и обновляет todo-лист."""

from __future__ import annotations

from typing import Any

from nemus.core.todo import current_todo
from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class TodoWriteTool(Tool):
    """Сохраняет полный план текущей задачи."""

    name = "todo_write"
    description = (
        "Составляет или обновляет план текущей задачи, который пользователь "
        "видит командой /todo. Используй для средних, сложных и длинных задач "
        "(три и больше шагов, несколько файлов, установка, отладка, долгая "
        "работа). Вызови ОДИН раз в начале с полным списком шагов, затем "
        "повторно после каждого заметного этапа — передавай ВЕСЬ список "
        "заново, а не только изменения. В работе одновременно держи ровно "
        "один пункт in_progress; выполненный помечай completed. Для простых "
        "задач (время, погода, короткий вопрос, одно действие) план не нужен."
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Полный план: от 1 до 30 пунктов.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Короткий шаг по-русски, без воды.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["title", "status"],
                },
            },
        },
        "required": ["items"],
    }
    risk = RiskLevel.SAFE

    def execute(self, items: Any = None, **_: object) -> ToolResult:
        todo = current_todo.get()
        if todo is None:
            return ToolResult.failure("План сейчас недоступен.")
        try:
            todo.replace(items)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success("План обновлён.\n" + todo.render())


def todo_tools() -> list[Tool]:
    return [TodoWriteTool()]
