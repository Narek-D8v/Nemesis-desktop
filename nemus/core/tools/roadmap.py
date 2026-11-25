"""Дополнительные инструменты «дорожной карты»: обзор, контекст и утилиты.

Все инструменты работают локально: ничего не скачивается, LLM не вызывается.
Внешние источники (погода, музыка, уведомления) подключаются только через
внедрённые callable-объекты — без них инструмент честно сообщает, что источник
не настроен.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from nemus.config import PROJECT_ROOT
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.clipboard import get_clipboard

_CALENDAR_KEY = "calendar_events"
_CLIPBOARD_KEY = "clipboard_history"
_WATCH_KEY = "watch_targets"
_POLLS_KEY = "polls"
_SOFT_MEMORY_KEY = "soft_memory"
_ARTICLES_KEY = "article_sessions"
_TUTORIALS_KEY = "tutorials"
_SEARCH_KEY = "search_history"

_STOPWORDS = frozenset(
    """
    и а но или да же бы ли не ни что как это тот эта это он она оно они мы вы я ты
    мне мне тебя его её их наш ваш мой твой в во на за по от до из с со к ко у о об
    для при над под про без через между после перед не над очень тут там тогда когда
    чтобы если потому поэтому так также уже ещё только просто всего будет был была
    были есть был бы с;


    the a an and or but if then than this that these those it its is are was were be
    been to of in on at for with by from as about into over under not no yes do does
    did have has had you your we our they their he she i me my so just very more most
    """.split()
)
_DECISION_MARKERS = ("решено", "решили", "итог", "финал", "done", "decided")
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".pyc",
        ".so",
        ".dll",
        ".exe",
        ".zip",
        ".pdf",
        ".mp3",
        ".mp4",
        ".woff",
        ".woff2",
        ".ttf",
        ".db",
        ".sqlite",
        ".ogg",
        ".wav",
    }
)
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", "venv", ".venv"})


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_result(payload: Any) -> ToolResult:
    return ToolResult.success(_dumps(payload), data=payload)


def _norm(text: Any) -> str:
    return str(text or "").casefold().replace("ё", "е")


def _tokens(text: Any) -> list[str]:
    return [t for t in re.findall(r"[\w-]+", _norm(text)) if len(t) > 2]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip().replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expand(path: Any, base: Path | None = None) -> Path:
    target = Path(str(path)).expanduser()
    if not target.is_absolute():
        target = (base or PROJECT_ROOT) / target
    return target


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def _slug(value: Any, default: str = "item") -> str:
    raw = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", str(value or "")).strip("_")
    return raw[:40] or default


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _text_of(item: Any) -> str:
    """Приводит запись памяти/заметок/дневника к тексту."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("content", "text", "message", "body", "snippet"):
            if item.get(key):
                return str(item[key]).strip()
        return ""
    for attr in ("text", "content", "message", "body"):
        value = getattr(item, attr, None)
        if value:
            return str(value).strip()
    return str(item).strip() if item else ""


class RoadmapStore:
    """Небольшое JSON-хранилище для инструментов дорожной карты.

    Каждый инструмент получает отдельный файл в ``base_dir / "roadmap"``.
    Запись атомарна (временный файл + ``os.replace``), чтение и запись
    защищены переentrant-блокировкой.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        self.directory = root / "roadmap"
        self._lock = threading.RLock()

    def _path(self, name: str) -> Path:
        safe = _slug(name, "store").replace("/", "_").replace("\\", "_")
        return self.directory / f"{safe}.json"

    def load(self, name: str, default: Any) -> Any:
        """Возвращает сохранённые данные или deepcopy значения ``default``."""
        path = self._path(name)
        with self._lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return copy.deepcopy(default)

    def save(self, name: str, data: Any) -> None:
        """Атомарно записывает данные в файл инструмента."""
        path = self._path(name)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".roadmap-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


class _StoredTool(Tool):
    """Общий предок инструментов с локальным хранилищем."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        self.store = RoadmapStore(self.base_dir)


# ---------------------------------------------------------------------------
#  Обзор окружения
# ---------------------------------------------------------------------------
def _enum_windows() -> list[dict[str, Any]]:
    """Перечисляет окна Windows через user32; при ошибке возвращает []."""
    if sys.platform != "win32":  # pragma: no cover - зависит от ОС
        return []
    try:  # noqa: BLE001 - внешний WinAPI не должен ронять инструмент
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found: list[dict[str, Any]] = []

        def collect(hwnd: int, _: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if title:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append({"title": title, "pid": int(pid.value)})
            return True

        user32.EnumWindows(callback_type(collect), 0)
        return found
    except Exception:
        return []


def _window_kind(title: str) -> str:
    lowered = _norm(title)
    if any(
        word in lowered
        for word in ("code", "editor", "python", "vscode", "pycharm", "nemesis", "terminal")
    ):
        return "code"
    if any(word in lowered for word in ("steam", "game", "cs2", "dota", "minecraft")):
        return "game"
    if any(word in lowered for word in ("docs", "chat", "mail")):
        return "work"
    return "other"


class InspectScreenTool(Tool):
    """Перечисляет заголовки открытых окон (без скриншотов)."""

    name = "inspect_screen"
    description = (
        "Показывает список открытых окон с PID и типом (code/game/work/other). Скриншоты не делает."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Сколько окон вернуть, по умолчанию 30.",
            }
        },
    }
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        windows: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        self.windows = windows or _enum_windows

    def execute(self, limit: int = 30, **_: Any) -> ToolResult:
        try:
            raw = self.windows() or []
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось перечислить окна: {exc}")
        items = [
            {
                "title": str(entry.get("title", "")),
                "pid": _as_int(entry.get("pid", 0), 0),
                "kind": _window_kind(str(entry.get("title", ""))),
            }
            for entry in raw
            if isinstance(entry, dict)
        ][: max(0, _as_int(limit, 30))]
        return _json_result({"count": len(items), "items": items})


class ClipboardHistoryTool(_StoredTool):
    """История текста буфера обмена с поиском и очисткой."""

    name = "clipboard_history"
    description = (
        "Читает текст буфера обмена и ведёт историю: read/history/clear, "
        "поиск по истории за последние часы."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "history", "clear"],
                "description": "Действие, по умолчанию history.",
            },
            "query": {"type": "string", "description": "Подстрока для поиска."},
            "hours": {
                "type": "integer",
                "description": "Глубина истории в часах, по умолчанию 24.",
            },
            "limit": {"type": "integer", "description": "Сколько записей вернуть."},
        },
    }
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        getter: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(base_dir)
        self.getter = getter or get_clipboard

    def execute(
        self,
        action: str = "history",
        query: str = "",
        hours: int = 24,
        limit: int = 50,
        **_: Any,
    ) -> ToolResult:
        entries = self.store.load(_CLIPBOARD_KEY, [])
        if not isinstance(entries, list):
            entries = []
        action = str(action or "history").lower()

        if action == "read":
            try:
                text = str(self.getter() or "")
            except Exception as exc:  # noqa: BLE001
                return ToolResult.failure(f"Не удалось прочитать буфер: {exc}")
            if not text.strip():
                return _json_result({"action": "read", "text": "", "added": False})
            if not entries or not isinstance(entries[0], dict) or entries[0].get("text") != text:
                entries.insert(0, {"ts": _now_iso(), "text": text})
                self.store.save(_CLIPBOARD_KEY, entries)
                return _json_result({"action": "read", "text": text, "added": True})
            return _json_result({"action": "read", "text": text, "added": False})

        if action == "clear":
            removed = len(entries)
            self.store.save(_CLIPBOARD_KEY, [])
            return _json_result({"action": "clear", "removed": removed})

        if action != "history":
            return ToolResult.failure(f"Неизвестное действие: {action}")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, _as_int(hours, 24)))
        needle = _norm(query)
        found = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            when = _parse_ts(entry.get("ts"))
            if when is None or when < cutoff:
                continue
            text = str(entry.get("text", ""))
            if needle and needle not in _norm(text):
                continue
            found.append(
                {
                    "ts": entry.get("ts"),
                    "text": text[:200] + ("…" if len(text) > 200 else ""),
                }
            )
        found = found[: max(0, _as_int(limit, 50))]
        return _json_result({"action": "history", "count": len(found), "items": found})


