"""Инструменты выполнения Python-кода и установки пакетов (саморасширение)."""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.sandbox import PathGuard, PathNotAllowed
from nemus.core.tools.shell import run_bounded, session_help

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 120
#: таймаут установки/обновления пакетов pip (секунды)
INSTALL_TIMEOUT = 600


class RunPythonTool(Tool):
    name = "run_python"
    description = (
        "Выполняет Python-код или запускает сохранённый .py-скрипт в окружении "
        "Немуси и возвращает stdout/stderr. Передай либо code (код строкой), либо "
        "path (путь к существующему скрипту) вместе с args. Рабочий каталог по "
        "умолчанию — твоя рабочая папка (workspace), а НЕ корень приложения: "
        "к файлам проекта обращайся по абсолютному пути. Используй, чтобы писать "
        "и запускать собственные скрипты, обрабатывать данные, автоматизировать "
        "задачи. Если скрипт работает дольше `timeout`, он НЕ прерывается и НЕ "
        "теряет вывод: он переходит в фоновую сессию, id которой и напечатанное "
        "до дедлайна будут в ответе — дочитывай через run_shell: action=read."
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
            # рабочий каталог по умолчанию — уже разрешённая (привязанная к
            # корню приложения) рабочая папка, а не случайный cwd процесса
            base = guard.workspace
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

        outcome = run_bounded(argv, str(base), timeout, label=f"python {script.name}")

        if outcome.session is not None:
            # Дедлайн истёк или каналы вывода ещё открыты: скрипт не прерываем,
            # а переводим в фон — иначе (старое поведение) вывод терялся бы,
            # а на Windows сам вызов мог зависнуть навсегда.
            if outcome.timed_out:
                status = (
                    f"выполняется дольше {timeout} с — переведён в фоновую "
                    f"сессию {outcome.session.id}"
                )
            else:
                status = (
                    f"завершился (код возврата: {outcome.returncode}), но дочерний "
                    "процесс держит канал вывода — сессия осталась открытой "
                    f"{outcome.session.id}"
                )
            body = outcome.output[-MAX_OUTPUT:]
            parts = [f"$ python {script.name}", f"статус: {status}"]
            parts.append("Вывод, который уже есть:" if body else "Вывода пока нет.")
            if body:
                parts.append(body)
            parts.append(session_help(outcome.session, tool="run_shell"))
            hint = outcome.session.prompt_hint()
            if hint:
                parts.append(hint)
            return ToolResult.success("\n".join(parts))

        out = outcome.output[-MAX_OUTPUT:]
        parts = [f"$ python {script.name}", f"код возврата: {outcome.returncode}"]
        if out:
            parts.append(f"вывод:\n{out}")
        elif outcome.returncode == 0:
            parts.append("(вывода нет)")
        return ToolResult(ok=outcome.returncode == 0, content="\n".join(parts))


class InstallPackageTool(Tool):
    name = "install_package"
    description = (
        "Устанавливает Python-пакеты через pip в окружение Немуси. "
        "Используй, когда не хватает библиотеки для выполнения задачи. "
        "Долгая сборка не считается ошибкой: по дедлайну установка переходит "
        "в фоновую сессию (id и прогресс будут в ответе) — читай через "
        "run_shell: action=read."
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
        outcome = run_bounded(
            argv,
            None,
            INSTALL_TIMEOUT,
            label=f"pip install {' '.join(pkgs)}",
        )

        if outcome.session is not None:
            if outcome.timed_out:
                status = (
                    f"установка идёт дольше {INSTALL_TIMEOUT} с — переведена в "
                    f"фоновую сессию {outcome.session.id}"
                )
            else:
                status = (
                    f"завершилась (код возврата: {outcome.returncode}), но дочерний "
                    "процесс держит канал вывода — сессия осталась открытой "
                    f"{outcome.session.id}"
                )
            body = outcome.output[-MAX_OUTPUT:]
            parts = [f"$ pip install {' '.join(pkgs)}", f"статус: {status}"]
            parts.append("Вывод, который уже есть:" if body else "Вывода пока нет.")
            if body:
                parts.append(body)
            parts.append(session_help(outcome.session, tool="run_shell"))
            return ToolResult.success("\n".join(parts))

        ok = outcome.returncode == 0
        body = outcome.output[-MAX_OUTPUT:]
        return ToolResult(ok=ok, content=body)


def python_tools() -> list[Tool]:
    return [RunPythonTool(), InstallPackageTool()]
