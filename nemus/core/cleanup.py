"""Очистка старых файлов, скриншотов и временных остатков Немуси.

Ассистент складывает снимки экрана, сгенерированные картинки и вложения
Telegram в рабочую папку. Часть служебных файлов (озвучка, аватары, сниппеты)
живёт в системном ``%TEMP%``. Сами по себе они не удаляются, поэтому папки
растут месяцами. Модуль удаляет только файлы старше заданного возраста и не
трогает базы, модели, конфиги и свежие файлы.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from nemus.config import PROJECT_ROOT

#: сколько дней файлы считаются свежими по умолчанию
DEFAULT_CLEANUP_DAYS = 7

#: подпапки рабочей папки, которые целиком считаются временными
_WORKSPACE_SCRATCH_DIRS = (
    "telegram",
    "images",
    "screenshots",
    "tmp",
    "Downloads",
    "downloads",
)

#: временные файлы в корне рабочей папки (скриншоты, снимки, голос)
_ROOT_SCRATCH_PATTERNS = (
    "screenshot*",
    "screen*",
    "camera_photo*",
    "voice*",
    "*.tmp",
    "*.part",
    "*.crdownload",
)

_ROOT_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

#: префиксы служебных файлов Немуси в системном временном каталоге
_TEMP_PREFIXES = ("nemus_", "nemus-")


@dataclass
class CleanupResult:
    """Итог очистки: что нашли, что удалили и сколько места освободили."""

    candidates: int = 0
    candidate_bytes: int = 0
    removed: int = 0
    freed: int = 0
    failed: int = 0
    areas: dict[str, int] = field(default_factory=dict)


def human_bytes(size: int) -> str:
    """Размер в человекочитаемом виде (для GUI и CLI)."""
    value = float(max(0, size))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".", ",")
        value /= 1024
    return f"{int(size)} Б"


def _resolve_workspace(workspace: str | os.PathLike[str]) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(workspace))))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _iter_files(root: Path):
    """Файлы внутри каталога без перехода по символьным ссылкам."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames if not (base / name).is_symlink()
        ]
        for name in filenames:
            yield base / name


def _remove_empty_dirs(root: Path) -> None:
    """Убирает пустые подпапки, оставшиеся после удаления файлов."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, _filenames in os.walk(
        root, topdown=False, followlinks=False
    ):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            path.rmdir()
        except OSError:
            continue


def cleanup_storage(
    workspace: str | os.PathLike[str],
    *,
    days: int = DEFAULT_CLEANUP_DAYS,
    dry_run: bool = False,
    tmp_dir: str | os.PathLike[str] | None = None,
    system_temp: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> CleanupResult:
    """Удаляет старые временные файлы Немуси.

    ``workspace`` — рабочая папка ассистента (``tools.workspace``). В ней
    очищаются известные временные подпапки, снимки экрана, картинки в корне
    и файлы голосового ввода. ``tmp_dir`` — необязательная служебная папка
    (обычно ``data/tmp``). В системном временном каталоге удаляются только
    объекты с префиксом ``nemus_``/``nemus-``.

    Файлы моложе ``days`` дней не трогаются. При ``dry_run=True`` ничего не
    удаляется, но возвращается количество и размер кандидатов.
    """
    if days < 0:
        raise ValueError("days не может быть отрицательным")

    workspace_path = _resolve_workspace(workspace)
    cutoff = (time.time() if now is None else now) - days * 86400
    result = CleanupResult()
    seen: set[Path] = set()

    def consider(path: Path, area: str) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                return
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                return
        except OSError:
            return
        try:
            identity = path.resolve(strict=False)
        except OSError:
            identity = path
        if identity in seen:
            return
        seen.add(identity)
        result.candidates += 1
        result.candidate_bytes += stat.st_size
        result.areas[area] = result.areas.get(area, 0) + 1
        if dry_run:
            return
        try:
            path.unlink()
        except OSError:
            result.failed += 1
            return
        result.removed += 1
        result.freed += stat.st_size

    # 1. Известные временные подпапки рабочей папки.
    for name in _WORKSPACE_SCRATCH_DIRS:
        for path in _iter_files(workspace_path / name):
            consider(path, name)

    # 2. Скриншоты, снимки камеры, голосовые и прочие временные файлы в корне.
    for pattern in _ROOT_SCRATCH_PATTERNS:
        for path in workspace_path.glob(pattern):
            consider(path, "workspace")

    # 3. Картинки в корне рабочей папки: обычно это результат генерации.
    try:
        root_entries = list(workspace_path.iterdir())
    except OSError:
        root_entries = []
    for path in root_entries:
        if path.is_file() and path.suffix.lower() in _ROOT_IMAGE_SUFFIXES:
            consider(path, "workspace")

    # 4. Служебная папка данных (data/tmp), если она есть.
    if tmp_dir is not None:
        for path in _iter_files(_resolve_workspace(tmp_dir)):
            consider(path, "data/tmp")

    # 5. Остатки в системном временном каталоге: только файлы Немуси.
    temp_root = Path(system_temp or tempfile.gettempdir())
    try:
        temp_entries = list(temp_root.iterdir())
    except OSError:
        temp_entries = []
    for path in temp_entries:
        if path.name.startswith(_TEMP_PREFIXES):
            consider(path, "temp")

    if not dry_run:
        for name in _WORKSPACE_SCRATCH_DIRS:
            _remove_empty_dirs(workspace_path / name)
        if tmp_dir is not None:
            _remove_empty_dirs(_resolve_workspace(tmp_dir))

    return result