class ProjectCleanupTool(Tool):
    """Показывает и по подтверждению удаляет старые временные файлы."""

    name = "project_cleanup"
    description = (
        "Находит старые *.log/*.tmp/*.bak и каталоги __pycache__/.pytest_cache/"
        "node_modules. preview=true только показывает список; для удаления "
        "нужны preview=false и confirm=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Каталог очистки."},
            "preview": {
                "type": "boolean",
                "description": "Только показать кандидатов, по умолчанию true.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Подтверждение удаления, по умолчанию false.",
            },
            "days": {
                "type": "integer",
                "description": "Возраст файлов в днях, по умолчанию 7.",
            },
        },
    }
    risk = RiskLevel.MEDIUM

    def __init__(
        self,
        base_dir: str | Path | None = None,
        allowed_roots: Any = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        roots = (PROJECT_ROOT,) if allowed_roots is None else _as_list(allowed_roots)
        self.allowed_roots = tuple(_expand(item).resolve() for item in roots)

    def _candidates(self, root: Path, days: int) -> tuple[list[Path], list[Path]]:
        cutoff = time.time() - max(0, days) * 86400
        dirs: list[Path] = []
        files: list[Path] = []
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(current)
            for name in list(dirnames):
                path = base / name
                if path.is_symlink():
                    dirnames.remove(name)
                    continue
                if name in {"__pycache__", ".pytest_cache", "node_modules"}:
                    try:
                        if path.stat().st_mtime < cutoff:
                            dirs.append(path)
                    except OSError:
                        pass
            for name in filenames:
                if not name.endswith((".log", ".tmp", ".bak")):
                    continue
                path = base / name
                if path.is_symlink():
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        files.append(path)
                except OSError:
                    pass
        return sorted(set(dirs)), sorted(set(files))

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames[:] = [d for d in dirnames if not (Path(current) / d).is_symlink()]
            for name in filenames:
                child = Path(current) / name
                if child.is_symlink():
                    continue
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
        return total

    def execute(
        self,
        root: str = "",
        preview: bool = True,
        confirm: bool = False,
        days: int = 7,
        **_: Any,
    ) -> ToolResult:
        target = _expand(root or PROJECT_ROOT).resolve()
        if not target.is_dir():
            return ToolResult.failure(f"Каталог не найден: {target}")
        if not _within(target, self.allowed_roots):
            return ToolResult.failure(f"Каталог вне разрешённых корней: {target}")
        dirs, files = self._candidates(target, _as_int(days, 7))
        files = [
            path
            for path in files
            if not any(path == directory or directory in path.parents for directory in dirs)
        ]
        candidates = [str(path) for path in [*dirs, *files]]
        freed = sum(self._dir_size(path) for path in dirs) + sum(
            path.stat().st_size for path in files if path.exists()
        )
        payload: dict[str, Any] = {
            "root": str(target),
            "candidates": candidates,
            "count": len(candidates),
            "freed_bytes": freed,
        }

        if preview:
            return _json_result({**payload, "preview": True, "removed": 0})
        if not confirm:
            return ToolResult.failure(
                "Удаление требует подтверждения: preview=false, confirm=true."
            )

        removed = 0
        for path in dirs:
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError:
                pass
        for path in files:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return _json_result({**payload, "preview": False, "removed": removed})


class BackupWorkspaceTool(_StoredTool):
    """Создаёт локальный zip-архив рабочего пространства."""

    name = "backup_workspace"
    description = (
        "Упаковывает указанные каталоги в data/backups/<label>-<timestamp>.zip "
        "и удаляет старые архивы сверх keep. Без сети."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Каталоги для архивации.",
            },
            "label": {"type": "string", "description": "Метка архива, по умолчанию auto."},
            "keep": {"type": "integer", "description": "Сколько архивов держать, по умолчанию 5."},
        },
    }
    risk = RiskLevel.MEDIUM

    def execute(
        self,
        paths: Any = None,
        label: str = "auto",
        keep: int = 5,
        **_: Any,
    ) -> ToolResult:
        raw = _as_list(paths)
        if not raw:
            raw = ["config", "memory", "skills", "data"]
        targets = [_expand(item) for item in raw]
        existing = [path for path in targets if path.exists()]
        if not existing:
            return ToolResult.failure("Нет каталогов для архивации.")

        backup_dir = self.base_dir / "backups"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            archive = backup_dir / f"{_slug(label, 'auto')}-{_stamp()}.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in existing:
                    if path.is_file():
                        zf.write(path, path.name)
                        continue
                    for current, dirnames, filenames in os.walk(path, followlinks=False):
                        base = Path(current)
                        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
                        for name in filenames:
                            child = base / name
                            if child.is_symlink():
                                continue
                            zf.write(child, child.relative_to(path.parent))
            size = archive.stat().st_size
        except (OSError, zipfile.BadZipFile) as exc:
            return ToolResult.failure(f"Не удалось создать архив: {exc}")

        old = sorted(backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[max(1, _as_int(keep, 5)) :]:
            try:
                stale.unlink()
            except OSError:
                pass
        return _json_result(
            {
                "archive": str(archive),
                "size": size,
                "paths": [str(path) for path in existing],
            }
        )


# ---------------------------------------------------------------------------
#  Контекст разговора
# ---------------------------------------------------------------------------
class CatchupSummaryTool(_StoredTool):
    """Сводит последние сообщения в три фиксированных абзаца."""

    name = "catchup_summary"
    description = (
        "Делает короткий обзор последних сообщений: обсуждение, решения, "
        "открытые вопросы. Работает эвристиками, без LLM."
    )
    parameters = {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Сообщения {role, content, ts?}; если не заданы — из памяти.",
            },
            "after_id": {
                "type": "integer",
                "description": "Начать после указанного id сообщения.",
            },
            "limit": {
                "type": "integer",
                "description": "Сколько сообщений взять, по умолчанию 200.",
            },
        },
    }
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        memory: Any = None,
    ) -> None:
        super().__init__(base_dir)
        self.memory = memory

    def _rows(self, messages: Any, after_id: int, limit: int) -> list[tuple[str, str]]:
        if messages is not None:
            source = _as_list(messages)
        elif self.memory is not None and hasattr(self.memory, "recent_messages"):
            try:
                source = self.memory.recent_messages(after_id=after_id, limit=limit)
            except TypeError:
                source = self.memory.recent_messages(limit=limit)
        else:
            source = []
        rows: list[tuple[str, str]] = []
        for item in source[: max(0, limit)]:
            if isinstance(item, dict):
                text = str(item.get("content") or item.get("text") or "")
                role = str(item.get("role") or "user")
                message_id = _as_int(item.get("id", 0), 0)
                if message_id and message_id <= after_id:
                    continue
            else:
                text, role = _text_of(item), "user"
            if text.strip():
                rows.append((role, text.strip()))
        return rows

    def execute(
        self,
        messages: Any = None,
        after_id: int = 0,
        limit: int = 200,
        **_: Any,
    ) -> ToolResult:
        rows = self._rows(messages, _as_int(after_id, 0), _as_int(limit, 200))
        texts = [text for role, text in rows if role in ("user", "assistant")]
        words = [
            word
            for text in texts
            for word in _tokens(text)
            if word not in _STOPWORDS and len(word) > 3
        ]
        top = [word for word, _ in Counter(words).most_common(5)]

        digest = "; ".join(text.replace("\n", " ")[:140] for text in texts[:4])
        if top:
            first = (
                f"Обсуждалось: {digest or 'сообщений не было'}. Ключевые темы: {', '.join(top)}."
            )
        else:
            first = f"Обсуждалось: {digest or 'сообщений не было'}."

        decisions: list[str] = []
        questions: list[str] = []
        for text in texts:
            for line in text.splitlines():
                clean = line.strip(" -•")
                if not clean:
                    continue
                if clean.casefold().startswith(_DECISION_MARKERS):
                    decisions.append(clean[:160])
                if "?" in clean or "？" in clean:
                    questions.append(clean[:160])
        second = (
            "Решения и факты: " + "; ".join(decisions[:5])
            if decisions
            else "Решения и факты: новых зафиксированных решений нет."
        )
        third = (
            "Открытые вопросы и следующие шаги: " + "; ".join(questions[:5])
            if questions
            else "Открытые вопросы и следующие шаги: уточнить следующий шаг."
        )
        paragraphs = [first, second + ".", third + "."]
        return ToolResult.success("\n\n".join(paragraphs), data={"paragraphs": paragraphs})


