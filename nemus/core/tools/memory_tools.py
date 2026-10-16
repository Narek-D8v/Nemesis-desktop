"""Инструменты долговременной памяти: запоминать и вспоминать факты."""

from __future__ import annotations

from typing import Any

from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class RememberFactTool(Tool):
    """Сохраняет важный факт о пользователе/мире в долговременную память."""

    name = "remember_fact"
    description = (
        "Запоминает важный факт на долгое время (имя, предпочтения, важную информацию). "
        "Используй, когда пользователь что-то важное о себе сообщает."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Краткий ключ, например 'имя_пользователя'.",
            },
            "value": {"type": "string", "description": "Что запомнить."},
        },
        "required": ["key", "value"],
    }
    risk = RiskLevel.SAFE

    #: подставляется фабрикой
    memory: Any = None

    def execute(self, key: str = "", value: str = "", **_: object) -> ToolResult:
        if self.memory is None:
            return ToolResult.failure("Память не подключена.")
        if not key.strip():
            return ToolResult.failure("Пустой ключ.")
        self.memory.set_fact(key.strip().lower(), value)
        return ToolResult.success(f"Запомнила: {key} = {value}")


class RecallFactTool(Tool):
    """Вспоминает сохранённые факты."""

    name = "recall_fact"
    description = (
        "Вспоминает сохранённый факт по ключу. Если ключ не указан — "
        "возвращает список всех сохранённых фактов."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Ключ факта (опционально).",
            }
        },
    }
    risk = RiskLevel.SAFE

    memory: Any = None

    def execute(self, key: str = "", **_: object) -> ToolResult:
        if self.memory is None:
            return ToolResult.failure("Память не подключена.")
        if key.strip():
            value = self.memory.get_fact(key.strip().lower())
            if value is None:
                return ToolResult.success(f"Нет факта по ключу '{key}'.")
            return ToolResult.success(f"{key} = {value}")
        facts = self.memory.all_facts()
        if not facts:
            return ToolResult.success("Память пуста.")
        body = "\n".join(f"- {k}: {v}" for k, v in facts.items())
        return ToolResult.success("Сохранённые факты:\n" + body)


def memory_tools() -> list[Tool]:
    return [RememberFactTool(), RecallFactTool()]
