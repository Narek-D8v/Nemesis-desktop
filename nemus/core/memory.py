"""Память Немуси на SQLite: история диалогов и долговременные факты."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nemus.utils.logger import logger

#: Сколько ждать освобождения базы при параллельной записи (GUI + демон).
#: Дефолтные 5 секунд мало: тяжёлая история или checkpoint WAL легко
#: превышают их, и второй процесс получает ``database is locked``.
SQLITE_TIMEOUT = 30.0
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: название нового диалога (пока в нём нет реплик)
DEFAULT_SESSION_TITLE = "Новый диалог"
#: ключ текущего диалога во внутренней таблице ``meta``
_CURRENT_SESSION_KEY = "current_session_id"


def _set_current_id(conn: sqlite3.Connection, session_id: int) -> None:
    """Запоминает текущий диалог в ``meta`` — выбор переживает перезапуск."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_CURRENT_SESSION_KEY, str(session_id)),
    )


class Memory:
    """Потокобезопасное хранилище на SQLite (история + факты)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=SQLITE_TIMEOUT,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()
        self._current_session_id = self._ensure_session()

    def _configure(self) -> None:
        """WAL + busy_timeout: читатели не блокируют писателя, писатели ждут.

        WAL не отменяет конфликты записи (писатель в SQLite всегда один), но
        позволяет GUI читать историю, пока демон дописывает сообщение, и
        даёт обоим процессам 30 секунд на ожидание вместо мгновенной ошибки.
        """
        self._conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        mode = ""
        for _ in range(10):
            try:
                row = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
                mode = str(row[0]).lower() if row else "unknown"
            except sqlite3.DatabaseError:
                # Другой процесс в этот момент дописывает схему. Режим WAL
                # хранится в заголовке БД, поэтому сначала смотрим, не
                # включил ли его сосед, и только потом пробуем снова.
                try:
                    row = self._conn.execute("PRAGMA journal_mode").fetchone()
                    mode = str(row[0]).lower() if row else "unknown"
                except sqlite3.DatabaseError:
                    mode = ""
            if mode == "wal":
                break
            time.sleep(0.1)
        if mode != "wal":
            # Сетевые/FAT-разделы WAL не поддерживают — не падаем, но
            # предупреждаем: на них параллельная запись будет блокироваться.
            logger.warning(
                f"SQLite: WAL недоступен для {self.db_path} (режим {mode or 'unknown'}); "
                "параллельная запись может ждать дольше."
            )
        self._conn.execute("PRAGMA synchronous = NORMAL")

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
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    covered_until INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Миграция старых БД: колонки tool_calls и session_id появились
            # позже, а CREATE TABLE IF NOT EXISTS существующую таблицу
            # не дополняет.
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(messages)")
            }
            # Два процесса (GUI и демон) могут стартовать одновременно и
            # увидеть старую схему оба. Второй ALTER получит duplicate column —
            # это не ошибка: колонку уже добавил первый.
            if "tool_calls" not in columns:
                self._add_column("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
            if "session_id" not in columns:
                # старые сообщения достаются диалогу по умолчанию (id = 1)
                self._add_column(
                    "ALTER TABLE messages ADD COLUMN session_id "
                    "INTEGER NOT NULL DEFAULT 1"
                )

    def _add_column(self, sql: str) -> None:
        try:
            self._conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def _ensure_session(self) -> int:
        """Гарантирует наличие диалога и возвращает текущий его id.

        Существующие (старые) сообщения уже принадлежат сессии 1 — она и
        создаётся первой. Выбор диалога переживает перезапуск через ``meta``.
        """
        with self._lock:
            # BEGIN IMMEDIATE сериализует инициализацию двух процессов: без
            # него оба могут увидеть пустую таблицу и создать две сессии.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id FROM sessions ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    now = _now()
                    sid = int(
                        self._conn.execute(
                            "INSERT INTO sessions (title, created_at, updated_at) "
                            "VALUES (?, ?, ?)",
                            (DEFAULT_SESSION_TITLE, now, now),
                        ).lastrowid
                    )
                    _set_current_id(self._conn, sid)
                    self._conn.commit()
                    return sid
                stored = self._conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (_CURRENT_SESSION_KEY,)
                ).fetchone()
                try:
                    sid = int(stored["value"]) if stored else 0
                except (TypeError, ValueError):
                    sid = 0
                exists = self._conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", (sid,)
                ).fetchone()
                if exists is None:
                    sid = int(row["id"])
                    _set_current_id(self._conn, sid)
                self._conn.commit()
                return sid
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------- sessions
    @property
    def current_session_id(self) -> int:
        """id активного диалога (все записи сообщений идут в него)."""
        return self._current_session_id

    def current_session_title(self) -> str:
        return self.session_title(self._current_session_id) or DEFAULT_SESSION_TITLE

    def session_title(self, session_id: int) -> str:
        """Название диалога (пустая строка, если диалога нет)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (int(session_id),)
            ).fetchone()
        return str(row["title"]) if row else ""

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Диалоги по свежести (последний первый) со счётчиком сообщений."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) "
                "AS messages FROM sessions s "
                "ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_session(self, title: str = "") -> int:
        """Создаёт диалог и делает его текущим. Возвращает id."""
        name = (title or "").strip() or DEFAULT_SESSION_TITLE
        now = _now()
        with self._lock, self._conn:
            sid = int(
                self._conn.execute(
                    "INSERT INTO sessions (title, created_at, updated_at) "
                    "VALUES (?, ?, ?)",
                    (name, now, now),
                ).lastrowid
            )
            _set_current_id(self._conn, sid)
        self._current_session_id = sid
        return sid

    def switch_session(self, session_id: int) -> int:
        """Делает диалог ``session_id`` текущим. Несуществующий — ValueError."""
        sid = int(session_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Диалог {sid} не найден")
            _set_current_id(self._conn, sid)
        self._current_session_id = sid
        return sid

    def rename_session(self, session_id: int, title: str) -> None:
        """Переименовывает диалог (пустое имя игнорируется)."""
        name = (title or "").strip()
        if not name:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (name, _now(), int(session_id)),
            )

    def auto_title(self, text: str, max_len: int = 48) -> None:
        """Называет текущий диалог по первой реплике пользователя.

        Срабатывает только пока у диалога название по умолчанию —
        вручное переименование и названия от создателя не затираются.
        """
        text = " ".join((text or "").split())
        if not text:
            return
        short = text if len(text) <= max_len else text[: max_len - 1] + "…"
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?",
                (self._current_session_id,),
            ).fetchone()
            if row is None or row["title"] != DEFAULT_SESSION_TITLE:
                return
            self._conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (short, _now(), self._current_session_id),
            )

    def delete_session(self, session_id: int) -> int:
        """Удаляет диалог вместе с его сообщениями.

        Если удалён текущий — текущим становится самый свежий из
        оставшихся (либо создаётся новый). Возвращает id текущего диалога.
        """
        sid = int(session_id)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            self._conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ?", (sid,)
            )
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            if sid == self._current_session_id:
                row = self._conn.execute(
                    "SELECT id FROM sessions ORDER BY updated_at DESC, id DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    now = _now()
                    new_id = int(
                        self._conn.execute(
                            "INSERT INTO sessions (title, created_at, updated_at) "
                            "VALUES (?, ?, ?)",
                            (DEFAULT_SESSION_TITLE, now, now),
                        ).lastrowid
                    )
                else:
                    new_id = int(row["id"])
                _set_current_id(self._conn, new_id)
                self._current_session_id = new_id
                return new_id
        return self._current_session_id

    # ------------------------------------------------------------- messages
    def add_message(
        self,
        role: str,
        content: str | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        tool_calls: Any = None,
    ) -> int:
        """Сохраняет сообщение и возвращает id строки в SQLite.

        Без tool_calls результаты инструментов (role=tool) при следующем
        старте загрузились бы «осиротевшими», и провайдеры отвергли бы
        весь запрос — см. ``Agent._load_history`` и ``load_messages_into``.
        с текущим диалогом (``session_id``), а свежесть диалога обновляется
        для порядка в списке сессий.
        """
        ts = _now()
        with self._lock, self._conn:
            rowid = self._conn.execute(
                "INSERT INTO messages "
                "(role, content, tool_calls, tool_call_id, name, session_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    role,
                    content,
                    _dump_tool_calls(tool_calls),
                    tool_call_id,
                    name,
                    self._current_session_id,
                    ts,
                ),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (ts, self._current_session_id),
            )
            return int(rowid.lastrowid)

    def recent_messages(self, limit: int = 40, after_id: int = 0) -> list[dict]:
        """Последние сообщения **текущего** диалога (в хронологии).

        ``after_id`` ограничивает выборку сообщениями с id больше границы —
        так после рестарта догружается всё, что не попало в выжимку.
        ``limit <= 0`` означает «без ограничения».
        """
        sql = (
            "SELECT id, role, content, tool_calls, tool_call_id, name FROM messages "
            "WHERE session_id = ?"
        )
        params: list[Any] = [self._current_session_id]
        if after_id:
            sql += " AND id > ?"
            params.append(int(after_id))
        sql += " ORDER BY id DESC"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_messages(self) -> None:
        """Очищает текущий диалог и его выжимку (остальные не трогает)."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (self._current_session_id,),
            )
            self._conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ?",
                (self._current_session_id,),
            )

    # ------------------------------------------------------------ summaries
    def get_summary(self, session_id: int | None = None) -> tuple[str, int] | None:
        """Возвращает ``(выжимка, covered_until)`` для диалога или ``None``."""
        sid = self._current_session_id if session_id is None else int(session_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT summary, covered_until FROM session_summaries "
                "WHERE session_id = ?",
                (sid,),
            ).fetchone()
        if row is None or not str(row["summary"] or "").strip():
            return None
        return str(row["summary"]), int(row["covered_until"] or 0)

    def set_summary(
        self,
        summary: str,
        covered_until: int,
        session_id: int | None = None,
    ) -> None:
        """Сохраняет выжимку и id последнего вошедшего в неё сообщения."""
        text = (summary or "").strip()
        if not text:
            return
        sid = self._current_session_id if session_id is None else int(session_id)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO session_summaries "
                "(session_id, summary, covered_until, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "summary = excluded.summary, "
                "covered_until = excluded.covered_until, "
                "updated_at = excluded.updated_at",
                (sid, text, int(covered_until), _now()),
            )

    def clear_summary(self, session_id: int | None = None) -> None:
        """Удаляет выжимку диалога (при сбросе/очистке)."""
        sid = self._current_session_id if session_id is None else int(session_id)
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ?", (sid,)
            )

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Читает служебное значение (например, session_id Telegram-чата)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

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
                tool_calls=_load_tool_calls(row.get("tool_calls")),
                tool_call_id=row["tool_call_id"],
                name=row["name"],
                db_id=row.get("id"),
            )
        )


def _dump_tool_calls(tool_calls: Any) -> str | None:
    """Сериализует tool-calls ассистента в JSON для колонки ``tool_calls``."""
    if not tool_calls:
        return None
    out: list[dict[str, Any]] = []
    for call in tool_calls:
        if isinstance(call, dict):
            out.append(
                {
                    "id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "arguments": call.get("arguments") or {},
                }
            )
        else:
            out.append(
                {"id": call.id, "name": call.name, "arguments": call.arguments}
            )
    return json.dumps(out, ensure_ascii=False)


def _load_tool_calls(raw: Any) -> list | None:
    """Восстанавливает tool-calls из JSON колонки (или ``None``)."""
    if not raw:
        return None
    from nemus.core.providers import ToolCall

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    calls = [
        ToolCall(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            arguments=item.get("arguments") or {},
        )
        for item in data
        if isinstance(item, dict) and item.get("name")
    ]
    return calls or None
