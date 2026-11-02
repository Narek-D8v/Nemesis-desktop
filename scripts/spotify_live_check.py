"""Живая проверка Spotify-сценариев (как просит пользователь) + замер времени."""

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

    def run(label: str, **kwargs) -> bool:
        started = time.perf_counter()
        result = tool.execute(**kwargs)
        spent = time.perf_counter() - started
        status = "OK" if result.ok else "FAIL"
        print(f"[{spent:5.2f}s] {label}: {status} | {result.content}")
        return result.ok

    run("playlists", action="playlists")
    run("play_track (мусорный запрос)", action="play_track", query="любая песня алены швец")
    time.sleep(2)
    run("current", action="current")
    run("play_artist", action="play_artist", query="алёна швец")
    time.sleep(2)
    run("current", action="current")
    run("play_playlist (свой)", action="play_playlist", query="Чисто для души")
    time.sleep(2)
    run("current", action="current")
    run("pause", action="pause")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
