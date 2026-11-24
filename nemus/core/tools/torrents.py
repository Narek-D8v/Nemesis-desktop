"""Торрент — отдельный инструмент последней инстанции.

Принимает только то, что прямо дал пользователь: magnet-ссылку или HTTPS-ссылку
на ``.torrent``. Сам инструмент ничего не ищет. ``libtorrent`` подключается
лениво: без пакета возвращается честная ошибка с подсказкой по установке, а не
падение агента и не «тихий успех».

Прогресс идёт по общей шине ``DownloadEvent(kind="torrent")`` с одним
``task_id``; файл, лимит, имя и SHA-256-манифест устроены так же, как в
``download_file``.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from nemus.config import PROJECT_ROOT
from nemus.core.download_events import DownloadEvent, new_download_id, publish_download
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.downloads import (
    DEFAULT_MAX_MB,
    HARD_MAX_MB,
    TIMEOUT,
    DownloadControls,
    _safe_name,
    _write_manifest,
    download_controls,
)

#: подсказка для честной ошибки, когда пакет не установлен
LIBTORRENT_INSTALL_HINT = (
    "Установите python-libtorrent: pip install python-libtorrent"
)
#: как часто опрашиваем состояние торрента, секунд
POLL_INTERVAL = 0.5
#: предел ожидания одной загрузки; после него — ошибка, а не вечный progress
MAX_WAIT_SECONDS = 24 * 60 * 60
#: максимум для файла метаданных .torrent, МБ
MAX_METADATA_MB = 4
#: размер чанка при чтении докачанного файла
_HASH_CHUNK_SIZE = 64 * 1024


class _TooLarge(Exception):
    """Торрент или метаданные превысили лимит."""


class _Cancelled(Exception):
    """Загрузка отменена через ``DownloadControls``."""


def _libtorrent() -> Any:
    """Ленивый импорт libtorrent; отсутствие пакета — понятная ошибка."""
    try:
        import libtorrent  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            f"libtorrent недоступен: {exc}. {LIBTORRENT_INSTALL_HINT}"
        ) from exc
    return libtorrent


def _source(value: str, expected: str) -> tuple[str, str]:
    """Проверяет источник: только переданный magnet или HTTPS ``.torrent``."""
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    if expected == "magnet":
        if parts.scheme != "magnet" or not any(
            key.lower() == "xt" for key in parse_qs(parts.query)
        ):
            raise ValueError("Нужна полная magnet-ссылка, которую дал пользователь.")
        return expected, raw
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not parts.path.lower().endswith(".torrent")
    ):
        raise ValueError("Нужна HTTPS-ссылка на .torrent, которую дал пользователь.")
    return expected, raw


def _hash_file(path: Path) -> tuple[int, str]:
    """Возвращает размер и SHA-256 докачанного файла."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


