"""Тесты запуска процессов: run_capture и внутренние вызовы без зависаний.

Регрессия к инциденту 30.10.2026: ``subprocess.run(..., timeout=...)`` на
Windows по таймауту убивал только прямого потомка, а затем блокировался в
``communicate()`` без таймаута — дочерний процесс, наследовавший канал вывода,
держал его открытым, и вызов висел вечно вместе со всем накопленным выводом.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.tools.shell import run_capture  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{name}: {detail}")


def test_streams_separated() -> None:
    """stdout и stderr не сливаются в один поток (три вызова парсят stdout)."""
    captured = run_capture(
        [
            sys.executable,
            "-c",
            "import sys; print('OUT-LINE'); print('ERR-LINE', file=sys.stderr)",
        ],
        timeout=20,
    )
    out = captured.stdout.decode("utf-8", errors="replace")
    err = captured.stderr.decode("utf-8", errors="replace")
    check("run_capture: код возврата", captured.returncode == 0, str(captured.returncode))
    check("run_capture: stdout отделён", "OUT-LINE" in out and "ERR-LINE" not in out, out)
    check("run_capture: stderr отделён", "ERR-LINE" in err and "OUT-LINE" not in err, err)


def test_timeout_kills_tree() -> None:
    """Таймаут возвращается в срок, вывод сохраняется, сироты убиты.

    Сценарий инцидента: прямой потомок порождает внука, который наследует
    канал вывода и держит его открытым после смерти родителя.
    """
    try:
        import psutil
    except ImportError:  # noqa: BLE001
        print("[skip] psutil недоступен — проверка сирот пропущена")
        psutil = None

    marker = f"nemus_marker_{uuid.uuid4().hex[:8]}"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300) # {marker}'])\n"
        "print('spawned', flush=True)\n"
        "time.sleep(300)\n"
    )

    started = time.monotonic()
    captured = run_capture([sys.executable, "-c", script], timeout=3)
    elapsed = time.monotonic() - started
    check("таймаут: вернулся в срок", elapsed < 15, f"{elapsed:.1f} c")
    check("таймаут: зафиксирован", captured.timed_out, str(captured.timed_out))
    out = captured.stdout.decode("utf-8", errors="replace")
    check("таймаут: вывод до остановки сохранён", "spawned" in out, out[:120])

    if psutil is None:
        return

    def marker_pids() -> list[int]:
        hits = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if marker in " ".join(proc.info["cmdline"] or []):
                    hits.append(int(proc.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                continue
        return hits

    # дерево процессов должно умереть вместе с прямым потомком
    for _ in range(30):
        if not marker_pids():
            break
        time.sleep(0.2)
    left = marker_pids()
    check("таймаут: дерево процессов убито (нет сирот)", not left, f"остались: {left}")


def test_uwp_apps_not_hanging() -> None:
    """Get-StartApps через PowerShell должен вернуться, а не подвесить ядро."""
    from nemus.core.tools.system import POWERSHELL_TIMEOUT, OpenApplicationTool

    started = time.monotonic()
    apps = OpenApplicationTool._load_uwp_apps()
    elapsed = time.monotonic() - started
    check(
        "system: список UWP-приложений вернулся",
        isinstance(apps, list) and elapsed < POWERSHELL_TIMEOUT + 6,
        f"{len(apps)} записей за {elapsed:.1f} c",
    )


def test_powershell_tts_not_hanging() -> None:
    """Скрипт PowerShell для синтеза речи возвращается со своим stdout."""
    try:
        from nemus.voice.tts import _run_powershell
    except Exception as exc:  # noqa: BLE001
        print(f"[skip] tts недоступен: {type(exc).__name__}: {exc}")
        return

    started = time.monotonic()
    raw = _run_powershell("Write-Output 'ps-ok'")
    elapsed = time.monotonic() - started
    check(
        "tts: PowerShell вернулся со stdout",
        "ps-ok" in raw and elapsed < 66,
        f"{elapsed:.1f} c, raw={raw[:60]!r}",
    )


def test_volume_cli_wrapper() -> None:
    """Обёртка volume._run сохраняет CompletedProcess и не зависает."""
    from nemus.core.tools.volume import _run

    result = _run([sys.executable, "-c", "print('vol-ok')"])
    check("volume: returncode", result.returncode == 0, str(result.returncode))
    check("volume: stdout", "vol-ok" in result.stdout, result.stdout[:80])
    check("volume: stderr пуст", not (result.stderr or "").strip(), result.stderr[:80])


def test_ruff_format_path() -> None:
    """Путь create_tool: ruff format через run_capture не зависает."""
    target = Path(tempfile.gettempdir()) / f"nemus_fmt_{uuid.uuid4().hex[:8]}.py"
    target.write_text("x  =  1\n", encoding="utf-8")
    started = time.monotonic()
    captured = run_capture([sys.executable, "-m", "ruff", "format", str(target)], timeout=30)
    elapsed = time.monotonic() - started
    formatted = target.read_text(encoding="utf-8")
    check(
        "create_tool: ruff format отработал",
        not captured.timed_out and "x = 1" in formatted and elapsed < 35,
        f"{elapsed:.1f} c, код={formatted!r}",
    )
    target.unlink(missing_ok=True)


def main() -> int:
    test_streams_separated()
    test_timeout_kills_tree()
    test_uwp_apps_not_hanging()
    test_powershell_tts_not_hanging()
    test_volume_cli_wrapper()
    test_ruff_format_path()

    if failures:
        print(f"\nПровалено проверок: {len(failures)}")
        for item in failures:
            print(" -", item)
        return 1
    print("\nВсе проверки запуска процессов пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
