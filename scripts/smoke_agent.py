"""Smoke-тест агентного цикла с фиктивным LLM (без API-ключей).

Запуск:  venv\\Scripts\\python.exe scripts\\smoke_agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import Agent  # noqa: E402
from nemus.core.llm import StreamEvent  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, ToolCall  # noqa: E402
from nemus.core.tools.basic import basic_tools  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


class FakeLLM:
    """Имитация LLMClient: первый шаг — вызов инструмента, второй — финал."""

    def __init__(self) -> None:
        self.step = 0

    def stream(self, messages, tools=None, **overrides):
        self.step += 1
        if self.step == 1:
            yield StreamEvent(kind="content", text="Секунду, посчитаю…")
            call = ToolCall(id="c1", name="calculate", arguments={"expression": "7*6"})
            yield StreamEvent(
                kind="final",
                response=LLMResponse(content="Секунду, посчитаю…", tool_calls=[call]),
            )
        else:
            yield StreamEvent(kind="content", text="Ответ: 42.")
            yield StreamEvent(
                kind="final", response=LLMResponse(content="Ответ: 42.")
            )


def main() -> int:
    cfg = get_config()
    registry = ToolRegistry()
    registry.register_many(basic_tools())

    agent = Agent(
        config=cfg,
        llm=FakeLLM(),  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
    )

    states: list[str] = []
    for event in agent.run("Сколько будет 7 умножить на 6?"):
        states.append(event.kind)
        if event.kind == "assistant_delta":
            print(f"[дельта] {event.text}")
        elif event.kind == "tool_call":
            print(f"[вызов] {event.tool_name} {event.tool_args}")
        elif event.kind == "tool_result":
            print(f"[результат] {event.result.to_model() if event.result else ''}")
        elif event.kind == "final":
            print(f"[ИТОГ] {event.text}")
        elif event.kind == "error":
            print(f"[ОШИБКА] {event.error}")

    expected = [
        "assistant_delta",
        "assistant_text",
        "tool_call",
        "tool_result",
        "assistant_delta",
        "final",
    ]
    assert states == expected, f"Неожиданная последовательность: {states}"
    print("\nAGENT SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
