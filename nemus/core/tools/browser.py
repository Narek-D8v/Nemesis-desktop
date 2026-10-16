"""Инструменты браузера (Playwright) — опциональные."""

from __future__ import annotations

from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class PlaywrightOpenTool(Tool):
    name = "browser_open"
    description = "Открывает страницу в браузере через Playwright (headless по умолчанию false)."
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

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                title = page.title()
                browser.close()
            return ToolResult.success(f"Открыта страница: {title} ({url})")
        except ModuleNotFoundError:
            return ToolResult.failure(
                "Playwright не установлен. Выполни: playwright install chromium"
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка Playwright: {exc}")


def browser_tools() -> list[Tool]:
    return [PlaywrightOpenTool()]
