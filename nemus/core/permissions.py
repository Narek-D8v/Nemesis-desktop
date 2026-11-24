"""Менеджер разрешений: оценка риска и решение о подтверждении."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from nemus.config import Config
from nemus.core.tools.base import RiskLevel, Tool


class Decision(str, Enum):
    """Решение по вызову инструмента."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


#: команды, которые меняют систему, а не просто читают её
_INSTALL_RE = re.compile(
    r"(?i)(?:\b(?:winget|choco|scoop)(?:\.exe|\.ps1)?\s+(?:install|upgrade|uninstall)\b"
    r"|\bmsiexec(?:\.exe)?\b"
    r"|\b(?:pip|pip3|python\s+-m\s+pip|npm|pnpm|yarn)\s+(?:install|i|add|upgrade)\b"
    r"|\b(?:apt|apt-get|dnf|yum|brew)\s+install\b"
    r"|\bstart-process\b"
    r"|[\"'][^\"']+\.(?:exe|msi)[\"'])"
)
#: инструменты, через которые выполняются такие команды
_COMMAND_TOOLS = frozenset({"run_shell", "run_python"})


def is_install_command(args: dict[str, Any] | None) -> bool:
    """True, если аргументы вызова содержат реальную установку/удаление."""
    if not args:
        return False
    text = " ".join(str(args.get(key, "") or "") for key in ("command", "input", "code"))
    return bool(_INSTALL_RE.search(text))


class PermissionManager:
    """Решает, можно ли выполнить инструмент без подтверждения."""

    def __init__(self, config: Config) -> None:
        self.mode = config.tools.permission_mode
        self.auto_allow = set(config.tools.auto_allow)
        self.deny = set(config.tools.deny)
        self.confirm_installs = bool(getattr(config.tools, "confirm_installs", True))

    def check(self, tool: Tool, args: dict[str, Any] | None = None) -> Decision:
        if tool.name in self.deny:
            return Decision.DENY
        # Установка меняет ноутбук: даже full-auto не должен тихо её выполнять.
        if self.confirm_installs:
            if tool.name == "download_file":
                return Decision.ASK
            if tool.name in _COMMAND_TOOLS and is_install_command(args):
                return Decision.ASK
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
