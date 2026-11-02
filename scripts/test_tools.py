"""Smoke-тест всех встроенных инструментов (безопасные, без сети)."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import PROJECT_ROOT  # noqa: E402
from nemus.core.tools.all import all_builtin_tools  # noqa: E402
from nemus.core.tools.sandbox import PathGuard  # noqa: E402

SAFE_CALLS = {
    "get_time": {},
    "calculate": {"expression": "2*(3+4)"},
    "get_system_info": {},
    "list_directory": {"path": "."},
    "read_file": {"path": "requirements-core.txt"},
    "search_files": {"pattern": "*.py", "path": "nemus"},
    "run_shell": {"command": "echo hello"},
    "run_python": {"code": "print(1+1)"},
}


def main() -> int:
    tools = {t.name: t for t in all_builtin_tools()}
    print("Всего инструментов:", len(tools))
    failures = []
    for name, kwargs in SAFE_CALLS.items():
        tool = tools.get(name)
        if tool is None:
            failures.append(f"{name}: отсутствует")
            print(f"[!] {name} отсутствует")
            continue
        result = tool.execute(**kwargs)
        status = "ok" if result.ok else "FAIL"
        print(f"[{status}] {name}: {result.content[:80].replace(chr(10), ' ')}")
        if not result.ok:
            failures.append(f"{name}: {result.content[:120]}")

    # проверяем схемы
    schemas = [t.schema() for t in all_builtin_tools()]
    assert all("function" in s and "parameters" in s["function"] for s in schemas)
    print("\nСхемы валидны:", len(schemas))

    # run_python: запуск сохранённого скрипта по пути + args, cwd = workspace
    rpt = tools["run_python"]
    ws = Path(rpt.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    script = ws / "hello_nemus.py"
    script.write_text(
        "import sys, os\nprint('привет', ' '.join(sys.argv[1:]))\nprint('CWD=' + os.getcwd())\n",
        encoding="utf-8",
    )
    res = rpt.execute(path="hello_nemus.py", args=["мир"])
    assert res.ok and "привет мир" in res.content, res.content
    assert str(ws.resolve()) in res.content, res.content
    print("[ok] run_python: path+args, cwd=workspace")
    script.unlink()

    # run_python: код без path тоже работает (уникальный временный файл)
    res = rpt.execute(code="print(6*7)")
    assert res.ok and "42" in res.content, res.content
    print("[ok] run_python: code")

    # run_python: нет code и path -> ошибка
    assert not rpt.execute().ok
    print("[ok] run_python: валидация")

    # run_python: длинный скрипт не зависает — уходит в фоновую сессию,
    # а напечатанное до дедлайна сохраняется.
    # Регрессия инцидента: subprocess.run(timeout=...) на Windows после kill
    # блокировался в communicate() навсегда, а вывод при таймауте терялся.
    started = time.monotonic()
    res = rpt.execute(
        code="import time\nprint('до дедлайна', flush=True)\ntime.sleep(60)\n",
        timeout=3,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"зависание run_python: {elapsed:.1f} c"
    assert res.ok and "до дедлайна" in res.content, res.content
    match = re.search(r"session=([0-9a-f]{8})", res.content)
    assert match, res.content
    killed = tools["run_shell"].execute(action="kill", session=match.group(1))
    assert killed.ok, killed.content
    print(f"[ok] run_python: таймаут -> фоновая сессия за {elapsed:.1f} c")

    # run_python: скрипт, читающий stdin, не блокирует вызов на консоли
    res = rpt.execute(code="print(input('Prompt> '))", timeout=3)
    assert res.ok and "Prompt>" in res.content, res.content
    print("[ok] run_python: stdin закрыт — запрос ввода не висит")

    # check_deps: pre-flight — есть/нет, но «нет» не считается сбоем задачи
    deps = tools["check_deps"]
    res = deps.execute(
        commands=[sys.executable, "definitely_missing_cmd_xyz"],
        python_packages=["json", "no_such_pkg_xyz"],
    )
    assert res.ok, res.content
    assert "OK" in res.content and sys.executable in res.content, res.content
    assert "MISSING  definitely_missing_cmd_xyz" in res.content, res.content
    assert "MISSING  no_such_pkg_xyz — pip install no_such_pkg_xyz" in res.content, (
        res.content
    )
    assert "НЕ ХВАТАЕТ: definitely_missing_cmd_xyz, no_such_pkg_xyz" in res.content, (
        res.content
    )
    # версия реально прощупалась (python --version отдаёт номер)
    assert re.search(rf"OK\s+{re.escape(sys.executable)} Python \d", res.content), (
        res.content
    )
    # строка через запятую принимается так же, как массив
    res2 = deps.execute(commands=f"{sys.executable}, no_such_cmd_abc")
    assert res2.ok and "MISSING  no_such_cmd_abc" in res2.content, res2.content
    # пустые аргументы — единственная настоящая ошибка
    assert not deps.execute().ok
    print("[ok] check_deps: pre-flight без сети, нехватка — не сбой задачи")

    if failures:
        print("Провалено:", failures)
        return 1

    # относительные пути считаются от корня приложения, а не от cwd:
    # иначе list_directory('.') показывал бы то корень, то workspace
    lst = tools["list_directory"].execute(path=".")
    assert lst.ok and str(PROJECT_ROOT) in lst.content, lst.content
    rf = tools["read_file"].execute(path="requirements-core.txt")
    assert rf.ok, rf.content
    sf = tools["search_files"].execute(pattern="pyproject.toml", path=".")
    assert sf.ok and str(PROJECT_ROOT) in sf.content, sf.content
    guard = PathGuard(workspace="data/workspace")
    assert guard.workspace == (PROJECT_ROOT / "data" / "workspace").resolve(), (
        guard.workspace
    )
    print("[ok] относительные пути — от корня приложения, а не от cwd")

    print("\nTOOLS SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