class TorrentDownloadTool(Tool):
    """Скачивает один файл по user-supplied magnet/HTTPS ``.torrent``."""

    name = "torrent_download"
    description = (
        "Скачивает файл через торрент в data/downloads как последний fallback, "
        "когда обычная загрузка и браузер не помогли. Принимает только magnet "
        "или HTTPS-ссылку на .torrent, которую прямо прислал пользователь: "
        "сам торренты не ищет. Требует явного согласия пользователя (в Telegram "
        "— инлайн-кнопка, в GUI — ConfirmDialog). Возвращает путь, размер и "
        "SHA-256; публикует прогресс с kind=torrent."
    )
    parameters = {
        "type": "object",
        "properties": {
            "magnet": {
                "type": "string",
                "description": "Magnet-ссылка, которую прислал пользователь.",
            },
            "torrent_url": {
                "type": "string",
                "description": "HTTPS-ссылка на .torrent от пользователя.",
            },
            "filename": {
                "type": "string",
                "description": "Имя файла; пути и '..' отбрасываются.",
            },
            "max_mb": {
                "type": "integer",
                "description": (
                    f"Максимальный размер в МБ (по умолчанию {DEFAULT_MAX_MB}, "
                    f"не больше {HARD_MAX_MB})."
                ),
            },
        },
        "required": [],
    }
    risk = RiskLevel.DANGEROUS

    #: папка загрузок; фабрика и тесты оставляют значение по умолчанию
    root: Path = PROJECT_ROOT / "data" / "downloads"
    #: реестр отмены; тесты и UI могут подменить свой
    controls: DownloadControls = download_controls
    #: внешний task_id; пустой → генерируется сам
    task_id: str = ""
    #: task_id последнего вызова, чтобы UI мог показать/отменить задачу
    last_task_id: str = ""

    def execute(
        self,
        magnet: str = "",
        torrent_url: str = "",
        filename: str = "",
        max_mb: int = DEFAULT_MAX_MB,
        **_: object,
    ) -> ToolResult:
        raw_magnet = str(magnet or "").strip()
        raw_url = str(torrent_url or "").strip()
        if raw_magnet and raw_url:
            return ToolResult.failure(
                "Укажите magnet или HTTPS-ссылку на .torrent, не оба сразу."
            )
        try:
            if raw_magnet:
                kind, source = _source(raw_magnet, "magnet")
            elif raw_url:
                kind, source = _source(raw_url, "torrent")
            else:
                return ToolResult.failure(
                    "Нужна magnet-ссылка или HTTPS-ссылка на .torrent, "
                    "которую прислал пользователь."
                )
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        try:
            limit_mb = max(1, min(int(max_mb or DEFAULT_MAX_MB), HARD_MAX_MB))
        except (TypeError, ValueError):
            limit_mb = DEFAULT_MAX_MB
        limit = limit_mb * 1024 * 1024

        fallback = urlsplit(source).path if kind == "torrent" else ""
        name = _safe_name(filename or fallback)
        root = Path(self.root).expanduser().resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.failure(f"Не удалось создать папку загрузок: {exc}")

        task_id = str(self.task_id or new_download_id())
        self.last_task_id = task_id
        cancel_event = self.controls.register(task_id)

        def emit(
            phase: str, *, done: int = 0, total: int = 0, message: str = ""
        ) -> None:
            publish_download(
                DownloadEvent(
                    kind="torrent",
                    phase=phase,
                    name=name,
                    done=done,
                    total=total,
                    path=str(root / name),
                    message=message,
                    task_id=task_id,
                )
            )

        try:
            lt = _libtorrent()
        except ImportError as exc:
            self.controls.forget(task_id)
            message = str(exc)
            if LIBTORRENT_INSTALL_HINT not in message:
                message = f"{message}. {LIBTORRENT_INSTALL_HINT}"
            emit("error", message=message)
            return ToolResult.failure(message)

        try:
            emit("start")
            session = lt.session()
            handle = self._add(session, lt, kind, source, root, limit)
            total = self._wait(handle, cancel_event, emit, limit)
            target = root / _safe_name(self._name(handle, name))
            if not target.is_file():
                raise OSError("Торрент завершён, но файл не найден в папке загрузок")
            size, digest = _hash_file(target)
            if size > limit:
                raise _TooLarge
            _write_manifest(target, digest)
            emit("done", done=size, total=total or size, message="Скачано")
        except _Cancelled:
            emit("cancelled", message="Торрент отменён")
            return ToolResult.failure("Торрент-загрузка отменена.")
        except _TooLarge:
            message = (
                f"Торрент больше лимита {limit_mb} МБ — загрузка прервана."
            )
            emit("error", message=message)
            return ToolResult.failure(message)
        except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
            emit("error", message=str(exc))
            return ToolResult.failure(f"Торрент-загрузка не удалась: {exc}")
        except Exception as exc:  # noqa: BLE001
            emit("error", message=str(exc))
            return ToolResult.failure(f"Ошибка libtorrent: {exc}")
        finally:
            self.controls.forget(task_id)

        return ToolResult.success(
            f"Скачано через торрент: {target.name}\n"
            f"Путь: {target}\n"
            f"Размер: {size} байт\n"
            f"SHA-256: {digest}\n"
            f"Источник: {source}"
        )

    @staticmethod
    def _add(
        session: Any,
        lt: Any,
        kind: str,
        source: str,
        root: Path,
        limit: int,
    ) -> Any:
        """Добавляет magnet или локально загруженный ``.torrent`` в сессию."""
        if kind == "magnet":
            params = lt.parse_magnet_uri(source)
            return session.add_torrent(
                TorrentDownloadTool._set_save_path(params, root)
            )
        metadata = TorrentDownloadTool._fetch_metadata(source, limit)
        info = lt.torrent_info(metadata)
        return session.add_torrent(
            TorrentDownloadTool._params_with_info(lt, info, root)
        )

    @staticmethod
    def _set_save_path(params: Any, root: Path) -> Any:
        """Пишет save_path атрибутом (2.x) или ключом dict (1.x)."""
        try:
            params.save_path = str(root)
        except AttributeError:
            if not isinstance(params, dict):
                raise
            params["save_path"] = str(root)
        return params

    @staticmethod
    def _params_with_info(lt: Any, info: Any, root: Path) -> Any:
        """Собирает add_torrent_params 2.x, при отсутствии — dict 1.x."""
        factory = getattr(lt, "add_torrent_params", None)
        if callable(factory):
            try:
                params = factory()
                params.ti = info
                params.save_path = str(root)
                return params
            except (AttributeError, TypeError):
                pass
        return {"ti": info, "save_path": str(root)}

    @staticmethod
    def _fetch_metadata(url: str, limit: int) -> bytes:
        """Скачивает только ``.torrent``; это не поиск и не сам файл."""
        ceiling = min(limit, MAX_METADATA_MB * 1024 * 1024)
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(TIMEOUT)) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > ceiling:
                    raise _TooLarge
                data = bytearray()
                for chunk in response.iter_bytes(_HASH_CHUNK_SIZE):
                    data.extend(chunk)
                    if len(data) > ceiling:
                        raise _TooLarge
                return bytes(data)

    @staticmethod
    def _name(handle: Any, fallback: str) -> str:
        """Имя одного файла из торрента; multi-file не поддерживается."""
        getter = getattr(handle, "name", None)
        if not callable(getter):
            return fallback
        return str(getter() or fallback)

    @staticmethod
    def _wait(
        handle: Any,
        cancel_event: Any,
        emit: Callable[..., None],
        limit: int,
    ) -> int:
        """Ждёт завершения, публикуя прогресс; возвращает последний total."""
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        total = 0
        while True:
            if cancel_event.is_set():
                raise _Cancelled
            status = handle.status()
            done = int(getattr(status, "total_done", 0) or 0)
            want = int(getattr(status, "total_want", 0) or 0)
            size = int(getattr(status, "total_size", 0) or 0)
            total = want or size or total
            if done > limit or total > limit:
                raise _TooLarge
            if done or total:
                emit("progress", done=done, total=total)
            if handle.is_seed():
                return total
            if time.monotonic() >= deadline:
                raise TimeoutError("Торрент не завершился за 24 часа")
            time.sleep(POLL_INTERVAL)


def torrent_tools() -> list[Tool]:
    return [TorrentDownloadTool()]


__all__ = ["LIBTORRENT_INSTALL_HINT", "TorrentDownloadTool", "torrent_tools"]
