"""Инструмент саморасширения: Немуся создаёт новые инструменты (плагины)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: модули, которым не место в плагинах: сеть, низкоуровневый доступ к процессу
#: и десериализация с исполнением кода. Файлы/ОС/консоль (os, sys, subprocess,
#: shutil, pathlib, tempfile, glob) разрешены — плагин не должен быть бесполезнее
#: run_python, который и так исполняет любой код.
_BLOCKED_MODULES = {
    "socket",
    "ctypes",
    "importlib",
    "pickle",
    "marshal",
    "signal",
    "pty",
    "builtins",
    "requests",
    "urllib",
    "http",
    "ftplib",
    "multiprocessing",
}
#: встроенные функции, которых не должно быть в коде плагина
#: (eval/exec — исполнение кода из строк; input/breakpoint — подвешивают поток)
_BLOCKED_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "input",
    "breakpoint",
}
_BLOCKED_NAMES = {"__builtins__", "__import__"}


def validate_plugin_code(code: str) -> str | None:
    """Статически проверяет код плагина. Возвращает текст ошибки или ``None``.

    Это барьер «по разумности», а не полноценная песочница: он отсекает сеть,
    низкоуровневый доступ (ctypes/signal), eval/exec и dunder-хаки, но os,
    файлы, subprocess и прочий Python разрешены — плагин не должен быть
    бесполезнее run_python. Инструмент всё равно помечен как DANGEROUS.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Синтаксическая ошибка: {exc}"

    has_tool_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BLOCKED_MODULES:
                    return f"Запрещён импорт модуля «{alias.name}»."
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _BLOCKED_MODULES:
                return f"Запрещён импорт из модуля «{node.module}»."
            if any(a.name == "*" for a in node.names):
                return "Запрещён импорт «*»."
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BLOCKED_CALLS:
                return f"Запрещён вызов «{func.id}»."
            if isinstance(func, ast.Attribute) and _is_dunder(func.attr):
                return f"Запрещён доступ к «{func.attr}»."
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                return f"Запрещён доступ к атрибуту «{node.attr}»."
        elif isinstance(node, ast.Name):
            if node.id in _BLOCKED_NAMES:
                return f"Запрещено использование «{node.id}»."
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and "Tool" in base.id:
                    has_tool_class = True
                elif isinstance(base, ast.Attribute) and "Tool" in base.attr:
                    has_tool_class = True

    if not has_tool_class:
        return "Код должен содержать класс-наследник Tool."
    return None


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _extract_tool_name(code: str) -> str:
    """Возвращает атрибут ``name`` класса-наследника Tool (или пусто)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_tool = any(
            (isinstance(base, ast.Name) and "Tool" in base.id)
            or (isinstance(base, ast.Attribute) and "Tool" in base.attr)
            for base in node.bases
        )
        if not is_tool:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign):
                targets = [stmt.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "name":
                    value = stmt.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        return value.value
    return ""


class CreateToolTool(Tool):
    """Сохраняет новый инструмент-плагин и загружает его в реестр."""

    name = "create_tool"
    description = (
        "Создаёт новый инструмент (плагин) на Python для собственного расширения (self-improvement), сохраняет его в папку plugins/ "
        "и сразу делает доступным. Используй это, когда подходящего встроенного инструмента нет или его нужно улучшить. Код должен содержать класс-наследник Tool. "
        "Пример каркаса:\n"
        "from nemus.core.tools.base import Tool, ToolResult\n"
        "\n"
        "\n"
        "class MyTool(Tool):\n"
        "    name = 'my_tool'\n"
        "    description = 'Что делает'\n"
        "    parameters = {'type':'object','properties':{'x':{'type':'string'}},'required':['x']}\n"
        "    def execute(self, x='', **_):\n"
        "        return ToolResult.success(f'сделал {x}')\n"    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Имя инструмента (латиницей, snake_case). "
                    "Должно совпадать с name = '...' внутри класса плагина."
                ),
            },
            "description": {"type": "string", "description": "Описание для модели."},
            "code": {
                "type": "string",
                "description": "Полный Python-код модуля плагина с классом Tool.",
            },
        },
        "required": ["name", "description", "code"],
    }
    risk = RiskLevel.DANGEROUS

    #: подставляется фабрикой после создания реестра
    registry: object = None
    plugins_dir: str | Path = "plugins"

    def execute(
        self, name: str = "", description: str = "", code: str = "", **_: object
    ) -> ToolResult:
        if not name or not name.isidentifier() or name.startswith("_"):
            return ToolResult.failure(
                "Имя должно быть корректным идентификатором (латиница/цифры/_, не с _)."
            )
        error = validate_plugin_code(code)
        if error:
            return ToolResult.failure(f"Код плагина отклонён: {error}")

        declared = _extract_tool_name(code)
        if not declared:
            return ToolResult.failure(
                "В коде не найден атрибут name у класса-наследника Tool. "
                "Добавь в класс строку вида: name = 'my_tool'."
            )
        if declared != name:
            return ToolResult.failure(
                f"Имя в аргументе '{name}' не совпадает с name = '{declared}' в коде. "
                f"Передай name='{declared}' либо поправь класс."
            )

        plugins_dir = Path(self.plugins_dir)
        plugins_dir.mkdir(parents=True, exist_ok=True)
        path = plugins_dir / f"tool_{name}.py"
        existed = path.exists()
        # Нормализуем код: перенос строк, завершающий перевод строки,
        # и, если доступен ruff, автоформатирование (чтобы плагин проходил ruff).
        code = code.rstrip("\n") + "\n"
        try:
            import subprocess

            path.write_text(code, encoding="utf-8")
            subprocess.run(
                [sys.executable, "-m", "ruff", "format", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if path.exists():
                code = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            path.write_text(code, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось сохранить плагин: {exc}")

        action = "обновлён" if existed else "создан"
        if self.registry is None:
            return ToolResult.success(
                f"Плагин {action} и сохранён в {path}, но реестр не подключён — перезапусти."
            )

        # (ре)загружаем плагины, чтобы инструмент был доступен сразу
        self.registry.load_plugins(plugins_dir)
        available = name in self.registry
        logger.info(f"create_tool: '{name}' {action} и загружен={available}")
        if available:
            return ToolResult.success(
                f"Инструмент '{name}' {action} и готов. Можешь вызывать его сразу — "
                f"повторно создавать не нужно. Файл: {path}."
            )
        return ToolResult.failure(
            f"Файл {path} сохранён, но инструмент '{name}' не загрузился. "
            "Проверь, что класс наследует Tool и в модуле нет ошибок при импорте."
        )


def generate_tools() -> list[Tool]:
    return [CreateToolTool()]
