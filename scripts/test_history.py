"""Тест истории диалога: tool-calls переживают перезапуск, «сироты» вычищаются.

Регресс к багу: результаты инструментов сохранялись в SQLite, а парные им
assistant.tool_calls — нет. После рестарта провайдеры отвергали запрос целиком
(«Missing corresponding tool call for tool response message») и агент падал
на первом же шаге.

Запуск:  venv\\Scripts\\python.exe scripts\\test_history.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import Agent, pair_tool_messages  # noqa: E402
from nemus.core.llm import LLMResponse  # noqa: E402
from nemus.core.memory import Memory, load_messages_into  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import Message, ToolCall  # noqa: E402
from nemus.core.tools.base import RiskLevel, Tool, ToolResult  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


def _tmp_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="nemus_hist_")) / "mem.db"


def test_round_trip() -> None:
    """assistant с tool-calls и парный ему результат переживают перезапуск."""
    db = _tmp_db()
    mem = Memory(db)
    mem.add_message("user", "посчитай 2+2")
    mem.add_message(
        "assistant",
        tool_calls=[ToolCall(id="c1", name="calculate", arguments={"expression": "2+2"})],
    )
    mem.add_message("tool", "4", tool_call_id="c1")
    mem.add_message("assistant", "Ответ: 4.")

    history: list[Message] = [Message(role="system", content="s")]
    load_messages_into(history, mem.recent_messages(limit=20))
    mem.close()

    assert [m.role for m in history] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ], [m.role for m in history]
    calls = history[2].tool_calls
    assert calls and calls[0].name == "calculate" and calls[0].arguments == {
        "expression": "2+2"
    }, calls
    assert history[3].tool_call_id == "c1" and history[3].content == "4"

    # payload, который уйдёт провайдеру, содержит и вызов, и результат
    payload = [m.to_dict() for m in history[1:]]
    assert payload[1]["tool_calls"][0]["id"] == "c1", payload[1]
    assert payload[2] == {"role": "tool", "tool_call_id": "c1", "content": "4"}
    print("ok  пары assistant.tool_calls ↔ tool пережили перезапуск")


def test_orphan_rows_dropped() -> None:
    """Строки role=tool из старой БД (без tool_calls у ассистента) вычищаются."""
    db = _tmp_db()
    mem = Memory(db)
    # как писала старая версия: результат есть, объявления вызова нет
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages (role, content, tool_call_id, ts) "
            "VALUES ('tool', 'старый результат', 'ghost', '2026-01-01')",
        )
        conn.execute(
            "INSERT INTO messages (role, content, ts) VALUES ('user', 'привет', '2026-01-01')",
        )

    history: list[Message] = [Message(role="system", content="s")]
    load_messages_into(history, mem.recent_messages(limit=20))
    mem.close()

    cleaned = pair_tool_messages(history[1:])
    roles = [m.role for m in cleaned]
    assert roles == ["user"], roles
    print("ok  осиротевшие tool-сообщения вычищены:", roles)


def test_tool_calls_without_result_stripped() -> None:
    """Вызов без записанного результата теряет tool_calls, но сохраняет текст."""
    messages = [
        Message(role="user", content="посчитай"),
        Message(
            role="assistant",
            content="считаю",
            tool_calls=[ToolCall(id="c9", name="calculate", arguments={})],
        ),
    ]
    cleaned = pair_tool_messages(messages)
    assert len(cleaned) == 2, [m.role for m in cleaned]
    assert cleaned[1].role == "assistant" and cleaned[1].content == "считаю"
    assert cleaned[1].tool_calls is None
    assert "tool_calls" not in cleaned[1].to_dict()
    print("ok  tool-calls без результата отброшены, текст ответа сохранён")


def test_partial_results_kept() -> None:
    """Из нескольких вызовов остаются только те, чей результат реально есть."""
    messages = [
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id="ok", name="get_time", arguments={}),
                ToolCall(id="lost", name="calculate", arguments={}),
            ],
        ),
        Message(role="tool", tool_call_id="ok", content="12:00"),
    ]
    cleaned = pair_tool_messages(messages)
    assert cleaned[0].tool_calls and [c.id for c in cleaned[0].tool_calls] == ["ok"]
    assert len(cleaned) == 2
    print("ok  незавершённые вызовы отброшены, завершённые оставлены")


def test_agent_boot_with_legacy_db() -> None:
    """Агент со старой БД (сироты) стартует и не тащит их в историю."""
    db = _tmp_db()
    Memory(db).close()
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            INSERT INTO messages (role, content, tool_call_id, ts)
                VALUES ('tool', 'осиротевший результат', 'ghost', '2026-01-01');
            INSERT INTO messages (role, content, ts)
                VALUES ('assistant', NULL, '2026-01-01');
            INSERT INTO messages (role, content, ts)
                VALUES ('user', 'привет', '2026-01-01');
            """,
        )

    cfg = get_config()
    agent = Agent(
        config=cfg,
        llm=object(),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=Memory(db),
    )
    roles = [m.role for m in agent.history if m.role != "system"]
    assert roles == ["assistant", "user"], roles
    print("ok  агент стартует со старой БД, история согласована:", roles)


