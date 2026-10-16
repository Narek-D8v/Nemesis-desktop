"""Базовый класс инструмента и модель результата."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    """Уровень риска инструмента (влияет на подтверждение)."""

    SAFE = "safe"
    MEDIUM = "medium"
    DANGEROUS = "dangerous"


@dataclass
class ToolResult:
    """Результат выполнения инструмента, возвращаемый модели."""

    ok: bool
    content: str
    data: Any = None
    #: вложения-изображения (data URL) — агент добавит их в контекст модели
    images: list[str] = field(default_factory=list)

    @classmethod
    def success(
        cls, content: str, data: Any = None, images: list[str] | None = None
    ) -> "ToolResult":
        return cls(True, content, data, images or [])

    @classmethod
    def failure(
        cls, content: str, data: Any = None, images: list[str] | None = None
    ) -> "ToolResult":
        return cls(False, content, data, images or [])

    def to_model(self) -> str:
        prefix = "" if self.ok else "ОШИБКА: "
        return prefix + self.content


class Tool(abc.ABC):
    """Абстрактный инструмент, доступный агенту."""

    #: машинное имя (латиницей, snake_case)
    name: str = ""
    #: описание для модели — что делает и когда использовать
    description: str = ""
    #: JSON-схема параметров (в стиле JSON Schema)
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    #: уровень риска
    risk: RiskLevel = RiskLevel.SAFE

    def schema(self) -> dict[str, Any]:
        """Схема инструмента в формате OpenAI function-calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abc.abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Выполняет инструмент. Реализуется наследниками."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tool {self.name} risk={self.risk.value}>"
