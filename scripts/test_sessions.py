"""Тест системы сохранения и переключения диалогов (сессий).

Регресс к задаче: вся история лежала одной кучей в `messages`, поэтому
новый диалог можно было начать только очисткой — старые реплики либо
терялись, либо тянулись в новый разговор после рестарта.

Что проверяем:
- старая БД без `session_id` мигрирует и её реплики достаются первому диалогу;
- сообщения разных диалогов не смешиваются;
- выбор диалога переживает перезапуск (`meta`);
- переименование и автоназвание по первой реплике;
- удаление: текущий диалог сменяется, последний пересоздаётся;
- агент переключается и подгружает историю нужного диалога.

Запуск:  venv\\Scripts\\python.exe scripts\\test_sessions.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import Agent  # noqa: E402
from nemus.core.llm import LLMResponse  # noqa: E402
from nemus.core.memory import DEFAULT_SESSION_TITLE, Memory, load_messages_into  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


def _tmp_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="nemus_sess_")) / "mem.db"


def _texts(mem: Memory) -> list[str]:
    rows = mem.recent_messages(limit=50)
    return [str(r["content"]) for r in rows if r["content"]]


def test_legacy_db_migration() -> None:
    """Старая БД (без session_id) открывается, реплики достаются диалогу №1."""
    db = _tmp_db()
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                name TEXT,
                ts TEXT NOT NULL
            );
            CREATE TABLE facts (key TEXT PRIMARY KEY, value TEXT, ts TEXT NOT NULL);
            INSERT INTO messages (role, content, ts)
                VALUES ('user', 'привет', '2026-01-01');
            INSERT INTO messages (role, content, ts)
                VALUES ('assistant', 'здравствуй', '2026-01-01');
            """
        )

    mem = Memory(db)
    sessions = mem.list_sessions()
    assert len(sessions) == 1, sessions
    assert sessions[0]["id"] == mem.current_session_id == 1, sessions
    assert sessions[0]["title"] == DEFAULT_SESSION_TITLE, sessions
    assert sessions[0]["messages"] == 2, sessions
    assert _texts(mem) == ["привет", "здравствуй"], _texts(mem)
    mem.close()
    print("ok  старая БД мигрировала, реплики достались диалогу №1")


def test_sessions_are_isolated() -> None:
    """Реплики разных диалогов не смешиваются ни при записи, ни при чтении."""
    mem = Memory(_tmp_db())
    first = mem.current_session_id
    mem.add_message("user", "первый вопрос")
    mem.add_message("assistant", "первый ответ")

    second = mem.create_session("Работа")
    assert second != first, (first, second)
    assert mem.current_session_id == second
    assert _texts(mem) == [], _texts(mem)

    mem.add_message("user", "второй вопрос")
    assert _texts(mem) == ["второй вопрос"], _texts(mem)

    mem.switch_session(first)
    assert _texts(mem) == ["первый вопрос", "первый ответ"], _texts(mem)

    titles = {s["id"]: s["title"] for s in mem.list_sessions()}
    assert titles[second] == "Работа", titles
    assert titles[first] == DEFAULT_SESSION_TITLE, titles
    mem.close()
    print("ok  диалоги изолированы: свои сообщения и названия")


def test_current_session_survives_restart() -> None:
    """Выбранный диалог остаётся текущим после переоткрытия БД."""
    db = _tmp_db()
    mem = Memory(db)
    mem.add_message("user", "до рестарта")
    other = mem.create_session("Другой")
    mem.switch_session(other)
    mem.add_message("user", "в другом")
    mem.close()

    reopened = Memory(db)
    assert reopened.current_session_id == other, reopened.current_session_id
    assert _texts(reopened) == ["в другом"], _texts(reopened)
    reopened.close()
    print("ok  текущий диалог переживает перезапуск")


def test_rename_and_auto_title() -> None:
    """Первая реплика называет диалог; вручное название не затирается."""
    mem = Memory(_tmp_db())
    assert mem.current_session_title() == DEFAULT_SESSION_TITLE

    mem.auto_title("  сделай   презентацию о гравитации  ")
    assert mem.current_session_title() == "сделай презентацию о гравитации"

    # чужое (не дефолтное) название автозаголовок не трогает
    mem.auto_title("ещё одна реплика")
    assert mem.current_session_title() == "сделай презентацию о гравитации"

    sid = mem.current_session_id
    mem.rename_session(sid, "  Моя тема  ")
    assert mem.current_session_title() == "Моя тема"
    mem.auto_title("попытка перезаписать")
    assert mem.current_session_title() == "Моя тема"

    long_text = "очень длинный вопрос " * 10
    mem.create_session()
    mem.auto_title(long_text)
    assert len(mem.current_session_title()) <= 48, mem.current_session_title()
    mem.close()
    print("ok  автоназвание по первой реплике и переименование")


