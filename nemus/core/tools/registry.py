"""Реестр инструментов: регистрация, поиск, схемы, загрузка плагинов."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterator

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger


class ToolRegistry:
    """Хранилище доступных агенту инструментов."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ------------------------------------------------------------- register
    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Инструмент должен иметь непустое имя.")
        if tool.name in self._tools:
            logger.warning(f"Инструмент '{tool.name}' уже зарегистрирован — перезапись.")
        self._tools[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # ---------------------------------------------------------------- lookup
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    # ---------------------------------------------------------------- schemas
    def schemas(self) -> list[dict[str, Any]]:
        """Список схем всех инструментов для передачи модели."""
        return [tool.schema() for tool in self._tools.values()]

    # --------------------------------------------------------------- execute
    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(f"Инструмент '{name}' не найден.")
        try:
            return tool.execute(**arguments)
        except TypeError as exc:
            return ToolResult.failure(f"Неверные аргументы для {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Ошибка выполнения инструмента {name}")
            return ToolResult.failure(f"{name} упал с ошибкой: {exc}")

    # --------------------------------------------------------------- plugins
    def load_plugins(self, directory: str | Path) -> int:
        """Загружает инструменты из ``*.py`` файлов каталога плагинов.

        Каждый модуль может определить функцию ``register(registry)``
        или объекты-наследники :class:`Tool`.
        """
        directory = Path(directory)
        if not directory.exists():
            return 0
        loaded = 0
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                loaded += self._load_plugin_file(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Не удалось загрузить плагин {path.name}: {exc}")
        return loaded

    def _load_plugin_file(self, path: Path) -> int:
        mod_name = f"nemus_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return 0
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        count = 0
        if hasattr(module, "register"):
            module.register(self)
            count += 1
        for attr in vars(module).values():
            if isinstance(attr, type) and issubclass(attr, Tool) and attr is not Tool:
                try:
                    self.register(attr())
                    count += 1
                except Exception:  # noqa: BLE001
                    continue
        logger.info(f"Плагин {path.name}: загружено инструментов — {count}")
        return count


__all__ = ["ToolRegistry", "Tool", "ToolResult", "RiskLevel"]
