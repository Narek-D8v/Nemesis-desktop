"""Инструменты выполнения Python-кода и установки пакетов (саморасширение)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.sandbox import PathGuard, PathNotAllowed

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 120


class RunPythonTool(Tool):
    name = "run_python"
    description = (
        "Выполняет Python-код или запускает сохранённый .py-скрипт в окружении "
        "Немуси и возвращает stdout/stderr. Передай либо code (код строкой), либо "
        "path (путь к существующему скрипту) вместе с args. Рабочий каталог по "
        "умолчанию — твоя рабочая папка (workspace). Используй, чтобы писать и "
        "запускать собственные скрипты, обрабатывать данные, автоматизировать задачи."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python-код для выполнения."},
            "path": {
                "type": "string",
                "description": "Путь к существующему .py-скрипту для запуска.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Аргументы командной строки для скрипта (path).",
            },
            "workdir": {
                "type": "string",
                "description": "Рабочий каталог (по умолчанию — workspace).",
            },
            "timeout": {
                "type": "integer",
                "description": "Таймаут в секундах (по умолчанию 120).",
            },
        },
    }
    risk = RiskLevel.DANGEROUS

    #: подставляется фабрикой (config.tools.workspace)
    workspace: str | Path = "data/workspace"
    #: подставляется фабрикой — ограничение рабочих путей
    guard: PathGuard | None = None

    def _guard(self) -> PathGuard:
        if self.guard is None:
            self.guard = PathGuard(self.workspace)
        return self.guard

    def execute(
        self,
        code: str = "",
        path: str = "",
        args: list[str] | None = None,
        workdir: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        **_: object,
    ) -> ToolResult:
        if not code.strip() and not path.strip():
            return ToolResult.failure("Укажите code (код) или path (скрипт).")

        guard = self._guard()
        if workdir:
            try:
                base = guard.resolve_write(workdir, what="Рабочий каталог")
            except PathNotAllowed as exc:
                return ToolResult.failure(str(exc))
        else:
            base = Path(self.workspace).expanduser().resolve()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось открыть рабочую папку: {exc}")

        try:
            timeout = max(1, int(timeout or DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT

        if path.strip():
            try:
                candidate = Path(path).expanduser()
                if not candidate.is_absolute():
                    candidate = base / candidate
                script = guard.resolve_write(candidate, what="Скрипт")
            except PathNotAllowed as exc:
                return ToolResult.failure(str(exc))
            if not script.is_file():
                return ToolResult.failure(f"Скрипт не найден: {script}")
        else:
            # код -> уникальный временный файл, чтобы не затирать чужие запуски
            snippet = Path(tempfile.gettempdir()) / f"nemus_snippet_{uuid.uuid4().hex[:8]}.py"
            snippet.write_text(code, encoding="utf-8")
            script = snippet

        argv = [sys.executable, "-X", "utf8", str(script)]
        if args:
            argv.extend(str(a) for a in args)

        try:
            proc = subprocess.run(
                argv,
                cwd=str(base),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(f"Таймаут {timeout} c: {script.name}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка запуска: {exc}")

        out = (proc.stdout or "")[-MAX_OUTPUT:]
        err = (proc.stderr or "")[-MAX_OUTPUT:]
        parts = [f"$ python {script.name}", f"код возврата: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return ToolResult(ok=proc.returncode == 0, content="\n".join(parts))


class InstallPackageTool(Tool):
    name = "install_package"
    description = (
        "Устанавливает Python-пакеты через pip в окружение Немуси. "
        "Используй, когда не хватает библиотеки для выполнения задачи."
    )
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Список пакетов, например ['requests','pandas'].",
            },
            "upgrade": {
                "type": "boolean",
                "description": "Обновить пакеты до последней версии.",
            },
        },
        "required": ["packages"],
    }
    risk = RiskLevel.DANGEROUS

    def execute(
        self, packages: list[str] | str = (), upgrade: bool = False, **_: object
    ) -> ToolResult:
        if isinstance(packages, str):
            pkgs = packages.split()
        else:
            pkgs = list(packages)
        if not pkgs:
            return ToolResult.failure("Не указаны пакеты.")

        argv = [sys.executable, "-m", "pip", "install", *pkgs]
        if upgrade:
            argv.append("--upgrade")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure("Установка заняла слишком много времени.")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка pip: {exc}")

        out = (proc.stdout or "")[-MAX_OUTPUT:]
        err = (proc.stderr or "")[-MAX_OUTPUT:]
        ok = proc.returncode == 0
        body = out if ok else f"{out}\n{err}"
        return ToolResult(ok=ok, content=body)


def python_tools() -> list[Tool]:
    return [RunPythonTool(), InstallPackageTool()]
