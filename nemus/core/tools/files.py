"""Инструменты работы с файловой системой."""

from __future__ import annotations

from pathlib import Path

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.sandbox import PathGuard, PathNotAllowed

MAX_READ_BYTES = 200_000
MAX_LIST_ENTRIES = 500


def _expand(path: str) -> Path:
    return Path(path).expanduser()


class _GuardedTool(Tool):
    """База для инструментов, изменяющих файлы (ограничение рабочей папкой)."""

    #: подставляется фабрикой из config.tools.workspace
    guard: PathGuard | None = None

    def _guard(self) -> PathGuard:
        if self.guard is None:
            self.guard = PathGuard()
        return self.guard


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "Показывает содержимое каталога. По умолчанию — текущий."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к каталогу."},
        },
    }
    risk = RiskLevel.SAFE

    def execute(self, path: str = ".", **_: object) -> ToolResult:
        target = _expand(path)
        if not target.exists():
            return ToolResult.failure(f"Путь не существует: {target}")
        if not target.is_dir():
            return ToolResult.failure(f"Это не каталог: {target}")
        entries = sorted(
            target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )[:MAX_LIST_ENTRIES]
        lines = []
        for entry in entries:
            if entry.is_dir():
                lines.append(f"[DIR]  {entry.name}")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"[FILE] {entry.name}  ({_human(size)})")
        body = "\n".join(lines) or "(пусто)"
        return ToolResult.success(f"Содержимое {target}:\n{body}")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Читает текстовый файл (до 200 КБ)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу."},
            "encoding": {
                "type": "string",
                "description": "Кодировка, по умолчанию utf-8.",
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.SAFE

    def execute(self, path: str = "", encoding: str = "utf-8", **_: object) -> ToolResult:
        target = _expand(path)
        if not target.is_file():
            return ToolResult.failure(f"Файл не найден: {target}")
        try:
            data = target.read_bytes()[:MAX_READ_BYTES]
            text = data.decode(encoding, errors="replace")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось прочитать: {exc}")
        return ToolResult.success(f"Файл {target}:\n{text}")


class WriteFileTool(_GuardedTool):
    name = "write_file"
    description = (
        "Записывает текст в файл (перезаписывает существующий). "
        "По умолчанию — только внутри рабочей папки ассистента."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу."},
            "content": {"type": "string", "description": "Содержимое для записи."},
            "encoding": {"type": "string", "description": "Кодировка, по умолчанию utf-8."},
        },
        "required": ["path", "content"],
    }
    risk = RiskLevel.MEDIUM

    def execute(
        self, path: str = "", content: str = "", encoding: str = "utf-8", **_: object
    ) -> ToolResult:
        try:
            target = self._guard().resolve_write(path, what="Файл")
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось записать: {exc}")
        return ToolResult.success(
            f"Записано {len(content)} символов в {target}."
        )


class AppendFileTool(_GuardedTool):
    name = "append_file"
    description = (
        "Дописывает текст в конец файла. "
        "По умолчанию — только внутри рабочей папки ассистента."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    risk = RiskLevel.MEDIUM

    def execute(self, path: str = "", content: str = "", **_: object) -> ToolResult:
        try:
            target = self._guard().resolve_write(path, what="Файл")
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(content)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось дописать: {exc}")
        return ToolResult.success(f"Дописано в {target}.")


class CreateDirectoryTool(_GuardedTool):
    name = "create_directory"
    description = (
        "Создаёт каталог (вместе с родительскими). "
        "По умолчанию — только внутри рабочей папки ассистента."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    risk = RiskLevel.MEDIUM

    def execute(self, path: str = "", **_: object) -> ToolResult:
        try:
            target = self._guard().resolve_write(path, what="Каталог")
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc))
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось создать каталог: {exc}")
        return ToolResult.success(f"Каталог создан: {target}")


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Ищет файлы по маске (glob) в каталоге рекурсивно."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Маска, например '*.py' или 'report*'.",
            },
            "path": {"type": "string", "description": "Каталог поиска."},
        },
        "required": ["pattern"],
    }
    risk = RiskLevel.SAFE

    def execute(self, pattern: str = "", path: str = ".", **_: object) -> ToolResult:
        base = _expand(path)
        if not base.is_dir():
            return ToolResult.failure(f"Каталог не найден: {base}")
        matches = [str(p) for p in base.rglob(pattern)][:200]
        if not matches:
            return ToolResult.success("Ничего не найдено.")
        return ToolResult.success("Найдено:\n" + "\n".join(matches))


class DeletePathTool(_GuardedTool):
    name = "delete_path"
    description = (
        "Удаляет файл или каталог (каталог — рекурсивно). "
        "По умолчанию — только внутри рабочей папки ассистента."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {
                "type": "boolean",
                "description": "Удалять каталоги рекурсивно (по умолчанию false).",
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.DANGEROUS

    def execute(
        self, path: str = "", recursive: bool = False, **_: object
    ) -> ToolResult:
        import shutil

        try:
            target = self._guard().resolve_write(path, what="Путь")
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc))
        if not target.exists():
            return ToolResult.failure(f"Не существует: {target}")
        try:
            if target.is_dir():
                if not recursive:
                    return ToolResult.failure(
                        "Это каталог. Укажите recursive=true для рекурсивного удаления."
                    )
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось удалить: {exc}")
        return ToolResult.success(f"Удалено: {target}")


def _human(size: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} ТБ"


def file_tools() -> list[Tool]:
    return [
        ListDirectoryTool(),
        ReadFileTool(),
        WriteFileTool(),
        AppendFileTool(),
        CreateDirectoryTool(),
        SearchFilesTool(),
        DeletePathTool(),
    ]
