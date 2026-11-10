"""Сборка ядра Немуси: реестр инструментов, LLM, разрешения, агент."""

from __future__ import annotations

from dataclasses import dataclass, field

from nemus.config import PROJECT_ROOT, Config
from nemus.core.agent import Agent, ConfirmCallback
from nemus.core.llm import LLMClient
from nemus.core.mcp import MCPManager
from nemus.core.memory import Memory
from nemus.core.notes import NoteBook
from nemus.core.permissions import PermissionManager
from nemus.core.reminder import ReminderScheduler
from nemus.core.tools.all import all_builtin_tools
from nemus.core.tools.base import Tool
from nemus.core.tools.generate import generate_tools
from nemus.core.tools.images import image_tools
from nemus.core.tools.memory_tools import memory_tools
from nemus.core.tools.notes_tools import notes_tools
from nemus.core.tools.registry import ToolRegistry
from nemus.core.tools.remind import remind_tools
from nemus.core.tools.sandbox import PathGuard
from nemus.core.tools.skills_tool import skill_tools
from nemus.core.tools.spotify import spotify_tools
from nemus.core.tools.telegram import telegram_tools
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
    notes: NoteBook | None = None
    tools: list[Tool] = field(default_factory=list)
    mcp_manager: MCPManager | None = None
    reminder: ReminderScheduler | None = None

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
        _apply_memory_tools(self.registry, config)
        _apply_notes(self.registry, config)
        _apply_skills(self.registry, config)
        _apply_images(self.registry, config)
        _apply_telegram(self.registry, config)
        _inject_settings(self.registry, config)
        _inject_memory(self.registry, self.memory)
        _inject_reminder(self.registry, self.reminder)
        self.notes = _inject_notes(self.registry, config)
        self.llm = LLMClient(config)
        _inject_llm(self.registry, self.llm)
        self.permissions = PermissionManager(config)

        # MCP: пересоздаём менеджер под новый конфиг и снимаем старые инструменты.
        if self.mcp_manager is not None:
            for name in self.mcp_manager.tool_names:
                self.registry.unregister(name)
            self.mcp_manager.close()
        self.mcp_manager = MCPManager(config, self.registry)
        self.mcp_manager.ensure_ready()

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

    # Генерация изображений: ключи провайдеров, модели и рабочая папка
    image_tool = registry.get("generate_image")
    if image_tool is not None:
        openai = config.llm.providers.get("openai")
        gemini = config.llm.providers.get("gemini")
        openrouter = config.llm.providers.get("openrouter")
        image_tool.openai_api_key = (openai.api_key if openai else "") or ""
        image_tool.openai_api_base = (openai.api_base if openai else "") or ""
        image_tool.gemini_api_key = (gemini.api_key if gemini else "") or ""
        image_tool.openrouter_api_key = (openrouter.api_key if openrouter else "") or ""
        image_tool.openrouter_api_base = (
            (openrouter.api_base if openrouter else "")
            or "https://openrouter.ai/api/v1"
        )
        images_cfg = getattr(config, "images", None)
        if images_cfg is not None:
            image_tool.provider = images_cfg.provider
            image_tool.openai_model = images_cfg.openai_model
            image_tool.gemini_model = images_cfg.gemini_model
            image_tool.openrouter_model = images_cfg.openrouter_model
            image_tool.pollinations_model = images_cfg.pollinations_model
        image_tool.workspace = config.tools.workspace

    # Презентации и видео: рабочая папка для готовых файлов
    for name in ("create_presentation", "create_video"):
        tool = registry.get(name)
        if tool is not None and hasattr(tool, "workspace"):
            tool.workspace = config.tools.workspace

    # Скиллы: папка и отключённые в настройках скиллы
    skills_cfg = getattr(config, "skills", None)
    if skills_cfg is not None:
        for name in ("load_skill", "create_skill"):
            tool = registry.get(name)
            if tool is not None:
                tool.skills_dir = skills_cfg.dir
                if hasattr(tool, "disabled"):
                    tool.disabled = list(skills_cfg.disabled)