class _FakeLLM:
    """Отдаёт заранее заданные ответы по одному на каждый вызов stream."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def stream(self, messages, tools=None, **_: Any):  # noqa: ANN001
        from nemus.core.llm import StreamEvent

        self.calls += 1
        yield StreamEvent(kind="final", response=self.responses.pop(0))


def test_empty_answer_retried() -> None:
    """Пустые ответы не завершают диалог и не съедают шаги агента."""
    from nemus.core.agent import EMPTY_ANSWER_RETRIES

    cfg = get_config()
    # пустых ответов больше, чем старый лимит повторов (2) и старый max_steps:
    # модель «долбится», пока не даст содержательный ответ
    empties = min(14, EMPTY_ANSWER_RETRIES - 1)
    llm = _FakeLLM([LLMResponse(content="") for _ in range(empties)] + [
        LLMResponse(content="Готово: презентация готова")
    ])
    agent = Agent(
        config=cfg,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )
    events = list(agent.run("сделай презентацию"))

    assert llm.calls == empties + 1, (
        f"ожидался повтор после {empties} пустых ответов, вызовов: {llm.calls}"
    )
    finals = [e for e in events if e.kind == "final"]
    assert len(finals) == 1 and finals[0].text == "Готово: презентация готова", [
        (e.kind, e.text) for e in events
    ]
    # пустые ответы не остались в истории, а подсказки — есть
    roles = [m.role for m in agent.history]
    assert roles.count("assistant") == 1, roles
    nudges = [m for m in agent.history if m.role == "user" and "пустым" in str(m.content)]
    assert len(nudges) == empties, (len(nudges), roles)
    print(f"ok  {empties} пустых ответов подряд: повторы без съедания шагов и рабочий финал")


def test_empty_answer_gives_error() -> None:
    """Если модель упорно пустая — пользователь видит ошибку, а не «Готова»."""
    from nemus.core.agent import EMPTY_ANSWER_RETRIES

    cfg = get_config()
    llm = _FakeLLM([LLMResponse(content="") for _ in range(EMPTY_ANSWER_RETRIES + 2)])
    agent = Agent(
        config=cfg,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )
    events = list(agent.run("сделай презентацию"))

    assert not [e for e in events if e.kind == "final"], [
        (e.kind, e.text) for e in events
    ]
    errors = [e for e in events if e.kind == "error"]
    assert len(errors) == 1 and "пустой" in errors[0].error, [
        (e.kind, e.error) for e in events
    ]
    assert llm.calls == EMPTY_ANSWER_RETRIES + 1, llm.calls
    print("ok  пустые ответы подряд: явная ошибка вместо молчаливого финала")


class _BoomTool(Tool):
    """Инструмент, который всегда падает (для проверки цикла доработки)."""

    name = "boom"
    description = "всегда падает"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE
    #: сколько раз его реально вызвали (для ассертов цикла)
    executions = 0

    def execute(self, **_: Any) -> ToolResult:
        type(self).executions += 1
        return ToolResult.failure("ZeroDivisionError: деление на ноль")


def test_error_sent_back_for_rework() -> None:
    """Ошибка инструмента возвращается модели с инструкцией «переделать»."""
    cfg = get_config()
    registry = ToolRegistry()
    registry.register(_BoomTool())
    llm = _FakeLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="b1", name="boom", arguments={})]
            ),
            LLMResponse(content="Исправил: задача готова"),
        ]
    )
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
    )
    events = list(agent.run("запусти скрипт"))

    tool_msgs = [m for m in agent.history if m.role == "tool"]
    assert len(tool_msgs) == 1, [m.role for m in agent.history]
    content = str(tool_msgs[0].content)
    assert "ЗАДАНИЕ НЕ ВЫПОЛНЕНО" in content, content
    assert "попытка 1 из" in content, content
    assert "НЕ отвечай пользователю" in content, content
    finals = [e.text for e in events if e.kind == "final"]
    assert finals == ["Исправил: задача готова"], finals
    # счётчик ведёт неудачи и сбрасывается только при успешном вызове
    assert agent._tool_failures == {"boom": 1}, agent._tool_failures
    agent._model_tool_content("boom", ToolResult.success("всё работает"))
    assert agent._tool_failures == {}, agent._tool_failures
    print("ok  ошибка инструмента вернулась модели с инструкцией переделать")


def test_error_directives_sequence() -> None:
    """После 3 неудач подряд — предложение сменить способ; отказы не долбим."""
    from nemus.core.tools.base import ToolResult

    cfg = get_config()
    agent = Agent(
        config=cfg,
        llm=_FakeLLM([LLMResponse(content="x")]),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )

    fail = ToolResult.failure("SyntaxError: invalid syntax")
    first = agent._model_tool_content("run_python", fail)
    second = agent._model_tool_content("run_python", fail)
    third = agent._model_tool_content("run_python", fail)
    assert "попытка 1 из 3" in first and "попытка 2 из 3" in second, (first, second)
    assert "Смени подход" in third, third
    assert "попытка 3" not in third, third

    # отказ политики/пользователя — повтор не предлагаем
    denied = ToolResult.failure("Инструмент 'run_shell' запрещён политикой.")
    out = agent._model_tool_content("run_shell", denied)
    assert "ЗАДАНИЕ НЕ ВЫПОЛНЕНО" not in out, out
    assert agent._tool_failures.get("run_shell") is None, agent._tool_failures

    # отказ на английском — тоже не дёргаем повторами
    denied_en = ToolResult.failure("Tool 'run_shell' was denied by policy.")
    out_en = agent._model_tool_content("run_shell", denied_en)
    assert "ЗАДАНИЕ НЕ ВЫПОЛНЕНО" not in out_en, out_en
    assert agent._tool_failures.get("run_shell") is None, agent._tool_failures
    print("ok  последовательность директив: 1-2 повтор, 3-я смена способа, отказы (ru/en) молча")


def test_loop_and_budget_stops() -> None:
    """Лимит шагов заменён: цикл одинаковых вызовов и бюджет времени."""
    from nemus.core.agent import LOOP_REPEAT_LIMIT, MAX_STEPS_HARD

    cfg = get_config()

    # 1) один и тот же падающий вызов подряд → тупик, а не 100 шагов
    registry = ToolRegistry()
    registry.register(_BoomTool())
    _BoomTool.executions = 0
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="b1", name="boom", arguments={})])
            for _ in range(MAX_STEPS_HARD)
        ]
    )
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
    )
    events = list(agent.run("запусти скрипт"))
    errors = [e.error for e in events if e.kind == "error"]
    assert len(errors) == 1 and "Тупик" in errors[0], errors
    assert str(LOOP_REPEAT_LIMIT) in errors[0], errors
    assert llm.calls == LOOP_REPEAT_LIMIT, llm.calls  # стоп на 3-м вызове, не на 100-м
    assert _BoomTool.executions == LOOP_REPEAT_LIMIT - 1, _BoomTool.executions
    assert not [e for e in events if e.kind == "final"], events

    # 2) бюджет времени: запрос останавливается по часам, а не по шагам
    agent2 = Agent(
        config=cfg,
        llm=_FakeLLM([LLMResponse(content="не должен дойти")]),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )
    agent2.run_budget = -1  # время уже вышло
    events2 = list(agent2.run("длинная задача"))
    errors2 = [e.error for e in events2 if e.kind == "error"]
    assert len(errors2) == 1 and "по времени" in errors2[0], errors2
    print("ok  вместо лимита шагов: стоп по циклу одинаковых вызовов и по времени")


class _TickTool(Tool):
    """Успешный инструмент с задержкой — имитирует долгий шаг (опрос, установка)."""

    name = "tick"
    description = "шаг с задержкой"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE
    #: задержка одного шага, сек
    delay = 0.15
    executions = 0

    def execute(self, **_: Any) -> ToolResult:
        type(self).executions += 1
        time.sleep(type(self).delay)
        return ToolResult.success("готово")


class _AlwaysCallLLM:
    """Каждый раз просит новую работу — ход не завершается сам."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, messages: Any, tools: Any = None, **_: Any):  # noqa: ANN001
        from nemus.core.llm import StreamEvent

        self.calls += 1
        yield StreamEvent(
            kind="final",
            response=LLMResponse(
                tool_calls=[
                    ToolCall(
                        id=f"c{self.calls}",
                        name="tick",
                        # разные аргументы — чтобы не сработал детектор цикла
                        arguments={"n": self.calls},
                    )
                ]
            ),
        )