class SearchHistoryTool(_StoredTool):
    """Ищет по памяти, заметкам, дневнику и консолидированному хранилищу."""

    name = "search_history"
    description = (
        "Ищет фрагменты по истории сообщений, заметкам и дневнику. "
        "Оценка по совпадению токенов и подстроке, без эмбеддингов."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Что найти."},
            "limit": {"type": "integer", "description": "Сколько результатов, по умолчанию 10."},
        },
        "required": ["query"],
    }
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        memory: Any = None,
        notes: Any = None,
        diary: Any = None,
    ) -> None:
        super().__init__(base_dir)
        self.memory = memory
        self.notes = notes
        self.diary = diary

    def _sources(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        if self.memory is not None and hasattr(self.memory, "recent_messages"):
            for item in _as_list(self.memory.recent_messages(limit=0)):
                text = _text_of(item)
                if text:
                    rows.append(("memory", text))
        if self.notes is not None and hasattr(self.notes, "notes"):
            for item in _as_list(self.notes.notes()):
                text = _text_of(item)
                if text:
                    rows.append(("notes", text))
        if self.diary is not None and hasattr(self.diary, "recent"):
            for item in _as_list(self.diary.recent()):
                text = _text_of(item)
                if text:
                    rows.append(("diary", text))
        for item in _as_list(self.store.load(_SEARCH_KEY, [])):
            if isinstance(item, dict):
                text = _text_of(item)
                source = str(item.get("source") or "store")
            else:
                text, source = _text_of(item), "store"
            if text:
                rows.append((source, text))
        return rows

    def execute(self, query: str = "", limit: int = 10, **_: Any) -> ToolResult:
        needle = _norm(query)
        if not needle:
            return ToolResult.failure("Пустой запрос.")
        tokens = _tokens(query)
        scored: list[dict[str, Any]] = []
        for source, text in self._sources():
            lowered = _norm(text)
            score = sum(1 for token in tokens if token in lowered) / max(1, len(tokens))
            if needle in lowered:
                score += 1.0
            if score <= 0:
                continue
            scored.append(
                {
                    "source": source,
                    "snippet": text[:300] + ("…" if len(text) > 300 else ""),
                    "score": round(score, 3),
                }
            )
        scored.sort(key=lambda row: row["score"], reverse=True)
        scored = scored[: max(0, _as_int(limit, 10))]
        return _json_result(scored)


# ---------------------------------------------------------------------------
#  Планирование и погода
# ---------------------------------------------------------------------------
class CalendarTool(_StoredTool):
    """Локальный календарь событий в JSON-хранилище."""

    name = "calendar"
    description = (
        "Добавляет, показывает и отменяет локальные события: "
        "add/list/upcoming/cancel. Даты — ISO 8601."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "upcoming", "cancel"],
                "description": "Действие, по умолчанию list.",
            },
            "title": {"type": "string", "description": "Название события."},
            "start": {"type": "string", "description": "Начало в ISO 8601."},
            "end": {"type": "string", "description": "Окончание в ISO 8601."},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "id": {"type": "string", "description": "Идентификатор для cancel."},
            "days": {
                "type": "integer",
                "description": "Горизонт для upcoming в днях, по умолчанию 7.",
            },
        },
    }
    risk = RiskLevel.SAFE

    @staticmethod
    def _sort_key(event: dict[str, Any]) -> str:
        return str(_parse_ts(event.get("start")) or datetime.max.replace(tzinfo=timezone.utc))

    def execute(
        self,
        action: str = "list",
        title: str = "",
        start: str = "",
        end: str = "",
        attendees: Any = None,
        id: str = "",
        days: int = 7,
        **_: Any,
    ) -> ToolResult:
        events = self.store.load(_CALENDAR_KEY, [])
        if not isinstance(events, list):
            events = []
        action = str(action or "list").lower()

        if action == "add":
            if not title.strip() or not start.strip():
                return ToolResult.failure("Нужны title и start (ISO 8601).")
            if _parse_ts(start) is None:
                return ToolResult.failure(f"Неверная дата start: {start}")
            if end and _parse_ts(end) is None:
                return ToolResult.failure(f"Неверная дата end: {end}")
            event = {
                "id": uuid.uuid4().hex[:8],
                "title": title.strip(),
                "start": start.strip(),
                "end": end.strip(),
                "attendees": [str(item) for item in _as_list(attendees)],
            }
            events.append(event)
            self.store.save(_CALENDAR_KEY, events)
            return _json_result({"action": "add", "event": event, "id": event["id"]})

        if action in ("list", "upcoming"):
            if action == "upcoming":
                now = datetime.now(timezone.utc)
                horizon = now + timedelta(days=max(0, _as_int(days, 7)))
                events = [
                    event
                    for event in events
                    if (begin := _parse_ts(event.get("start"))) is not None
                    and now <= begin <= horizon
                ]
            events.sort(key=self._sort_key)
            return _json_result({"action": action, "count": len(events), "items": events})

        if action == "cancel":
            kept = [event for event in events if str(event.get("id")) != str(id)]
            removed = len(events) - len(kept)
            if removed:
                self.store.save(_CALENDAR_KEY, kept)
            return _json_result({"action": "cancel", "removed": removed, "id": id})

        return ToolResult.failure(f"Неизвестное действие: {action}")


