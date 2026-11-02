"""Замер шагов SpotifyTool: поиск, снимок очереди, старт, проверка."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def timed(label: str, func, *args, **kwargs):
    started = time.perf_counter()
    value = func(*args, **kwargs)
    print(f"  [{time.perf_counter() - started:5.2f}s] {label}")
    return value


def main() -> int:
    from nemus.config import load_config
    from nemus.core.tools.spotify import SpotifyTool

    tool = SpotifyTool()
    tool.settings = load_config().spotify
    sp = timed("_get_client", tool._get_client)
    device = timed("_active_device_id", tool._active_device_id, sp)

    track = timed("search track", tool._search_first, sp, "любая песня алены швец", "track")
    print("  трек:", track.get("name"), (track.get("album") or {}).get("uri"))
    snapshot = timed("_snapshot_queue", tool._snapshot_queue, sp)
    print("  очередь:", len(snapshot))

    timed(
        "start uris=[track] (ожидаем тихий отказ Free)",
        sp.start_playback,
        device_id=device,
        uris=[track["uri"]],
    )
    print("  wait_playing:", timed("_wait_playing", tool._wait_playing, sp))

    timed(
        "start album+offset",
        sp.start_playback,
        device_id=device,
        context_uri=track["album"]["uri"],
        offset={"uri": track["uri"]},
    )
    print("  wait_playing:", timed("_wait_playing", tool._wait_playing, sp))
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