def test_delete_session() -> None:
    """Удаление: текущий сменяется, последний пересоздаётся, реплики гибнут."""
    db = _tmp_db()
    mem = Memory(db)
    mem.add_message("user", "живу в первом")
    first = mem.current_session_id
    second = mem.create_session("Второй")
    mem.add_message("user", "живу во втором")
    mem.switch_session(first)

    result = mem.delete_session(second)
    assert result == first, (result, first)
    assert mem.current_session_id == first
    assert [s["id"] for s in mem.list_sessions()] == [first], mem.list_sessions()
    assert _texts(mem) == ["живу в первом"], _texts(mem)

    # удалили единственный — появился новый пустой, а не «битый» текущий
    result = mem.delete_session(first)
    assert result != first, (result, first)
    assert mem.current_session_id == result
    assert mem.list_sessions(), "не осталось ни одного диалога"
    assert _texts(mem) == [], _texts(mem)
    mem.close()

    reopened = Memory(db)
    assert reopened.current_session_id == result, reopened.current_session_id
    reopened.close()
    print("ok  удаление: подмена текущего и пересоздание последнего")


def test_clear_only_touches_current() -> None:
    """«Очистить» чистит только текущий диалог."""
    mem = Memory(_tmp_db())
    keep = mem.current_session_id
    mem.add_message("user", "останусь")
    other = mem.create_session("Другой")
    assert mem.current_session_id == other
    mem.add_message("user", "исчезну")
    mem.clear_messages()

    assert _texts(mem) == [], _texts(mem)  # чистили именно текущий
    mem.switch_session(keep)
    assert _texts(mem) == ["останусь"], _texts(mem)
    assert mem.current_session_id == keep
    mem.close()
    print("ok  clear_messages не трогает соседние диалоги")


def test_non_current_session_ops() -> None:
    """Чужой диалог переименовывается и удаляется, текущий не тронут."""
    mem = Memory(_tmp_db())
    first = mem.current_session_id
    mem.add_message("user", "живу здесь")
    other = mem.create_session("Чужой")
    mem.add_message("user", "чужая реплика")
    mem.switch_session(first)

    mem.rename_session(other, "  Переименованный  ")
    assert mem.current_session_id == first, mem.current_session_id
    assert mem.session_title(other) == "Переименованный", mem.session_title(other)
    assert mem.session_title(first) == DEFAULT_SESSION_TITLE
    assert mem.session_title(9999) == "", "несуществующий диалог должен быть пустым"

    result = mem.delete_session(other)
    assert result == first, (result, first)
    assert mem.current_session_id == first, mem.current_session_id
    assert _texts(mem) == ["живу здесь"], _texts(mem)
    assert [s["id"] for s in mem.list_sessions()] == [first], mem.list_sessions()
    assert mem.session_title(other) == "", "удалённый диалог ещё существует"
    mem.close()
    print("ok  чужой диалог переименован и удалён, текущий не тронут")


