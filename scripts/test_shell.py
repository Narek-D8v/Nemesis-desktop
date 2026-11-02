"""Тесты инструмента оболочки: защита от зависаний и живой вывод сессий.

Регрессия к инциденту 30.10.2026: `cd D:\\Nemesis && npx hyperframes --version`
вис вечно — `subprocess.run` на Windows после таймаута ждал закрытия каналов
вывода, которые держали внуки (npx → node).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.tools.all import all_builtin_tools  # noqa: E402
from nemus.core.tools.sandbox import PathGuard  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{name}: {detail}")


def get_shell_tool():
    tools = {t.name: t for t in all_builtin_tools()}
    tool = tools["run_shell"]
    if getattr(tool, "guard", None) is None:
        tool.guard = PathGuard()
    return tool


def test_basic(tool) -> None:
    result = tool.execute(command="echo hello")
    check("run: обычная команда", result.ok and "hello" in result.content, result.content[:120])


def test_encoding(tool) -> None:
    """Кириллица в выводе cmd (OEM cp866) должна читаться, а не превращаться в мусор."""
    result = tool.execute(command="echo Привет, мир")
    body = result.content
    check("run: кириллица из cmd", "Привет, мир" in body, body[:160].replace("\n", " "))
    check("run: нет символов замены", "\ufffd" not in body, "найден U+FFFD")


def test_shell_autodetect(tool) -> None:
    from nemus.core.tools.shell import _git_bash

    if _git_bash():
        result = tool.execute(command="which bash && echo POSIX_OK")
        check(
            "run: POSIX-команда уходит в bash",
            "оболочка: bash" in result.content,
            result.content[:160].replace("\n", " "),
        )
        if "оболочка: bash" in result.content:
            check("run: bash реально выполнил", "POSIX_OK" in result.content, result.content[:200])
    else:
        print("[skip] Git Bash не найден — автовыбор оболочки не проверяем")

    result = tool.execute(command="echo %TEMP%")
    check("run: %VAR% остаётся в cmd", "оболочка: cmd" in result.content, result.content[:120])

    result = tool.execute(command="echo hi", shell="powershell")
    check("run: явный shell=powershell", "оболочка: powershell" in result.content, result.content[:120])


def test_timeout_becomes_session(tool) -> None:
    """Дедлайн не убивает команду и не теряет вывод: она уходит в фоновую сессию."""
    ticker = Path(tempfile.gettempdir()) / "nemus_shell_ticker.py"
    ticker.write_text(
        "import time\n"
        "for i in range(60):\n"
        "    print(i, flush=True)\n"
        "    time.sleep(0.3)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{ticker}"'

    started = time.monotonic()
    result = tool.execute(command=command, timeout=2)
    elapsed = time.monotonic() - started
    check("timeout: вернулся в срок", elapsed < 15, f"{elapsed:.1f} c")
    check("timeout: не ошибка, а сессия", result.ok, result.content[:200])
    check("timeout: отдан id сессии", "сессия" in result.content, result.content[:200])
    session_id = _session_id(result.content)
    check("timeout: id сессии разбирается", bool(session_id), result.content[:200])
    if not session_id:
        ticker.unlink(missing_ok=True)
        return

    time.sleep(1)
    read = tool.execute(action="read", session=session_id, wait=3)
    check(
        "read: вывод идёт в реальном времени",
        read.ok and re.search(r"(?m)^\d+\r?$", read.content) is not None,
        read.content[:200].replace("\n", " | "),
    )

    kill = tool.execute(action="kill", session=session_id)
    check("kill: сессия остановлена", kill.ok, kill.content[:200])
    again = tool.execute(action="read", session=session_id, wait=1)
    check("kill: сессия больше не найдена", not again.ok, again.content[:120])
    ticker.unlink(missing_ok=True)


def test_interactive_prompt(tool) -> None:
    """Запрос ввода распознаётся, а ответ уходит в stdin живой сессии."""
    script = Path(tempfile.gettempdir()) / "nemus_shell_prompt_probe.py"
    script.write_text(
        'print("Ok to proceed? (y/n) ", end="", flush=True)\n'
        "x = input()\n"
        'print("got:" + x, flush=True)\n',
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}"'
    started = tool.execute(action="start", command=command, wait=3)
    session_id = _session_id(started.content)
    check("start: сессия запущена", started.ok and bool(session_id), started.content[:240])
    if not session_id:
        script.unlink(missing_ok=True)
        return

    # вывод должен появиться до того, как мы что-нибудь отправим
    first = started.content
    prompt_seen = "proceed" in first.lower()
    if not prompt_seen:
        polled = tool.execute(action="read", session=session_id, wait=5)
        first += "\n" + polled.content
        prompt_seen = "proceed" in polled.content.lower()
    check("read: вывод появился до ввода", prompt_seen, first[:240].replace("\n", " | "))
    check(
        "запрос ввода распознан моделью",
        "⚠️ Процесс ожидает ввода" in first,
        first[:300].replace("\n", " | "),
    )

    sent = tool.execute(action="write", session=session_id, input="y")
    check("write: ответ программы получен", sent.ok and "got:y" in sent.content, sent.content[:240])

    final = ""
    for _ in range(10):
        read = tool.execute(action="read", session=session_id, wait=1)
        final = read.content
        if "завершён" in final:
            break
    check("read: процесс завершился", "завершён" in final, final[:200].replace("\n", " | "))
    script.unlink(missing_ok=True)


def test_no_orphan_processes() -> None:
    """Убийство сессии должно убивать и дочерние процессы (иначе — сироты)."""
    try:
        import psutil
    except ImportError:  # noqa: BLE001
        print("[skip] psutil недоступен — проверка сирот пропущена")
        return

    from nemus.core.tools.shell import _kill_tree

    proc = subprocess.Popen(
        ["cmd", "/c", "ping -n 60 127.0.0.1 >nul"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.5)
    parent = psutil.Process(proc.pid)
    children = parent.children(recursive=True)
    check("kill_tree: дочерний процесс запущен", bool(children), f"детей: {len(children)}")

    _kill_tree(proc)
    gone, alive = psutil.wait_procs([parent, *children], timeout=15)
    check("kill_tree: дерево процессов убито", not alive, f"остались: {alive}")
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()


def test_unknown_action(tool) -> None:
    result = tool.execute(action="teleport", command="echo hi")
    check("run: неизвестное действие — понятная ошибка", not result.ok, result.content[:120])


def _session_id(text: str) -> str:
    for token in text.replace("\n", " ").split():
        stripped = token.strip(".,;:()")
        if len(stripped) == 8 and all(c in "0123456789abcdef" for c in stripped):
            return stripped
        for marker in ("session=", "сессию", "сессия"):
            if marker in stripped and "=" in stripped:
                candidate = stripped.split("=", 1)[1]
                if len(candidate) == 8 and all(c in "0123456789abcdef" for c in candidate):
                    return candidate
    return ""


def main() -> int:
    tool = get_shell_tool()
    print("Оболочка по умолчанию:", tool._resolve_shell("echo hi", ""))
    test_basic(tool)
    test_encoding(tool)
    test_shell_autodetect(tool)
    test_timeout_becomes_session(tool)
    test_interactive_prompt(tool)
    test_no_orphan_processes()
    test_unknown_action(tool)

    if failures:
        print(f"\nПровалено проверок: {len(failures)}")
        for item in failures:
            print(" -", item)
        return 1
    print("\nВсе проверки run_shell пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
