"""Инструмент выполнения команд оболочки."""

from __future__ import annotations

import platform
import subprocess

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.sandbox import PathGuard, PathNotAllowed

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 60


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "Выполняет команду в системной оболочке и возвращает вывод. "
        "На Windows доступны shell='cmd' (по умолчанию) или 'powershell'; "
        "на Unix — 'bash'/'sh'. Используй для запуска программ, git, сборки и т.п. "
        "Рабочий каталог по умолчанию — рабочая папка ассистента."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Команда для выполнения."},
            "shell": {
                "type": "string",
                "enum": ["cmd", "powershell", "bash", "sh"],
                "description": "Какую оболочку использовать.",
            },
            "cwd": {
                "type": "string",
                "description": "Рабочий каталог (необязательно).",
            },
            "timeout": {
                "type": "integer",
                "description": "Таймаут в секундах (по умолчанию 60).",
            },
        },
        "required": ["command"],
    }
    risk = RiskLevel.DANGEROUS

    #: подставляется фабрикой из config.tools.workspace
    guard: PathGuard | None = None

    def execute(
        self,
        command: str = "",
        shell: str = "",
        cwd: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        **_: object,
    ) -> ToolResult:
        if not command.strip():
            return ToolResult.failure("Пустая команда.")

        if self.guard is None:
            self.guard = PathGuard()
        self.guard.ensure_dir()
        if cwd:
            try:
                workdir = str(
                    self.guard.resolve_write(cwd, what="Рабочий каталог")
                )
            except PathNotAllowed as exc:
                return ToolResult.failure(str(exc))
        else:
            workdir = str(self.guard.workspace)

        shell = shell or ("cmd" if platform.system() == "Windows" else "sh")
        argv = self._build_argv(command, shell)
        try:
            proc = subprocess.run(
                argv,
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(f"Таймаут {timeout} c: {command}")
        except FileNotFoundError as exc:
            return ToolResult.failure(f"Оболочка недоступна: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка выполнения: {exc}")

        out = (proc.stdout or "")[-MAX_OUTPUT:]
        err = (proc.stderr or "")[-MAX_OUTPUT:]
        parts = [f"$ {command}", f"код возврата: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        result = "\n".join(parts)
        return ToolResult(ok=proc.returncode == 0, content=result)

    @staticmethod
    def _build_argv(command: str, shell: str) -> list[str]:
        shell = shell.lower()
        if platform.system() == "Windows":
            if shell == "powershell":
                return [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ]
            return ["cmd", "/c", command]
        if shell == "bash":
            return ["bash", "-lc", command]
        return ["sh", "-c", command]


def shell_tools() -> list[Tool]:
    return [RunShellTool()]
