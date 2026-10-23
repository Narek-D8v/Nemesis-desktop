"""Базовый класс realtime-сессии и общие типы."""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from nemus.core.language import REPLY_AUTO, normalize, reply_language_directive
from nemus.core.permissions import Decision, PermissionManager
from nemus.core.tools.base import Tool, ToolResult
from nemus.core.tools.registry import ToolRegistry
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets
from nemus.voice.params import SAMPLE_RATE


class RealtimeState(str, Enum):
    """Состояние realtime-сессии (для GUI)."""

    CONNECTING = "connecting"
    READY = "ready"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"
    CLOSED = "closed"


@dataclass
class RealtimeCallbacks:
    """Колбэки, вызываемые из потока realtime-сессии."""

    on_state: Callable[[RealtimeState], None] | None = None
    on_user_text: Callable[[str], None] | None = None
    on_assistant_text: Callable[[str], None] | None = None
    on_error: Callable[[str], None] | None = None


class BaseRealtimeSession(ABC):
    """Абстрактная сессия реального времени.

    Управляет собственным event-циклом asyncio в отдельном потоке.
    Подклассы реализуют протокол конкретного провайдера.
    """

    provider: str = ""
    send_rate: int = SAMPLE_RATE
    receive_rate: int = 24000

    def __init__(
        self,
        callbacks: RealtimeCallbacks,
        registry: ToolRegistry,
        permissions: PermissionManager,
        confirm: Callable[[Tool, dict[str, Any]], bool] | None = None,
        system_prompt: str = "",
        barge_in: bool = True,
        noise_reducer: Any = None,
        reply_language: str = REPLY_AUTO,
    ) -> None:
        self.cb = callbacks
        self.registry = registry
        self.permissions = permissions
        self.confirm = confirm
        self.system_prompt = system_prompt
        self.barge_in = barge_in
        self.noise_reducer = noise_reducer
        #: язык ответов: auto — язык собеседника, либо фиксированный тег
        self.reply_language = normalize(reply_language)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_state: RealtimeState | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Запускает сессию в фоновом потоке. Возвращает False при сбое."""
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name=f"realtime-{self.provider}"
        )
        self._thread.start()
        return True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Realtime {self.provider} аварийно завершён")
            self._emit_error(str(exc))
        finally:
            self._running = False
            self._emit_state(RealtimeState.CLOSED)

    @abstractmethod
    async def _run(self) -> None:
        """Основной цикл сессии (реализуется подклассом)."""

    def stop(self) -> None:
        """Просит сессию корректно закрыться."""
        self._running = False
        self._schedule(self._close())

    @abstractmethod
    async def _close(self) -> None:
        """Закрывает соединение (реализуется подклассом)."""

    def send_text(self, text: str) -> None:
        """Отправляет текстовый ввод в сессию (потокобезопасно)."""
        if self._running:
            self._schedule(self._send_text(text))

    @abstractmethod
    async def _send_text(self, text: str) -> None: ...

    # --------------------------------------------------------------- helpers
    def language_instruction(self) -> str:
        """Инструкция о языке ответа (добавляется к системному промпту)."""
        return reply_language_directive(self.reply_language)

    def _schedule(self, coro) -> None:
        """Запускает корутину в потоке сессии."""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _emit_state(self, state: RealtimeState) -> None:
        # каждые 60 мс приходит новый аудио-чанк: не дёргаем GUI повтором
        if state == self._last_state:
            return
        self._last_state = state
        if self.cb.on_state:
            try:
                self.cb.on_state(state)
            except Exception:  # noqa: BLE001
                pass

    def _emit_user_text(self, text: str) -> None:
        if text and self.cb.on_user_text:
            self.cb.on_user_text(text)

    def _emit_assistant_text(self, text: str) -> None:
        if text and self.cb.on_assistant_text:
            self.cb.on_assistant_text(text)

    def _emit_error(self, message: str) -> None:
        message = redact_secrets(message)
        logger.warning(f"Realtime {self.provider}: {message}")
        if self.cb.on_error:
            self.cb.on_error(message)

    # ----------------------------------------------------------- tool calls
    def _execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Выполняет инструмент с учётом разрешений и подтверждения."""
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult.failure(f"Инструмент '{name}' не найден.")
        decision = self.permissions.check(tool)
        if decision == Decision.DENY:
            return ToolResult.failure(f"Инструмент '{name}' запрещён политикой.")
        if decision == Decision.ASK:
            if self.confirm is None or not self.confirm(tool, args):
                return ToolResult.failure(f"Пользователь отклонил '{name}'.")
        return self.registry.execute(name, args)
