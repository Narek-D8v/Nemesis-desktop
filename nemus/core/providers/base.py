"""Общие модели данных LLM-слоя, не зависящие от провайдера."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

#: содержимое сообщения: обычный текст или список блоков (OpenAI multimodal)
Content = str | list[dict[str, Any]] | None


class ProviderError(RuntimeError):
    """Ошибка обращения к провайдеру LLM."""


@dataclass
class ToolCall:
    """Запрос модели на вызов инструмента."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_litellm(cls, raw: Any) -> "ToolCall":
        import json

        function = raw.function
        args = function.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        return cls(id=raw.id, name=function.name, arguments=args or {})


@dataclass
class Message:
    """Сообщение в формате, близком к OpenAI."""

    role: Role
    content: Content = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id,
                "content": self.content if isinstance(self.content, str) else "",
            }
        msg: dict[str, Any] = {
            "role": self.role,
            "content": self.content if self.content is not None else "",
        }
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": __import__("json").dumps(
                            tc.arguments, ensure_ascii=False
                        ),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg


@dataclass
class LLMResponse:
    """Ответ модели: текст и/или запросы инструментов."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)
