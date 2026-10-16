"""Офлайн-тесты инструмента browser_open: fallback на системный браузер."""

from __future__ import annotations

import builtins
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.core.tools import browser as browser_mod  # noqa: E402


def _capture_open() -> list[str]:
    """Подменяет webbrowser.open и возвращает список открытых URL."""
    calls: list[str] = []

    def fake(url: str, new: int = 0) -> bool:
        calls.append(url)
        return True

    browser_mod.webbrowser.open = fake
    return calls


class _Page:
    def goto(self, *args: object, **kwargs: object) -> None:
        return None

    def title(self) -> str:
        return "Example"


class _Browser:
    def new_page(self) -> _Page:
        return _Page()

    def close(self) -> None:
        return None


class _BrowserBadNav(_Browser):
    def new_page(self) -> _Page:
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")


class _CtxRaises:
    """Playwright есть, но движок не установлен: падает уже на входе."""

    def __enter__(self) -> object:
        raise RuntimeError("Executable doesn't exist. Run playwright install")

    def __exit__(self, *args: object) -> bool:
        return False


class _Ctx:
    def __init__(self, browser: _Browser) -> None:
        self._browser = browser

    def __enter__(self) -> object:
        launch = lambda **_: self._browser  # noqa: E731
        return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch))

    def __exit__(self, *args: object) -> bool:
        return False


def _ctx(browser: _Browser):
    return lambda: _Ctx(browser)


def _install_fake_playwright(factory) -> None:
    fake_pkg = types.ModuleType("playwright")
    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = factory
    sys.modules["playwright"] = fake_pkg
    sys.modules["playwright.sync_api"] = fake_api


def _remove_fake_playwright() -> None:
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)


def test_fallback_when_playwright_missing() -> None:
    calls = _capture_open()
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name.startswith("playwright"):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        res = browser_mod.PlaywrightOpenTool().execute(url="https://example.com")
    finally:
        builtins.__import__ = real_import
    assert res.ok, res.content
    assert calls == ["https://example.com"], calls
    print("ok  нет playwright -> системный браузер")


def test_fallback_when_engine_missing() -> None:
    calls = _capture_open()
    _install_fake_playwright(lambda: _CtxRaises())
    try:
        res = browser_mod.PlaywrightOpenTool().execute(url="https://youtube.com")
    finally:
        _remove_fake_playwright()
    assert res.ok, res.content
    assert calls == ["https://youtube.com"], calls
    print("ok  нет движка playwright -> системный браузер")


def test_navigation_error_is_reported() -> None:
    calls = _capture_open()
    _install_fake_playwright(_ctx(_BrowserBadNav()))
    try:
        res = browser_mod.PlaywrightOpenTool().execute(url="https://bad.invalid")
    finally:
        _remove_fake_playwright()
    assert not res.ok, res.content
    assert calls == [], "при ошибке навигации системный браузер не нужен"
    print("ok  ошибка навигации -> failure без fallback")


def test_success_path_uses_playwright() -> None:
    calls = _capture_open()
    _install_fake_playwright(_ctx(_Browser()))
    try:
        res = browser_mod.PlaywrightOpenTool().execute(url="https://ok.example")
    finally:
        _remove_fake_playwright()
    assert res.ok, res.content
    assert "Example" in res.content, res.content
    assert calls == [], "при работающем playwright системный браузер не нужен"
    print("ok  работает playwright -> без системного браузера")


if __name__ == "__main__":
    test_fallback_when_playwright_missing()
    test_fallback_when_engine_missing()
    test_navigation_error_is_reported()
    test_success_path_uses_playwright()
    print("\nBROWSER TESTS PASSED")
