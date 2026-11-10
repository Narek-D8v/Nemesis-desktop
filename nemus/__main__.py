"""Точка входа: ``python -m nemus`` (GUI) или ``python -m nemus --telegram``."""

from __future__ import annotations

import sys


def main() -> int:
    """Запускает GUI или headless Telegram-бота по флагу ``--telegram``."""
    if "--telegram" in sys.argv[1:]:
        argv = [arg for arg in sys.argv[1:] if arg != "--telegram"]
        from nemus.telegram_main import main as telegram_main

        return telegram_main(argv)
    from nemus.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
