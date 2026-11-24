"""Скачивание GGUF-моделей с Hugging Face в локальную папку.

Поддерживает:
* repo id (``owner/model``) и имя файла внутри репозитория;
* полную ссылку Hugging Face (``.../resolve/main/model.gguf``);
* докачку с ``.part`` и HTTP Range после обрыва;
* разбитые GGUF: все шарды скачиваются и склеиваются в один файл;
* токен приватных репозиториев из ``HF_TOKEN``/``HUGGING_FACE_HUB_TOKEN``.

Файл кладётся в ``llm.llama_cpp.models_dir/<repo>/<путь-в-репозитории>``,
поэтому два репозитория с одинаковым именем файла не перетирают друг друга.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable

from nemus.config import PROJECT_ROOT
from nemus.core.download_events import DownloadEvent, publish_download
from nemus.core.providers.gguf_split import (
    GGUFMergeCancelled,
    GGUFMergeError,
    is_split_gguf,
    merge_gguf_shards,
    merged_filename,
    split_part,
)
from nemus.core.providers.hf_catalog import HFCatalogError, list_hf_gguf_files
from nemus.utils.logger import logger

if TYPE_CHECKING:
    from nemus.config import Config

ProgressCallback = Callable[[int, int], None]
StatusCallback = Callable[[str], None]

#: размер блока чтения; 1 МиБ достаточно, чтобы не дёргать диск на каждом байте
CHUNK = 1024 * 1024
#: как часто обновлять прогресс (сек)
PROGRESS_INTERVAL = 0.25
#: таймаут соединения и чтения одного блока
HTTP_TIMEOUT = 60.0

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class HFDownloadError(RuntimeError):
    """Не удалось скачать модель с Hugging Face."""


class HFDownloadCancelled(HFDownloadError):
    """Загрузка отменена пользователем; ``.part`` остаётся для докачки."""


@dataclass(frozen=True)
class HFSource:
    repo_id: str
    filename: str
    revision: str = "main"


@dataclass(frozen=True)
class ModelDownloadEvent:
    """Событие загрузки модели для GUI и Telegram."""

    phase: str  # start | progress | done | error | cancelled
    repo: str
    filename: str
    path: str = ""
    done: int = 0
    total: int = 0
    message: str = ""

    @property
    def name(self) -> str:
        return f"{self.repo}/{self.filename}" if self.repo else self.filename

    @property
    def percent(self) -> int | None:
        if self.total <= 0:
            return None
        return max(0, min(100, int(self.done * 100 / self.total)))


DownloadListener = Callable[[ModelDownloadEvent], None]
_LISTENERS: list[DownloadListener] = []
_LISTENERS_LOCK = threading.Lock()


def subscribe_model_download(listener: DownloadListener) -> None:
    """Подписывает GUI/Telegram на прогресс загрузки моделей."""
    with _LISTENERS_LOCK:
        if listener not in _LISTENERS:
            _LISTENERS.append(listener)


def unsubscribe_model_download(listener: DownloadListener) -> None:
    """Снимает подписку (выход из приложения/остановка бота)."""
    with _LISTENERS_LOCK:
        try:
            _LISTENERS.remove(listener)
        except ValueError:
            pass


def _publish(event: ModelDownloadEvent) -> None:
    with _LISTENERS_LOCK:
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Подписчик прогресса модели упал: {exc}")
    publish_download(
        DownloadEvent(
            kind="model",
            phase=event.phase,
            name=event.name,
            done=event.done,
            total=event.total,
            path=event.path,
            message=event.message,
        )
    )


def _clean_part(text: str) -> str:
    """Безопасное имя одного сегмента пути (без ``..`` и спецсимволов)."""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in (text or "")
    )
    cleaned = cleaned.strip("._")
    return cleaned or "model"


def _safe_relative_path(filename: str) -> PurePosixPath:
    """Проверяет, что имя файла не выходит за пределы папки модели."""
    raw = urllib.parse.unquote(filename or "").replace("\\", "/").strip()
    if "?" in raw:
        raw = urllib.parse.urlparse(raw).path
    raw = raw.lstrip("/")
    path = PurePosixPath(raw)
    parts = [p for p in path.parts if p not in ("", ".", "..")]
    if not parts:
        raise HFDownloadError("Не указано имя GGUF-файла в репозитории.")
    safe = PurePosixPath(*[_clean_part(p) for p in parts])
    if safe.suffix.lower() != ".gguf":
        raise HFDownloadError(
            f"Нужен файл .gguf, а указан: {safe.name}. "
            "В репозитории выберите именно GGUF-файл."
        )
    return safe


def _repo_from_url(text: str) -> tuple[str, str, str] | None:
    """Разбирает ссылку Hugging Face; ``(repo, revision, file)`` или None."""
    raw = (text or "").strip()
    if "huggingface.co" not in raw and "hf.co" not in raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    # /owner/repo, /owner/repo/resolve/main/file.gguf, /owner/repo/blob/main/...
    repo_id = "/".join(parts[:2])
    revision = "main"
    filename = ""
    for marker in ("resolve", "blob", "tree"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                revision = parts[idx + 1]
            filename = "/".join(parts[idx + 2 :])
            break
    if len(parts) > 2 and not filename:
        filename = "/".join(parts[2:])
    return repo_id, revision, filename


def parse_hf_source(
    repo: str,
    filename: str = "",
    revision: str = "",
) -> HFSource:
    """Собирает источник из repo id/URL, имени файла и ревизии."""
    repo_text = (repo or "").strip()
    file_text = (filename or "").strip()
    rev_text = (revision or "").strip()

    # в поле файла тоже может быть вставлена полная ссылка
    file_url = _repo_from_url(file_text)
    if file_url is not None:
        repo_text, url_rev, file_text = file_url
        rev_text = rev_text or url_rev

    url = _repo_from_url(repo_text)
    if url is not None:
        repo_text, url_rev, url_file = url
        rev_text = rev_text or url_rev
        file_text = file_text or url_file

    repo_text = repo_text.strip().strip("/")
    repo_parts = [p for p in repo_text.split("/") if p]
    if len(repo_parts) < 2:
        raise HFDownloadError(
            "Укажите репозиторий Hugging Face в виде owner/model "
            "или полную ссылку на файл."
        )
    repo_id = "/".join(repo_parts[:2])
    if not file_text:
        raise HFDownloadError(
            "Укажите GGUF-файл в репозитории, например model-q4_k_m.gguf."
        )
    safe_file = _safe_relative_path(file_text)
    return HFSource(repo_id=repo_id, filename=safe_file.as_posix(), revision=rev_text or "main")


def hf_local_path(cfg: "Config", source: HFSource | None = None) -> Path:
    """Куда именно скачивается модель внутри ``models_dir``."""
    lc = cfg.llm.llama_cpp
    if source is None:
        source = parse_hf_source(lc.hf_repo, lc.hf_file, lc.hf_revision)
    repo_dir = _clean_part(source.repo_id.split("/")[-1])
    if source.revision and source.revision != "main":
        # разные ревизии одного файла не должны перетирать друг друга
        repo_dir = f"{repo_dir}@{_clean_part(source.revision)}"
    relative = _safe_relative_path(source.filename)
    root = Path(lc.models_dir or "data/models").expanduser()
    return (root / repo_dir / Path(*relative.parts)).resolve()


def _hf_url(source: HFSource, endpoint: str) -> str:
    base = (endpoint or "https://huggingface.co").rstrip("/")
    repo = urllib.parse.quote(source.repo_id, safe="/")
    revision = urllib.parse.quote(source.revision, safe="")
    filename = urllib.parse.quote(source.filename, safe="/")
    return f"{base}/{repo}/resolve/{revision}/{filename}?download=true"


def _token() -> str:
    return (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or ""
    ).strip()


def _target_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def format_bytes(value: float) -> str:
    """Человекочитаемый размер для статуса GUI/лога."""
    size = float(max(0, value))
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if size < 1024 or unit == "ГиБ":
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} Б"
        size /= 1024
    return f"{size:.1f} ГиБ"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    if exc.code in (401, 403):
        return (
            "Hugging Face закрыл доступ к файлу. Для приватной или gated-модели "
            "задайте токен в переменной HF_TOKEN."
        )
    if exc.code == 404:
        return "Файл не найден в репозитории. Проверьте owner/model и имя .gguf."
    return f"Hugging Face ответил HTTP {exc.code}: {exc.reason}"


def _download_hf_model_source(
    cfg: "Config",
    source: HFSource,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    force: bool = False,
) -> Path:
    """Скачивает уже разобранный источник в ``models_dir``."""
    lc = cfg.llm.llama_cpp
    target = hf_local_path(cfg, source)
    if target.is_file() and target.stat().st_size > 0 and not force:
        if progress is not None:
            size = target.stat().st_size
            progress(size, size)
        return target

    with _target_lock(target):
        if target.is_file() and target.stat().st_size > 0 and not force:
            if progress is not None:
                size = target.stat().st_size
                progress(size, size)
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        if force:
            part.unlink(missing_ok=True)

        endpoint = lc.hf_endpoint or "https://huggingface.co"
        url = _hf_url(source, endpoint)
        existing = part.stat().st_size if part.exists() else 0
        logger.info(
            f"Hugging Face: качаю {source.repo_id}/{source.filename} "
            f"({format_bytes(existing)} уже есть)"
        )

        response = None
        for attempt in range(2):
            headers = {"User-Agent": "nemus-hf-downloader"}
            token = _token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if existing and attempt == 0:
                headers["Range"] = f"bytes={existing}-"
            request = urllib.request.Request(url, headers=headers)
            try:
                response = urllib.request.urlopen(request, timeout=HTTP_TIMEOUT)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and existing and attempt == 0:
                    # .part не совпал с удалённым файлом — начинаем заново
                    part.unlink(missing_ok=True)
                    existing = 0
                    continue
                raise HFDownloadError(_http_error_message(exc)) from exc
            except (OSError, urllib.error.URLError) as exc:
                raise HFDownloadError(
                    f"Не удалось связаться с Hugging Face: {exc}"
                ) from exc
        if response is None:  # pragma: no cover - защита от будущих правок
            raise HFDownloadError("Hugging Face не вернул ответ.")

        status = getattr(response, "status", 200)
        length = int(response.headers.get("Content-Length") or 0)
        total = 0
        mode = "ab"
        if status == 206 and existing:
            content_range = response.headers.get("Content-Range") or ""
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                total = int(match.group(1))
            elif length:
                total = existing + length
        else:
            # сервер не поддержал Range: качаем файл сначала
            existing = 0
            mode = "wb"
            total = length

        done = existing
        if progress is not None:
            progress(done, total)
        last_report = 0.0
        next_log = max(1, total // 10) if total else 0
        try:
            with part.open(mode) as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise HFDownloadCancelled("Загрузка модели отменена.")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total and next_log and done >= next_log and done < total:
                        percent = min(99, int(done * 100 / total))
                        logger.info(
                            f"Hugging Face: {percent}% "
                            f"({format_bytes(done)} из {format_bytes(total)})"
                        )
                        next_log += max(1, total // 10)
                    now = time.monotonic()
                    if progress is not None and (
                        now - last_report >= PROGRESS_INTERVAL
                        or (total and done >= total)
                    ):
                        progress(done, total)
                        last_report = now
                fh.flush()
        except HFDownloadCancelled:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise HFDownloadError(
                f"Загрузка прервалась на {format_bytes(done)}: {exc}. "
                "Повторный запуск продолжит с этого места."
            ) from exc
        finally:
            response.close()

        if total and done != total:
            raise HFDownloadError(
                f"Файл скачан не полностью: {format_bytes(done)} из "
                f"{format_bytes(total)}. Повторный запуск продолжит загрузку."
            )
        try:
            os.replace(part, target)
        except OSError as exc:
            raise HFDownloadError(
                f"Не удалось сохранить модель в {target}: {exc}. "
                "Проверьте права на папку data/models."
            ) from exc
        if progress is not None:
            progress(done, done)
        logger.info(
            f"Hugging Face: модель сохранена в {target} "
            f"({format_bytes(target.stat().st_size)})"
        )
        return target


def _download_split_model(
    cfg: "Config",
    source: HFSource,
    *,
    progress: ProgressCallback | None = None,
    status: StatusCallback | None = None,
    cancel: threading.Event | None = None,
    force: bool = False,
) -> Path:
    """Скачивает все шарды и склеивает их в один GGUF."""
    lc = cfg.llm.llama_cpp
    part = split_part(source.filename)
    if part is None:
        raise HFDownloadError("Файл не является частью разбитого GGUF.")
    output_source = HFSource(
        repo_id=source.repo_id,
        filename=merged_filename(source.filename),
        revision=source.revision,
    )
    output = hf_local_path(cfg, output_source)
    if output.is_file() and output.stat().st_size > 0 and not force:
        if progress is not None:
            size = output.stat().st_size
            progress(size, size)
        return output

    try:
        catalog = list_hf_gguf_files(
            source.repo_id, endpoint=lc.hf_endpoint or "https://huggingface.co"
        )
    except HFCatalogError as exc:
        raise HFDownloadError(str(exc)) from exc

    members = []
    for item in catalog:
        item_part = split_part(item.filename)
        if item_part is None:
            continue
        if item_part.prefix == part.prefix and item_part.count == part.count:
            members.append((item_part.index, item))
    members.sort(key=lambda pair: pair[0])
    if len(members) != part.count or [n for n, _ in members] != list(
        range(1, part.count + 1)
    ):
        raise HFDownloadError(
            f"На Hugging Face найдено {len(members)} из {part.count} частей "
            f"{source.filename}."
        )

    shard_sources = [
        HFSource(
            repo_id=source.repo_id,
            filename=item.filename,
            revision=source.revision,
        )
        for _, item in members
    ]
    shard_paths = [hf_local_path(cfg, shard) for shard in shard_sources]
    sizes = [int(item.size) for _, item in members]
    planned = sum(sizes) if all(size > 0 for size in sizes) else 0
    # Скачивание и склейка — примерно одинаковый объём работы. Держим
    # прогресс непрерывным: первая половина — загрузка, вторая — merge.
    overall_total = planned * 2 if planned else 0
    completed = 0
    for index, (shard_source, shard_path) in enumerate(
        zip(shard_sources, shard_paths, strict=True), start=1
    ):
        if cancel is not None and cancel.is_set():
            raise HFDownloadCancelled("Загрузка модели отменена.")
        existing = shard_path.stat().st_size if shard_path.is_file() else 0
        if existing and not force:
            completed += existing
            if progress is not None:
                progress(completed, overall_total or completed)
            continue
        if status is not None:
            status(f"Скачиваю часть {index} из {part.count}…")
        base = completed

        def report(done: int, total: int, base: int = base) -> None:
            if progress is None:
                return
            if overall_total:
                progress(min(overall_total, base + done), overall_total)
            else:
                progress(done, total)

        downloaded = _download_hf_model_source(
            cfg,
            shard_source,
            progress=report,
            cancel=cancel,
            force=force,
        )
        completed += downloaded.stat().st_size

    actual_download = sum(
        path.stat().st_size for path in shard_paths if path.is_file()
    )
    merge_total = actual_download
    merge_done_base = actual_download
    if progress is not None:
        progress(merge_done_base, merge_done_base + merge_total)
    if status is not None:
        status("Склеиваю части в один GGUF…")

    def merge_progress(done: int, _total: int) -> None:
        if progress is not None:
            progress(merge_done_base + done, merge_done_base + merge_total)

    try:
        merged = merge_gguf_shards(
            shard_paths,
            output,
            progress=merge_progress,
            cancel=cancel,
        )
    except GGUFMergeCancelled as exc:
        raise HFDownloadCancelled(str(exc)) from exc
    except GGUFMergeError as exc:
        raise HFDownloadError(str(exc)) from exc

    # После успешной склейки шарды больше не нужны и только занимают место.
    # При ошибке они остаются, чтобы повторный запуск не качал всё заново.
    for path in shard_paths:
        for extra in (
            path,
            path.with_name(path.name + ".part"),
            path.with_name(path.name + ".part.json"),
        ):
            try:
                extra.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(f"Hugging Face: не удалось удалить {extra}: {exc}")
    logger.info(
        f"Hugging Face: шарды склеены в {merged} "
        f"({format_bytes(merged.stat().st_size)})"
    )
    return merged


def download_hf_model(
    cfg: "Config",
    *,
    repo: str | None = None,
    filename: str | None = None,
    revision: str | None = None,
    progress: ProgressCallback | None = None,
    status: StatusCallback | None = None,
    cancel: threading.Event | None = None,
    force: bool = False,
) -> Path:
    """Скачивает выбранный GGUF и сообщает прогресс подписчикам.

    Если выбран первый шард разбитого GGUF, скачивает все части и
    склеивает их в один файл. Итоговый путь всегда указывает на целый GGUF.
    """
    lc = cfg.llm.llama_cpp
    repo_text = lc.hf_repo if repo is None else repo
    file_text = lc.hf_file if filename is None else filename
    rev_text = lc.hf_revision if revision is None else revision
    try:
        source = parse_hf_source(repo_text, file_text, rev_text)
        output_name = merged_filename(source.filename)
        target_source = HFSource(
            repo_id=source.repo_id,
            filename=output_name,
            revision=source.revision,
        )
        target = hf_local_path(cfg, target_source)
    except HFDownloadError as exc:
        _publish(
            ModelDownloadEvent(
                phase="error",
                repo=repo_text,
                filename=file_text,
                message=str(exc),
            )
        )
        raise

    _publish(
        ModelDownloadEvent(
            phase="start",
            repo=source.repo_id,
            filename=output_name,
            path=str(target),
        )
    )

    def report(done: int, total: int) -> None:
        _publish(
            ModelDownloadEvent(
                phase="progress",
                repo=source.repo_id,
                filename=output_name,
                path=str(target),
                done=done,
                total=total,
            )
        )
        if progress is not None:
            progress(done, total)

    try:
        if is_split_gguf(source.filename):
            path = _download_split_model(
                cfg,
                source,
                progress=report,
                status=status,
                cancel=cancel,
                force=force,
            )
        else:
            if status is not None:
                status("Скачиваю GGUF…")
            path = _download_hf_model_source(
                cfg, source, progress=report, cancel=cancel, force=force
            )
    except HFDownloadCancelled as exc:
        _publish(
            ModelDownloadEvent(
                phase="cancelled",
                repo=source.repo_id,
                filename=output_name,
                path=str(target),
                message=str(exc),
            )
        )
        raise
    except HFDownloadError as exc:
        _publish(
            ModelDownloadEvent(
                phase="error",
                repo=source.repo_id,
                filename=output_name,
                path=str(target),
                message=str(exc),
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _publish(
            ModelDownloadEvent(
                phase="error",
                repo=source.repo_id,
                filename=output_name,
                path=str(target),
                message=str(exc),
            )
        )
        raise HFDownloadError(f"Не удалось скачать модель: {exc}") from exc

    size = path.stat().st_size if path.is_file() else 0
    _publish(
        ModelDownloadEvent(
            phase="done",
            repo=source.repo_id,
            filename=output_name,
            path=str(path),
            done=size,
            total=size,
        )
    )
    return path


def ensure_hf_model(
    cfg: "Config",
    model: str,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> Path | None:
    """Скачивает модель, если выбрана локальная заглушка/алиас.

    Явный путь ``llama_cpp/data/models/foo.gguf`` не подменяется: если файла
    нет, пользователь должен увидеть ошибку, а не молча получить другую модель.
    """
    lc = cfg.llm.llama_cpp
    if not lc.auto_download or not (lc.hf_repo or lc.hf_file):
        return None
    raw = (model or "").strip()
    if raw.startswith("llama_cpp/"):
        raw = raw[len("llama_cpp/") :]
    aliases = {"", "local", "default", "model", (lc.model_alias or "").strip()}
    if raw not in aliases:
        # Явный путь не подменяем, но если это ровно тот файл, который
        # должен лежать после HF-загрузки, — восстанавливаем его.
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        try:
            expected = hf_local_path(cfg)
        except HFDownloadError:
            return None
        if candidate.resolve() != expected:
            return None
    else:
        local = Path(lc.model_path).expanduser() if lc.model_path else None
        if local is not None and local.is_file() and local.stat().st_size > 0:
            return local.resolve()

    path = download_hf_model(cfg, progress=progress, cancel=cancel)
    lc.model_path = str(path)
    return path


__all__ = [
    "HFDownloadCancelled",
    "HFDownloadError",
    "HFSource",
    "download_hf_model",
    "ensure_hf_model",
    "format_bytes",
    "hf_local_path",
    "parse_hf_source",
]
