"""Тест экономии контекста: история, вывод инструментов, reasoning_effort.

Проверяет то, из-за чего «мгновенно кончались лимиты»:
- в модель уходила вся история и полные выводы инструментов;
- Gemini 3 по умолчанию думал на полную и жёг выходные токены.

Запуск:  venv\\Scripts\\python.exe scripts\\test_context_budget.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import Config  # noqa: E402
from nemus.core.agent import (  # noqa: E402
    Agent,
    is_promise_only,
    is_simple_request,
    quick_reply,
)
from nemus.core.llm import ChatTarget, LLMClient, StreamEvent  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, Message, ToolCall  # noqa: E402
from nemus.core.tools.base import Tool, ToolResult  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


def _agent() -> Agent:
    cfg = Config()
    return Agent(
        cfg,
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=None,
    )


def test_history_is_trimmed_but_pairs_stay() -> None:
    """История режется по бюджету, но tool-результат не остаётся без вызова."""
    agent = _agent()
    agent.max_history_chars = 900
    agent.history = [Message(role="system", content="с" * 200)]
    for index in range(12):
        call = ToolCall(id=f"c{index}", name="run_shell", arguments={"cmd": "x" * 20})
        agent.history.append(Message(role="user", content=f"задача {index} " + "у" * 120))
        agent.history.append(
            Message(role="assistant", content=None, tool_calls=[call])
        )
        agent.history.append(
            Message(role="tool", tool_call_id=f"c{index}", content="т" * 120)
        )
    trimmed = agent._history_for_model()

    assert trimmed[0].role == "system"
    assert len(trimmed) < len(agent.history), (len(trimmed), len(agent.history))
    assert "задача 11" in str(trimmed[-3].content), trimmed[-3].content
    announced = {
        call.id
        for message in trimmed
        if message.role == "assistant"
        for call in (message.tool_calls or [])
    }
    for message in trimmed:
        if message.role == "tool":
            assert message.tool_call_id in announced, (
                "tool-результат без вызова сломает запрос провайдеру"
            )
    # исходная история не портится: диалог и БД остаются целыми
    assert len(agent.history) == 1 + 12 * 3
    print("ok  история режется по бюджету, пары assistant/tool не рвутся")


def test_user_image_reaches_model() -> None:
    """Фото пользователя уходит модели как image_url, а не только путём."""

    class FakeVisionLLM:
        def __init__(self) -> None:
            self.seen: list[Message] = []

        def stream(self, messages, tools=None, **overrides):
            self.seen = messages
            yield StreamEvent(
                kind="final", response=LLMResponse(content="Вижу изображение.")
            )

    agent = _agent()
    fake = FakeVisionLLM()
    agent.llm = fake  # type: ignore[assignment]
    events = list(
        agent.run("Что на фото?", images=["data:image/png;base64,AAAA"])
    )
    assert events and events[-1].kind == "final", events
    blocks = [
        block
        for message in fake.seen
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert blocks, "изображение не попало в запрос к модели"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    print("ok  фото пользователя уходит в модель как vision-вложение")


def test_large_system_keeps_user_message() -> None:
    """Большой промпт со скиллами не должен вырезать саму задачу."""
    agent = _agent()
    agent.max_history_chars = 100
    agent.history = [
        Message(role="system", content="с" * 5000),
        Message(role="user", content="нарисуй кота"),
    ]
    trimmed = agent._history_for_model()
    assert trimmed[0].role == "system"
    assert any(
        message.role == "user" and "кота" in str(message.content)
        for message in trimmed
    ), [m.role for m in trimmed]
    print("ok  большой system-промпт не съедает сообщение пользователя")


def test_tool_output_clipped() -> None:
    """Огромный вывод инструмента обрезается, начало и конец сохраняются."""
    agent = _agent()
    agent.max_tool_output = 200
    text = "НАЧАЛО" + "x" * 5000 + "ОШИБКА В КОНЦЕ"
    clipped = agent._clip_tool_result(text)
    assert len(clipped) < 400, len(clipped)
    assert clipped.startswith("НАЧАЛО"), clipped[:40]
    assert clipped.endswith("ОШИБКА В КОНЦЕ"), clipped[-40:]
    assert "обрезан" in clipped, clipped
    print("ok  вывод инструмента обрезан, ошибка в конце сохранена")


def test_reasoning_effort_for_gemini3_only() -> None:
    """Gemini 3+ получает экономный reasoning_effort, старые модели — нет."""
    cfg = Config()
    cfg.llm.reasoning_effort = "low"
    client = LLMClient(cfg)
    gemini3 = client._build_kwargs(
        ChatTarget("gemini", "gemini/gemini-3.5-flash-lite"), None
    )
    assert gemini3.get("reasoning_effort") == "low", gemini3
    gemini2 = client._build_kwargs(
        ChatTarget("gemini", "gemini/gemini-2.5-flash"), None
    )
    assert "reasoning_effort" not in gemini2, gemini2
    openai = client._build_kwargs(ChatTarget("openai", "gpt-5-mini"), None)
    assert "reasoning_effort" not in openai, openai
    print("ok  reasoning_effort применяется только к Gemini 3+")


def test_run_token_ceiling_stops_task() -> None:
    """Задача останавливается, когда ушла за потолок токенов."""
    class _Noop(Tool):
        name = "noop"
        description = "ничего не делает"
        parameters = {"type": "object", "properties": {}}

        def execute(self, **kwargs: object) -> ToolResult:
            return ToolResult.success("ok")

    class _FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, history: object, tools: object = None):
            self.calls += 1
            yield StreamEvent(
                kind="final",
                response=LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id=f"c{self.calls}", name="noop", arguments={})
                    ],
                    usage={"total_tokens": 100},
                ),
            )

    cfg = Config()
    registry = ToolRegistry()
    registry.register(_Noop())
    fake = _FakeLLM()
    agent = Agent(
        cfg,
        llm=fake,  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
        memory=None,
    )
    agent.max_run_tokens = 50
    agent.max_steps = 5
    events = list(agent.run("работай"))
    assert fake.calls == 1, f"после потолка агент продолжил звать модель: {fake.calls}"
    assert any(
        event.kind == "error" and "токенов" in event.error for event in events
    ), [event.kind for event in events]
    print("ok  потолок токенов на задачу останавливает прогон")


def test_promise_is_not_final() -> None:
    """«Сейчас сделаю!» без инструментов — не финал: агент требует работы."""
    assert is_promise_only("Сейчас сделаю!")
    assert is_promise_only("Приступаю к задаче.")
    assert is_promise_only("Ок, сейчас выполню и покажу файл.")
    assert not is_promise_only("Готово: отчёт сохранён в report.md")
    assert not is_promise_only("Сейчас сделаю, готово: файл лежит рядом")
    assert not is_promise_only("Отчёт готов: 12 страниц, ссылка в файле.")

    class _FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, history: object, tools: object = None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    kind="final", response=LLMResponse(content="Сейчас сделаю!")
                )
            else:
                yield StreamEvent(
                    kind="final",
                    response=LLMResponse(
                        content="Готово: отчёт сохранён в data/workspace/report.md"
                    ),
                )

    cfg = Config()
    fake = _FakeLLM()
    agent = Agent(
        cfg,
        llm=fake,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=None,
    )
    events = list(agent.run("Сделай отчёт о работе за неделю"))
    assert fake.calls == 2, f"агент принял обещание за финал: вызовов {fake.calls}"
    assert events[-1].kind == "final", [e.kind for e in events]
    assert "Готово" in events[-1].text, events[-1].text
    assert any(
        message.role == "user" and "только обещанием" in str(message.content)
        for message in agent.history
    ), "агенту не отправлено требование выполнить задачу"
    print("ok  обещание «сейчас сделаю» не считается финальным ответом")


def test_fast_paths() -> None:
    """Прямое обращение — мгновенно; короткий вопрос — без 20k промпта."""
    assert quick_reply("Немуся?!") == "Да, я здесь. Чем помочь?"
    assert quick_reply("Немуся, привет!") == "Привет! Я здесь. Чем помочь?"
    assert quick_reply("Как дела?") == "Всё хорошо, спасибо! А у тебя?"
    assert is_simple_request("Что ты умеешь?")
    assert not is_simple_request("Который час?")
    assert not is_simple_request("Открой блокнот")
    assert not is_simple_request("Напиши функцию сортировки")
    # шутка должна идти полным агентом: только там ИИ может выбрать реакцию
    assert not is_simple_request("Слушай анекдот: Oct 31 == Dec 25")

    class _FakeFastLLM:
        def __init__(self) -> None:
            self.calls: list[tuple[list[Message], object, dict]] = []

        def complete(self, messages: list[Message], tools: object = None, **kwargs):
            self.calls.append((messages, tools, kwargs))
            return LLMResponse(content="Я умею отвечать и выполнять задачи.")

        def stream(self, *args: object, **kwargs: object):
            raise AssertionError("быстрый путь не должен запускать агентский стрим")

    cfg = Config()
    cfg.llm.fast_model = "gemini/gemini-3.5-flash-lite"
    fake = _FakeFastLLM()
    agent = Agent(
        cfg,
        llm=fake,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=None,
    )
    events = list(agent.run("Что ты умеешь?"))
    assert [e.kind for e in events] == ["final"], [e.kind for e in events]
    assert events[0].text.startswith("Я умею"), events[0].text
    assert len(fake.calls) == 1, fake.calls
    messages, tools, kwargs = fake.calls[0]
    assert tools is None, "в быстром пути не должно быть схем инструментов"
    assert kwargs.get("attempts") == 1, kwargs
    assert kwargs.get("model") == "gemini/gemini-3.5-flash-lite", kwargs
    assert sum(len(str(m.content or "")) for m in messages) < 2500, [
        len(str(m.content or "")) for m in messages
    ]

    class _NoLLM:
        def complete(self, *args: object, **kwargs: object):
            raise AssertionError("прямое обращение не должно звать модель")

    instant = Agent(
        cfg,
        llm=_NoLLM(),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=None,
    )
    events = list(instant.run("Немуся?"))
    assert [e.kind for e in events] == ["final"], [e.kind for e in events]
    print("ok  быстрый путь: обращение мгновенно, короткий вопрос без инструментов")


def test_defaults_are_frugal() -> None:
    """Дефолты не должны провоцировать расход лимитов."""
    cfg = Config()
    assert cfg.llm.max_tokens <= 4096, cfg.llm.max_tokens
    assert cfg.llm.reasoning_effort in ("none", "minimal", "low"), cfg.llm.reasoning_effort
    assert cfg.tools.max_history_chars <= 60000, cfg.tools.max_history_chars
    assert cfg.tools.max_tool_output <= 12000, cfg.tools.max_tool_output
    print("ok  дефолты: max_tokens, reasoning_effort и лимиты контекста")


def main() -> int:
    test_history_is_trimmed_but_pairs_stay()
    test_user_image_reaches_model()
    test_large_system_keeps_user_message()
    test_tool_output_clipped()
    test_reasoning_effort_for_gemini3_only()
    test_run_token_ceiling_stops_task()
    test_promise_is_not_final()
    test_fast_paths()
    test_defaults_are_frugal()
    print("\nCONTEXT BUDGET TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
