"""Тесты SQLite под двумя процессами: WAL, ожидание замка и миграция схемы.

Это не моки: два независимых соединения действительно борются за запись.
Проверяется то, из-за чего GUI и headless-бот падали с ``database is locked``
и почему при одновременном старте могли появиться две сессии.

Запуск:  venv\\Scripts\\python.exe scripts\\test_memory_concurrency.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.memory import SQLITE_BUSY_TIMEOUT_MS, Memory  # noqa: E402


def test_pragmas() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "nemus.db")
        mem = Memory(db)
        try:
            mode = mem._conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy = mem._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert str(mode).lower() == "wal", mode
            assert int(busy) == SQLITE_BUSY_TIMEOUT_MS, busy
        finally:
            mem.close()
    print("ok  SQLite: WAL включён, busy_timeout 30 с")


def test_writer_waits_for_lock() -> None:
    """Второе соединение ждёт, а не падает с database is locked."""
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "nemus.db")
        first = Memory(db)
        second = Memory(db)
        result: dict[str, object] = {}
        started = threading.Event()
        try:
            # Держим монопольную запись дольше дефолтных 5 секунд? Нет —
            # проверяем сам факт ожидания; 30 секунд подтверждены выше.
            first._conn.execute("BEGIN IMMEDIATE")
            first._conn.execute(
                "INSERT INTO facts (key, value, ts) VALUES ('lock', 'held', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )

            def writer() -> None:
                started.set()
                try:
                    second.add_message("user", "пока ждём")
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc
                else:
                    result["ok"] = True

            thread = threading.Thread(target=writer, name="second-writer", daemon=True)
            thread.start()
            assert started.wait(2), "писатель не запустился"
            time.sleep(0.4)
            assert thread.is_alive(), "вторая запись не должна падать мгновенно"
            assert "error" not in result, result.get("error")

            first._conn.commit()
            thread.join(10)
            assert not thread.is_alive(), "вторая запись не дождалась освобождения"
            assert result.get("ok"), result.get("error")
            assert any(
                row["content"] == "пока ждём" for row in second.recent_messages()
            )
        finally:
            first.close()
            second.close()
    print("ok  SQLite: вторая запись ждёт коммита и не теряется")


def test_schema_migration_race() -> None:
    """Два процесса одновременно дополняют старую схему и создают одну сессию."""
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "nemus.db")
        old = sqlite3.connect(db)
        old.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
            "content TEXT, tool_call_id TEXT, name TEXT, ts TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO messages (role, content, ts) VALUES ('user', 'старое', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        old.commit()
        old.close()

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        modes: list[str] = []

        def open_memory() -> None:
            try:
                barrier.wait(timeout=5)
                mem = Memory(db)
                modes.append(
                    str(mem._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                )
                mem.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=open_memory) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        assert not any(thread.is_alive() for thread in threads), "открытие зависло"
        assert not errors, errors
        assert modes == ["wal", "wal"], f"один процесс остался без WAL: {modes}"

        check = sqlite3.connect(db)
        try:
            columns = {
                row[1] for row in check.execute("PRAGMA table_info(messages)")
            }
            assert "tool_calls" in columns, columns
            assert "session_id" in columns, columns
            assert check.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert (
                check.execute("SELECT session_id FROM messages").fetchone()[0] == 1
            )
        finally:
            check.close()
    print("ok  SQLite: гонка ALTER TABLE и создания сессии безопасна")


def main() -> int:
    test_pragmas()
    test_writer_waits_for_lock()
    test_schema_migration_race()
    print("\nMEMORY CONCURRENCY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
