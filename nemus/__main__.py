"""Точка входа: ``python -m nemus`` (GUI), ``--telegram`` или ``--cleanup``."""

from __future__ import annotations

import sys


def main() -> int:
    """Запускает GUI, headless-бота или локальный llama.cpp сервер."""
    if "--llama-server" in sys.argv[1:]:
        # В собранном exe нет отдельного python.exe, поэтому llama.cpp сервер
        # поднимается этим же бинарником: аргументы дальше — уже CLI
        # llama_cpp.server (--model, --port, ...).
        args = [arg for arg in sys.argv[1:] if arg != "--llama-server"]
        sys.argv = [sys.argv[0], *args]
        import runpy

        runpy.run_module("llama_cpp.server", run_name="__main__")
        return 0
    if "--telegram" in sys.argv[1:]:
        argv = [arg for arg in sys.argv[1:] if arg != "--telegram"]
        from nemus.telegram_main import main as telegram_main

        return telegram_main(argv)
    if "--cleanup" in sys.argv[1:]:
        argv = [arg for arg in sys.argv[1:] if arg != "--cleanup"]
        from nemus.cleanup_main import main as cleanup_main

        return cleanup_main(argv)
    from nemus.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
