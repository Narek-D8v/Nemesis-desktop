"""Smoke-тест всех встроенных инструментов (безопасные, без сети)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.tools.all import all_builtin_tools  # noqa: E402

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

    if failures:
        print("Провалено:", failures)
        return 1
    print("\nTOOLS SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
