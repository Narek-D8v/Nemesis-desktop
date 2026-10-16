"""Сборка ядра Немуси: реестр инструментов, LLM, разрешения, агент."""

from __future__ import annotations

from dataclasses import dataclass, field

from nemus.config import PROJECT_ROOT, Config
from nemus.core.agent import Agent, ConfirmCallback
from nemus.core.llm import LLMClient
from nemus.core.memory import Memory
from nemus.core.permissions import PermissionManager
from nemus.core.tools.all import all_builtin_tools
from nemus.core.tools.base import Tool
from nemus.core.tools.generate import generate_tools
from nemus.core.tools.memory_tools import memory_tools
from nemus.core.tools.registry import ToolRegistry
from nemus.core.tools.sandbox import PathGuard
from nemus.core.tools.spotify import spotify_tools
from nemus.utils.logger import logger

PLUGINS_DIR = PROJECT_ROOT / "plugins"


@dataclass
class Core:
    """Контейнер основных подсистем ассистента."""

    config: Config
    llm: LLMClient
    registry: ToolRegistry
    permissions: PermissionManager
    agent: Agent
    memory: Memory | None = None
    tools: list[Tool] = field(default_factory=list)

    def rebuild_agent(self, confirm: ConfirmCallback | None = None) -> None:
        self.agent = Agent(
            config=self.config,
            llm=self.llm,
            registry=self.registry,
            permissions=self.permissions,
            confirm=confirm,
            memory=self.memory,
        )

    def reload(self, config: Config, confirm: ConfirmCallback | None = None) -> None:
        """Пересоздаёт LLM, разрешения и агент под новый конфиг."""
        self.config = config
        if config.spotify.enabled and "spotify" not in self.registry:
            self.registry.register_many(spotify_tools())
        _inject_settings(self.registry, config)
        _inject_memory(self.registry, self.memory)
        self.llm = LLMClient(config)
        _inject_llm(self.registry, self.llm)
        self.permissions = PermissionManager(config)
        self.rebuild_agent(confirm)


def _inject_settings(registry: ToolRegistry, config: Config) -> None:
    """Передаёт инструментам настройки из конфига (реестр, пути, ключи)."""
    create_tool = registry.get("create_tool")
    if create_tool is not None:
        create_tool.registry = registry
        create_tool.plugins_dir = PLUGINS_DIR

    spotify = registry.get("spotify")
    if spotify is not None:
        spotify.settings = config.spotify

    for name in ("see_screen", "control_screen"):
        tool = registry.get(name)
        if tool is not None:
            tool.settings = config.screen
            if hasattr(tool, "llm_config"):
                tool.llm_config = config.llm

    # Ограничение файловых операций рабочей папкой
    guard = PathGuard(
        workspace=config.tools.workspace,
        allow_outside=config.tools.allow_outside_workspace,
    )
    for name in (
        "write_file",
        "append_file",
        "create_directory",
        "delete_path",
        "screenshot",
        "run_python",
        "run_shell",
    ):
        tool = registry.get(name)
        if tool is not None and hasattr(tool, "guard"):
            tool.guard = guard

    run_python = registry.get("run_python")
    if run_python is not None:
        run_python.workspace = config.tools.workspace


def _inject_memory(registry: ToolRegistry, memory: Memory | None) -> None:
    for tool_name in ("remember_fact", "recall_fact"):
        tool = registry.get(tool_name)
        if tool is not None:
            tool.memory = memory


def _inject_llm(registry: ToolRegistry, llm: LLMClient) -> None:
    """Отдаёт инструментам экрана ссылку на LLM для определения vision."""
    for name in ("see_screen", "control_screen"):
        tool = registry.get(name)
        if tool is not None and hasattr(tool, "llm"):
            tool.llm = llm


def build_core(config: Config, confirm: ConfirmCallback | None = None) -> Core:
    """Создаёт и связывает подсистемы ядра."""
    registry = ToolRegistry()
    registry.register_many(all_builtin_tools())
    registry.register_many(generate_tools())
    registry.register_many(memory_tools())
    if config.spotify.enabled:
        registry.register_many(spotify_tools())

    plugin_count = registry.load_plugins(PLUGINS_DIR)
    logger.info(
        f"Инструментов зарегистрировано: {len(registry)} (плагинов: {plugin_count})"
    )

    # --- память ---
    memory = Memory(config.memory.database)

    # --- инъекция контекста в инструменты, которым нужен реестр/память/пути ---
    _inject_settings(registry, config)
    _inject_memory(registry, memory)

    llm = LLMClient(config)
    _inject_llm(registry, llm)
    permissions = PermissionManager(config)
    agent = Agent(
        config=config,
        llm=llm,
        registry=registry,
        permissions=permissions,
        confirm=confirm,
        memory=memory,
    )

    return Core(
        config=config,
        llm=llm,
        registry=registry,
        permissions=permissions,
        agent=agent,
        memory=memory,
        tools=list(registry),
    )
