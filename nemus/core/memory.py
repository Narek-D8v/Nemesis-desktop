"""Память Немуси на SQLite: история диалогов и долговременные факты."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    """Потокобезопасное хранилище на SQLite (история + факты)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    ts TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    ts TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------- messages
    def add_message(
        self,
        role: str,
        content: str | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages (role, content, tool_call_id, name, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (role, content, tool_call_id, name, _now()),
            )

    def recent_messages(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id, name FROM messages "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_messages(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM messages")

    # ----------------------------------------------------------------- facts
    def set_fact(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO facts (key, value, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, value, _now()),
            )

    def get_fact(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM facts WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def all_facts(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM facts").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def load_messages_into(history: list, rows: list[dict]) -> None:
    """Преобразует строки БД в объекты Message и добавляет в историю."""
    from nemus.core.providers import Message

    for row in rows:
        history.append(
            Message(
                role=row["role"],
                content=row["content"],
                tool_call_id=row["tool_call_id"],
                name=row["name"],
            )
        )