class WeatherTool(Tool):
    """Погода только через внедрённый provider — без сетевых запросов."""

    name = "weather"
    description = (
        "Возвращает погоду для места через подключённый источник. "
        "Без источника сообщает, что погода не настроена."
    )
    parameters = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "Местоположение, по умолчанию home.",
            }
        },
    }
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        provider: Callable[..., Any] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        self.provider = provider

    def _call(self, location: str) -> Any:
        if self.provider is None:
            return None
        try:
            signature = inspect.signature(self.provider)
            positional = [
                param
                for param in signature.parameters.values()
                if param.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            wide = any(
                param.kind is inspect.Parameter.VAR_POSITIONAL
                for param in signature.parameters.values()
            )
        except (TypeError, ValueError):
            positional, wide = [], False
        if len(positional) >= 2 or wide:
            return self.provider(self.base_dir, location)
        return self.provider(location)

    def execute(self, location: str = "home", **_: Any) -> ToolResult:
        if self.provider is None:
            return ToolResult.failure(
                "Погода не настроена: передай provider(base_dir, location) "
                "или настрой API в конфиге"
            )
        try:
            value = self._call(location or "home")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось получить погоду: {exc}")
        if isinstance(value, dict):
            return _json_result(value)
        return ToolResult.success(str(value), data=value)


class RepoGuideTool(Tool):
    """Компактная карта репозитория: каталоги, точки входа, тесты, символы."""

    name = "repo_guide"
    description = "Показывает структуру проекта, точки входа, тесты и имена классов/функций."
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Корень проекта."},
            "limit": {"type": "integer", "description": "Максимум файлов, по умолчанию 400."},
        },
    }
    risk = RiskLevel.SAFE

    _SYMBOL = re.compile(r"^(?:class|def|async def)\s+(\w+)", re.MULTILINE)

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"

    def execute(self, root: str = "", limit: int = 400, **_: Any) -> ToolResult:
        base = _expand(root or PROJECT_ROOT).resolve()
        if not base.is_dir():
            return ToolResult.failure(f"Каталог не найден: {base}")
        cap = max(1, _as_int(limit, 400))
        dirs: set[str] = set()
        entrypoints: list[str] = []
        tests: list[str] = []
        symbols: list[str] = []
        scanned = 0
        for current, dirnames, filenames in os.walk(base, followlinks=False):
            folder = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_DIRS and not (folder / name).is_symlink()
            )
            relative = folder.relative_to(base)
            if relative.parts:
                dirs.add(relative.parts[0])
            if scanned >= cap:
                continue
            for name in sorted(filenames):
                if scanned >= cap:
                    break
                path = folder / name
                if path.is_symlink() or path.suffix.lower() in _BINARY_SUFFIXES:
                    continue
                rel = path.relative_to(base).as_posix()
                scanned += 1
                if name.startswith("README") or name in {
                    "main.py",
                    "__main__.py",
                    "pyproject.toml",
                    "package.json",
                }:
                    entrypoints.append(rel)
                if name.startswith("test_") or name.endswith("_test.py"):
                    tests.append(rel)
                if path.suffix.lower() == ".py" and len(symbols) < 200:
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    symbols.extend(self._SYMBOL.findall(text))
        payload = {
            "root": str(base),
            "dirs": sorted(dirs),
            "entrypoints": sorted(set(entrypoints)),
            "tests": sorted(set(tests))[:200],
            "symbols": symbols[:200],
            "files_scanned": scanned,
        }
        return _json_result(payload)


class ChartTool(_StoredTool):
    """Строит локальный PNG-график через matplotlib (Agg)."""

    name = "chart"
    description = "Строит график bar/line/pie по спискам labels и values и сохраняет PNG."
    parameters = {
        "type": "object",
        "properties": {
            "labels": {"type": "array", "items": {"type": "string"}},
            "values": {"type": "array", "items": {}},
            "kind": {
                "type": "string",
                "enum": ["bar", "line", "pie"],
                "description": "Тип графика, по умолчанию bar.",
            },
            "title": {"type": "string"},
            "output": {"type": "string", "description": "Путь к PNG (опционально)."},
        },
        "required": ["labels", "values"],
    }
    risk = RiskLevel.MEDIUM

    def execute(
        self,
        labels: Any = None,
        values: Any = None,
        kind: str = "bar",
        title: str = "",
        output: str = "",
        **_: Any,
    ) -> ToolResult:
        labels = [str(item) for item in _as_list(labels)]
        raw_values = _as_list(values)
        if not labels or len(labels) != len(raw_values):
            return ToolResult.failure("labels и values должны быть одинаковой длины.")
        numbers = []
        for item in raw_values:
            try:
                numbers.append(float(str(item).strip().replace("%", "").replace(",", ".")))
            except ValueError:
                return ToolResult.failure(f"Неверное число: {item}")
        kind = str(kind or "bar").lower()
        if kind not in {"bar", "line", "pie"}:
            return ToolResult.failure(f"Неизвестный тип графика: {kind}")
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
        except ImportError:
            return ToolResult.failure(
                "matplotlib не установлен: установите matplotlib, чтобы строить графики."
            )

        figure, axes = plt.subplots(figsize=(8, 4.5))
        try:
            if kind == "bar":
                axes.bar(labels, numbers)
            elif kind == "line":
                axes.plot(labels, numbers, marker="o")
            else:
                axes.pie(numbers, labels=labels, autopct="%1.1f%%")
            axes.set_title(str(title or ""))
            figure.tight_layout()
            if output:
                target = _expand(output, self.base_dir)
            else:
                charts = self.base_dir / "charts"
                charts.mkdir(parents=True, exist_ok=True)
                target = charts / f"{_slug(title, 'chart')}-{_stamp()}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            figure.savefig(target, format="png")
        except (OSError, ValueError) as exc:
            return ToolResult.failure(f"Не удалось сохранить график: {exc}")
        finally:
            plt.close(figure)
        return ToolResult.success(f"График сохранён: {target}", data={"path": str(target)})


class SoftMemoryTool(_StoredTool):
    """Мягкая память с важностью, тегами и фильтрацией по времени."""

    name = "soft_memory"
    description = (
        "Запоминает заметки с тегами/эмоцией/важностью, вспоминает по запросу "
        "за N дней и забывает по id или подстроке."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remember", "recall", "forget"],
                "description": "Действие, по умолчанию recall.",
            },
            "text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "emotion": {"type": "string"},
            "importance": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Важность от 1 до 5, по умолчанию 3.",
            },
            "days": {"type": "integer", "description": "Глубина recall, по умолчанию 21."},
            "query": {"type": "string", "description": "Подстрока для поиска."},
            "tag": {"type": "string", "description": "Тег для фильтра."},
            "id": {"type": "string", "description": "Идентификатор для forget."},
        },
    }
    risk = RiskLevel.SAFE

    def execute(
        self,
        action: str = "recall",
        text: str = "",
        tags: Any = None,
        emotion: str = "",
        importance: int = 3,
        days: int = 21,
        query: str = "",
        tag: str = "",
        id: str = "",
        **_: Any,
    ) -> ToolResult:
        entries = self.store.load(_SOFT_MEMORY_KEY, [])
        if not isinstance(entries, list):
            entries = []
        action = str(action or "recall").lower()

        if action == "remember":
            if not str(text).strip():
                return ToolResult.failure("Пустой текст для запоминания.")
            entry = {
                "id": uuid.uuid4().hex[:8],
                "ts": _now_iso(),
                "text": str(text).strip(),
                "tags": [str(item) for item in _as_list(tags)],
                "emotion": str(emotion or ""),
                "importance": min(5, max(1, _as_int(importance, 3))),
            }
            entries.append(entry)
            self.store.save(_SOFT_MEMORY_KEY, entries)
            return _json_result({"action": "remember", "entry": entry})

        if action == "recall":
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, _as_int(days, 21)))
            needle, wanted_tag = _norm(query), _norm(tag)
            found = []
            for entry in entries:
                when = _parse_ts(entry.get("ts")) if isinstance(entry, dict) else None
                if when is None or when < cutoff:
                    continue
                body = str(entry.get("text", "")) if isinstance(entry, dict) else ""
                entry_tags = (
                    [str(item) for item in _as_list(entry.get("tags"))]
                    if isinstance(entry, dict)
                    else []
                )
                if (
                    needle
                    and needle not in _norm(body)
                    and not any(needle in _norm(item) for item in entry_tags)
                ):
                    continue
                if wanted_tag and not any(wanted_tag == _norm(item) for item in entry_tags):
                    continue
                found.append(entry)
            found.sort(
                key=lambda item: (
                    _as_int(item.get("importance", 3), 3),
                    str(_parse_ts(item.get("ts")) or ""),
                ),
                reverse=True,
            )
            return _json_result({"action": "recall", "count": len(found), "items": found})

        if action == "forget":
            needle = _norm(query)
            kept = []
            removed = 0
            for entry in entries:
                matches_id = bool(id) and str(entry.get("id")) == str(id)
                matches_text = bool(needle) and needle in _norm(entry.get("text", ""))
                if matches_id or matches_text:
                    removed += 1
                else:
                    kept.append(entry)
            if removed:
                self.store.save(_SOFT_MEMORY_KEY, kept)
            return _json_result({"action": "forget", "removed": removed})

        return ToolResult.failure(f"Неизвестное действие: {action}")


