"""Тест единой шины прогресса загрузок и адаптера моделей (без сети).

Запуск:  .\\venv\\Scripts\\python.exe scripts\\test_download_events.py
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.download_events import (  # noqa: E402
    DownloadEvent,
    new_download_id,
    publish_download,
    render_progress_bar,
    subscribe_download,
    unsubscribe_download,
)
from nemus.core.providers import hf_download  # noqa: E402


def test_download_event_bus_and_bar() -> None:
    event = DownloadEvent(
        kind="file", phase="progress", name="setup.exe",
        done=84, total=125, task_id="t1",
    )
    assert event.percent == 67
    assert render_progress_bar(67) == "█████████████░░░░░░░ 67%"
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    try:
        publish_download(event)
    finally:
        unsubscribe_download(token)
    assert seen == [event]
    print("ok  event bus and 20-char progress bar")


def test_percent_none_and_unknown_bar() -> None:
    event = DownloadEvent(kind="file", phase="start", name="setup.exe", total=0)
    assert event.percent is None
    assert render_progress_bar(None) == "░" * 20
    assert render_progress_bar(None, width=10) == "░" * 10
    assert render_progress_bar(100) == "█" * 20 + " 100%"
    print("ok  percent is None when total <= 0")


def test_bad_listener_does_not_stop_publish() -> None:
    seen: list[DownloadEvent] = []

    def bad_listener(_event: DownloadEvent) -> None:
        raise RuntimeError("broken UI listener")

    event = DownloadEvent(kind="torrent", phase="done", name="film.iso")
    bad_token = subscribe_download(bad_listener)
    good_token = subscribe_download(seen.append)
    try:
        publish_download(event)
    finally:
        unsubscribe_download(bad_token)
        unsubscribe_download(good_token)
    assert seen == [event]
    print("ok  listener exceptions are isolated")


def test_new_download_id_is_unique() -> None:
    ids = {new_download_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(isinstance(item, str) and item for item in ids)
    print("ok  new_download_id")


def test_concurrent_publish_reaches_listeners() -> None:
    seen: list[DownloadEvent] = []
    seen_lock = threading.Lock()

    def listener(event: DownloadEvent) -> None:
        with seen_lock:
            seen.append(event)

    token = subscribe_download(listener)
    threads = [
        threading.Thread(target=publish_download, args=(
            DownloadEvent(kind="file", phase="progress", name=f"part-{i}",
                          done=i, total=10),
        ))
        for i in range(10)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
    finally:
        unsubscribe_download(token)
    assert len(seen) == 10
    assert {event.name for event in seen} == {f"part-{i}" for i in range(10)}
    print("ok  concurrent publish")


def test_model_download_publishes_unified_event() -> None:
    legacy_seen: list[hf_download.ModelDownloadEvent] = []
    unified_seen: list[DownloadEvent] = []

    def legacy_listener(event: hf_download.ModelDownloadEvent) -> None:
        legacy_seen.append(event)

    hf_download.subscribe_model_download(legacy_listener)
    token = subscribe_download(unified_seen.append)
    model_event = hf_download.ModelDownloadEvent(
        phase="progress", repo="owner", filename="model.gguf",
        path="data/models/model.gguf", done=84, total=125, message="скачано",
    )
    try:
        hf_download._publish(model_event)
    finally:
        unsubscribe_download(token)
        hf_download.unsubscribe_model_download(legacy_listener)

    assert legacy_seen == [model_event], "старый канал должен работать как прежде"
    assert len(unified_seen) == 1, unified_seen
    event = unified_seen[0]
    assert event.kind == "model"
    assert event.phase == model_event.phase
    assert event.name == model_event.name
    assert event.done == model_event.done
    assert event.total == model_event.total
    assert event.path == model_event.path
    assert event.message == model_event.message
    assert event.percent == 67
    print("ok  model download adapter is additive")


def test_model_progress_without_legacy_listener_is_single() -> None:
    """Без legacy-подписчика одна загрузка модели даёт одно событие в шине."""
    unified: list[DownloadEvent] = []
    token = subscribe_download(unified.append)
    assert not hf_download._LISTENERS, "тест не должен наследовать чужих слушателей"
    try:
        hf_download._publish(
            hf_download.ModelDownloadEvent(
                phase="progress", repo="owner", filename="model.gguf",
                done=1, total=2,
            )
        )
    finally:
        unsubscribe_download(token)
    assert len(unified) == 1, unified
    print("ok  model progress delivered once on the unified bus")


def main() -> int:
    test_download_event_bus_and_bar()
    test_percent_none_and_unknown_bar()
    test_bad_listener_does_not_stop_publish()
    test_new_download_id_is_unique()
    test_concurrent_publish_reaches_listeners()
    test_model_download_publishes_unified_event()
    test_model_progress_without_legacy_listener_is_single()
    print("\nDOWNLOAD EVENT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
