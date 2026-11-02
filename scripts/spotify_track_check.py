"""Проверка: запуск трека на Free-аккаунте и стабильность воспроизведения."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from nemus.config import load_config
    from nemus.core.tools.spotify import SpotifyTool

    tool = SpotifyTool()
    tool.settings = load_config().spotify

    result = tool.execute(action="play_track", query="любая песня алены швец")
    print("play_track:", "OK" if result.ok else "FAIL", "|", result.content)
    for i in range(6):
        time.sleep(1.5)
        pb = tool.execute(action="current").content
        print(f"  +{(i + 1) * 1.5:.1f}s:", pb)
    print("pause:", tool.execute(action="pause").content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