def _render(value: Any) -> str:
    if value is None:
        return "не подключено"
    if isinstance(value, str):
        return value.strip() or "нет данных"
    if isinstance(value, dict):
        return "\n".join(f"- {key}: {item}" for key, item in value.items()) or "нет данных"
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {_text_of(item)}" for item in value) or "нет данных"
    return str(value)


class MorningDashboardTool(Tool):
    """Утренний дашборд из четырёх секций, только локальные провайдеры."""

    name = "morning_dashboard"
    description = (
        "Собирает утренний обзор: задачи, погода, планы, новости. "
        "Неподключённые источники помечаются как «не подключено»."
    )
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        todo_provider: Callable[[], Any] | None = None,
        calendar_provider: Callable[[], Any] | None = None,
        weather_provider: Callable[[], Any] | None = None,
        news_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        self.providers = {
            "Задачи": todo_provider,
            "Погода": weather_provider,
            "Планы": calendar_provider,
            "Новости": news_provider,
        }

    def execute(self, **_: Any) -> ToolResult:
        sections = []
        for heading, provider in self.providers.items():
            if provider is None:
                body = "не подключено"
            else:
                try:
                    body = _render(provider())
                except Exception as exc:  # noqa: BLE001
                    body = f"недоступно: {exc}"
            sections.append(f"## {heading}\n{body}")
        text = "\n\n".join(sections) + "\n"
        return ToolResult.success(text, data={"sections": sections})


# ---------------------------------------------------------------------------
#  Опросы, статьи, состояние
# ---------------------------------------------------------------------------
class PollTool(_StoredTool):
    """Локальные опросы с созданием, голосованием и закрытием."""

    name = "poll"
    description = "Создаёт опрос, считает голоса и закрывает опрос."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "vote", "results", "close"],
                "description": "Действие, по умолчанию results.",
            },
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "poll_id": {"type": "string"},
            "choice": {"description": "Индекс или список индексов вариантов."},
            "voter": {"type": "string", "description": "Кто голосует, по умолчанию local."},
            "multiple": {
                "type": "boolean",
                "description": "Разрешить несколько вариантов, по умолчанию false.",
            },
        },
    }
    risk = RiskLevel.SAFE

    def execute(
        self,
        action: str = "results",
        question: str = "",
        options: Any = None,
        poll_id: str = "",
        choice: Any = None,
        voter: str = "local",
        multiple: bool = False,
        **_: Any,
    ) -> ToolResult:
        polls = self.store.load(_POLLS_KEY, {})
        if not isinstance(polls, dict):
            polls = {}
        action = str(action or "results").lower()

        if action == "create":
            choices = [str(item) for item in _as_list(options) if str(item).strip()]
            if not str(question).strip() or len(choices) < 2:
                return ToolResult.failure("Нужны question и минимум два варианта.")
            new_id = uuid.uuid4().hex[:8]
            polls[new_id] = {
                "id": new_id,
                "question": str(question).strip(),
                "options": choices,
                "multiple": bool(multiple),
                "closed": False,
                "votes": {},
            }
            self.store.save(_POLLS_KEY, polls)
            return _json_result(
                {"action": "create", "poll_id": new_id, "question": polls[new_id]["question"]}
            )

        poll = polls.get(str(poll_id))
        if not isinstance(poll, dict):
            return ToolResult.failure(f"Опрос не найден: {poll_id}")

        if action == "vote":
            if poll.get("closed"):
                return ToolResult.failure("Опрос закрыт.")
            picks = (
                [_as_int(item, -1) for item in _as_list(choice)]
                if isinstance(choice, (list, tuple))
                else [_as_int(choice, -1)]
            )
            if not picks or any(pick < 0 or pick >= len(poll["options"]) for pick in picks):
                return ToolResult.failure("Неверный номер варианта.")
            key = str(voter or "local")
            if key in poll["votes"] and not poll.get("multiple"):
                return ToolResult.failure("Этот участник уже проголосовал.")
            poll["votes"][key] = picks if poll.get("multiple") else picks[0]
            self.store.save(_POLLS_KEY, polls)
            return _json_result(
                {"action": "vote", "poll_id": poll_id, "voter": key, "choice": picks}
            )

        if action == "results":
            counts = {str(option): 0 for option in poll["options"]}
            for picked in poll["votes"].values():
                choices = picked if isinstance(picked, (list, tuple)) else [picked]
                for index in choices:
                    counts[str(poll["options"][_as_int(index, 0)])] += 1
            return _json_result(
                {
                    "action": "results",
                    "poll_id": poll_id,
                    "question": poll["question"],
                    "counts": counts,
                    "total": len(poll["votes"]),
                    "closed": bool(poll.get("closed")),
                }
            )

        if action == "close":
            poll["closed"] = True
            self.store.save(_POLLS_KEY, polls)
            return _json_result({"action": "close", "poll_id": poll_id, "closed": True})

        return ToolResult.failure(f"Неизвестное действие: {action}")


