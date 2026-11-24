"""Офлайн-тесты инструментов браузера: open и fallback-загрузка файла.

Сеть и реальный Playwright не нужны: ``sync_playwright`` подменяется заглушкой.
"""

from __future__ import annotations

import builtins
import hashlib
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.core.download_events import (  # noqa: E402
    DownloadEvent,
    subscribe_download,
    unsubscribe_download,
)
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


class _FakeDownload:
    """Заглушка ``Download``: save_as пишет заданные байты в .part."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.suggested_filename = "setup.exe"
        self.saved: list[str] = []

    def save_as(self, path: str) -> None:
        self.saved.append(path)
        Path(path).write_bytes(self.payload)


class _FakeDownloadInfo:
    def __init__(self, download: _FakeDownload) -> None:
        self.value = download

    def __enter__(self) -> "_FakeDownloadInfo":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _DownloadPage(_Page):
    def __init__(self, download: _FakeDownload) -> None:
        self.download = download
        self.calls: list[str] = []

    def expect_download(self, timeout: int | None = None) -> _FakeDownloadInfo:
        self.calls.append("expect_download")
        return _FakeDownloadInfo(self.download)

    def goto(self, *args: object, **kwargs: object) -> None:
        self.calls.append("goto")

    def click(self, selector: str, **kwargs: object) -> None:
        self.calls.append(f"click:{selector}")


class _BrowserWithDownload(_Browser):
    def __init__(self, page: _DownloadPage) -> None:
        self.page = page

    def new_page(self) -> _Page:
        return self.page


def _force_import_error() -> object:
    """Подменяет импорт Playwright на падающий; возвращает исходный import."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name.startswith("playwright"):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    return real_import


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


def test_browser_download_saves_file_and_publishes(tmp_path: Path) -> None:
    """Заглушка Playwright сохраняет файл, events идут через общую шину."""
    payload = b"browser-bytes"
    download = _FakeDownload(payload)
    page = _DownloadPage(download)
    _install_fake_playwright(_ctx(_BrowserWithDownload(page)))
    tool = browser_mod.BrowserDownloadFileTool()
    tool.root = tmp_path / "data" / "downloads"
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    try:
        result = tool.execute("https://example.test/setup.exe", "setup.exe")
    finally:
        unsubscribe_download(token)
        _remove_fake_playwright()

    assert result.ok, result.content
    assert page.calls == ["expect_download", "goto"], page.calls
    target = tool.root / "setup.exe"
    assert target.read_bytes() == payload
    assert (tool.root / "setup.exe.sha256").read_text() == (
        hashlib.sha256(payload).hexdigest()
    )
    assert len(download.saved) == 1, download.saved
    assert download.saved[0] == str(tool.root / "setup.exe.part")
    assert not (tool.root / "setup.exe.part").exists()
    assert seen and all(event.kind == "browser" for event in seen)
    assert {event.phase for event in seen} >= {"start", "done"}
    assert seen[-1].phase == "done"
    assert seen[-1].done == seen[-1].total == len(payload)
    assert {event.task_id for event in seen} == {tool.last_task_id}
    assert "setup.exe" in result.content
    print("ok  browser_download_file: файл сохранён и события опубликованы")


def test_browser_download_without_playwright(tmp_path: Path) -> None:
    """Без пакета Playwright — честный failure, без системного браузера."""
    calls = _capture_open()
    tool = browser_mod.BrowserDownloadFileTool()
    tool.root = tmp_path / "data" / "downloads"
    real_import = _force_import_error()
    try:
        result = tool.execute("https://example.test/setup.exe", "setup.exe")
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]
    assert not result.ok, result.content
    assert "Playwright" in result.content, result.content
    assert calls == [], calls
    assert not (tool.root / "setup.exe").exists()
    print("ok  нет playwright -> failure без открытия системного браузера")


def test_browser_download_without_engine(tmp_path: Path) -> None:
    """Пакет есть, движка нет — тоже failure, а не silent fallback."""
    calls = _capture_open()
    _install_fake_playwright(lambda: _CtxRaises())
    tool = browser_mod.BrowserDownloadFileTool()
    tool.root = tmp_path / "data" / "downloads"
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    try:
        result = tool.execute("https://example.test/setup.exe", "setup.exe")
    finally:
        unsubscribe_download(token)
        _remove_fake_playwright()
    assert not result.ok, result.content
    assert "Playwright" in result.content, result.content
    assert calls == [], calls
    assert any(
        event.kind == "browser" and event.phase == "error" for event in seen
    ), seen
    print("ok  нет движка playwright -> failure с событием error")


def test_browser_download_uses_selector_and_reports_progress(tmp_path: Path) -> None:
    """Селектор кликает по ссылке; публикуются start/done с одним task_id."""
    payload = b"click-bytes"
    download = _FakeDownload(payload)
    page = _DownloadPage(download)
    _install_fake_playwright(_ctx(_BrowserWithDownload(page)))
    tool = browser_mod.BrowserDownloadFileTool()
    tool.root = tmp_path / "data" / "downloads"
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    try:
        result = tool.execute(
            "https://example.test/page.html", "file.bin", selector="#download"
        )
    finally:
        unsubscribe_download(token)
        _remove_fake_playwright()
    assert result.ok, result.content
    assert page.calls == ["expect_download", "goto", "click:#download"], page.calls
    assert [event.phase for event in seen] == ["start", "done"], seen
    assert (tool.root / "file.bin").read_bytes() == payload
    print("ok  selector кликает, прогресс публикуется")


def test_browser_download_rejects_http_and_oversize(tmp_path: Path) -> None:
    """Только HTTPS и лимит размера; часть и манифест не остаются."""
    tool = browser_mod.BrowserDownloadFileTool()
    tool.root = tmp_path / "data" / "downloads"
    bad = tool.execute("http://example.test/setup.exe", "setup.exe")
    assert not bad.ok and "HTTPS" in bad.content, bad.content

    download = _FakeDownload(b"x" * (1024 * 1024 + 1))
    _install_fake_playwright(_ctx(_BrowserWithDownload(_DownloadPage(download))))
    try:
        big = tool.execute("https://example.test/setup.exe", "setup.exe", max_mb=1)
    finally:
        _remove_fake_playwright()
    assert not big.ok, big.content
    assert "лимит" in big.content.lower(), big.content
    assert not (tool.root / "setup.exe").exists()
    assert not (tool.root / "setup.exe.part").exists()
    assert not (tool.root / "setup.exe.sha256").exists()
    print("ok  http запрещён, файл больше лимита удалён")


def test_browser_download_registered() -> None:
    assert "browser_download_file" in {t.name for t in browser_mod.browser_tools()}
    print("ok  browser_download_file зарегистрирован в browser_tools()")


if __name__ == "__main__":
    test_fallback_when_playwright_missing()
    test_fallback_when_engine_missing()
    test_navigation_error_is_reported()
    test_success_path_uses_playwright()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_browser_download_saves_file_and_publishes(root / "a")
        test_browser_download_without_playwright(root / "b")
        test_browser_download_without_engine(root / "c")
        test_browser_download_uses_selector_and_reports_progress(root / "d")
        test_browser_download_rejects_http_and_oversize(root / "e")
    test_browser_download_registered()
    print("\nBROWSER TESTS PASSED")
