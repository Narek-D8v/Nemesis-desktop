"""Тесты очистки старых файлов, скриншотов и временных остатков."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nemus.core.cleanup import cleanup_storage, human_bytes  # noqa: E402


def _make(path: Path, *, days_old: float = 0, size: int = 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - days_old * 86400
    os.utime(path, (stamp, stamp))


def test_cleanup_old_and_keep_recent() -> None:
    with tempfile.TemporaryDirectory(prefix="nemus_cleanup_test_") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        data_tmp = root / "data_tmp"
        system_temp = root / "temp"
        system_temp.mkdir()

        old = [
            workspace / "telegram" / "1" / "old.ogg",
            workspace / "images" / "old.png",
            workspace / "screen.png",
            workspace / "artist.png",
            data_tmp / "old.tmp",
            system_temp / "nemus_tts_old.wav",
        ]
        recent = [
            workspace / "telegram" / "1" / "new.ogg",
            workspace / "images" / "new.png",
            workspace / "screen_new.png",
            data_tmp / "new.tmp",
            system_temp / "nemus_tts_new.wav",
        ]
        untouched = [
            workspace / "keep.txt",
            system_temp / "other.txt",
        ]
        for path in old:
            _make(path, days_old=10)
        for path in recent:
            _make(path, days_old=0)
        for path in untouched:
            _make(path, days_old=10)

        preview = cleanup_storage(
            workspace,
            days=7,
            dry_run=True,
            tmp_dir=data_tmp,
            system_temp=system_temp,
        )
        assert preview.candidates == len(old), preview
        assert preview.removed == 0
        assert all(path.exists() for path in old), "dry-run не должен удалять"

        result = cleanup_storage(
            workspace,
            days=7,
            tmp_dir=data_tmp,
            system_temp=system_temp,
        )
        assert result.candidates == len(old), result
        assert result.removed == len(old), result
        assert result.failed == 0, result
        assert result.freed == len(old) * 16, result
        assert not any(path.exists() for path in old), "старые файлы остались"
        assert all(path.exists() for path in recent), "свежие файлы удалены"
        assert all(path.exists() for path in untouched), "чужие файлы удалены"

    print("ok  очистка удаляет старое и не трогает свежее и чужое")


def test_cleanup_invalid_days() -> None:
    try:
        cleanup_storage(".", days=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("отрицательный срок должен отклоняться")
    print("ok  отрицательный срок отклоняется")


def test_human_bytes() -> None:
    assert human_bytes(0) == "0 Б"
    assert human_bytes(1536) == "1,5 КБ"
    print("ok  размер файлов читается по-человечески")


if __name__ == "__main__":
    test_cleanup_old_and_keep_recent()
    test_cleanup_invalid_days()
    test_human_bytes()
    print("\nCLEANUP TESTS PASSED")