class ArticleSessionTool(_StoredTool):
    """Сессии чтения статей без загрузки страниц."""

    name = "article_session"
    description = "Держит текущую статью по chat_id: open/note/summary/close. Сам URL не загружает."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "note", "summary", "close"],
                "description": "Действие, по умолчанию summary.",
            },
            "url": {"type": "string"},
            "chat_id": {"type": "string", "description": "Ключ чата, по умолчанию local."},
            "summary": {"type": "string"},
            "highlight": {"type": "string"},
        },
    }
    risk = RiskLevel.SAFE

    def execute(
        self,
        action: str = "summary",
        url: str = "",
        chat_id: str = "local",
        summary: str = "",
        highlight: str = "",
        **_: Any,
    ) -> ToolResult:
        sessions = self.store.load(_ARTICLES_KEY, {})
        if not isinstance(sessions, dict):
            sessions = {}
        key = str(chat_id or "local")
        action = str(action or "summary").lower()

        if action == "open":
            previous = sessions.get(key)
            old_url = previous.get("url") if isinstance(previous, dict) else ""
            session = {
                "url": str(url or ""),
                "summary": str(summary or ""),
                "highlights": []
                if old_url != str(url or "")
                else list(previous.get("highlights", [])),
            }
            sessions[key] = session
            self.store.save(_ARTICLES_KEY, sessions)
            return _json_result({"action": "open", "chat_id": key, "session": session})

        if action == "note":
            session = sessions.get(key)
            if not isinstance(session, dict):
                session = {"url": "", "summary": "", "highlights": []}
            if str(highlight).strip():
                session["highlights"].append(str(highlight).strip())
            sessions[key] = session
            self.store.save(_ARTICLES_KEY, sessions)
            return _json_result({"action": "note", "chat_id": key, "session": session})

        if action == "summary":
            session = sessions.get(key)
            if not isinstance(session, dict):
                return ToolResult.failure("Статья не открыта.")
            return _json_result({"action": "summary", "chat_id": key, "session": session})

        if action == "close":
            removed = sessions.pop(key, None) is not None
            if removed:
                self.store.save(_ARTICLES_KEY, sessions)
            return _json_result({"action": "close", "chat_id": key, "closed": removed})

        return ToolResult.failure(f"Неизвестное действие: {action}")


class FatigueTool(Tool):
    """Эвристическая оценка усталости и рекомендация отдыха."""

    name = "fatigue"
    description = (
        "Оценивает риск усталости по времени работы, энергии, перерывам и тяжёлым задачам."
    )
    parameters = {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Минуты без отдыха."},
            "energy": {"type": "number", "description": "Энергия от 0 до 1."},
            "breaks": {"type": "integer", "description": "Сколько было перерывов."},
            "heavy_tasks": {"type": "integer", "description": "Тяжёлые задачи."},
        },
    }
    risk = RiskLevel.SAFE

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"

    def execute(
        self,
        minutes: int = 0,
        energy: float = 0.5,
        breaks: int = 0,
        heavy_tasks: int = 0,
        **_: Any,
    ) -> ToolResult:
        work = max(0, _as_int(minutes, 0))
        level_energy = min(1.0, max(0.0, _as_float(energy, 0.5)))
        rest = max(0, _as_int(breaks, 0))
        heavy = max(0, _as_int(heavy_tasks, 0))
        risk = min(
            1.0,
            work / 240 * 0.4
            + (1.0 - level_energy) * 0.3
            + max(0, 2 - rest) * 0.1
            + min(heavy, 5) * 0.05,
        )
        if work >= 180 and rest < 2:
            recommendation = "пора сделать чай и отдохнуть"
        elif risk >= 0.7:
            recommendation = "нужен короткий перерыв"
        elif risk >= 0.4:
            recommendation = "стоит сделать небольшую паузу"
        else:
            recommendation = "можно продолжать в том же темпе"
        return _json_result(
            {
                "minutes": work,
                "energy": level_energy,
                "breaks": rest,
                "heavy_tasks": heavy,
                "risk": round(risk, 3),
                "score": round(risk, 3),
                "level": "high" if risk >= 0.7 else "medium" if risk >= 0.4 else "low",
                "recommendation": recommendation,
            }
        )


