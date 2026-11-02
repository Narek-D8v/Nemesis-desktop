"""Тесты скиллов Claude Agent Skills: SKILL.md, промпт, инструменты, GUI-вкладка.

Запуск:  venv\\Scripts\\python.exe scripts\\test_skills.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import Config, get_config  # noqa: E402
from nemus.core.agent import build_system_prompt  # noqa: E402
from nemus.core.factory import _apply_skills, _inject_settings, build_core  # noqa: E402
from nemus.core.skills import (  # noqa: E402
    load_skills,
    parse_skill,
    save_skill,
    skills_root,
    slugify,
)
from nemus.core.tools.registry import ToolRegistry  # noqa: E402
from nemus.core.tools.skills_tool import skill_tools  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="nemus_skills_"))


def _cfg_for(path: Path) -> Config:
    cfg = Config()
    cfg.skills.dir = str(path)
    cfg.mcp.enabled = False
    return cfg


def test_slugify() -> None:
    assert slugify("pdf-tools") == "pdf-tools"
    assert slugify("PDF инструменты") == "pdf-instrumenty"
    assert slugify("  Привет,   мир!  ") == "privet-mir"
    assert slugify("2024 отчёт") == "2024-otchet"
    for bad in ("", "!!!", "---"):
        try:
            slugify(bad)
            raise AssertionError(f"ожидалась ошибка для {bad!r}")
        except ValueError:
            pass
    print("ok  slugify: латиница, кириллица, мусор, ошибки")


def test_parse_skill() -> None:
    meta, body = parse_skill("---\nname: pdf\ndescription: Работа с PDF\n---\n\nШаг 1.\n")
    assert meta["name"] == "pdf" and meta["description"] == "Работа с PDF"
    assert body == "Шаг 1."

    # без frontmatter — ошибка, а не тихий пропуск
    for bad in ("обычный текст", "---\nname: [сломано\n---\nтекст"):
        try:
            parse_skill(bad)
            raise AssertionError(f"ожидалась ошибка для {bad!r}")
        except ValueError:
            pass

    # описание во frontmatter необязательно — берётся первая строка
    meta, body = parse_skill("---\nname: x\n---\nПервая строка — и есть описание.\n")
    assert "description" not in meta and body.startswith("Первая")
    print("ok  parse_skill: frontmatter / ошибки / фолбэк описания")


def test_save_and_load(tmp: Path) -> None:
    skill = save_skill(
        "PDF инструменты", "Склейка и разбиение PDF", "1. Открыть файл.\n2. Сохранить.", tmp
    )
    assert skill.name == "pdf-instrumenty"
    assert skill.skill_file.read_text(encoding="utf-8").startswith("---\n")

    # вспомогательные файлы рядом со SKILL.md попадают в список
    (skill.path / "helper.py").write_text("print('x')\n", encoding="utf-8")
    save_skill("второй скилл", "Описание второго", "Тело.", tmp)

    found = load_skills(tmp)
    assert [s.name for s in found] == ["pdf-instrumenty", "vtoroy-skill"], found

    # отключённый скилл не виден модели (по машинному имени скилла/папки)
    only = load_skills(tmp, disabled=["PDF-Instrumenty"])
    assert [s.name for s in only] == ["vtoroy-skill"], only

    loaded = load_skills(tmp)[0]
    assert loaded.description == "Склейка и разбиение PDF"
    assert loaded.files == ["helper.py"], loaded.files
    assert "Шаг 1" in loaded.body or "1. Открыть" in loaded.body

    # пустая папка / несуществующая — без исключений
    empty = tmp / "empty_dir"
    empty.mkdir()
    assert load_skills(empty) == []
    assert load_skills(tmp / "nope") == []

    # валидация записи
    for args in (("", "опис", "тело"), ("name", "", "тело"), ("name", "опис", "  ")):
        try:
            save_skill(*args, tmp)
            raise AssertionError(f"ожидалась ошибка для {args!r}")
        except ValueError:
            pass
    print("ok  save/load: запись, файлы, отключение, валидация")


def test_prompt_block(tmp: Path) -> None:
    save_skill("pdf-tools", "Склейка PDF-файлов", "Инструкция.", tmp)
    cfg = _cfg_for(tmp)
    prompt = build_system_prompt(cfg, ToolRegistry())
    assert "Скиллы (формат Claude Agent Skills" in prompt, prompt
    assert "- pdf-tools: Склейка PDF-файлов" in prompt, prompt
    assert "load_skill" in prompt

    # отключённый скилл из промпта уходит
    cfg.skills.disabled = ["pdf-tools"]
    assert "pdf-tools" not in build_system_prompt(cfg, ToolRegistry())

    # скиллы выключены целиком — блока нет
    cfg.skills.disabled = []
    cfg.skills.enabled = False
    assert "Скиллы (формат" not in build_system_prompt(cfg, ToolRegistry())

    # пустая папка — блока нет, а не пустая заглушка
    cfg.skills.enabled = True
    cfg.skills.dir = str(tmp / "nope")
    assert "Скиллы (формат" not in build_system_prompt(cfg, ToolRegistry())
    print("ok  промпт: блок скиллов / отключение / пусто")


def test_tools(tmp: Path) -> None:
    save_skill("pdf-tools", "Склейка PDF", "Шаг 1: сделать.\nШаг 2: проверить.", tmp)
    cfg = _cfg_for(tmp)
    registry = ToolRegistry()
    registry.register_many(skill_tools())
    _inject_settings(registry, cfg)

    res = registry.execute("load_skill", {"name": "PDF-Tools"})
    assert res.ok and "Шаг 1: сделать." in res.content, res.content
    assert "Скилл «pdf-tools»" in res.content

    res = registry.execute("load_skill", {"name": "нет-такого"})
    assert not res.ok and "не найден" in res.content, res.content

    res = registry.execute(
        "create_skill",
        {"name": "новый навык", "description": "Тестовый скилл", "content": "Делать X."},
    )
    assert res.ok, res.content
    created = skills_root(cfg.skills.dir) / "novyy-navyk" / "SKILL.md"
    assert created.exists(), created
    assert "description: Тестовый скилл" in created.read_text(encoding="utf-8")

    # обязательные поля
    res = registry.execute(
        "create_skill", {"name": "x", "description": "", "content": "тело"}
    )
    assert not res.ok, res.content

    # выключенные скиллы не подаются модели
    _inject_settings(registry, cfg)
    cfg.skills.disabled = ["pdf-tools"]
    for tool in registry:
        if hasattr(tool, "disabled"):
            tool.disabled = list(cfg.skills.disabled)
    assert not registry.execute("load_skill", {"name": "pdf-tools"}).ok
    print("ok  инструменты: load_skill / create_skill / валидация / disabled")


def test_factory(tmp: Path) -> None:
    save_skill("pdf-tools", "Склейка PDF", "Инструкция.", tmp)
    cfg = _cfg_for(tmp)
    core = build_core(cfg)

    assert "load_skill" in core.registry.names, core.registry.names
    assert "create_skill" in core.registry.names
    tool = core.registry.get("load_skill")
    assert getattr(tool, "skills_dir", None) == cfg.skills.dir
    res = core.registry.execute("load_skill", {"name": "pdf-tools"})
    assert res.ok and "Инструкция." in res.content, res.content

    prompt = build_system_prompt(cfg, core.registry)
    assert "- pdf-tools:" in prompt

    # skills.enabled: false снимает инструменты
    cfg.skills.enabled = False
    registry = ToolRegistry()
    registry.register_many(skill_tools())
    _apply_skills(registry, cfg)
    assert "load_skill" not in registry and "create_skill" not in registry
    print("ok  фабрика: регистрация, инъекция папки, отключение")


def test_gui_tab(tmp: Path) -> None:
    save_skill("pdf-tools", "Склейка PDF", "Инструкция.", tmp)
    from PySide6.QtWidgets import QApplication

    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])
    _ = app
    dlg = SettingsDialog(core=None)
    try:
        assert hasattr(dlg, "chk_skills_enabled"), "вкладка скиллов не построена"
        assert dlg.chk_skills_enabled.isChecked() == dlg.config.skills.enabled

        # вкладка читает папку из конфига — подставляем тестовую и обновляем
        dlg.config.skills.dir = str(tmp)
        dlg.config.skills.disabled = ["vtoroy-skill"]
        dlg._skills_refresh()
        assert set(dlg._skill_checks) == {
            "novyy-navyk",
            "pdf-instrumenty",
            "pdf-tools",
            "vtoroy-skill",
        }, list(dlg._skill_checks)
        assert dlg._skill_checks["pdf-instrumenty"].isChecked()  # не отключён
        assert not dlg._skill_checks["vtoroy-skill"].isChecked()  # отключён
        assert dlg.lbl_skills_empty.isHidden()  # список не пуст — плашка скрыта

        # обновление списка не теряет состояние
        dlg._skills_refresh()
        assert set(dlg._skill_checks) == {
            "novyy-navyk",
            "pdf-instrumenty",
            "pdf-tools",
            "vtoroy-skill",
        }
        # запись отключённых — ровно то, что не отмечено галочкой
        cfg = dlg.config
        cfg.skills.disabled = [
            name for name, box in dlg._skill_checks.items() if not box.isChecked()
        ]
        assert cfg.skills.disabled == ["vtoroy-skill"]
    finally:
        dlg.close()
        dlg.deleteLater()
    print("ok  GUI: вкладка «Скиллы», список, галочки, обновление")


def test_nested_repo_layout() -> None:
    """Репозиторий скиллов, положенный целиком в .agents/skills, виден модели.

    Regression: скиллы hyperframes лежат вложенно (hyperframes/skills/<имя>/),
    загрузчик смотрел только первый уровень — и модель их не видела.
    """
    root = Path(tempfile.mkdtemp(prefix="nemus_skills_nested_"))
    try:
        nested = root / "hyperframes" / "skills" / "hyperframes"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(
            "---\nname: hyperframes\n"
            "description: Рендер видео через HyperFrames\n---\n\nИнструкция рендера.\n",
            encoding="utf-8",
        )
        # скрытые копии (.claude/skills/…) не должны дублироваться
        hidden = root / "hyperframes" / ".claude" / "skills" / "captions-overlay"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text(
            "---\nname: captions-overlay\ndescription: Тест\n---\n\nТело.\n",
            encoding="utf-8",
        )
        # обычный скилл первого уровня — по-прежнему работает
        save_skill("flat-skill", "Плоский скилл", "Тело.", root)

        names = [s.name for s in load_skills(root)]
        assert "hyperframes" in names, names
        assert "flat-skill" in names, names
        assert "captions-overlay" not in names, names
        assert len(names) == len(set(names)), names

        # промпт показывает вложенный скилл и его можно загрузить
        prompt = build_system_prompt(_cfg_for(root), ToolRegistry())
        assert "- hyperframes:" in prompt, prompt
        print("ok  вложенный репозиторий скиллов найден, скрытые копии отсеяны")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    tmp = _TMP
    try:
        test_slugify()
        test_parse_skill()
        test_save_and_load(tmp)
        test_nested_repo_layout()
        test_prompt_block(tmp)
        test_tools(tmp)
        test_factory(tmp)
        test_gui_tab(tmp)
        # реальный конфиг не должен ломаться
        assert get_config().skills.dir
        print("\nSKILLS TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
