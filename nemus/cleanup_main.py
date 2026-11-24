"""CLI очистки старых файлов: ``python -m nemus --cleanup``.

Команда показывает, что будет удалено, и удаляет только с ``--yes``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nemus.config import DATA_DIR, get_config, load_config
from nemus.core.cleanup import (
    DEFAULT_CLEANUP_DAYS,
    cleanup_storage,
    human_bytes,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m nemus --cleanup",
        description="Удалить старые скриншоты, вложения и временные файлы Немуси.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="путь к config.yaml (по умолчанию config/config.yaml)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_CLEANUP_DAYS,
        help=f"хранить файлы не старше N дней (по умолчанию {DEFAULT_CLEANUP_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать, что будет удалено",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="действительно удалить файлы (без флага — только предпросмотр)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.days < 0:
        print("Ошибка: --days не может быть отрицательным.")
        return 2

    config = load_config(args.config) if args.config else get_config()
    workspace = Path(config.tools.workspace)
    remove = args.yes and not args.dry_run
    result = cleanup_storage(
        workspace,
        days=args.days,
        dry_run=not remove,
        tmp_dir=DATA_DIR / "tmp",
    )

    print(f"Рабочая папка: {workspace}")
    print(f"Старше {args.days} дн.: {result.candidates} файлов, "
          f"{human_bytes(result.candidate_bytes)}")
    if result.candidates == 0:
        print("Старых файлов не найдено.")
        return 0
    if not remove:
        if args.dry_run:
            print("Предпросмотр: ничего не удалено.")
        else:
            print("Ничего не удалено. Для удаления добавьте --yes.")
        return 0
    print(
        f"Удалено: {result.removed} файлов, освобождено {human_bytes(result.freed)}."
    )
    if result.failed:
        print(
            f"Не удалось удалить: {result.failed} файлов — возможно, "
            "они заняты другой программой."
        )
        return 1
    return 0
