"""Тест автоматической выжимки контекста.

Проверяет:
- хранение выжимки и id сообщений в SQLite;
- скользящую выжимку в агенте без потери system-промпта;
- отдельные сессии/выжимки Telegram-чатов.

Запуск:  venv\\Scripts\\python.exe scripts\\test_context_summary.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import Config  # noqa: E402
from nemus.core.agent import Agent  # noqa: E402
from nemus.core.llm import StreamEvent  # noqa: E402
from nemus.core.memory import Memory, load_messages_into  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, Message, ToolCall  # noqa: E402
from nemus.core.telegram_bot import TelegramBot  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


class _SummaryLLM:
    """Мок LLM: выжимка через complete, ответ агента через stream."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.prompts: list[list[Message]] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.prompts.append(list(messages))
        if self.fail:
            raise RuntimeError("выжимка недоступна")
        return LLMResponse(content=f"ВЫЖИМКА {self.calls}")

    def stream(self, messages, tools=None, **kwargs):
        yield StreamEvent(kind="final", response=LLMResponse(content="Готово"))


def _agent(llm: _SummaryLLM | None = None, memory: Memory | None = None) -> Agent:
    cfg = Config()
    agent = Agent(
        cfg,
        llm=llm or _SummaryLLM(),
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=memory,
    )
    agent.max_history_chars = 900
    agent.max_summary_chars = 300
    return agent


def _push(agent: Agent, message: Message) -> None:
    agent.history.append(message)
    agent._persist(message)


def _seed(agent: Agent, count: int, start: int = 0) -> None:
    for index in range(start, start + count):
        _push(agent, Message(role="user", content=f"шаг {index} " + "у" * 120))
        _push(agent, Message(role="assistant", content="ок " + "а" * 120))


def _text(message: Message) -> str:
    content = message.content
    return content if isinstance(content, str) else str(content)


def test_summary_replaces_old_history_but_keeps_system() -> None:
    """Переполнение рождает выжимку; system и последние реплики остаются."""
    llm = _SummaryLLM()
    agent = _agent(llm)
    agent.history = [Message(role="system", content="СИСТЕМА " + "с" * 200)]
    _seed(agent, 12)

    sent = agent._history_for_model()

    assert sent[0].role == "system"
    assert "СИСТЕМА" in _text(sent[0])
    assert "ВЫЖИМКА 1" in _text(sent[0]), "выжимка не попала в system-блок"
    assert "шаг 11" in _text(sent[-2]), "последняя реплика пользователя потерялась"
    assert all("шаг 0 " not in _text(message) for message in sent), (
        "старые сообщения ушли в запрос вместе с выжимкой"
    )
    assert len(agent.history) == 1 + 12 * 2, "исходная история не должна мутировать"
    print("ok  выжимка заменяет старую историю, system и хвост на месте")


def test_summary_is_incremental_and_persisted() -> None:
    """Повторное переполнение дописывает выжимку и сохраняет её в SQLite."""
    mem = Memory(_tmp_db())
    llm = _SummaryLLM()
    agent = _agent(llm, mem)
    agent.history = [Message(role="system", content="СИСТЕМА")]
    _seed(agent, 8)

    agent._history_for_model()
    first = agent._summary
    assert first, "выжимка не создана"
    stored = mem.get_summary()
    assert stored is not None and stored[0] == first, stored

    _seed(agent, 8, start=8)
    agent._history_for_model()

    assert llm.calls >= 2, "вторая выжимка не запрошена"
    prompt = _text(llm.prompts[-1][-1])
    assert "Предыдущая выжимка" in prompt, "новая выжимка не учитывает старую"
    assert agent._summary != first, "выжимка не обновилась"

    restarted = _agent(_SummaryLLM(), mem)
    assert restarted._summary == agent._summary, "выжимка не восстановилась"
    assert not any(
        "шаг 0 " in _text(message) for message in restarted.history[1:]
    ), "после рестарта загрузились сообщения из выжимки"
    assert "ВЫЖИМКА" in _text(restarted._history_for_model()[0])
    mem.close()
    print("ok  выжимка инкрементальная, хранится в SQLite и переживает рестарт")


def test_summary_failure_falls_back_to_trim() -> None:
    """Если LLM выжимки недоступен, чат не ломается — работает обрезка."""
    llm = _SummaryLLM(fail=True)
    agent = _agent(llm)
    agent.history = [Message(role="system", content="СИСТЕМА")]
    _seed(agent, 12)

    sent = agent._history_for_model()

    assert llm.calls == 1
    assert sent[0].role == "system"
    assert "ВЫЖИМКА" not in _text(sent[0])
    assert "шаг 11" in _text(sent[-2]), "при откате потерялась текущая задача"
    print("ok  ошибка выжимки откатывается на обычную обрезку")


def test_large_system_prompt_does_not_trigger_summary() -> None:
    """Большой system-промпт не считается историей и не запускает выжимку."""
    llm = _SummaryLLM()
    agent = _agent(llm)
    agent.history = [Message(role="system", content="СИСТЕМА " + "с" * 5000)]
    _seed(agent, 1)

    sent = agent._history_for_model()

    assert llm.calls == 0, "выжимка запустилась из-за system-промпта"
    assert sent is agent.history
    print("ok  большой system-промпт не запускает выжимку")


def test_reset_clears_summary() -> None:
    """reset очищает и память выжимки, и сохранённую строку в SQLite."""
    mem = Memory(_tmp_db())
    agent = _agent(_SummaryLLM(), mem)
    agent.history = [Message(role="system", content="СИСТЕМА")]
    _seed(agent, 8)
    agent._history_for_model()
    assert agent._summary and mem.get_summary() is not None

    agent.reset()

    assert agent._summary == ""
    assert mem.get_summary() is None
    mem.close()
    print("ok  reset очищает выжимку в памяти и SQLite")


