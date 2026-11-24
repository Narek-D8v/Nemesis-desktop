"""Тесты личного дневника Немуси: файл, инструменты, промпт и запись."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import Config  # noqa: E402
from nemus.core.agent import build_system_prompt  # noqa: E402
from nemus.core.diary import (  # noqa: E402
    DiaryBook,
    compose_diary_entry,
    diary_access,
)
from nemus.core.emotions import EmotionManager, EmotionStore  # noqa: E402
from nemus.core.tools.diary_tools import diary_tools  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


def _book() -> tuple[DiaryBook, Path]:
    path = Path(tempfile.mkdtemp(prefix="nemus_diary_")) / "diary.md"
    return DiaryBook(path), path


def test_diary_add_and_read() -> None:
    book, path = _book()
    first = book.add("Сегодня было тихо, и я сама не поняла, чего хочу.", at=1_700_000_000)
    second = book.add("Он ответил поздно. Я не злюсь, но запомнила.", at=1_700_003_600)
    assert path.exists()
    assert first.stamp.startswith("2023-11-")
    assert len(book.entries()) == 2
    assert book.entries(1)[0].text == second.text
    assert book.search("поздно")[0].text == second.text
    assert "2023-11-" in book.format_for_prompt(2)
    print("ok  дневник: записи с датой, чтение и поиск")


def test_diary_keeps_human_paragraphs() -> None:
    book, _ = _book()
    entry = book.add(
        "Первая мысль.\n\nВторая, с   лишними пробелами."
    )
    assert entry.text == "Первая мысль.\n\nВторая, с лишними пробелами."
    assert book.entries()[-1].text == entry.text
    print("ok  дневник: абзацы сохраняются, запись не склеивается в отчёт")


def test_diary_voice_is_human() -> None:
    class CapturingLLM:
        def __init__(self) -> None:
            self.messages = []

        def complete(self, messages, **kwargs):
            self.messages = messages
            return type("Response", (), {"content": "Ну и день."})()

    cfg = Config()
    cfg.character.diary_file = str(Path(tempfile.mkdtemp()) / "diary.md")
    state = type(
        "State",
        (),
        {
            "mood": -30,
            "energy": 40,
            "trust": 50,
            "hurt": 20,
            "warmth": 55,
            "loneliness": 60,
        },
    )()
    llm = CapturingLLM()
    compose_diary_entry(cfg, llm, "", state)
    system = llm.messages[0].content
    user = llm.messages[1].content
    assert "будто тебя никто не читает" in system
    assert "живым разговорным языком" in system
    assert "«я чувствую»" in system
    assert "не перечисляй эти цифры" in user
    assert "1–5 предложений" in user
    print("ok  дневник: голос живой и неровный, без отчёта и цифр состояния")


def test_missing_diary_is_created_and_private() -> None:
    book, path = _book()
    assert not book.exists
    assert book.ensure(), "отсутствующий дневник должен создаваться"
    assert book.exists and book.entries() == []
    assert not book.ensure(), "существующий дневник не перезаписывается"
    assert "Дневник Немуси" in path.read_text(encoding="utf-8")

    assert diary_access(0, 80, 80) == "share"
    assert diary_access(4, 80, 80) == "share"
    assert diary_access(5, 80, 80) == "refuse"
    assert diary_access(0, 79, 80) == "refuse"
    assert diary_access(0, 80, 79) == "refuse"
    assert diary_access(80, 39, 39) == "offended"
    assert diary_access(0, 39, 80) == "refuse"
    print("ok  дневник: отсутствующий файл создаётся, доступ закрыт по умолчанию")


def test_diary_tools() -> None:
    book, _ = _book()
    tools = {tool.name: tool for tool in diary_tools()}
    assert set(tools) == {"write_diary", "read_diary"}
    for tool in tools.values():
        tool.diary = book
    r = tools["write_diary"].execute(text="Мне сегодня было одиноко.")
    assert r.ok and "сохранена" in r.content
    r = tools["read_diary"].execute()
    assert r.ok and "одиноко" in r.content
    r = tools["read_diary"].execute(query="счастье")
    assert r.ok and "нет" in r.content.lower()
    for tool in tools.values():
        tool.diary = None
    assert not tools["write_diary"].execute(text="тест").ok
    print("ok  дневник: инструменты write_diary/read_diary")


def test_diary_in_prompt() -> None:
    book, path = _book()
    book.add("Мне нравится, когда он просто пишет без повода.")
    cfg = Config()
    cfg.character.diary = True
    cfg.character.diary_file = str(path)
    registry = ToolRegistry()
    registry.register_many(diary_tools())
    for tool in registry:
        if tool.name in {"write_diary", "read_diary"}:
            tool.diary = book
    prompt = build_system_prompt(cfg, registry)
    assert "write_diary" in prompt
    assert "Твой личный дневник" in prompt
    assert "без повода" in prompt
    assert "живо и неровно" in prompt
    assert "литературной полировки" in prompt
    print("ok  дневник: свежие записи попадают в системный промпт")


def test_empty_diary_is_noticed_in_prompt() -> None:
    _, path = _book()
    cfg = Config()
    cfg.character.diary = True
    cfg.character.diary_file = str(path)
    registry = ToolRegistry()
    registry.register_many(diary_tools())
    prompt = build_system_prompt(cfg, registry)
    assert "пока нет записей" in prompt
    assert "write_diary" in prompt
    print("ok  дневник: пустой дневник виден в промпте и его можно начать")


def test_diary_generation_can_stay_silent() -> None:
    class FakeLLM:
        def __init__(self, text: str) -> None:
            self.text = text

        def complete(self, *args, **kwargs):
            return type("Response", (), {"content": self.text})()

    cfg = Config()
    cfg.character.diary_file = str(Path(tempfile.mkdtemp()) / "diary.md")
    state = type(
        "State",
        (),
        {
            "mood": -30,
            "energy": 40,
            "trust": 50,
            "hurt": 20,
            "warmth": 55,
            "loneliness": 60,
        },
    )()
    assert compose_diary_entry(cfg, FakeLLM("ПУСТО"), "", state) == ""
    text = compose_diary_entry(
        cfg, FakeLLM("Сегодня я долго смотрела в окно и думала о нём."), "", state
    )
    assert text.startswith("Сегодня")
    print("ok  дневник: модель может промолчать или написать запись")


def test_manager_writes_diary_on_strong_feeling() -> None:
    class FakeLLM:
        def complete(self, *args, **kwargs):
            return type(
                "Response",
                (),
                {"content": "Сегодня я разозлилась сильнее, чем показала."},
            )()

    path = Path(tempfile.mkdtemp(prefix="nemus_diary_")) / "diary.md"
    cfg = Config()
    cfg.character.diary_file = str(path)
    cfg.character.state_file = str(path.with_name("emotions.json"))
    store = EmotionStore(cfg.character.state_file)
    store.observe_user("Ты дура", cfg.character, now=1000.0)
    book = DiaryBook(path)
    manager = EmotionManager(
        cfg,
        store=store,
        diary=book,
        llm=FakeLLM(),
        history=lambda: "Пользователь: Ты дура",
        interval=3600,
    )
    manager._maybe_write_diary(store.snapshot())
    entries = book.entries()
    assert entries and "разозлилась" in entries[-1].text
    assert entries[-1].text != ""
    print("ok  дневник: сильное чувство рождает личную запись")


def main() -> int:
    test_diary_add_and_read()
    test_diary_keeps_human_paragraphs()
    test_diary_voice_is_human()
    test_missing_diary_is_created_and_private()
    test_diary_tools()
    test_diary_in_prompt()
    test_empty_diary_is_noticed_in_prompt()
    test_diary_generation_can_stay_silent()
    test_manager_writes_diary_on_strong_feeling()
    print("\nDIARY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
