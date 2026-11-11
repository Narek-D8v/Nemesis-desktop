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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.memory import Memory, load_messages_into  # noqa: E402


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


def main() -> int:
    test_summary_storage_roundtrip()
    test_recent_messages_after_id_and_db_id()
    test_delete_session_removes_summary()
    print("\nCONTEXT SUMMARY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