def test_summary_tail_starts_at_user_and_keeps_tool_pair() -> None:
    """Хвост после выжимки начинается с пользователя, пара tool не рвётся."""
    agent = _agent(_SummaryLLM())
    agent.history = [Message(role="system", content="СИСТЕМА")]
    _seed(agent, 8)
    current = Message(role="user", content="текущая задача " + "т" * 120)
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"})
    assistant = Message(role="assistant", content=None, tool_calls=[call])
    result = Message(
        role="tool",
        content="содержимое",
        tool_call_id="call-1",
        name="read_file",
    )
    for message in (current, assistant, result):
        _push(agent, message)

    sent = agent._history_for_model()
    tail = sent[1:]

    assert tail[0].role == "user", "хвост не начинается с реплики пользователя"
    assert any("текущая задача" in _text(message) for message in tail), (
        "текущая задача потерялась"
    )
    assert any(message.role == "assistant" and message.tool_calls for message in tail)
    assert any(message.role == "tool" for message in tail)
    print("ok  граница выжимки не рвёт текущую задачу и пару tool")



def _tmp_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="nemus_summary_")) / "mem.db"


def test_summary_storage_roundtrip() -> None:
    """Выжимка сохраняется, переживает переоткрытие и чистится с диалогом."""
    db = _tmp_db()
    mem = Memory(db)
    first = mem.current_session_id
    mid = mem.add_message("user", "привет")
    assert isinstance(mid, int) and mid > 0, mid
    mem.set_summary("кратко: поздоровались", mid)
    mem.close()

    reopened = Memory(db)
    assert reopened.current_session_id == first
    assert reopened.get_summary() == ("кратко: поздоровались", mid)
    reopened.clear_messages()
    assert reopened.get_summary() is None, "выжимка пережила очистку диалога"
    reopened.close()
    print("ok  выжимка хранится в SQLite и очищается вместе с диалогом")


def test_recent_messages_after_id_and_db_id() -> None:
    """Сообщения после covered_until и id для восстановления выжимки."""
    mem = Memory(_tmp_db())
    first = mem.add_message("user", "один")
    second = mem.add_message("assistant", "два")
    rows = mem.recent_messages(limit=10, after_id=first)
    assert [row["id"] for row in rows] == [second], rows

    history: list[Any] = []
    load_messages_into(history, mem.recent_messages(limit=10))
    assert [m.db_id for m in history] == [first, second], history
    mem.close()
    print("ok  recent_messages(after_id) и Message.db_id работают")


def test_delete_session_removes_summary() -> None:
    """Удаление диалога не оставляет осиротевшую выжимку."""
    mem = Memory(_tmp_db())
    mid = mem.add_message("user", "удали меня")
    sid = mem.current_session_id
    mem.set_summary("старая выжимка", mid)
    mem.delete_session(sid)
    assert mem.get_summary(sid) is None, mem.get_summary(sid)
    mem.close()
    print("ok  удаление диалога удаляет его выжимку")


def test_summary_limit_follows_config() -> None:
    """Потолок выжимки не раздувается сверх max_summary_chars."""
    agent = _agent()
    agent.max_history_chars = 60000
    agent.max_summary_chars = 500
    assert agent._summary_limit() == 500

    agent.max_history_chars = 900
    agent.max_summary_chars = 300
    assert agent._summary_limit() == 225

    agent.max_summary_chars = 100
    assert agent._summary_limit() == 100, "лимит проигнорировал настройку"

    agent.max_summary_chars = 0
    assert agent._summary_limit() == 0, "0 должен отключать выжимку"
    print("ok  лимит выжимки следует за max_summary_chars")


def test_telegram_chat_sessions_are_isolated_and_restored() -> None:
    """У каждого Telegram-чата своя сессия; /new не трогает соседей."""
    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    mem = Memory(_tmp_db())

    def core() -> SimpleNamespace:
        return SimpleNamespace(
            config=cfg,
            llm=_SummaryLLM(),
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=mem,
        )

    bot = TelegramBot(cfg, core())
    bot._prepare_session(42)
    list(bot.agent.run("запомни число 7"))
    bot._prepare_session(43)
    list(bot.agent.run("запомни число 9"))

    bot._prepare_session(42)
    texts = [_text(message) for message in bot.agent.history if message.content]
    assert any("число 7" in text for text in texts), texts
    assert not any("число 9" in text for text in texts), texts

    restarted = TelegramBot(cfg, core())
    restarted._prepare_session(42)
    texts = [_text(message) for message in restarted.agent.history if message.content]
    assert any("число 7" in text for text in texts), texts

    bot._new_session_for_chat(42)
    bot._prepare_session(42)
    texts = [_text(message) for message in bot.agent.history if message.content]
    assert not any("число 7" in text for text in texts), "новый диалог не очистился"
    bot._prepare_session(43)
    texts = [_text(message) for message in bot.agent.history if message.content]
    assert any("число 9" in text for text in texts), "новый диалог затёр соседний чат"
    mem.close()
    print("ok  Telegram-чаты изолированы, /new не трогает соседей")


def main() -> int:
    test_summary_storage_roundtrip()
    test_recent_messages_after_id_and_db_id()
    test_delete_session_removes_summary()
    test_summary_replaces_old_history_but_keeps_system()
    test_summary_is_incremental_and_persisted()
    test_summary_failure_falls_back_to_trim()
    test_large_system_prompt_does_not_trigger_summary()
    test_reset_clears_summary()
    test_summary_tail_starts_at_user_and_keeps_tool_pair()
    test_summary_limit_follows_config()
    test_telegram_chat_sessions_are_isolated_and_restored()
    print("\nCONTEXT SUMMARY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