def test_heartbeat_extends_budget() -> None:
    """Живой прогресс продлевает бюджет: ход не умирает по часам от старта.

    Регресс к жалобе «тайм-аут убивал опрос фоновой установки»: раньше
    дедлайн считался от начала запроса, теперь — от последнего завершённого
    шага, но с жёстким потолком, чтобы ход не висел вечно.
    """
    cfg = get_config()
    registry = ToolRegistry()
    _TickTool.executions = 0
    registry.register(_TickTool())
    agent = Agent(
        config=cfg,
        llm=_AlwaysCallLLM(),  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
    )
    agent.max_steps = 50
    agent.run_budget = 0.6  # простой: 0.6 с без завершённого шага → стоп
    agent.budget_streak = 3  # жёсткий потолок: 1.8 с суммарно

    events = list(agent.run("долгая задача"))
    errors = [e.error for e in events if e.kind == "error"]

    # Без продления стоп пришёл бы на 3-м шаге (0.6 с от старта запроса);
    # с продлением шаги идут все 1.8 с, остановка — уже по жёсткому пределу.
    assert _TickTool.executions >= 5, (
        f"бюджет не продлевался: только {_TickTool.executions} шагов",
        _TickTool.executions,
    )
    assert len(errors) == 1 and "по времени" in errors[0], errors
    assert "жёсткий предел" in errors[0], errors
    assert "продлевался по прогрессу" in errors[0], errors
    print(
        f"ok  heartbeat: {_TickTool.executions} шагов за "
        f"{agent.run_budget * agent.budget_streak:g} с (без продления — стоп на 3-м)"
    )


