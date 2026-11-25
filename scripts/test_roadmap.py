"""Локальные тесты инструментов дорожной карты.

Запуск:  D:\\Nemesis\\venv\\Scripts\\python.exe scripts\\test_roadmap.py
Сеть, реальные установки и LLM не используются: всё на temp-каталогах и фейках.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nemus.core.tools.roadmap import (  # noqa: E402
    ArticleSessionTool,
    BackgroundWatchTool,
    BackupWorkspaceTool,
    BatchMediaTool,
    CalendarTool,
    CatchupSummaryTool,
    ChartTool,
    ClipboardHistoryTool,
    FatigueTool,
    InspectScreenTool,
    MemeTool,
    MorningDashboardTool,
    MusicMoodTool,
    PatchFileTool,
    PollTool,
    ProjectCleanupTool,
    RepoGuideTool,
    RoadmapStore,
    SearchHistoryTool,
    SoftMemoryTool,
    TutorialTool,
    WeatherTool,
    roadmap_tools,
)


def _data(result: Any) -> Any:
    assert result.ok, result.content
    return json.loads(result.content)


def test_store() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_store_") as raw:
        store = RoadmapStore(raw)
        assert store.load("missing", {"a": 1}) == {"a": 1}
        store.save("thing", {"x": [1, 2]})
        assert store.load("thing", {}) == {"x": [1, 2]}
        assert (Path(raw) / "roadmap" / "thing.json").is_file()
    print("ok  RoadmapStore: атомарная запись и чтение")


def test_inspect_screen() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_screen_") as raw:

        def fake_windows() -> list[dict[str, Any]]:
            return [
                {"title": "VSCode — Nemesis", "pid": 11},
                {"title": "Steam: Dota 2", "pid": 22},
                {"title": "Docs and Mail", "pid": 33},
                {"title": "Неизвестное окно", "pid": 44},
            ]

        payload = _data(InspectScreenTool(raw, windows=fake_windows).execute(limit=3))
        assert payload["count"] == 3
        assert [item["kind"] for item in payload["items"]] == ["code", "game", "work"]
        assert payload["items"][0]["pid"] == 11
    print("ok  inspect_screen: классификация окон без скриншотов")


def test_clipboard_history() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_clipboard_") as raw:
        texts = ["первый текст", "первый текст", "погода в городе"]
        calls: list[int] = []

        def getter() -> str:
            calls.append(1)
            return texts[min(len(calls) - 1, len(texts) - 1)]

        tool = ClipboardHistoryTool(raw, getter=getter)
        assert _data(tool.execute(action="read"))["added"] is True
        assert _data(tool.execute(action="read"))["added"] is False
        assert _data(tool.execute(action="read"))["added"] is True

        all_entries = _data(tool.execute(action="history"))
        assert all_entries["count"] == 2, all_entries
        found = _data(tool.execute(action="history", query="погода"))
        assert found["count"] == 1, found
        assert "погода" in found["items"][0]["text"]
        cleared = _data(tool.execute(action="clear"))
        assert cleared["removed"] == 2
        assert _data(tool.execute(action="history"))["count"] == 0
    print("ok  clipboard_history: read, dedupe, search, clear")


def test_project_cleanup() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_cleanup_") as raw:
        root = Path(raw) / "work"
        old_log = root / "old.log"
        fresh_log = root / "fresh.log"
        cache = root / "__pycache__"
        cache.mkdir(parents=True)
        old_log.write_text("x" * 100)
        fresh_log.write_text("y" * 100)
        (cache / "mod.pyc").write_text("z" * 50)
        stamp = time.time() - 10 * 86400
        for path in (old_log, cache, cache / "mod.pyc"):
            os.utime(path, (stamp, stamp))

        tool = ProjectCleanupTool(base_dir=raw, allowed_roots=[root])
        preview = _data(tool.execute(root=str(root), preview=True, days=7))
        assert preview["preview"] is True
        assert preview["count"] >= 2, preview
        assert old_log.exists() and cache.exists()

        assert not tool.execute(root=str(root), preview=False, days=7).ok
        assert not tool.execute(root=str(raw), preview=True, days=7).ok

        applied = _data(tool.execute(root=str(root), preview=False, confirm=True, days=7))
        assert applied["removed"] >= 2, applied
        assert not old_log.exists()
        assert not cache.exists()
        assert fresh_log.exists()
        assert applied["freed_bytes"] > 0
    print("ok  project_cleanup: preview не удаляет, confirm удаляет старое")


def test_backup_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_backup_") as raw:
        source = Path(raw) / "config"
        source.mkdir()
        (source / "settings.yaml").write_text("key: value", encoding="utf-8")
        backups = Path(raw) / "backups"
        backups.mkdir()
        stale = backups / "stale.zip"
        stale.write_bytes(b"old")
        old_stamp = time.time() - 86400
        os.utime(stale, (old_stamp, old_stamp))

        result = _data(BackupWorkspaceTool(raw).execute(paths=[str(source)], label="test", keep=1))
        archive = Path(result["archive"])
        assert archive.is_file() and result["size"] > 0
        with zipfile.ZipFile(archive) as packed:
            assert "config/settings.yaml" in packed.namelist()
        assert not stale.exists()
    print("ok  backup_workspace: zip с относительными путями и ротацией")


def test_catchup_summary() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_catchup_") as raw:
        messages = [
            {"role": "user", "content": "Привет, расскажи про погоду"},
            {"role": "assistant", "content": "решено сделать обзор\nКакая погода завтра?"},
        ]
        result = CatchupSummaryTool(raw).execute(messages=messages)
        parts = result.content.split("\n\n")
        assert len(parts) == 3, parts
        assert "Обсуждалось" in parts[0]
        assert "погод" in parts[0].lower()
        assert "решено" in parts[1].lower()
        assert "?" in parts[2]
    print("ok  catchup_summary: ровно три абзаца")


def test_search_history() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_search_") as raw:

        class FakeMemory:
            def recent_messages(self, limit: int = 0) -> list[dict[str, str]]:
                return [{"role": "user", "content": "Погода в Москве, ёлка"}]

        class FakeNotes:
            def notes(self) -> list[str]:
                return ["планы на погоду"]

        class FakeDiary:
            def recent(self) -> list[str]:
                return ["дневник: погода"]

        tool = SearchHistoryTool(raw, memory=FakeMemory(), notes=FakeNotes(), diary=FakeDiary())
        rows = _data(tool.execute(query="погод ёлка", limit=10))
        assert len(rows) == 3, rows
        assert {row["source"] for row in rows} == {"memory", "notes", "diary"}
        assert all(row["score"] > 0 for row in rows)
        assert rows[0]["source"] == "memory"
        assert not tool.execute(query="").ok
    print("ok  search_history: токены, подстрока и ё→е")


def test_calendar() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_calendar_") as raw:
        tool = CalendarTool(raw)
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        event_id = _data(tool.execute(action="add", title="Встреча", start=start))["id"]
        upcoming = _data(tool.execute(action="upcoming", days=1))
        assert any(item["id"] == event_id for item in upcoming["items"])
        assert _data(tool.execute(action="list"))["count"] == 1
        cancelled = _data(tool.execute(action="cancel", id=event_id))
        assert cancelled["removed"] == 1
        assert _data(tool.execute(action="list"))["count"] == 0
        assert not tool.execute(action="add", title="x", start="not-a-date").ok
    print("ok  calendar: add/upcoming/cancel и проверка ISO")


def test_weather() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_weather_") as raw:
        failure = WeatherTool(raw).execute(location="home")
        assert not failure.ok
        assert "Погода не настроена" in failure.content

        def fake_provider(location: str) -> dict[str, Any]:
            return {"temp": 21, "location": location}

        payload = _data(WeatherTool(raw, provider=fake_provider).execute(location="Москва"))
        assert payload == {"temp": 21, "location": "Москва"}

        def two_arg_provider(base_dir: Path, location: str) -> str:
            assert base_dir == Path(raw)
            return f"ясно в {location}"

        text = WeatherTool(raw, provider=two_arg_provider).execute(location="Дом")
        assert text.ok and "ясно в Дом" in text.content
    print("ok  weather: отказ без provider и работа с фейком")


def test_repo_guide() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_repo_") as raw:
        root = Path(raw) / "repo"
        (root / "nemus").mkdir(parents=True)
        (root / "README.md").write_text("# проект", encoding="utf-8")
        (root / "nemus" / "app.py").write_text(
            "class App:\n    pass\n\ndef run():\n    pass\n", encoding="utf-8"
        )
        payload = _data(RepoGuideTool(raw).execute(root=str(root)))
        assert "README.md" in payload["entrypoints"], payload
        assert "App" in payload["symbols"] and "run" in payload["symbols"]
        assert "nemus" in payload["dirs"]
    print("ok  repo_guide: README, точки входа и символы")


def test_chart() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_chart_") as raw:
        result = ChartTool(raw).execute(
            labels=["январь", "февраль"],
            values=["10%", "1,5"],
            title="Тест",
        )
        assert result.ok, result.content
        image = Path(result.data["path"])
        assert image.is_file() and image.stat().st_size > 0
        assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    print("ok  chart: PNG создаётся, проценты и запятые парсятся")


def test_soft_memory() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_soft_") as raw:
        tool = SoftMemoryTool(raw)
        low = _data(tool.execute(action="remember", text="погода низкая", importance=1))["entry"]
        high = _data(
            tool.execute(
                action="remember",
                text="погода важная",
                importance=5,
                tags=["work"],
            )
        )["entry"]
        recalled = _data(tool.execute(action="recall", query="погода"))
        assert [item["id"] for item in recalled["items"]] == [high["id"], low["id"]], recalled
        tagged = _data(tool.execute(action="recall", tag="work"))
        assert [item["id"] for item in tagged["items"]] == [high["id"]]
        forgotten = _data(tool.execute(action="forget", id=high["id"]))
        assert forgotten["removed"] == 1
    print("ok  soft_memory: важность, теги и forget")


def test_morning_dashboard() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_dash_") as raw:
        tool = MorningDashboardTool(
            raw,
            todo_provider=lambda: ["задача один"],
            weather_provider=lambda: "+20 градусов",
        )
        text = tool.execute().content
        for heading in ("## Задачи", "## Погода", "## Планы", "## Новости"):
            assert heading in text
        assert "задача один" in text and "+20 градусов" in text
        assert "## Планы\nне подключено" in text
        assert "## Новости\nне подключено" in text
    print("ok  morning_dashboard: четыре секции и заглушки")


def test_poll() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_poll_") as raw:
        tool = PollTool(raw)
        poll_id = _data(tool.execute(action="create", question="Куда идём?", options=["A", "B"]))[
            "poll_id"
        ]
        assert (
            _data(tool.execute(action="vote", poll_id=poll_id, choice=0, voter="ann"))["voter"]
            == "ann"
        )
        assert not tool.execute(action="vote", poll_id=poll_id, choice=1, voter="ann").ok
        results = _data(tool.execute(action="results", poll_id=poll_id))
        assert results["counts"]["A"] == 1 and results["total"] == 1
        assert _data(tool.execute(action="close", poll_id=poll_id))["closed"]
        assert not tool.execute(action="vote", poll_id=poll_id, choice=1, voter="bob").ok
    print("ok  poll: create/vote/results/close и защита от повтора")


def test_article_session() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_article_") as raw:
        tool = ArticleSessionTool(raw)
        _data(tool.execute(action="open", url="http://one", summary="S1"))
        noted = _data(tool.execute(action="note", highlight="важное место"))
        assert len(noted["session"]["highlights"]) == 1
        reopened = _data(tool.execute(action="open", url="http://two", summary="S2"))
        assert reopened["session"]["highlights"] == []
        current = _data(tool.execute(action="summary"))
        assert current["session"]["url"] == "http://two"
        assert _data(tool.execute(action="close"))["closed"]
    print("ok  article_session: open/note/summary/close")


def test_fatigue() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_fatigue_") as raw:
        tool = FatigueTool(raw)
        tired = _data(tool.execute(minutes=200, breaks=1, energy=0.2))
        assert "пора сделать чай и отдохнуть" in tired["recommendation"]
        assert tired["risk"] > 0
        fresh = _data(tool.execute(minutes=30, breaks=3, energy=0.9))
        assert "чай" not in fresh["recommendation"]
    print("ok  fatigue: чай при долгой работе без перерывов")


def test_music_mood() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_music_") as raw:
        local = _data(MusicMoodTool(raw).execute(mood="focus"))
        assert local["queries"] and any("coding" in q for q in local["queries"])

        def fake_provider(mood: str) -> dict[str, Any]:
            return {"tracks": [f"track for {mood}"], "queries": []}

        queued = _data(
            MusicMoodTool(raw, provider=fake_provider).execute(mood="happy", action="queue")
        )
        assert queued["tracks"] == ["track for happy"] and queued["queued"]
    print("ok  music_mood: локальная карта и фейковый provider")


def test_background_watch() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_watch_") as raw:
        watched = Path(raw) / "watched.txt"
        watched.write_text("one", encoding="utf-8")
        tool = BackgroundWatchTool(raw)
        _data(tool.execute(action="add", name="file", kind="path", target=str(watched)))
        first = _data(tool.execute(action="check"))
        assert first["changed"] == [], first
        time.sleep(0.01)
        watched.write_text("much longer text", encoding="utf-8")
        second = _data(tool.execute(action="check"))
        assert "file" in second["changed"], second
        assert _data(tool.execute(action="remove", name="file"))["removed"]
    print("ok  background_watch: фиксируется изменение файла")


def test_tutorial() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_tutorial_") as raw:
        tool = TutorialTool(raw)
        started = _data(tool.execute(action="start", topic="T", steps=["s1", "s2", "s3"]))
        assert started["step"] == "s1" and started["progress"] == 0
        assert _data(tool.execute(action="next"))["step"] == "s2"
        assert _data(tool.execute(action="back"))["step"] == "s1"
        current = _data(tool.execute(action="step", answer="гда"))
        assert current["step"] == "s1" and current["progress"] == 0
        assert _data(tool.execute(action="close"))["closed"]
    print("ok  tutorial: start/next/back/step/close")


def test_batch_media() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_media_") as raw:
        image_path = Path(raw) / "photo.png"
        Image.new("RGB", (40, 20), (10, 20, 30)).save(image_path)
        bad_path = Path(raw) / "notes.txt"
        bad_path.write_text("не картинка", encoding="utf-8")

        outputs = _data(
            BatchMediaTool(raw).execute(
                paths=[str(image_path), str(bad_path)],
                operation="resize",
                width=20,
            )
        )
        assert outputs[0]["ok"], outputs
        with Image.open(outputs[0]["output"]) as resized:
            assert resized.size == (20, 10)
        assert not outputs[1]["ok"] and outputs[1]["reason"]
    print("ok  batch_media: ресайз и пропуск не-изображений")


def test_meme() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_meme_") as raw:
        result = MemeTool(raw).execute(top_text="Привет мир", bottom_text="Конец")
        assert result.ok, result.content
        path = Path(result.data["path"])
        assert path.is_file()
        with Image.open(path) as image:
            assert image.size == (800, 800)
    print("ok  meme: PNG на сером холсте")


def test_patch_file() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_patch_") as raw:
        target = Path(raw) / "file.txt"
        target.write_text("old value", encoding="utf-8")
        tool = PatchFileTool(raw, allowed_roots=[raw])
        assert not tool.execute(path=str(target), old="old", new="new").ok
        assert not tool.execute(path=str(target), old="missing", new="new", confirm=True).ok
        payload = _data(tool.execute(path=str(target), old="old", new="new", confirm=True))
        assert target.read_text(encoding="utf-8") == "new value"
        assert Path(payload["backup"]).read_text(encoding="utf-8") == "old value"
        assert not tool.execute(path=str(ROOT / "README.md"), old="x", new="y", confirm=True).ok
    print("ok  patch_file: confirm, уникальность и .bak")


def test_factory() -> None:
    with tempfile.TemporaryDirectory(prefix="roadmap_factory_") as raw:
        tools = roadmap_tools(raw, allowed_roots=[raw])
        names = [tool.name for tool in tools]
        assert len(tools) == 21 and len(set(names)) == 21, names
        assert all(tool.schema()["function"]["name"] == tool.name for tool in tools)
        assert not WeatherTool(raw).execute().ok
    print("ok  roadmap_tools: фабрика собирает все 21 инструмент")


def main() -> None:
    test_store()
    test_inspect_screen()
    test_clipboard_history()
    test_project_cleanup()
    test_backup_workspace()
    test_catchup_summary()
    test_search_history()
    test_calendar()
    test_weather()
    test_repo_guide()
    test_chart()
    test_soft_memory()
    test_morning_dashboard()
    test_poll()
    test_article_session()
    test_fatigue()
    test_music_mood()
    test_background_watch()
    test_tutorial()
    test_batch_media()
    test_meme()
    test_patch_file()
    test_factory()
    print("\nROADMAP TESTS PASSED")


if __name__ == "__main__":
    main()
