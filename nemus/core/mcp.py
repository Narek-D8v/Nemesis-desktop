"""Model Context Protocol (MCP) интеграция.

Подключает MCP-прокси-сервера (через stdio) и добавляет их инструменты в
реестр инструментов так же, как плагины. Для модели это обычные инструменты:
они видят их в списке схем и вызывают через ``registry.execute()``.

Схема (config.yaml)::

    mcp:
      enabled: true
      refresh_seconds: 60
      servers:
        - name: Файлы
          command: npx
          args: ["-y", "@modelcontextprotocol/server-filesystem", "data"]
          cwd: .
          env: {}
          enabled: true

Каждый сервер — stdio-процесс (Python или Node.js). Подключение и сессия
живут в постоянном событийном цикле фонового потока, поэтому инструменты
можно вызывать многократно без переподключения. Список инструментов
обновляется по TTL (``refresh_seconds``) либо принудительно (``refresh()``).

Имена регистрируемых инструментов неймспейсятся: ``<сервер>__<инструмент>``
(например ``filesystem__read_file``), чтобы инструменты разных серверов
не перетирали друг друга в общем реестре. Оригинальное имя сохраняется
в ``MCPTool._remote_name`` и уходит серверу при вызове.

Все MCP-инструменты получают :attr:`RiskLevel.DANGEROUS`: это внешний код,
который не аудируется правилами разрешений Немуси.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from nemus.config import PROJECT_ROOT, Config, MCPServerConfig
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

try:  # опциональная зависимость: без пакета mcp интеграция отключается
    import mcp
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError as exc:  # pragma: no cover - зависит от окружения
    mcp = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    _MCP_IMPORT_ERROR: Exception | None = exc
else:
    _MCP_IMPORT_ERROR = None

#: таймаут на один вызов инструмента (секунды)
CALL_TIMEOUT = 120.0
#: таймаут на подключение сервера и получение списка инструментов
CONNECT_TIMEOUT = 60.0
#: остановка фонового цикла MCP (секунды)
LOOP_STOP_TIMEOUT = 2.0
#: ожидание чужого refresh (секунды)
REFRESH_LOCK_TIMEOUT = 10.0
#: корректное закрытие всех подключений (секунды)
CLOSE_TIMEOUT = 5.0

#: Готовые пресеты MCP-серверов: (ключ, команда, аргументы, env).
#: «{path}» подставляется рабочей папкой ассистента; ключи — для
#: локализованных названий ``mcp.preset.<ключ>`` в GUI.
MCP_PRESETS: tuple[tuple[str, str, list[str], dict[str, str]], ...] = (
    (
        "filesystem",
        "npx",
        ["-y", "@modelcontextprotocol/server-filesystem", "{path}"],
        {},
    ),
    ("everything", "npx", ["-y", "@modelcontextprotocol/server-everything"], {}),
    ("memory", "npx", ["-y", "@modelcontextprotocol/server-memory"], {}),
    ("fetch", "npx", ["-y", "@modelcontextprotocol/server-fetch"], {}),
    ("git", "npx", ["-y", "@modelcontextprotocol/server-git"], {}),
    ("github", "npx", ["-y", "@modelcontextprotocol/server-github"], {"GITHUB_TOKEN": ""}),
    ("time", "npx", ["-y", "mcp-server-time"], {}),
)


def parse_claude_config(text: str) -> list[MCPServerConfig]:
    """Разбирает ``mcp_config.json`` (формат Claude Desktop) в список серверов.

    Ожидается объект ``{"mcpServers": {"имя": {"command": ..., "args": [...],
    "env": {...}}, ...}}``. Имя ключа становится названием сервера; значения
    ``${env:VAR}`` не подставляются — копируются как есть.
    """
    data = json.loads(text)
    raw = data.get("mcpServers") or {}
    servers: list[MCPServerConfig] = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        command = str(spec.get("command") or "").strip()
        if not command:
            continue
        servers.append(
            MCPServerConfig(
                name=str(name),
                command=command,
                args=[str(a) for a in (spec.get("args") or [])],
                cwd=".",
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                enabled=True,
            )
        )
    return servers


# ------------------------------------------------------------- event loop
class _LoopRunner:
    """Постоянный asyncio-цикл в фоновом потоке.

    Нужен потому, что у GUI-агента уже есть работающий Qt-цикл, а MCP-сессии
    привязаны к одному событийному циклу на всё время жизни.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever,
                    name="nemus-mcp-loop",
                    daemon=True,
                )
                thread.start()
                self._loop = loop
                self._thread = thread
            assert self._loop is not None
            return self._loop

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Выполняет корутину в фоновом цикле и ждёт результат."""
        loop = self._ensure()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout)

    def stop(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=LOOP_STOP_TIMEOUT)


_LOOP = _LoopRunner()


# ---------------------------------------------------------------- helpers
def _resolve(path: str | os.PathLike[str] | None) -> str:
    """Приводит относительный путь к абсолютному относительно корня проекта."""
    if not path:
        return str(PROJECT_ROOT)
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return str(resolved)


def _sanitize_ns(text: str) -> str:
    """Нормализует фрагмент имени в допустимый для LiteLLM namespace."""
    cleaned = re.sub(r"[^\w-]+", "_", text.strip(), flags=re.UNICODE)
    return cleaned.strip("_")


def _server_namespace(server: MCPServerConfig, index: int, used: set[str]) -> str:
    """Уникальный префикс имён инструментов сервера («filesystem», «github_2»…).

    Берётся из человекочитаемого ``name``, иначе — из имени команды; коллизии
    между серверами разрешаются суффиксом ``_2``, ``_3``…
    """
    base = _sanitize_ns(server.name) or _sanitize_ns(Path(server.command).name)
    if not base:
        base = f"server{index}"
    ns = base
    suffix = 2
    while ns in used:
        ns = f"{base}_{suffix}"
        suffix += 1
    used.add(ns)
    return ns


class _ServerConnection:
    """Открытое подключение к одному MCP-серверу.

    ``stop`` и ``task`` относятся к задаче-супервизору, которая открывает
    и закрывает ``AsyncExitStack`` в одном и том же контексте задачи
    (требование anyio/stdio_client).
    """

    __slots__ = ("key", "ns", "session", "stop", "task", "config")

    def __init__(
        self, key: str, session: Any, ns: str = "", config: MCPServerConfig | None = None
    ) -> None:
        self.key = key
        self.ns = ns
        self.config = config
        self.session = session
        self.stop = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None


async def _serve_server(
    key: str, server: MCPServerConfig, ready: asyncio.Future[Any], ns: str = ""
) -> Any:
    """Держит stdio-сессию сервера открытой до сигнала ``stop``."""
    params = StdioServerParameters(
        command=server.command,
        args=list(server.args),
        env={**os.environ, **server.env} if server.env else None,
        cwd=_resolve(server.cwd),
    )
    stack = AsyncExitStack()
    try:
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(params)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        tools_result = await session.list_tools()
        connection = _ServerConnection(key, session, ns, server)
        if not ready.done():
            ready.set_result((connection, tools_result))
        await connection.stop.wait()
    except BaseException as exc:  # noqa: BLE001
        if not ready.done():
            ready.set_exception(exc)
        else:
            logger.warning(f"MCP-сервер '{key}' завершился: {exc}")
    finally:
        await stack.aclose()


async def _call_tool(
    session: Any, name: str, arguments: dict[str, Any]
) -> Any:
    """Вызывает инструмент и возвращает сырой ``CallToolResult``."""
    return await session.call_tool(
        name, arguments, read_timeout_seconds=CALL_TIMEOUT
    )


def _render_result(result: Any) -> tuple[str, list[str], bool]:
    """Превращает ``CallToolResult`` в текст, изображения и флаг ошибки."""
    parts: list[str] = []
    images: list[str] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "") or ""
            if text:
                parts.append(text)
        elif block_type == "image":
            data = getattr(block, "data", "") or ""
            mime = getattr(block, "mime_type", "") or "image/png"
            if data:
                images.append(f"data:{mime};base64,{data}")
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            uri = getattr(resource, "uri", None)
            parts.append(str(text or uri or ""))
        else:
            parts.append(str(block))
    text = "\n".join(part for part in parts if part)
    return text, images, bool(getattr(result, "is_error", False))


# ------------------------------------------------------------------- probe
def package_installed() -> bool:
    """True, если опциональный пакет ``mcp`` установлен."""
    return mcp is not None


def probe_server(server: MCPServerConfig, timeout: float = CONNECT_TIMEOUT) -> tuple[bool, str]:
    """Проверяет один сервер: подключается, читает инструменты, отключается.

    Нужно для кнопки «Проверить» во вкладке MCP: вызывается в фоновом потоке
    GUI, поэтому готовый ``async``-доступ здесь не требуется. Возвращает
    ``(ok, message)``: при успехе — имена инструментов, при ошибке — текст
    исключения.
    """
    if mcp is None:
        return False, str(_MCP_IMPORT_ERROR or "Пакет mcp не установлен (pip install mcp).")

    async def _one_shot() -> str:
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env={**os.environ, **server.env} if server.env else None,
            cwd=_resolve(server.cwd),
        )
        async with AsyncExitStack() as stack:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            tools_result = await session.list_tools()
            names = [tool.name for tool in (getattr(tools_result, "tools", None) or [])]
            return ", ".join(names)

    try:
        names = _LOOP.run(_one_shot(), timeout)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"MCP-проверка '{server.command}' не удалась: {exc}")
        return False, str(exc)
    return True, names or "(сервер отвечает, инструментов нет)"


# ---------------------------------------------------------------- MCPTool
class MCPTool(Tool):
    """Инструмент, полученный от внешнего MCP-сервера.

    ``name`` — неймспейсенное имя (``сервер__инструмент``), которое видит
    модель; ``_remote_name`` — оригинальное имя на сервере, уходит ему
    в ``call_tool``.
    """

    _session: Any = None
    _server_key: str = ""
    _remote_name: str = ""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        """Синхронно вызывает MCP-инструмент через фоновый event loop."""
        session = self._session
        if session is None:
            return ToolResult.failure(
                f"Инструмент '{self.name}' не подключён (нет MCP-сессии)."
            )
        remote = self._remote_name or self.name
        logger.debug(f"MCP → {self._server_key}: {remote}({kwargs})")
        try:
            raw = _LOOP.run(_call_tool(session, remote, kwargs), CALL_TIMEOUT + 10)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Ошибка вызова MCP-инструмента {self.name}")
            return ToolResult.failure(f"{self.name} упал: {exc}")
        text, images, is_error = _render_result(raw)
        logger.debug(
            f"MCP ← {self._server_key}: {remote}: {len(text)} симв."
            f"{', ошибка' if is_error else ''}"
        )
        if is_error:
            return ToolResult.failure(text or f"{self.name} вернул ошибку.")
        return ToolResult.success(text or "(пустой результат)", images=images)


# ------------------------------------------------------------- MCPManager
class MCPManager:
    """Управляет подключениями MCP-серверов и регистрацией их инструментов."""

    def __init__(self, config: Config, registry: Any) -> None:
        self.config = config
        self.registry = registry
        self.refresh_seconds = max(1, int(config.mcp.refresh_seconds))
        self._tool_cache: dict[str, MCPTool] = {}
        self._connections: dict[str, _ServerConnection] = {}
        self._last_refresh = 0.0
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        servers = self._server_list()
        self.enabled = bool(mcp is not None and config.mcp.enabled and servers)
        if _MCP_IMPORT_ERROR is not None:
            logger.debug("Пакет mcp не установлен — MCP-инструменты отключены.")
        self._watcher: threading.Thread | None = None
        if self.enabled:
            self._watcher = threading.Thread(
                target=self._watch, name="nemus-mcp-watch", daemon=True
            )
            self._watcher.start()
        atexit.register(self.close)

    # ------------------------------------------------------------- public
    @property
    def tool_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tool_cache)

    def server_status(self, server: MCPServerConfig) -> tuple[bool, int]:
        """Статус сервера для GUI: ``(подключён, сколько инструментов даёт)``.

        Сопоставление — по значениям полей конфига, поэтому работает и с
        копиями моделей, которые держит диалог настроек.
        """
        key: str | None = None
        with self._lock:
            counts: dict[str, int] = {}
            for name in self._tool_cache:
                tool_key = self._tool_cache[name]._server_key
                counts[tool_key] = counts.get(tool_key, 0) + 1
        for conn_key, connection in list(self._connections.items()):
            if connection.config == server:
                key = conn_key
                break
        if key is None:
            return False, 0
        return True, counts.get(key, 0)

    def ensure_ready(self, *, force: bool = False) -> bool:
        """Подключает серверы и регистрирует инструменты (по TTL)."""
        if not self.enabled:
            return False
        now = time.monotonic()
        if force or not self._connections:
            self.refresh()
        elif now - self._last_refresh > self.refresh_seconds:
            self._refresh_tools()
        with self._lock:
            return bool(self._tool_cache)

    def _watch(self) -> None:
        """Фоновая проверка TTL: перечитывает списки инструментов."""
        while not self._stop.wait(self.refresh_seconds):
            try:
                self.ensure_ready()
            except Exception:  # noqa: BLE001
                logger.exception("Периодическое обновление MCP не удалось")

    def _refresh_tools(self) -> None:
        """Перечитывает инструменты на уже открытых сессиях (без реконнекта)."""
        if not self._refresh_lock.acquire(blocking=False):
            # кто-то уже обновляет соединения — пропускаем этот тик
            return
        try:
            self._refresh_tools_locked()
        finally:
            self._refresh_lock.release()

    def _refresh_tools_locked(self) -> None:
        try:
            listed = _LOOP.run(self._list_all())
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось перечитать список MCP-инструментов")
            return
        with self._lock:
            old_names = list(self._tool_cache)
            self._tool_cache.clear()
        for name in old_names:
            self.registry.unregister(name)
        for key, connection, tools_result, error in listed:
            if error is not None:
                logger.warning(f"MCP-сервер '{key}': {error}")
                continue
            self._register_tools(connection, tools_result)
        self._last_refresh = time.monotonic()

    async def _list_all(
        self,
    ) -> list[tuple[str, _ServerConnection, Any, BaseException | None]]:
        """Запрашивает списки инструментов у всех живых сессий."""
        listed: list[tuple[str, _ServerConnection, Any, BaseException | None]] = []
        for key, connection in list(self._connections.items()):
            try:
                tools_result = await connection.session.list_tools()
            except BaseException as exc:  # noqa: BLE001
                listed.append((key, connection, None, exc))
                continue
            listed.append((key, connection, tools_result, None))
        return listed

    def refresh(self) -> None:
        """Принудительно перечитывает инструменты со всех серверов."""
        if not self.enabled:
            return
        if not self._refresh_lock.acquire(timeout=REFRESH_LOCK_TIMEOUT):
            logger.warning("MCP: обновление уже выполняется — пропуск")
            return
        try:
            self._refresh_locked()
        finally:
            self._refresh_lock.release()

    def _refresh_locked(self) -> None:
        servers = self._server_list()
        try:
            opened = _LOOP.run(self._reload(servers))
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось обновить MCP-подключения")
            return

        with self._lock:
            old_names = list(self._tool_cache)
            self._tool_cache.clear()
        for name in old_names:
            self.registry.unregister(name)
        for key, connection, tools_result, error in opened:
            if error is not None:
                logger.warning(f"MCP-сервер '{key}': {error}")
                continue
            self._register_tools(connection, tools_result)
        self._last_refresh = time.monotonic()

    def close(self) -> None:
        """Останавливает наблюдатель и закрывает все подключения."""
        self._stop.set()
        if not self.enabled:
            return
        acquired = self._refresh_lock.acquire(timeout=CLOSE_TIMEOUT)
        try:
            try:
                _LOOP.run(self._close_all(), timeout=CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось корректно закрыть MCP-подключения")
        finally:
            if acquired:
                self._refresh_lock.release()

    # ------------------------------------------------------------ internal
    def _server_list(self) -> list[MCPServerConfig]:
        """Включённые в конфиге серверы."""
        servers = getattr(self.config.mcp, "servers", None) or []
        return [server for server in servers if server.enabled]

    async def _reload(
        self, servers: list[MCPServerConfig]
    ) -> list[tuple[str, _ServerConnection | None, Any, BaseException | None]]:
        """Пересоздаёт все подключения; вызывается в фоновом цикле."""
        await self._close_all()

        loop = asyncio.get_running_loop()
        pending: list[tuple[str, asyncio.Future[Any]]] = []
        tasks: dict[str, asyncio.Task[Any]] = {}
        used_ns: set[str] = set()
        for index, server in enumerate(servers):
            key = f"{server.command}#{index}"
            ns = _server_namespace(server, index, used_ns)
            ready: asyncio.Future[Any] = loop.create_future()
            task = asyncio.create_task(
                _serve_server(key, server, ready, ns), name=f"mcp:{key}"
            )
            # задача сама закроет стек в своём контексте; ошибки — через ready
            pending.append((key, ready))
            tasks[key] = task
            task.add_done_callback(lambda t, k=key: self._on_supervisor_done(k, t))

        opened: list[
            tuple[str, _ServerConnection | None, Any, BaseException | None]
        ] = []
        for key, ready in pending:
            try:
                connection, tools_result = await asyncio.wait_for(
                    ready, timeout=CONNECT_TIMEOUT
                )
            except BaseException as exc:  # noqa: BLE001
                opened.append((key, None, None, exc))
                continue
            connection.task = tasks[key]
            if tasks[key].done() and tasks[key].exception() is not None:
                opened.append((key, None, None, tasks[key].exception()))
                continue
            self._connections[key] = connection
            opened.append((key, connection, tools_result, None))
        return opened

    def _on_supervisor_done(self, key: str, task: asyncio.Task[Any]) -> None:
        """Убирает соединение, если супервизор неожиданно завершился."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(f"MCP-сервер '{key}' остановлен: {exc}")
        self._connections.pop(key, None)

    async def _close_all(self) -> None:
        """Останавливает все задачи-супервизоры и закрывает подключения."""
        connections = list(self._connections.values())
        self._connections.clear()
        for connection in connections:
            connection.stop.set()
        for connection in connections:
            task = connection.task
            if task is None or task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001
                task.cancel()

    def _register_tools(self, connection: _ServerConnection, tools_result: Any) -> None:
        """Регистрирует инструменты одного сервера в реестре (с неймспейсом)."""
        tools = getattr(tools_result, "tools", None) or []
        for spec in tools:
            name = getattr(spec, "name", None)
            if not name:
                continue
            schema = getattr(spec, "input_schema", None)
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            tool = MCPTool()
            tool.name = f"{connection.ns}__{name}" if connection.ns else name
            tool.description = getattr(spec, "description", "") or ""
            tool.parameters = schema
            tool.risk = RiskLevel.DANGEROUS
            tool._session = connection.session
            tool._server_key = connection.key
            tool._remote_name = name
            try:
                self.registry.register(tool)
            except Exception:  # noqa: BLE001
                logger.exception(f"Не удалось зарегистрировать MCP-инструмент '{name}'")
                continue
            with self._lock:
                self._tool_cache[tool.name] = tool
            logger.info(
                f"Зарегистрирован MCP-инструмент '{tool.name}' ({connection.key})"
            )
