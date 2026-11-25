"""Регрессионный тест быстрого пути простой реплики при включённом характере.

Заглушки LLM живут в ``bench_agent_latency``; реальных вызовов нет.
Запуск:  venv\\Scripts\\python.exe scripts\\test_agent_latency.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_agent_latency import (  # noqa: E402
    test_simple_request_uses_fast_path_with_character_enabled,
)


def main() -> int:
    test_simple_request_uses_fast_path_with_character_enabled()
    print("ok  простая реплика: final без tool_call, быстрый путь, характер включён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