class MusicMoodTool(Tool):
    """Подбор запросов под настроение без сети и без управления плеером."""

    name = "music_mood"
    description = "Предлагает поисковые запросы или треки под настроение. Плеером не управляет."
    parameters = {
        "type": "object",
        "properties": {
            "mood": {
                "type": "string",
                "description": "Настроение: calm/sad/angry/focus/happy.",
            },
            "action": {
                "type": "string",
                "enum": ["suggest", "queue"],
                "description": "Действие, по умолчанию suggest.",
            },
        },
    }
    risk = RiskLevel.SAFE

    _MAPPING = {
        "calm": ["lo-fi calm instrumental", "ambient relaxation", "спокойная музыка"],
        "sad": ["melancholic piano", "slow indie sad songs", "грустные песни"],
        "angry": ["aggressive metal", "drum and bass rage", "энергичный rock"],
        "focus": ["deep focus ambient", "coding background music", "инструментал для работы"],
        "happy": ["upbeat pop hits", "sunny indie playlist", "весёлая музыка"],
    }

    def __init__(
        self,
        base_dir: str | Path | None = None,
        provider: Callable[..., Any] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        self.provider = provider

    def _call(self, mood: str) -> Any:
        if self.provider is None:
            return None
        try:
            signature = inspect.signature(self.provider)
            positional = [
                param
                for param in signature.parameters.values()
                if param.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            positional = []
        if len(positional) >= 2:
            return self.provider(self.base_dir, mood)
        return self.provider(mood)

    def execute(self, mood: str = "", action: str = "suggest", **_: Any) -> ToolResult:
        key = _norm(mood) or "calm"
        action = str(action or "suggest").lower()
        if action not in {"suggest", "queue"}:
            return ToolResult.failure(f"Неизвестное действие: {action}")
        queries = list(self._MAPPING.get(key, self._MAPPING["calm"]))
        tracks: list[Any] = []
        if self.provider is not None:
            try:
                returned = self._call(mood or key)
            except Exception as exc:  # noqa: BLE001
                return ToolResult.failure(f"Провайдер музыки недоступен: {exc}")
            if isinstance(returned, dict):
                tracks = _as_list(returned.get("tracks"))
                queries = [str(item) for item in _as_list(returned.get("queries"))] or queries
            elif isinstance(returned, (list, tuple)):
                tracks = list(returned)
            elif isinstance(returned, str):
                tracks = [returned]
        return _json_result(
            {
                "action": action,
                "mood": str(mood or "calm"),
                "queries": queries,
                "tracks": tracks,
                "queued": action == "queue",
            }
        )


class BackgroundWatchTool(_StoredTool):
    """Состояние наблюдения за путями, URL и процессами (без потоков)."""

    name = "background_watch"
    description = (
        "Следит за изменениями файлов, URL и процессов по запросу check. "
        "Фоновых потоков не создаёт."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "check", "remove"],
                "description": "Действие, по умолчанию list.",
            },
            "name": {"type": "string", "description": "Имя цели."},
            "kind": {
                "type": "string",
                "enum": ["path", "url", "process"],
                "description": "Тип цели.",
            },
            "target": {"type": "string", "description": "Путь, URL или имя процесса."},
            "interval_min": {
                "type": "integer",
                "description": "Интервал в минутах, по умолчанию 60.",
            },
        },
    }
    risk = RiskLevel.SAFE

    def __init__(
        self,
        base_dir: str | Path | None = None,
        fetcher: Callable[[str], Any] | None = None,
        process_provider: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(base_dir)
        self.fetcher = fetcher
        self.process_provider = process_provider

    def _snapshot(self, kind: str, target: str) -> dict[str, Any]:
        if kind == "path":
            path = _expand(target, self.base_dir)
            if not path.exists():
                return {"missing": True}
            try:
                stat = path.stat()
                return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
            except OSError:
                return {"missing": True}
        if kind == "url":
            if self.fetcher is None:
                return {"text": None}
            try:
                return {"text": str(self.fetcher(target) or "")}
            except Exception as exc:  # noqa: BLE001
                return {"text": None, "error": str(exc)}
        if self.process_provider is None:
            return {"present": None}
        try:
            return {"present": bool(self.process_provider(target))}
        except Exception as exc:  # noqa: BLE001
            return {"present": None, "error": str(exc)}

    def execute(
        self,
        action: str = "list",
        name: str = "",
        kind: str = "path",
        target: str = "",
        interval_min: int = 60,
        **_: Any,
    ) -> ToolResult:
        watches = self.store.load(_WATCH_KEY, {})
        if not isinstance(watches, dict):
            watches = {}
        action = str(action or "list").lower()

        if action == "add":
            if not name.strip() or not target.strip():
                return ToolResult.failure("Нужны name и target.")
            if str(kind) not in {"path", "url", "process"}:
                return ToolResult.failure(f"Неизвестный тип цели: {kind}")
            watches[name] = {
                "name": name,
                "kind": str(kind),
                "target": target,
                "interval_min": max(1, _as_int(interval_min, 60)),
                "snapshot": None,
                "checked_at": None,
            }
            self.store.save(_WATCH_KEY, watches)
            return _json_result({"action": "add", "target": watches[name]})

        if action == "remove":
            removed = watches.pop(name, None) is not None
            if removed:
                self.store.save(_WATCH_KEY, watches)
            return _json_result({"action": "remove", "name": name, "removed": removed})

        if action == "list":
            return _json_result(
                {"action": "list", "count": len(watches), "items": list(watches.values())}
            )

        if action == "check":
            changed: list[str] = []
            items: list[dict[str, Any]] = []
            selected = [watches[name]] if name in watches else list(watches.values())
            for watch in selected:
                current = self._snapshot(str(watch.get("kind")), str(watch.get("target")))
                previous = watch.get("snapshot")
                is_changed = previous is not None and previous != current
                if is_changed:
                    changed.append(str(watch.get("name")))
                watch["snapshot"] = current
                watch["checked_at"] = _now_iso()
                items.append(
                    {
                        "name": watch.get("name"),
                        "kind": watch.get("kind"),
                        "changed": is_changed,
                        "snapshot": current,
                    }
                )
            self.store.save(_WATCH_KEY, watches)
            return _json_result(
                {"action": "check", "checked": len(items), "changed": changed, "items": items}
            )

        return ToolResult.failure(f"Неизвестное действие: {action}")


class TutorialTool(_StoredTool):
    """Пошаговые обучающие сценарии в рамках чата."""

    name = "tutorial"
    description = "Ведёт пошаговый туториал: start/next/step/back/close с прогрессом."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "next", "step", "back", "close"],
                "description": "Действие, по умолчанию step.",
            },
            "topic": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "string"}},
            "answer": {"type": "string"},
            "chat_id": {"type": "string", "description": "Ключ чата, по умолчанию local."},
        },
    }
    risk = RiskLevel.SAFE

    def execute(
        self,
        action: str = "step",
        topic: str = "",
        steps: Any = None,
        answer: str = "",
        chat_id: str = "local",
        **_: Any,
    ) -> ToolResult:
        tutorials = self.store.load(_TUTORIALS_KEY, {})
        if not isinstance(tutorials, dict):
            tutorials = {}
        key = str(chat_id or "local")
        action = str(action or "step").lower()

        if action == "start":
            ordered = [str(item) for item in _as_list(steps) if str(item).strip()]
            if not ordered:
                return ToolResult.failure("Нужны шаги туториала.")
            tutorials[key] = {
                "topic": str(topic or ""),
                "steps": ordered,
                "progress": 0,
                "answers": [],
            }
            self.store.save(_TUTORIALS_KEY, tutorials)
            return _json_result(
                {
                    "action": "start",
                    "chat_id": key,
                    "topic": tutorials[key]["topic"],
                    "step": ordered[0],
                    "progress": 0,
                    "total": len(ordered),
                }
            )

        session = tutorials.get(key)
        if not isinstance(session, dict):
            return ToolResult.failure("Туториал не запущен.")

        if action == "close":
            tutorials.pop(key, None)
            self.store.save(_TUTORIALS_KEY, tutorials)
            return _json_result({"action": "close", "chat_id": key, "closed": True})

        if str(answer).strip():
            session.setdefault("answers", []).append(str(answer).strip())
        total = len(session.get("steps", []))
        progress = _as_int(session.get("progress", 0), 0)
        if action == "next":
            progress = min(total, progress + 1)
        elif action == "back":
            progress = max(0, progress - 1)
        elif action != "step":
            return ToolResult.failure(f"Неизвестное действие: {action}")
        session["progress"] = progress
        self.store.save(_TUTORIALS_KEY, tutorials)
        return _json_result(
            {
                "action": action,
                "chat_id": key,
                "topic": session.get("topic", ""),
                "step": session["steps"][progress] if progress < total else None,
                "progress": progress,
                "total": total,
                "done": progress >= total,
            }
        )


# ---------------------------------------------------------------------------
#  Работа с медиа и файлами
# ---------------------------------------------------------------------------
class BatchMediaTool(_StoredTool):
    """Пакетная обработка изображений через локальный Pillow."""

    name = "batch_media"
    description = (
        "Ресайз/сжатие/водяной знак для нескольких изображений. "
        "Результаты пишутся в data/batch_media."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "operation": {
                "type": "string",
                "enum": ["resize", "compress", "watermark"],
                "description": "Операция, по умолчанию compress.",
            },
            "width": {"type": "integer", "description": "Целевая ширина для resize."},
            "quality": {"type": "integer", "description": "Качество JPEG, по умолчанию 85."},
            "watermark": {"type": "string", "description": "Текст водяного знака."},
            "output_dir": {"type": "string", "description": "Каталог результата."},
        },
        "required": ["paths"],
    }
    risk = RiskLevel.MEDIUM

    def execute(
        self,
        paths: Any = None,
        operation: str = "compress",
        width: int = 0,
        quality: int = 85,
        watermark: str = "",
        output_dir: str = "",
        **_: Any,
    ) -> ToolResult:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return ToolResult.failure(
                "Pillow не установлен: установите pillow для работы с изображениями."
            )
        operation = str(operation or "compress").lower()
        if operation not in {"resize", "compress", "watermark"}:
            return ToolResult.failure(f"Неизвестная операция: {operation}")
        target_dir = (
            _expand(output_dir, self.base_dir) if output_dir else self.base_dir / "batch_media"
        )
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.failure(f"Не удалось создать каталог: {exc}")

        outputs: list[dict[str, Any]] = []
        for raw in _as_list(paths):
            source = _expand(raw, self.base_dir)
            item: dict[str, Any] = {"path": str(source), "ok": False}
            if not source.is_file():
                item["reason"] = "файл не найден"
                outputs.append(item)
                continue
            try:
                with Image.open(source) as opened:
                    image = opened.copy()
            except Exception:  # noqa: BLE001 - битые файлы пропускаем
                item["reason"] = "не изображение"
                outputs.append(item)
                continue
            if operation == "resize" and _as_int(width, 0) > 0:
                new_width = _as_int(width, 0)
                new_height = max(1, round(image.height * new_width / image.width))
                image = image.resize((new_width, new_height))
            if operation == "watermark" and str(watermark).strip():
                canvas = image.convert("RGBA")
                draw = ImageDraw.Draw(canvas)
                draw.text(
                    (5, canvas.height - 20),
                    str(watermark),
                    fill=(255, 255, 255, 255),
                )
                image = canvas.convert("RGB")
            suffix = source.suffix.lower() or ".png"
            target = target_dir / f"out_{_slug(source.stem, 'image')}{suffix}"
            save_args: dict[str, Any] = {}
            if suffix in {".jpg", ".jpeg", ".webp"}:
                image = image.convert("RGB")
                save_args["quality"] = min(100, max(1, _as_int(quality, 85)))
            try:
                image.save(target, **save_args)
            except (OSError, ValueError) as exc:
                item["reason"] = f"не удалось сохранить: {exc}"
                outputs.append(item)
                continue
            item.update({"ok": True, "output": str(target)})
            outputs.append(item)
        return _json_result(outputs)


