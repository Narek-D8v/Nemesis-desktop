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


class FakeFastLLM:
    """Лёгкая модель быстрого пути: всегда возвращает заданный текст."""

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, messages, **overrides):
        return LLMResponse(content=self.text, model="fake-fast")


class TruncatedFastLLM:
    """Лёгкая модель, упёршаяся в потолок токенов и оборвавшая фразу."""

    def complete(self, messages, **overrides):
        return LLMResponse(
            content="Я могу искать информацию, помогать с",
            finish_reason="length",
            model="fake-fast",
        )


class PromiseLLM:
    """Модель, которая всё время обещает работу и не вызывает инструменты."""

    def stream(self, messages, tools=None, **overrides):
        text = "Секунду, сейчас запустим!"
        yield StreamEvent(kind="content", text=text)
        yield StreamEvent(kind="final", response=LLMResponse(content=text))


def _new_agent(llm) -> Agent:
    cfg = get_config()
    registry = ToolRegistry()
    registry.register_many(basic_tools())
    return Agent(
        config=cfg,
        llm=llm,
        registry=registry,
        permissions=PermissionManager(cfg),
    )


def _drain(gen):
    """Прокручивает генератор и возвращает (события, return-значение)."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def test_fast_reply_rejects_promise() -> None:
    """Быстрый путь откатывается, если лёгкая модель обещает/спрашивает."""
    agent = _new_agent(FakeFastLLM("Секунду, сейчас запустим!"))
    events, handled = _drain(agent._run_fast_reply())
    assert handled is False and events == [], (events, handled)
    assert not any(m.role == "assistant" for m in agent.history), (
        "обещание не должно попадать в историю и в чат"
    )

    agent = _new_agent(
        FakeFastLLM(
            "Ой, для этого мне понадобится запустить скрипт на твоём "
            "компьютере. Делаем?"
        )
    )
    events, handled = _drain(agent._run_fast_reply())
    assert handled is False and events == [], (events, handled)

    agent = _new_agent(FakeFastLLM("У меня нет доступа к твоему компьютеру."))
    events, handled = _drain(agent._run_fast_reply())
    assert handled is False and events == [], (events, handled)
    assert not any(m.role == "assistant" for m in agent.history), (
        "выдуманный отказ не должен попадать в историю и в чат"
    )

    agent = _new_agent(FakeFastLLM("Как здорово! Рада за тебя."))
    events, handled = _drain(agent._run_fast_reply())
    assert handled is True, "светская реплика должна закрываться быстро"
    assert len(events) == 1 and events[0].kind == "final", events
    assert events[0].text == "Как здорово! Рада за тебя."
    print("ok  быстрый ответ: обещание и отказ откатываются в основной цикл")


def test_fast_reply_rejects_truncated() -> None:
    """Оборванный по max_tokens ответ не уходит пользователю как финал."""
    agent = _new_agent(TruncatedFastLLM())
    events, handled = _drain(agent._run_fast_reply())
    assert handled is False and events == [], (events, handled)
    assert not any(m.role == "assistant" for m in agent.history), (
        "обрезанный ответ не должен попадать в историю и в чат"
    )
    print("ok  обрезанный быстрый ответ откатывается в основной цикл")


def test_promise_nudge_we_forms() -> None:
    """Обещание в мы-форме («сейчас запустим») не закрывает задачу."""
    agent = _new_agent(PromiseLLM())
    errors = []
    for event in agent.run("Напиши функцию сортировки"):
        if event.kind == "error":
            errors.append(event.error)
    assert errors, "nudge обязан остановить работу с понятной ошибкой"
    assert "обещанием" in errors[0], errors
    print("ok  обещание вместо работы: nudge ловит «сейчас запустим»")


def test_gui_suffix_about_voice() -> None:
    """GUI-агент знает: ответы озвучиваются, скрипты для звука не нужны."""
    from nemus.core.factory import GUI_AGENT_SUFFIX

    assert "озвучивается" in GUI_AGENT_SUFFIX, GUI_AGENT_SUFFIX
    assert "скрипт" in GUI_AGENT_SUFFIX.lower(), GUI_AGENT_SUFFIX
    print("ok  GUI-суффикс: авто-озвучка ответов, без скриптов для звука")


def _scenario_tool_cycle() -> None:
    agent = _new_agent(FakeLLM())

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


def main() -> int:
    _scenario_tool_cycle()
    failures = 0
    for test in (
        test_fast_reply_rejects_promise,
        test_fast_reply_rejects_truncated,
        test_promise_nudge_we_forms,
        test_gui_suffix_about_voice,
    ):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"\nПровалено тестов: {failures}")
        return 1
    print("\nAGENT SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
