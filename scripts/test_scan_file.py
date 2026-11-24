"""Проверка инструмента scan_file: разбор вердикта и защита от угроз."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.core.tools.shell import CapturedRun  # noqa: E402
from nemus.core.tools.system import ScanFileTool, _scan_verdict  # noqa: E402

assert _scan_verdict("Scan finished. found no threats.") is True
assert _scan_verdict("Scan finished. found threats: Trojan") is False
assert _scan_verdict("Scan finished.") is True
assert _scan_verdict("CmdTool: Failed with hr = 0x80508023") is None
print("ok  scan_file: вердикт сканера разбирается")

tool = ScanFileTool()
missing = tool.execute(path="C:/definitely/missing/file.exe")
assert not missing.ok and "не найден" in missing.content.lower()
print("ok  scan_file: отсутствующий файл не запускает сканер")

target = str(Path(__file__).resolve())
with (
    patch(
        "nemus.core.tools.system._defender_cli",
        return_value="C:/Program Files/Windows Defender/MpCmdRun.exe",
    ),
    patch("nemus.core.tools.system.run_capture") as capture,
):
    capture.return_value = CapturedRun(
        0, b"Scan finished.\nfound no threats.\n", b""
    )
    clean = tool.execute(path=target)
    assert clean.ok and "угроз не найдено" in clean.content
    print("ok  scan_file: чистая проверка")

    capture.return_value = CapturedRun(
        0, b"Scan finished.\nfound threats: Trojan:Win32/Test\n", b""
    )
    dirty = tool.execute(path=target)
    assert not dirty.ok and "угроз" in dirty.content.lower()
    print("ok  scan_file: угроза блокирует запуск")

    capture.return_value = CapturedRun(0, b"", b"", timed_out=True)
    slow = tool.execute(path=target)
    assert not slow.ok and "не уложилась" in slow.content
    print("ok  scan_file: таймаут считается провалом")

print("SCAN FILE TESTS PASSED")