class MemeTool(_StoredTool):
    """Создаёт локальный мем-PNG с верхним и нижним текстом."""

    name = "meme"
    description = (
        "Рисует мем по шаблону или на сером холсте 800x800 и сохраняет PNG. Сеть не используется."
    )
    parameters = {
        "type": "object",
        "properties": {
            "template": {"type": "string", "description": "Путь к картинке-шаблону."},
            "top_text": {"type": "string"},
            "bottom_text": {"type": "string"},
            "output": {"type": "string", "description": "Путь к PNG (опционально)."},
        },
    }
    risk = RiskLevel.MEDIUM

    @staticmethod
    def _wrap(text: str, width: int = 32) -> list[str]:
        lines: list[str] = []
        current = ""
        for word in str(text).split():
            candidate = f"{current} {word}".strip()
            if len(candidate) > width and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    def execute(
        self,
        template: str = "",
        top_text: str = "",
        bottom_text: str = "",
        output: str = "",
        **_: Any,
    ) -> ToolResult:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return ToolResult.failure("Pillow не установлен: установите pillow для создания мемов.")
        image = None
        if template:
            path = _expand(template, self.base_dir)
            if path.is_file():
                try:
                    image = Image.open(path).convert("RGB")
                except Exception:  # noqa: BLE001
                    image = None
        if image is None:
            image = Image.new("RGB", (800, 800), (128, 128, 128))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        lines_top = self._wrap(top_text)
        lines_bottom = self._wrap(bottom_text)
        line_height = 16
        for index, line in enumerate(lines_top):
            width = draw.textlength(line, font=font)
            draw.text(
                ((image.width - width) / 2, 8 + index * line_height),
                line,
                fill="white",
                font=font,
                stroke_width=1,
                stroke_fill="black",
            )
        for index, line in enumerate(lines_bottom):
            width = draw.textlength(line, font=font)
            draw.text(
                (
                    (image.width - width) / 2,
                    image.height - 10 - (len(lines_bottom) - index) * line_height,
                ),
                line,
                fill="white",
                font=font,
                stroke_width=1,
                stroke_fill="black",
            )
        if output:
            target = _expand(output, self.base_dir)
        else:
            memes = self.base_dir / "memes"
            memes.mkdir(parents=True, exist_ok=True)
            target = memes / f"meme-{_stamp()}.png"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target, format="PNG")
        except OSError as exc:
            return ToolResult.failure(f"Не удалось сохранить мем: {exc}")
        return ToolResult.success(f"Мем сохранён: {target}", data={"path": str(target)})


class PatchFileTool(Tool):
    """Точечная замена текста в файле с резервной копией."""

    name = "patch_file"
    description = (
        "Заменяет уникальную строку в файле внутри проекта. "
        "Требует confirm=true и сохраняет копию с суффиксом .bak."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу."},
            "old": {"type": "string", "description": "Заменяемый фрагмент (ровно одно вхождение)."},
            "new": {"type": "string", "description": "Новый фрагмент."},
            "confirm": {
                "type": "boolean",
                "description": "Подтверждение изменения, по умолчанию false.",
            },
        },
        "required": ["path", "old", "new"],
    }
    risk = RiskLevel.MEDIUM

    def __init__(
        self,
        base_dir: str | Path | None = None,
        allowed_roots: Any = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else PROJECT_ROOT / "data"
        roots = (PROJECT_ROOT,) if allowed_roots is None else _as_list(allowed_roots)
        self.allowed_roots = tuple(_expand(item).resolve() for item in roots)

    def execute(
        self,
        path: str = "",
        old: str = "",
        new: str = "",
        confirm: bool = False,
        **_: Any,
    ) -> ToolResult:
        if not confirm:
            return ToolResult.failure("Изменение файла требует подтверждения: confirm=true.")
        if not path or not old:
            return ToolResult.failure("Нужны path и old.")
        target = _expand(path).resolve()
        if not _within(target, self.allowed_roots):
            return ToolResult.failure(f"Файл вне разрешённых корней: {target}")
        if not target.is_file():
            return ToolResult.failure(f"Файл не найден: {target}")
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return ToolResult.failure(f"Не удалось прочитать файл: {exc}")
        if text.count(old) != 1:
            return ToolResult.failure(
                f"Фрагмент должен встречаться ровно один раз, найдено: {text.count(old)}."
            )
        backup = target.with_name(target.name + ".bak")
        try:
            shutil.copy2(target, backup)
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
        except OSError as exc:
            return ToolResult.failure(f"Не удалось изменить файл: {exc}")
        return _json_result({"path": str(target), "backup": str(backup), "replacements": 1})


def roadmap_tools(base_dir: str | Path | None = None, **deps: Any) -> list[Tool]:
    """Собирает все инструменты дорожной карты с внедрёнными зависимостями."""
    roots = deps.get("allowed_roots")
    return [
        InspectScreenTool(base_dir, windows=deps.get("windows")),
        ClipboardHistoryTool(base_dir, getter=deps.get("getter")),
        ProjectCleanupTool(base_dir, allowed_roots=roots),
        BackupWorkspaceTool(base_dir),
        CatchupSummaryTool(base_dir, memory=deps.get("memory")),
        SearchHistoryTool(
            base_dir,
            memory=deps.get("memory"),
            notes=deps.get("notes"),
            diary=deps.get("diary"),
        ),
        CalendarTool(base_dir),
        WeatherTool(base_dir, provider=deps.get("provider")),
        RepoGuideTool(base_dir),
        ChartTool(base_dir),
        SoftMemoryTool(base_dir),
        MorningDashboardTool(
            base_dir,
            todo_provider=deps.get("todo_provider"),
            calendar_provider=deps.get("calendar_provider"),
            weather_provider=deps.get("weather_provider"),
            news_provider=deps.get("news_provider"),
        ),
        PollTool(base_dir),
        ArticleSessionTool(base_dir),
        FatigueTool(base_dir),
        MusicMoodTool(base_dir, provider=deps.get("music_provider", deps.get("provider"))),
        BackgroundWatchTool(
            base_dir,
            fetcher=deps.get("fetcher"),
            process_provider=deps.get("process_provider"),
        ),
        TutorialTool(base_dir),
        BatchMediaTool(base_dir),
        MemeTool(base_dir),
        PatchFileTool(base_dir, allowed_roots=roots),
    ]


__all__ = [
    "ArticleSessionTool",
    "BackupWorkspaceTool",
    "BackgroundWatchTool",
    "BatchMediaTool",
    "CalendarTool",
    "CatchupSummaryTool",
    "ChartTool",
    "ClipboardHistoryTool",
    "FatigueTool",
    "InspectScreenTool",
    "MemeTool",
    "MorningDashboardTool",
    "MusicMoodTool",
    "PatchFileTool",
    "PollTool",
    "ProjectCleanupTool",
    "RepoGuideTool",
    "RoadmapStore",
    "SearchHistoryTool",
    "SoftMemoryTool",
    "TutorialTool",
    "WeatherTool",
    "roadmap_tools",
]
