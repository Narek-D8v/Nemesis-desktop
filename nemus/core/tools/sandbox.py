"""Ограничение файловых операций инструментов рабочей папкой.

Инструменты, изменяющие файловую систему (запись, удаление, выполнение
скриптов), по умолчанию работают только внутри рабочей папки ассистента
(``tools.workspace``). Это не даёт модели писать/удалять файлы в системных
каталогах. Чтение (``read_file``/``list_directory``/``search_files``) не
ограничивается — оно безопасно и часто нужно для произвольных файлов.

Отключить ограничение можно флагом ``tools.allow_outside_workspace: true``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from nemus.config import PROJECT_ROOT


class PathNotAllowed(PermissionError):
    """Путь выходит за пределы разрешённых каталогов."""


def _expand(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


@dataclass
class PathGuard:
    """Проверяет, что путь для записи/удаления лежит в разрешённых каталогах."""

    workspace: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "workspace")
    allow_outside: bool = False
    extra_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        self.workspace = _expand(self.workspace).resolve()
        self.extra_roots = tuple(_expand(p).resolve() for p in self.extra_roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.workspace, *self.extra_roots)

    def _is_within(self, resolved: Path) -> bool:
        for root in self.roots:
            if resolved == root or root in resolved.parents:
                return True
        return False

    def ensure_dir(self) -> None:
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def resolve_read(self, path: str | os.PathLike[str]) -> Path:
        """Путь для чтения — без ограничений (только раскрытие ~ и переменных)."""
        return _expand(path)

    def resolve_write(self, path: str | os.PathLike[str], *, what: str = "Путь") -> Path:
        """Путь для записи/удаления. Бросает :class:`PathNotAllowed` вне песочницы.

        Относительные пути трактуются относительно рабочей папки.
        """
        raw = _expand(path)
        if self.allow_outside:
            return raw
        candidate = raw if raw.is_absolute() else self.workspace / raw
        resolved = candidate.resolve()
        if not self._is_within(resolved):
            raise PathNotAllowed(
                f"{what} вне рабочей папки: {resolved}. "
                f"Разрешено внутри {self.workspace} "
                f"(изменить: tools.allow_outside_workspace)."
            )
        return resolved
