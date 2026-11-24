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
from nemus.core.agent import (  # noqa: E402
    _INTERNAL_PREFIX,
    Agent,
    QualityGate,
    _is_poll_call,
    _requested_video_seconds,
    pair_tool_messages,
)
from nemus.core.llm import LLMResponse  # noqa: E402
from nemus.core.memory import Memory, load_messages_into  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import Message, ProviderError, ToolCall  # noqa: E402
from nemus.core.tools.base import RiskLevel, Tool, ToolResult  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402
from nemus.core.tools.skills_tool import FindSkillsTool  # noqa: E402
from nemus.core.tools.todo_tool import TodoWriteTool  # noqa: E402


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


class _FlakyLLM:
    """Первый запрос падает временной ошибкой, второй отвечает."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, messages, tools=None, **_: Any):  # noqa: ANN001
        from nemus.core.llm import StreamEvent

        self.calls += 1
        if self.calls == 1:
            raise ProviderError("429 Too Many Requests", retryable=True)
        yield StreamEvent(
            kind="final",
            response=LLMResponse(content="Ответ после паузы"),
        )


def test_provider_error_retried_inside_run() -> None:
    """Временный отказ всех провайдеров не обрывает задачу с первого раза."""
    from unittest import mock

    cfg = get_config()
    llm = _FlakyLLM()
    agent = Agent(
        config=cfg,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )
    with mock.patch("nemus.core.agent.PROVIDER_RETRY_DELAYS", (0.0,)):
        events = list(
            agent.run(
                "Объясни подробно, чем отличаются TCP и UDP, и приведи примеры "
                "использования в реальных проектах."
            )
        )

    assert llm.calls == 2, llm.calls
    assert not [e for e in events if e.kind == "error"], events
    finals = [e for e in events if e.kind == "final"]
    assert finals and finals[-1].text == "Ответ после паузы", events
    print("ok  временный отказ провайдеров → автоповтор шага, без ошибки")


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
    assert llm.calls == EMPTY_ANSWER_RETRIES, llm.calls
    print("ok  пустые ответы подряд: явная ошибка вместо молчаливого финала")


class _TruncatedLLM:
    """Первый ответ оборван по лимиту, второй — нормальный."""

    def __init__(self) -> None:
        self.calls = 0
        self.tokens: list[int] = []

    def stream(self, messages, tools=None, **kwargs: Any):  # noqa: ANN001
        from nemus.core.llm import StreamEvent

        self.calls += 1
        self.tokens.append(int(kwargs.get("max_tokens") or 0))
        if self.calls == 1:
            yield StreamEvent(
                kind="final",
                response=LLMResponse(content="", finish_reason="length"),
            )
            return
        yield StreamEvent(
            kind="final",
            response=LLMResponse(content="Готово: сайт собран и проверен"),
        )


def test_truncated_answer_raises_token_budget() -> None:
    """finish=length не повторяется с тем же лимитом: бюджет растёт."""
    cfg = get_config()
    llm = _TruncatedLLM()
    agent = Agent(
        config=cfg,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )
    events = list(agent.run("сделай сайт кофейни"))

    assert llm.calls == 2, llm.calls
    assert llm.tokens[0] >= 8192, llm.tokens
    assert llm.tokens[1] > llm.tokens[0], llm.tokens
    finals = [e for e in events if e.kind == "final"]
    assert len(finals) == 1 and "сайт собран" in finals[0].text, events
    print("ok  finish=length: повтор с увеличенным max_tokens, а не тот же обрыв")


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


class _WriteTool(Tool):
    """Подставной производственный инструмент для quality gate."""

    name = "write_file"
    description = "записывает файл"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE
    executions = 0

    def execute(self, **_: Any) -> ToolResult:
        type(self).executions += 1
        return ToolResult.success("файл записан")


def test_quality_gate_blocks_shortcuts() -> None:
    """Код/артефакт нельзя начать без research и плана."""
    cfg = get_config()
    _WriteTool.executions = 0
    registry = ToolRegistry()
    registry.register_many([FindSkillsTool(), TodoWriteTool(), _WriteTool()])
    llm = _FakeLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="w1", name="write_file", arguments={})]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="f1", name="find_skills", arguments={"query": "код"}
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="p1",
                        name="todo_write",
                        arguments={
                            "items": [
                                {"title": "research", "status": "completed"},
                                {"title": "build", "status": "in_progress"},
                            ]
                        },
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="w2", name="write_file", arguments={})]
            ),
            LLMResponse(content="Готово: скрипт написан и проверен."),
        ]
    )
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
    )
    events = list(agent.run("напиши скрипт для отчёта"))

    tool_messages = [m for m in agent.history if m.role == "tool"]
    assert "QUALITY GATE" in str(tool_messages[0].content), tool_messages[0].content
    assert _WriteTool.executions == 1, _WriteTool.executions
    assert [e.text for e in events if e.kind == "final"] == [
        "Готово: скрипт написан и проверен."
    ]
    print("ok  quality gate: production заблокирован до research и todo_write")


class _VerifyWebsiteStub(Tool):
    """Заглушка браузерной проверки для quality gate."""

    name = "verify_website"
    description = "проверяет сайт"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE

    def execute(self, **_: Any) -> ToolResult:
        return ToolResult.success("сайт проверен")


def test_website_requires_verification() -> None:
    """Сайт нельзя сдать без verify_website, обычный код — можно."""
    from nemus.core.language import is_website_request

    assert is_website_request("сделай сайт кофейни")
    assert is_website_request("сверстай лендинг для кофейни")
    assert not is_website_request("открой сайт кофейни")
    assert not is_website_request("напиши скрипт для отчёта")

    registry = ToolRegistry()
    registry.register_many(
        [FindSkillsTool(), TodoWriteTool(), _WriteTool(), _VerifyWebsiteStub()]
    )
    gate = QualityGate(registry, "сделай сайт кофейни")
    assert gate.needs_verify and not gate.verified_done
    gate.research_done = gate.plan_done = gate.production_done = True
    reason = gate.final_reason("Готово: сайт собран")
    assert "verify_website" in reason, reason
    gate.observe("verify_website", True)
    assert gate.final_reason("Готово: сайт проверен") == ""
    gate.observe("write_file", True)
    assert not gate.verified_done
    assert "verify_website" in gate.final_reason("Готово: сайт поправлен")
    gate.observe("verify_website", True)
    assert gate.final_reason("Готово: сайт перепроверен") == ""
    gate.observe("send_telegram", True)
    assert gate.verified_done  # отправка архива не отменяет проверку

    code_gate = QualityGate(registry, "напиши скрипт для отчёта")
    code_gate.research_done = code_gate.plan_done = True
    code_gate.production_done = True
    assert not code_gate.needs_verify
    assert code_gate.final_reason("Готово") == ""
    print("ok  quality gate: сайт требует verify_website, код — нет")


def test_telegram_gate_requires_brainstorm_answers() -> None:
    """В Telegram производство заблокировано, пока не заданы вопросы и не пришли ответы."""
    cfg = get_config()
    _WriteTool.executions = 0
    registry = ToolRegistry()
    registry.register_many([FindSkillsTool(), TodoWriteTool(), _WriteTool()])
    questions = (
        "1. Какой формат у отчёта?\n"
        "A) Markdown (рекомендую)\n"
        "B) HTML\n"
        "C) PDF\n"
        "D) Свой вариант: _________\n\n"
        "2. Где сохранить файл?\n"
        "A) В рабочей папке (рекомендую)\n"
        "B) На рабочем столе\n"
        "C) В папке загрузок\n"
        "D) Свой вариант: ___________"
    )
    bad_questions = (
        "1. В каком стиле?\n"
        "A) Векторный (рекомендую)\n"
        "D) Свой вариант: _________\n\n"
        "2. Какая длина?\n"
        "A) 30 секунд (рекомендую)\n"
        "D) Свой вариант: _________"
    )
    bad_gate = QualityGate(
        registry, "сделай видео", needs_brief=True
    )
    bad_reason = bad_gate.brainstorm_problem(bad_questions)
    assert "не хватает вариантов" in bad_reason, bad_reason
    llm = _FakeLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="w1", name="write_file", arguments={})]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="f1", name="find_skills", arguments={"query": "отчёт"}
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="p1",
                        name="todo_write",
                        arguments={
                            "items": [
                                {"title": "research", "status": "completed"},
                                {"title": "брейншторм", "status": "in_progress"},
                            ]
                        },
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="w2", name="write_file", arguments={})]
            ),
            LLMResponse(content=questions),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="f2", name="find_skills", arguments={"query": "отчёт"}
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="p2",
                        name="todo_write",
                        arguments={
                            "items": [
                                {"title": "research", "status": "completed"},
                                {"title": "build", "status": "in_progress"},
                            ]
                        },
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="w3", name="write_file", arguments={})]
            ),
            LLMResponse(content="Готово: отчёт написан."),
        ]
    )
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
        system_suffix="СЕЙЧАС ТЫ РАБОТАЕШЬ В TELEGRAM. Отвечай коротко.",
    )
    first = list(agent.run("напиши скрипт для отчёта"))
    assert [e.text for e in first if e.kind == "final"] == [questions], [
        (e.kind, e.text) for e in first
    ]
    assert _WriteTool.executions == 0, _WriteTool.executions
    blocked = "\n".join(
        str(message.content)
        for message in agent.history
        if message.role == "tool"
    )
    assert "брейншторм не пройден" in blocked, blocked

    answers = (
        "Ответы пользователя на вопросы брейншторма:\n"
        "Выбор: 1A, 2A\n\n"
        "1. Какой формат у отчёта? A) Markdown\n"
        "2. Где сохранить файл? A) В рабочей папке\n\n"
        "Все вопросы согласованы. Приступай к работе по этим ответам."
    )
    second = list(agent.run(answers))
    assert _WriteTool.executions == 1, _WriteTool.executions
    assert [e.text for e in second if e.kind == "final"] == [
        "Готово: отчёт написан."
    ], [(e.kind, e.text) for e in second]
    print(
        "ok  Telegram: production заблокирован до ответов на брейншторм"
    )


def test_old_internal_rows_dropped() -> None:
    """Старые служебные директивы из БД не попадают в роль пользователя."""
    db = _tmp_db()
    mem = Memory(db)
    mem.add_message("user", "QUALITY GATE: вызови find_skills и todo_write")
    mem.add_message("user", "обычный вопрос")
    history: list[Message] = [Message(role="system", content="s")]
    load_messages_into(history, mem.recent_messages(limit=20))
    mem.close()
    assert [m.content for m in history[1:]] == ["обычный вопрос"]
    print("ok  старые служебные директивы вычищены из истории")


def test_telegram_open_brief_answers_unlock() -> None:
    """Открытые ответы на фактические вопросы разблокируют производство."""
    cfg = get_config()
    _WriteTool.executions = 0
    registry = ToolRegistry()
    registry.register_many([FindSkillsTool(), TodoWriteTool(), _WriteTool()])
    questions = (
        "1. Какой у вас адрес?\n"
        "2. Какие телефон и мессенджеры?\n"
        "3. Что в меню и какие цены?\n"
        "4. Какие часы работы?"
    )
    llm = _FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="f1", name="find_skills", arguments={"query": "сайт"}
                    )
                ]
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="p1",
                        name="todo_write",
                        arguments={
                            "items": [
                                {"title": "research", "status": "completed"},
                                {"title": "брейншторм", "status": "in_progress"},
                            ]
                        },
                    )
                ]
            ),
            LLMResponse(content=questions),
            LLMResponse(
                tool_calls=[ToolCall(id="w1", name="write_file", arguments={})]
            ),
            LLMResponse(content="Готово: сайт собран."),
        ]
    )
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
        system_suffix="СЕЙЧАС ТЫ РАБОТАЕШЬ В TELEGRAM. Отвечай коротко.",
    )
    first = list(agent.run("создай сайт кофейни"))
    assert [e.text for e in first if e.kind == "final"] == [questions], [
        (e.kind, e.text) for e in first
    ]
    assert _WriteTool.executions == 0, _WriteTool.executions

    answers = (
        "Адрес: Дмитровка, 12. Телефон: +7 999 123-45-67. "
        "Меню: эспрессо 250 ₽, капучино 320 ₽. Часы: 8:00–22:00."
    )
    second = list(agent.run(answers))
    assert _WriteTool.executions == 1, _WriteTool.executions
    assert [e.text for e in second if e.kind == "final"] == [
        "Готово: сайт собран."
    ], [(e.kind, e.text) for e in second]
    print(
        "ok  Telegram: открытые ответы на факты разблокировали работу"
    )


def test_internal_directives_not_saved_as_user() -> None:
    """Служебные указания не должны попадать в чат как реплики человека."""
    db = _tmp_db()
    mem = Memory(db)
    cfg = get_config()
    llm = _FakeLLM(
        [LLMResponse(content=""), LLMResponse(content="Готово: ответ по делу.")]
    )
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=mem,
    )
    list(agent.run("простой вопрос"))
    rows = mem.recent_messages(limit=20)
    assert not any(_INTERNAL_PREFIX in str(row.get("content") or "") for row in rows)
    assert any(row.get("role") == "user" for row in rows)
    assert any(
        _INTERNAL_PREFIX in str(message.content)
        for message in agent.history
        if message.role == "user"
    )
    mem.close()
    print("ok  служебные директивы не сохраняются как сообщения пользователя")


def test_polling_is_not_deadlock() -> None:
    """Ожидание фонового рендера не должно считаться тупиком."""
    assert _is_poll_call(
        ToolCall(id="p", name="run_shell", arguments={"action": "read"})
    )
    assert not _is_poll_call(
        ToolCall(id="r", name="run_shell", arguments={"action": "run"})
    )
    gate = QualityGate(
        ToolRegistry(), "Сделай видео про блины на 2 минуты с шагами"
    )
    gate.active = True
    gate.research_done = gate.plan_done = True
    reason = gate.block_reason(
        "create_video",
        {
            "duration": 120,
            "scenes": [
                {"title": "Интро", "duration": 5},
                {"title": "Рецепт", "duration": 5},
                {"title": "Итог", "duration": 5},
            ],
        },
    )
    assert "15 с" in reason and "120 с" in reason, reason
    assert _requested_video_seconds("2-минутное видео") == 120
    print("ok  polling не тупик; короткое видео отклоняется гейтом")


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
    test_provider_error_retried_inside_run()
    test_empty_answer_gives_error()
    test_truncated_answer_raises_token_budget()
    test_error_sent_back_for_rework()
    test_error_directives_sequence()
    test_quality_gate_blocks_shortcuts()
    test_website_requires_verification()
    test_telegram_gate_requires_brainstorm_answers()
    test_telegram_open_brief_answers_unlock()
    test_internal_directives_not_saved_as_user()
    test_old_internal_rows_dropped()
    test_polling_is_not_deadlock()
    test_loop_and_budget_stops()
    test_heartbeat_extends_budget()
    test_limits_from_config()
    print("\nВСЕ ТЕСТЫ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