class _FakeLLM:
    """Возвращает заданные ответы (по одному на каждый вызов stream)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def stream(self, messages: Any, tools: Any = None, **_: Any):  # noqa: ANN001
        from nemus.core.llm import StreamEvent

        self.calls += 1
        yield StreamEvent(kind="final", response=self.responses.pop(0))


def test_agent_switches_sessions() -> None:
    """Агент переключает диалог: история пересобирается под него."""
    cfg = get_config()
    mem = Memory(_tmp_db())
    llm = _FakeLLM([LLMResponse(content="Отвечу в первом")])
    agent = Agent(
        config=cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=mem,
    )
    first = mem.current_session_id
    list(agent.run("привет, ты тут?"))

    # диалог получил название по первой реплике
    assert mem.current_session_title() == "привет, ты тут?", (
        mem.current_session_title()
    )
    roles = [m.role for m in agent.history if m.role != "system"]
    assert roles == ["user", "assistant"], roles

    # новый диалог — чистая история, старый не потерян
    created = agent.new_session()
    assert created != first, (created, first)
    assert [m.role for m in agent.history if m.role != "system"] == []
    assert mem.recent_messages() == [], mem.recent_messages()

    back = agent.switch_session(first)
    assert back == first
    roles = [m.role for m in agent.history if m.role != "system"]
    assert roles == ["user", "assistant"], roles
    assert agent.history[-1].content == "Отвечу в первом", agent.history[-1]

    # перезапуск агента на том же диалоге — та же история
    restarted = Agent(
        config=cfg,
        llm=object(),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=Memory(mem.db_path),
    )
    roles = [m.role for m in restarted.history if m.role != "system"]
    assert roles == ["user", "assistant"], roles

    # агент без памяти просто не умеет переключаться
    orphan = Agent(
        config=cfg,
        llm=object(),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
    )
    assert orphan.new_session() == 0 and orphan.switch_session(1) == 0
    mem.close()
    print("ok  агент переключает диалоги и подгружает их историю")


def test_load_messages_keeps_tool_pairs() -> None:
    """Рендер диалога не ломает пары tool_calls ↔ результат."""
    mem = Memory(_tmp_db())
    from nemus.core.providers import ToolCall

    mem.add_message("user", "посчитай")
    mem.add_message(
        "assistant",
        tool_calls=[ToolCall(id="c1", name="calculate", arguments={"e": "2+2"})],
    )
    mem.add_message("tool", "4", tool_call_id="c1")
    mem.add_message("assistant", "Ответ: 4.")

    history: list[Any] = []
    load_messages_into(history, mem.recent_messages(limit=20))
    mem.close()

    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[1].tool_calls and history[1].tool_calls[0].name == "calculate"
    assert history[2].tool_call_id == "c1"
    print("ok  при рендере диалога пары tool_calls сохранены")


def test_sessions_window() -> None:
    """Кнопка в шапке открывает отдельное окно со списком диалогов."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from nemus.gui.main_window import MainWindow

    cfg = get_config()
    mem = Memory(_tmp_db())
    first = mem.current_session_id
    mem.add_message("user", "первый диалог: вопрос")
    second = mem.create_session("Второй диалог")
    mem.add_message("user", "второй диалог: вопрос")
    mem.switch_session(first)

    agent = Agent(
        config=cfg,
        llm=object(),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=mem,
    )
    core = SimpleNamespace(
        config=cfg, llm=SimpleNamespace(model="test/model"), memory=mem, agent=agent
    )
    _app = QApplication.instance() or QApplication([])
    window = MainWindow(core)  # type: ignore[arg-type]
    window.show()

    # списка диалогов в сайдбаре больше нет — только одна кнопка в шапке
    assert not hasattr(window, "sessions_block")
    assert not hasattr(window, "session_list")
    assert window.session_btn.menu() is None

    # клик по кнопке открывает отдельное окно со списком
    window.session_btn.click()
    dialog = window._sessions_dialog
    assert dialog is not None and dialog.isVisible(), dialog
    assert dialog.objectName() == "SessionsDialog"

    # карточка и панель действий разделены визуально (разные objectName → QSS)
    lst = dialog.session_list
    assert lst.objectName() == "SessionList"
    card = lst.parent()
    bar = card.parent().findChild(type(card), "SessionBar")
    assert card.objectName() == "SessionCard" and bar is not None, (
        card.objectName(),
        bar,
    )

    # список содержит оба диалога, текущий помечен маркером «●» и выделен
    texts = [lst.item(i).text() for i in range(lst.count())]
    assert any(t.startswith("● ") and "Новый диалог" in t for t in texts), texts
    assert "Второй диалог" in texts, texts
    current = lst.currentItem()
    assert current is not None and current.text().startswith("● "), (
        current.text() if current else None
    )

    # клик по строке переключает диалог, двигает маркер и закрывает окно-выборщик
    other = next(
        lst.item(i) for i in range(lst.count()) if "Второй диалог" in lst.item(i).text()
    )
    dialog._on_item_clicked(other)
    assert mem.current_session_id == second, mem.current_session_id
    assert not dialog.isVisible()
    dialog.refresh()
    texts = [lst.item(i).text() for i in range(lst.count())]
    assert any(t.startswith("● Второй диалог") for t in texts), texts
    html = "".join(window.chat._blocks)
    assert "второй диалог: вопрос" in html and "первый диалог" not in html, html[-400:]

    # кнопка «＋ Новый» прямо в окне создаёт диалог и он появляется в списке
    dialog.show()
    dialog._new_session()
    assert lst.count() == 3, lst.count()

    window.close()
    mem.close()
    print("ok  окно диалогов: кнопка открывает, клик переключает, список свежий")


def main() -> int:
    test_legacy_db_migration()
    test_sessions_are_isolated()
    test_current_session_survives_restart()
    test_rename_and_auto_title()
    test_delete_session()
    test_clear_only_touches_current()
    test_non_current_session_ops()
    test_agent_switches_sessions()
    test_load_messages_keeps_tool_pairs()
    test_sessions_window()
    print("\nВСЕ ТЕСТЫ СЕССИЙ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
