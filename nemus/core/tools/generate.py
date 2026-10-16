"""Инструмент саморасширения: Немуся создаёт новые инструменты (плагины)."""

from __future__ import annotations

import ast
from pathlib import Path

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: модули, которые плагину запрещено импортировать (исполнение/сеть/ОС)
_BLOCKED_MODULES = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "socket",
    "ctypes",
    "importlib",
    "pickle",
    "marshal",
    "multiprocessing",
    "signal",
    "pty",
    "builtins",
    "requests",
    "urllib",
    "http",
    "ftplib",
    "tempfile",
    "pathlib",
    "glob",
}
#: встроенные функции, которых не должно быть в коде плагина
_BLOCKED_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
    "breakpoint",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
}
_BLOCKED_NAMES = {"__builtins__", "__import__"}


def validate_plugin_code(code: str) -> str | None:
    """Статически проверяет код плагина. Возвращает текст ошибки или ``None``.

    Это барьер «по разумности», а не полноценная песочница: он отсекает
    очевидно опасные конструкции (импорт ОС/сети, eval/exec, доступ к dunder),
    но не заменяет изоляцию. Инструмент всё равно помечен как DANGEROUS.
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


class CreateToolTool(Tool):
    """Сохраняет новый инструмент-плагин и загружает его в реестр."""

    name = "create_tool"
    description = (
        "Создаёт новый инструмент (плагин) на Python, сохраняет его в папку plugins/ "
        "и сразу делает доступным. Код должен содержать класс-наследник Tool. "
        "Пример каркаса:\n"
        "from nemus.core.tools.base import Tool, ToolResult\n"
        "class MyTool(Tool):\n"
        "    name = 'my_tool'\n"
        "    description = 'Что делает'\n"
        "    parameters = {'type':'object','properties':{'x':{'type':'string'}},'required':['x']}\n"
        "    def execute(self, x='', **_):\n"
        "        return ToolResult.success(f'сделал {x}')"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Имя инструмента (латиницей, snake_case).",
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

        plugins_dir = Path(self.plugins_dir)
        plugins_dir.mkdir(parents=True, exist_ok=True)
        path = plugins_dir / f"tool_{name}.py"
        try:
            path.write_text(code, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось сохранить плагин: {exc}")

        if self.registry is None:
            return ToolResult.success(
                f"Плагин сохранён в {path}, но реестр не подключён — перезапусти."
            )

        # (ре)загружаем плагины
        self.registry.load_plugins(plugins_dir)
        available = name in self.registry
        logger.info(f"create_tool: '{name}' сохранён и загружен={available}")
        if available:
            return ToolResult.success(
                f"Инструмент '{name}' создан и доступен. Можешь вызывать его сразу."
            )
        return ToolResult.failure(
            f"Файл {path} сохранён, но инструмент '{name}' не загрузился. "
            "Проверь код."
        )


def generate_tools() -> list[Tool]:
    return [CreateToolTool()]