def _apply_telegram(registry: ToolRegistry, config: Config) -> None:
    """Включает инструменты Telegram (файлы и реакции), если бот настроен."""
    tg = getattr(config, "telegram", None)
    ready = bool(tg and tg.enabled and tg.token and tg.allowed_users)
    names = ("send_telegram", "react_telegram")
    if ready:
        for tool in telegram_tools():
            if tool.name not in registry:
                registry.register_many([tool])
    else:
        for name in names:
            registry.unregister(name)


def _apply_images(registry: ToolRegistry, config: Config) -> None:
    """Регистрирует генерацию изображений: бесплатный провайдер не требует ключа."""
    if "generate_image" not in registry:
        registry.register_many(image_tools())


def _apply_skills(registry: ToolRegistry, config: Config) -> None:
    """Включает/выключает инструменты скиллов по ``skills.enabled``."""
    if getattr(config, "skills", None) is None:
        return
    if config.skills.enabled:
        if "load_skill" not in registry:
            registry.register_many(skill_tools())
    else:
        registry.unregister("load_skill")
        registry.unregister("create_skill")


def _apply_memory_tools(registry: ToolRegistry, config: Config) -> None:
    """Включает/выключает инструменты долговременной памяти по ``memory.long_term``."""
    if config.memory.long_term:
        if "remember_fact" not in registry:
            registry.register_many(memory_tools())
    else:
        registry.unregister("remember_fact")
        registry.unregister("recall_fact")


def _inject_reminder(
    registry: ToolRegistry, reminder: ReminderScheduler | None
) -> None:
    """Отдаёт инструменту remind общий планировщик напоминаний."""
    tool = registry.get("remind")
    if tool is not None and hasattr(tool, "scheduler"):
        tool.scheduler = reminder


def _inject_memory(registry: ToolRegistry, memory: Memory | None) -> None:
    for tool_name in ("remember_fact", "recall_fact"):
        tool = registry.get(tool_name)
        if tool is not None:
            tool.memory = memory


def _apply_notes(registry: ToolRegistry, config: Config) -> None:
    """Включает/выключает инструменты заметок по ``memory.notes``."""
    if config.memory.notes:
        if "write_note" not in registry:
            registry.register_many(notes_tools())
    else:
        registry.unregister("write_note")
        registry.unregister("read_notes")


def _inject_notes(registry: ToolRegistry, config: Config) -> NoteBook | None:
    """Создаёт NoteBook по конфигу и отдаёт его инструментам заметок."""
    if not config.memory.notes:
        return None
    notebook = NoteBook(config.memory.notes_file)
    for tool_name in ("write_note", "read_notes"):
        tool = registry.get(tool_name)
        if tool is not None:
            tool.notebook = notebook
    return notebook


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
    reminder = ReminderScheduler()
    registry.register_many(remind_tools(reminder))
    _apply_memory_tools(registry, config)
    _apply_notes(registry, config)
    _apply_skills(registry, config)
    _apply_images(registry, config)
    _apply_telegram(registry, config)
    if config.spotify.enabled:
        registry.register_many(spotify_tools())

    plugin_count = registry.load_plugins(PLUGINS_DIR)
    logger.info(
        f"Инструментов зарегистрировано: {len(registry)} (плагинов: {plugin_count})"
    )

    # --- Model Context Protocol (MCP) ---
    mcp_manager = MCPManager(config, registry)
    if mcp_manager.ensure_ready():
        logger.info(f"Загружено МП-инструментов: {len(mcp_manager.tool_names)}")

    # --- память ---
    memory = Memory(config.memory.database)

    # --- инъекция контекста в инструменты, которым нужен реестр/память/пути ---
    _inject_settings(registry, config)
    _inject_memory(registry, memory)
    notes = _inject_notes(registry, config)

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
        notes=notes,
        tools=list(registry),
        mcp_manager=mcp_manager,
        reminder=reminder,
    )
