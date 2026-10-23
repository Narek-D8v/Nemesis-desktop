"""Инструменты браузера (Playwright) — опциональные.

Если Playwright или его движок недоступны (например, в сборке без браузеров),
инструмент открывает страницу в системном браузере по умолчанию.
"""

from __future__ import annotations

import webbrowser

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: таймаут загрузки страницы в Playwright (мс)
PAGE_LOAD_TIMEOUT_MS = 30_000


class PlaywrightOpenTool(Tool):
    name = "browser_open"
    description = (
        "Открывает страницу по URL в браузере. Использует Playwright "
        "(headless по умолчанию false); если он недоступен — системный браузер."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "headless": {
                "type": "boolean",
                "description": "Фоновый режим (по умолчанию false).",
            },
        },
        "required": ["url"],
    }
    risk = RiskLevel.MEDIUM

    def execute(self, url: str = "", headless: bool = False, **_: object) -> ToolResult:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError:
            return self._open_in_system_browser(url)

        started = False
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                started = True
                try:
                    page = browser.new_page()
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT_MS,
                    )
                    title = page.title()
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            if started:
                return ToolResult.failure(f"Ошибка Playwright: {exc}")
            logger.warning(
                f"Playwright недоступен ({exc}); открываю системный браузер"
            )
            return self._open_in_system_browser(url)
        return ToolResult.success(f"Открыта страница: {title} ({url})")

    @staticmethod
    def _open_in_system_browser(url: str) -> ToolResult:
        """Открывает URL в браузере по умолчанию (когда Playwright недоступен)."""
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось открыть браузер: {exc}")
        if not opened:
            return ToolResult.failure(f"Системный браузер не открылся. URL: {url}")
        return ToolResult.success(f"Открыта страница в системном браузере: {url}")


def browser_tools() -> list[Tool]:
    return [PlaywrightOpenTool()]