def test_limits_from_config() -> None:
    """Лимиты шагов и времени берутся из конфига (настраиваются в GUI)."""
    cfg = get_config()
    old_steps, old_budget = cfg.tools.max_steps, cfg.tools.run_budget_seconds
    try:
        cfg.tools.max_steps = 2
        cfg.tools.run_budget_seconds = 123
        registry = ToolRegistry()
        registry.register(_BoomTool())
        # разные аргументы — чтобы не сработал детектор цикла, а сработал лимит
        llm = _FakeLLM(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id=f"c{i}", name="boom", arguments={"x": i})]
                )
                for i in range(10)
            ]
        )
        agent = Agent(
            config=cfg,
            llm=llm,  # type: ignore[arg-type]
            registry=registry,
            permissions=PermissionManager(cfg),
        )
        assert agent.max_steps == 2, agent.max_steps
        assert agent.run_budget == 123, agent.run_budget

        events = list(agent.run("длинная задача"))
        errors = [e.error for e in events if e.kind == "error"]
        assert len(errors) == 1 and "шагов (2)" in errors[0], errors
        assert llm.calls == 2, llm.calls  # остановка ровно на лимите из конфига
    finally:
        cfg.tools.max_steps = old_steps
        cfg.tools.run_budget_seconds = old_budget
    print("ok  лимиты шагов/времени берутся из конфига и останавливают цикл")


def main() -> int:
    test_round_trip()
    test_orphan_rows_dropped()
    test_tool_calls_without_result_stripped()
    test_partial_results_kept()
    test_agent_boot_with_legacy_db()
    test_empty_answer_retried()
    test_empty_answer_gives_error()
    test_error_sent_back_for_rework()
    test_error_directives_sequence()
    test_loop_and_budget_stops()
    test_heartbeat_extends_budget()
    test_limits_from_config()
    print("\nВСЕ ТЕСТЫ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
