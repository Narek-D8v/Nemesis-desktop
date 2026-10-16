"""Менеджер разрешений: оценка риска и решение о подтверждении."""

from __future__ import annotations

from enum import Enum

from nemus.config import Config
from nemus.core.tools.base import RiskLevel, Tool


class Decision(str, Enum):
    """Решение по вызову инструмента."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionManager:
    """Решает, можно ли выполнить инструмент без подтверждения."""

    def __init__(self, config: Config) -> None:
        self.mode = config.tools.permission_mode
        self.auto_allow = set(config.tools.auto_allow)
        self.deny = set(config.tools.deny)

    def check(self, tool: Tool) -> Decision:
        if tool.name in self.deny:
            return Decision.DENY
        if self.mode == "full-auto":
            return Decision.ALLOW
        if self.mode == "confirm-all":
            return Decision.ALLOW if tool.name in self.auto_allow else Decision.ASK
        # smart
        if tool.name in self.auto_allow:
            return Decision.ALLOW
        if tool.risk == RiskLevel.SAFE:
            return Decision.ALLOW
        return Decision.ASK

    def describe(self, decision: Decision) -> str:
        return {
            Decision.ALLOW: "разрешено",
            Decision.ASK: "требует подтверждения",
            Decision.DENY: "запрещено",
        }[decision]
