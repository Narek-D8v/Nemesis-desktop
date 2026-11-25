"""Модуль собирает все встроенные инструменты в единый список."""

from __future__ import annotations

from nemus.core.tools.base import Tool
from nemus.core.tools.basic import basic_tools
from nemus.core.tools.browser import browser_tools
from nemus.core.tools.downloads import download_tools
from nemus.core.tools.files import file_tools
from nemus.core.tools.keyboard import keyboard_tools
from nemus.core.tools.presentations import presentation_tools
from nemus.core.tools.python_exec import python_tools
from nemus.core.tools.screen import screen_tools
from nemus.core.tools.shell import shell_tools
from nemus.core.tools.system import (
    InstallVerifiedTool,
    VerifyInstallerTool,
    system_tools,
)
from nemus.core.tools.todo_tool import todo_tools
from nemus.core.tools.torrents import torrent_tools
from nemus.core.tools.video import video_tools
from nemus.core.tools.web import web_tools
from nemus.core.tools.website import website_tools


def all_builtin_tools() -> list[Tool]:
    tools: list[Tool] = []
    tools.extend(basic_tools())
    tools.extend(file_tools())
    tools.extend(shell_tools())
    tools.extend(python_tools())
    tools.extend(web_tools())
    tools.extend(download_tools())
    tools.extend(system_tools())
    # Проверка и установка — отдельный шлюз безопасности, регистрируется явно.
    tools.extend([VerifyInstallerTool(), InstallVerifiedTool()])
    tools.extend(todo_tools())
    tools.extend(keyboard_tools())
    tools.extend(browser_tools())
    tools.extend(torrent_tools())
    tools.extend(screen_tools())
    tools.extend(presentation_tools())
    tools.extend(video_tools())
    tools.extend(website_tools())
    return tools
