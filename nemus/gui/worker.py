"""Фоновое выполнение агента: поток + события для GUI."""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QThread, Signal

from nemus.core.agent import Agent, AgentEvent
from nemus.core.tools.base import Tool


class AgentWorker(QThread):
    """Выполняет :meth:`Agent.run` в отдельном потоке, транслируя события в GUI."""

    event = Signal(object)  # AgentEvent
    confirm_requested = Signal(object)  # (Tool, dict)
    finished_run = Signal()

    def __init__(self, agent: Agent, text: str, parent: Any = None) -> None:
        super().__init__(parent)
        self.agent = agent
        self.text = text
        self._confirm_ready = threading.Event()
        self._confirm_value = False

    # --------------------------------------------- вызывается рабочим потоком
    def _request_confirmation(self, tool: Tool, args: dict[str, Any]) -> bool:
        self._confirm_ready.clear()
        self._confirm_value = False
        self.confirm_requested.emit((tool, dict(args)))
        self._confirm_ready.wait()
        return self._confirm_value

    # --------------------------------------------- вызывается главным потоком
    def resolve_confirmation(self, allowed: bool) -> None:
        """Разрешает или отклоняет подтверждение."""
        self._confirm_value = allowed
        self._confirm_ready.set()

    # ---------------------------------------------------------------- QThread
    def run(self) -> None:  # noqa: D102
        self.agent.confirm = self._request_confirmation
        try:
            for agent_event in self.agent.run(self.text):
                self.event.emit(agent_event)
        except Exception as exc:  # noqa: BLE001
            self.event.emit(AgentEvent(kind="error", error=str(exc)))
        finally:
            self.finished_run.emit()


__all__ = ["AgentWorker"]
