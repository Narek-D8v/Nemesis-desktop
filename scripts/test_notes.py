"""Тесты заметок о пользователе: NoteBook, инструменты, промпт, фабрика."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import Config, get_config  # noqa: E402
from nemus.core.agent import build_system_prompt  # noqa: E402
from nemus.core.notes import NoteBook  # noqa: E402
from nemus.core.tools.notes_tools import notes_tools  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


def _tmp_book() -> tuple[NoteBook, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="nemus_notes_")) / "notes.md"
    return NoteBook(tmp), tmp


def test_notebook_add_and_read() -> None:
    book, path = _tmp_book()
    assert book.notes() == [], "пустой файл — заметок нет"
    line = book.add("Пользователь любит кофе без сахара")
    assert line.startswith("- ") and "кофе" in line
    assert path.exists(), "файл создан при первой заметке"
    assert "# Заметки о пользователе" in path.read_text(encoding="utf-8")
    book.add("Работает над проектом Nemesis")
    assert len(book.notes()) == 2
    assert len(book.recent(1)) == 1
    assert book.recent(0) == book.notes(), "limit <= 0 — все заметки"
    try:
        book.add("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("пустая заметка должна отклоняться")


def test_notebook_search() -> None:
    book, _ = _tmp_book()
    book.add("Любит кофе без сахара")
    book.add("Кошку зовут Барсик")
    assert len(book.search("КОФЕ")) == 1, "поиск без учёта регистра"
    assert book.search("гаити") == []
    assert book.search("") == book.notes(), "пустый запрос — всё"
    assert "кофе" in book.format_for_prompt()


def test_note_tools() -> None:
    book, path = _tmp_book()
    tools = {t.name: t for t in notes_tools()}
    assert set(tools) == {"write_note", "read_notes"}
    assert tools["write_note"].notebook is None, "до инъекции — пусто"

    for tool in tools.values():
        tool.notebook = book

    r = tools["write_note"].execute(text="Имя — Иван, живёт в Москве")
    assert r.ok, r.content
    assert path.exists()

    r = tools["write_note"].execute(text="   ")
    assert not r.ok, "пустая заметка отклоняется"

    r = tools["read_notes"].execute()
    assert r.ok and "Иван" in r.content

    r = tools["read_notes"].execute(query="москв")
    assert r.ok and "Иван" in r.content
    r = tools["read_notes"].execute(query="жёлудь")
    assert r.ok and "нет" in r.content.lower()

    for tool in tools.values():
        tool.notebook = None
    assert not tools["write_note"].execute(text="тест").ok, "без NoteBook — отказ"


def test_prompt_includes_notes() -> None:
    cfg = get_config(reload=True)
    tmp = Path(tempfile.mkdtemp(prefix="nemus_notes_")) / "notes.md"
    cfg.memory.notes = True
    cfg.memory.notes_file = str(tmp)

    registry = ToolRegistry()
    registry.register_many(notes_tools())

    prompt = build_system_prompt(cfg, registry)
    assert "write_note" in prompt, "правило про заметки отсутствует"
    assert "заметк" in prompt.lower()
    assert "Заметок пока нет" not in prompt and "Твои заметки" not in prompt, (
        "пустые заметки не должны давать блок"
    )

    NoteBook(tmp).add("Имя — Иван, живёт в Москве")
    prompt = build_system_prompt(cfg, registry)
    assert "Твои заметки о пользователе" in prompt
    assert "Иван" in prompt

    # Инструментов нет в реестре — правила и блок не добавляются.
    prompt = build_system_prompt(cfg, ToolRegistry())
    assert "Твои заметки о пользователе" not in prompt
    assert "write_note" not in prompt


def test_prompt_without_notes_config() -> None:
    cfg = Config()
    cfg.memory.notes = False
    registry = ToolRegistry()
    registry.register_many(notes_tools())
    prompt = build_system_prompt(cfg, registry)
    assert "Твои заметки" not in prompt


def test_factory_wiring() -> None:
    from nemus.core.factory import _apply_notes, _inject_notes

    cfg = Config()
    cfg.memory.notes = False
    registry = ToolRegistry()
    _apply_notes(registry, cfg)
    assert "write_note" not in registry and "read_notes" not in registry
    assert _inject_notes(registry, cfg) is None

    cfg.memory.notes = True
    cfg.memory.notes_file = str(
        Path(tempfile.mkdtemp(prefix="nemus_notes_")) / "notes.md"
    )
    _apply_notes(registry, cfg)
    assert "write_note" in registry and "read_notes" in registry
    _apply_notes(registry, cfg)
    assert list(registry.names).count("write_note") == 1, "повторная — не дублирует"

    notebook = _inject_notes(registry, cfg)
    assert notebook is not None
    r = registry.execute("write_note", {"text": "Проверка инъекции"})
    assert r.ok, r.content
    r = registry.execute("read_notes", {})
    assert r.ok and "инъекции" in r.content


def main() -> int:
    tests = [
        test_notebook_add_and_read,
        test_notebook_search,
        test_note_tools,
        test_prompt_includes_notes,
        test_prompt_without_notes_config,
        test_factory_wiring,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print("ВСЕ ТЕСТЫ ЗАМЕТОК ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
