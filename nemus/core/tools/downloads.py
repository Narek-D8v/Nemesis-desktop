"""Скачивание установщиков в ``data/downloads`` с ограничением размера.

Отдельный инструмент нужен, чтобы агент не собирал ``curl`` с относительным
путём: ``run_shell`` работает из workspace, а не из корня проекта, поэтому
``data\\downloads`` превращался в несуществующую папку. Здесь папка создаётся
сама, имя файла очищается, а размер ограничен по умолчанию.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from nemus.config import PROJECT_ROOT
from nemus.core.tools.base import RiskLevel, Tool, ToolResult

#: потолок размера одного файла по умолчанию, МБ
DEFAULT_MAX_MB = 500
#: абсолютный потолок, чтобы модель не запросила 1 ТБ
HARD_MAX_MB = 2048
#: таймауты HTTP-клиента, секунд
TIMEOUT = 120.0
#: всё, что не годится для имени файла
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class _TooLarge(Exception):
    """Загрузка превысила лимит."""


def _http_client() -> httpx.Client:
    """Клиент с redirects — в тестах подменяется на MockTransport."""
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(TIMEOUT))


def _safe_name(raw: str) -> str:
    """Оставляет только имя файла, без путей и прочих символов."""
    base = Path(str(raw or "").replace("\\", "/")).name
    cleaned = _UNSAFE_NAME.sub("_", base).strip("._")
    return cleaned or "download.bin"


class DownloadFileTool(Tool):
    name = "download_file"
    description = (
        "Скачивает файл по HTTPS в data/downloads (папка создаётся сама) и "
        "возвращает абсолютный путь, размер и SHA-256. Используй для "
        "установщиков, архивов и других файлов приложений; для картинок есть "
        "download_image. Перед запуском скачанный установщик проверь через "
        "scan_file и подпись. Ссылка принимается только HTTPS."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Полная HTTPS-ссылка на файл производителя.",
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
        },
        "required": ["url"],
    }
    risk = RiskLevel.MEDIUM

    #: папка загрузок; фабрика оставляет значение по умолчанию
    root: Path = PROJECT_ROOT / "data" / "downloads"

    def execute(
        self, url: str = "", filename: str = "", max_mb: int = DEFAULT_MAX_MB, **_: object
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
        digest = hashlib.sha256()
        total = 0

        try:
            with _http_client() as client:
                with client.stream("GET", raw_url) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > limit:
                                raise _TooLarge
                        except ValueError:
                            pass
                    with partial.open("wb") as handle:
                        for chunk in response.iter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > limit:
                                raise _TooLarge
                            digest.update(chunk)
                            handle.write(chunk)
            partial.replace(target)
        except _TooLarge:
            partial.unlink(missing_ok=True)
            return ToolResult.failure(
                f"Файл больше лимита {limit_mb} МБ — загрузка прервана, "
                "частичный файл удалён."
            )
        except (httpx.HTTPError, OSError) as exc:
            partial.unlink(missing_ok=True)
            return ToolResult.failure(f"Скачивание не удалось: {exc}")

        return ToolResult.success(
            f"Скачано: {target.name}\n"
            f"Путь: {target}\n"
            f"Размер: {total} байт\n"
            f"SHA-256: {digest.hexdigest()}\n"
            f"Источник: {raw_url}"
        )


def download_tools() -> list[Tool]:
    return [DownloadFileTool()]
