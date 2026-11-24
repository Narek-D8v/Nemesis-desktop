"""Инструменты браузера (Playwright) — опциональные.

``browser_open`` при отсутствии Playwright/движка открывает страницу в
системном браузере. ``browser_download_file`` — резервный загрузчик для
случаев, когда обычный ``download_file`` не справился: он не делает скрытых
fallback'ов и честно сообщает об отсутствии Playwright.
"""

from __future__ import annotations

import hashlib
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from nemus.config import PROJECT_ROOT
from nemus.core.download_events import DownloadEvent, new_download_id, publish_download
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.downloads import (
    DEFAULT_MAX_MB,
    HARD_MAX_MB,
    TIMEOUT,
    _safe_name,
    _write_manifest,
)
from nemus.utils.logger import logger

#: таймаут загрузки страницы в Playwright (мс)
PAGE_LOAD_TIMEOUT_MS = 30_000
#: таймаут ожидания скачивания и навигации загрузчика, мс
DOWNLOAD_TIMEOUT_MS = int(TIMEOUT * 1000)
#: размер чанка при чтении сохранённого файла
_HASH_CHUNK_SIZE = 64 * 1024


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


class BrowserDownloadFileTool(Tool):
    """Загружает файл через браузер, когда ``download_file`` не справился."""

    name = "browser_download_file"
    description = (
        "Резервная загрузка файла через браузер по HTTPS, если download_file "
        "не справился (сайт отдаёт файл только после клика/JS). Сохраняет в "
        "data/downloads, ограничивает размер и публикует прогресс. По "
        "умолчанию не открывает системный браузер и не запускает файл."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Полная HTTPS-ссылка на страницу или файл.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Имя файла; по умолчанию берётся из URL. Пути и '..' "
                    "отбрасываются."
                ),
            },
            "max_mb": {
                "type": "integer",
                "description": (
                    f"Максимальный размер в МБ (по умолчанию {DEFAULT_MAX_MB}, "
                    f"не больше {HARD_MAX_MB})."
                ),
            },
            "selector": {
                "type": "string",
                "description": (
                    "CSS-селектор ссылки/кнопки, по которой нужно кликнуть, "
                    "чтобы началась загрузка; пусто — файл идёт сразу по URL."
                ),
            },
        },
        "required": ["url"],
    }
    risk = RiskLevel.MEDIUM

    #: папка загрузок; фабрика оставляет значение по умолчанию
    root: Path = PROJECT_ROOT / "data" / "downloads"
    #: внешний task_id; пустой → генерируется сам
    task_id: str = ""
    #: task_id последнего вызова, чтобы внешний код мог отменить/показать задачу
    last_task_id: str = ""

    def execute(
        self,
        url: str = "",
        filename: str = "",
        max_mb: int = DEFAULT_MAX_MB,
        selector: str = "",
        **_: object,
    ) -> ToolResult:
        raw_url = str(url or "").strip()
        parts = urlsplit(raw_url)
        if parts.scheme != "https" or not parts.hostname:
            return ToolResult.failure(
                "Разрешены только HTTPS-ссылки с указанным хостом."
            )

        try:
            limit_mb = max(1, min(int(max_mb or DEFAULT_MAX_MB), HARD_MAX_MB))
        except (TypeError, ValueError):
            limit_mb = DEFAULT_MAX_MB
        limit = limit_mb * 1024 * 1024

        name = _safe_name(filename or parts.path)
        root = Path(self.root).expanduser().resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.failure(f"Не удалось создать папку загрузок: {exc}")

        target = root / name
        if target.exists():
            suffix = Path(name).suffix
            stem = name[: -len(suffix)] if suffix else name
            target = root / f"{stem}-{int(time.time())}{suffix}"
        partial = target.with_name(target.name + ".part")
        manifest = target.with_name(target.name + ".sha256")

        task_id = str(self.task_id or new_download_id())
        self.last_task_id = task_id

        def emit(phase: str, *, done: int = 0, total: int = 0, message: str = "") -> None:
            publish_download(
                DownloadEvent(
                    kind="browser",
                    phase=phase,
                    name=name,
                    done=done,
                    total=total,
                    path=str(target),
                    message=message,
                    task_id=task_id,
                )
            )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            message = (
                f"Playwright недоступен: {exc}. Установите пакет и движок: "
                "pip install playwright; playwright install chromium"
            )
            emit("error", message=message)
            return ToolResult.failure(message)

        emit("start")
        started = False
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                started = True
                try:
                    # new_page() по умолчанию разрешает downloads в Playwright.
                    page = browser.new_page()
                    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as info:
                        page.goto(
                            raw_url,
                            wait_until="domcontentloaded",
                            timeout=DOWNLOAD_TIMEOUT_MS,
                        )
                        if selector:
                            page.click(selector, timeout=DOWNLOAD_TIMEOUT_MS)
                    download = info.value
                    # Пишем во временный файл: неполная загрузка не станет файлом.
                    download.save_as(str(partial))
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            partial.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            if started:
                message = f"Ошибка загрузки в браузере: {exc}"
            else:
                message = (
                    f"Playwright/браузер недоступен: {exc}. Возможно, не "
                    "установлен движок: playwright install chromium"
                )
                logger.warning(f"Playwright недоступен для загрузки: {exc}")
            emit("error", message=message)
            return ToolResult.failure(message)

        try:
            size = partial.stat().st_size
            if size > limit:
                partial.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
                message = (
                    f"Файл больше лимита {limit_mb} МБ — загрузка прервана, "
                    "частичный файл удалён."
                )
                emit("error", message=message)
                return ToolResult.failure(message)

            digest = hashlib.sha256()
            with partial.open("rb") as source:
                while chunk := source.read(_HASH_CHUNK_SIZE):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            _write_manifest(target, sha256)
            partial.replace(target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            message = f"Не удалось сохранить скачанный файл: {exc}"
            emit("error", message=message)
            return ToolResult.failure(message)

        emit("done", done=size, total=size, message="Скачано")
        return ToolResult.success(
            f"Скачано через браузер: {target.name}\n"
            f"Путь: {target}\n"
            f"Размер: {size} байт\n"
            f"SHA-256: {sha256}\n"
            f"Источник: {raw_url}"
        )


def browser_tools() -> list[Tool]:
    return [PlaywrightOpenTool(), BrowserDownloadFileTool()]
