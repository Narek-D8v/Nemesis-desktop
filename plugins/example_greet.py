"""Пример плагина для Немуси.

Любой ``*.py`` в этой папке автоматически загружается при старте.
Можно либо определить функцию ``register(registry)``, либо просто
объявить классы-наследники :class:`Tool` — они добавятся сами.
"""

from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class GreetTool(Tool):
    """Демонстрационный инструмент: приветствие по имени."""

    name = "greet"
    description = "Возвращает дружелюбное приветствие по имени (пример плагина)."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Имя для приветствия."}
        },
        "required": ["name"],
    }
    risk = RiskLevel.SAFE

    def execute(self, name: str = "", **_: object) -> ToolResult:
        return ToolResult.success(f"Привет, {name}! Рада помочь. 🙌")
