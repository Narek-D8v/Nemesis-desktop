"""Скачивание установщиков в ``data/downloads`` с ограничением размера.

Отдельный инструмент нужен, чтобы агент не собирал ``curl`` с относительным
путём: ``run_shell`` работает из workspace, а не из корня проекта, поэтому
``data\\downloads`` превращался в несуществующую папку. Здесь папка создаётся
сама, имя файла очищается, а размер ограничен по умолчанию.

Крупные файлы качаются параллельными ``Range``-сегментами; части лежат в
``.parts`` и сохраняются при отмене/сетевой ошибке для докачки.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from nemus.config import PROJECT_ROOT
from nemus.core.download_events import DownloadEvent, new_download_id, publish_download
from nemus.core.tools.base import RiskLevel, Tool, ToolResult

#: потолок размера одного файла по умолчанию, МБ
DEFAULT_MAX_MB = 500
#: абсолютный потолок, чтобы модель не запросила 1 ТБ
HARD_MAX_MB = 2048
#: таймауты HTTP-клиента, секунд
TIMEOUT = 120.0
#: размер одного сегмента, МБ
SEGMENT_SIZE_MB = 8
#: не больше этого количества сегментов на файл
MAX_SEGMENTS = 8
#: размер чанка при чтении ответа
_CHUNK_SIZE = 64 * 1024
#: папка с частями внутри root
_PARTS_DIR = ".parts"
#: всё, что не годится для имени файла
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class _TooLarge(Exception):
    """Загрузка превысила лимит."""


class _Cancelled(Exception):
    """Загрузка отменена через ``DownloadControls``."""


def _http_client() -> httpx.Client:
    """Клиент с redirects — в тестах подменяется на MockTransport."""
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(TIMEOUT))


def _safe_name(raw: str) -> str:
    """Оставляет только имя файла, без путей и прочих символов."""
    base = Path(str(raw or "").replace("\\", "/")).name
    cleaned = _UNSAFE_NAME.sub("_", base).strip("._")
    return cleaned or "download.bin"


def _plan_segments(total: int) -> list[tuple[int, int]]:
    """Возвращает границы сегментов ``(start, end)``, обе включительно."""
    if total <= 0:
        return []
    size = SEGMENT_SIZE_MB * 1024 * 1024
    if total > size * MAX_SEGMENTS:
        size = (total + MAX_SEGMENTS - 1) // MAX_SEGMENTS
    return [
        (start, min(start + size, total) - 1)
        for start in range(0, total, size)
    ]


def _merge_and_hash(parts: list[Path], target: Path) -> tuple[int, str]:
    """Сливает части по порядку в ``target``, считая размер и SHA-256."""
    digest = hashlib.sha256()
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                while chunk := source.read(_CHUNK_SIZE):
                    output.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
    return total, digest.hexdigest()


def _write_manifest(target: Path, sha256: str) -> None:
    """Пишет рядом с файлом манифест, содержащий только hex-хеш."""
    target.with_name(target.name + ".sha256").write_text(sha256, encoding="ascii")


def _read_manifest(target: Path) -> str | None:
    """Читает хеш из манифеста; отсутствие/мусор → None."""
    try:
        raw = target.with_name(target.name + ".sha256").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError):
        return None
    if len(raw) != 64 or any(char not in "0123456789abcdefABCDEF" for char in raw):
        return None
    return raw


def _parts_dir(root: Path, name: str, url: str) -> Path:
    """Папка частей зависит от имени файла и хеша исходного URL."""
    fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return root / _PARTS_DIR / f"{name}-{fingerprint}"


def _remove_parts(parts_dir: Path) -> None:
    """Удаляет части одной загрузки и пустую родительскую папку."""
    shutil.rmtree(parts_dir, ignore_errors=True)
    try:
        parts_dir.parent.rmdir()
    except OSError:
        pass


class DownloadControls:
    """Реестр stop-событий по ``task_id`` для Telegram/GUI и инструментов."""

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register(self, task_id: str) -> threading.Event:
        """Создаёт (или возвращает существующий) Event для задачи."""
        with self._lock:
            event = self._events.get(task_id)
            if event is None:
                event = threading.Event()
                self._events[task_id] = event
            return event

    def cancel(self, task_id: str) -> None:
        """Ставит stop-флаг; можно вызвать до старта загрузки."""
        self.register(task_id).set()

    def forget(self, task_id: str) -> None:
        """Убирает задачу из реестра."""
        with self._lock:
            self._events.pop(task_id, None)


#: общий реестр: слушатели событий знают task_id и могут отменить загрузку
download_controls = DownloadControls()
#: короткий псевдоним для внешних потребителей (Telegram/GUI)
controls = download_controls


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
    #: реестр отмены; тесты и UI могут подменить свой
    controls: DownloadControls = download_controls
    #: внешний task_id (для отмены до старта); пустой → генерируется сам
    task_id: str = ""
    #: task_id последнего вызова, чтобы внешний код мог отменить загрузку
    last_task_id: str = ""

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
        manifest = target.with_name(target.name + ".sha256")
        parts_dir = _parts_dir(root, name, raw_url)

        task_id = str(self.task_id or new_download_id())
        self.last_task_id = task_id
        cancel_event = self.controls.register(task_id)
        state = {"done": 0, "total": 0}
        emit_lock = threading.Lock()

        def emit(
            phase: str,
            *,
            done: int | None = None,
            total: int | None = None,
            message: str = "",
        ) -> None:
            with emit_lock:
                if done is not None:
                    state["done"] = done
                if total is not None:
                    state["total"] = total
                publish_download(
                    DownloadEvent(
                        kind="file",
                        phase=phase,
                        name=name,
                        done=state["done"],
                        total=state["total"],
                        path=str(target),
                        message=message,
                        task_id=task_id,
                    )
                )

        try:
            with _http_client() as client:
                declared, accept_ranges, validator = self._probe(client, raw_url)
                if declared > limit:
                    raise _TooLarge
                emit("start", done=0, total=declared)
                if accept_ranges and declared:
                    merged = self._download_segments(
                        client, raw_url, declared, validator, parts_dir, partial,
                        target, cancel_event, emit,
                    )
                    if merged is None:
                        # сервер проигнорировал Range: уходим в один поток
                        _remove_parts(parts_dir)
                        size, digest = self._download_single(
                            client, raw_url, partial, target, limit, cancel_event,
                            emit, declared,
                        )
                    else:
                        size, digest = merged
                else:
                    size, digest = self._download_single(
                        client, raw_url, partial, target, limit, cancel_event,
                        emit, declared,
                    )
            if size > limit:
                raise _TooLarge
            _write_manifest(target, digest)
            partial.replace(target)
            _remove_parts(parts_dir)
            emit("done", done=size, total=declared or size, message="Скачано")
        except _Cancelled as exc:
            # части намеренно остаются: следующий вызов продолжит с них
            partial.unlink(missing_ok=True)
            emit("cancelled", message=str(exc) or "Загрузка отменена")
            return ToolResult.failure("Загрузка отменена; части сохранены для докачки.")
        except _TooLarge as exc:
            partial.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            _remove_parts(parts_dir)
            emit("error", message=str(exc) or "Файл больше лимита")
            return ToolResult.failure(
                f"Файл больше лимита {limit_mb} МБ — загрузка прервана, "
                "частичный файл удалён."
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            partial.unlink(missing_ok=True)
            emit("error", message=str(exc))
            return ToolResult.failure(f"Скачивание не удалось: {exc}")
        finally:
            self.controls.forget(task_id)

        return ToolResult.success(
            f"Скачано: {target.name}\n"
            f"Путь: {target}\n"
            f"Размер: {size} байт\n"
            f"SHA-256: {digest}\n"
            f"Источник: {raw_url}"
        )

    @staticmethod
    def _probe(client: httpx.Client, url: str) -> tuple[int, bool, tuple[str, str]]:
        """Заголовок ответа HEAD; при ошибке/неподдержке — обычный GET."""
        try:
            with client.stream("HEAD", url) as response:
                if response.status_code >= 400:
                    return 0, False, ("", "")
                try:
                    declared = int(response.headers.get("content-length") or 0)
                except ValueError:
                    declared = 0
                accept_ranges = "bytes" in response.headers.get(
                    "accept-ranges", ""
                ).lower()
                validator = (
                    response.headers.get("etag", ""),
                    response.headers.get("last-modified", ""),
                )
                return declared, accept_ranges, validator
        except (httpx.HTTPError, OSError):
            return 0, False, ("", "")

    @staticmethod
    def _prepare_parts(
        parts_dir: Path, total: int, validator: tuple[str, str], count: int
    ) -> list[Path]:
        """Готовит папку частей; при смене размера/версии начинаем заново."""
        parts_dir.mkdir(parents=True, exist_ok=True)
        meta_path = parts_dir / "meta.json"
        try:
            previous = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            previous = None
        if isinstance(previous, dict):
            mismatch = previous.get("total") != total
            for key, current in zip(("etag", "last-modified"), validator, strict=True):
                stored = previous.get(key)
                if stored and current and stored != current:
                    mismatch = True
            if mismatch:
                for old in parts_dir.glob("part-*"):
                    old.unlink(missing_ok=True)
        meta_path.write_text(
            json.dumps({
                "total": total,
                "etag": validator[0],
                "last-modified": validator[1],
            }),
            encoding="utf-8",
        )
        return [parts_dir / f"part-{index:02d}" for index in range(count)]

    def _download_segments(
        self,
        client: httpx.Client,
        raw_url: str,
        total: int,
        validator: tuple[str, str],
        parts_dir: Path,
        partial: Path,
        target: Path,
        cancel_event: threading.Event,
        emit: Callable[..., None],
    ) -> tuple[int, str] | None:
        """Качает сегменты параллельно; None означает откат на один поток."""
        segments = _plan_segments(total)
        part_paths = self._prepare_parts(parts_dir, total, validator, len(segments))
        for part in part_paths:
            part.touch(exist_ok=True)
        if cancel_event.is_set():
            raise _Cancelled("Загрузка отменена")

        sizes = [part.stat().st_size for part in part_paths]
        size_lock = threading.Lock()
        fallback = threading.Event()

        def report() -> None:
            with size_lock:
                done = sum(sizes)
            emit("progress", done=done, total=total)

        def worker(index: int, start: int, end: int, part: Path) -> None:
            expected = end - start + 1
            existing = sizes[index]
            if existing > expected:
                # чужая/старая часть не подходит — начинаем сегмент с нуля
                with part.open("wb"):
                    pass
                existing = 0
                with size_lock:
                    sizes[index] = 0
            if existing == expected:
                return
            begin = start + existing
            headers = {"Range": f"bytes={begin}-{end}"}
            with client.stream("GET", raw_url, headers=headers) as response:
                if response.status_code in (200, 416):
                    fallback.set()
                    return
                if response.status_code != 206:
                    response.raise_for_status()
                    fallback.set()
                    return
                with part.open("ab") as handle:
                    for chunk in response.iter_bytes(_CHUNK_SIZE):
                        if fallback.is_set():
                            return
                        if cancel_event.is_set():
                            raise _Cancelled("Загрузка отменена")
                        handle.write(chunk)
                        existing += len(chunk)
                        if existing > expected:
                            raise _TooLarge(
                                f"Сегмент {index:02d} вернул больше ожидаемого"
                            )
                        with size_lock:
                            sizes[index] = existing
                        report()
            if existing != expected:
                raise OSError(f"Сегмент {index:02d} скачан не полностью")

        with ThreadPoolExecutor(max_workers=min(MAX_SEGMENTS, len(segments))) as pool:
            futures = [
                pool.submit(worker, index, start, end, part_paths[index])
                for index, (start, end) in enumerate(segments)
            ]
            for future in futures:
                future.result()
        if fallback.is_set():
            return None

        size, digest = _merge_and_hash(part_paths, partial)
        if size != total:
            raise OSError(f"Файл скачан не полностью: {size} из {total} байт")
        return size, digest

    @staticmethod
    def _download_single(
        client: httpx.Client,
        raw_url: str,
        partial: Path,
        target: Path,
        limit: int,
        cancel_event: threading.Event,
        emit: Callable[..., None],
        hint_total: int = 0,
    ) -> tuple[int, str]:
        """Прежний однопоточный путь: без Range и без папки частей."""
        digest = hashlib.sha256()
        done = 0
        with client.stream("GET", raw_url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > limit:
                        raise _TooLarge("Файл больше лимита")
                except ValueError:
                    pass
            total = int(declared) if declared and declared.strip().isdigit() else hint_total
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK_SIZE):
                    if cancel_event.is_set():
                        raise _Cancelled("Загрузка отменена")
                    done += len(chunk)
                    if done > limit:
                        raise _TooLarge("Файл больше лимита")
                    digest.update(chunk)
                    handle.write(chunk)
                    emit("progress", done=done, total=total)
        return done, digest.hexdigest()


def download_tools() -> list[Tool]:
    return [DownloadFileTool()]
