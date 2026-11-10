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
    get_skill,
    load_skills,
    parse_skill,
    remove_skill,
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

    found = load_skills(tmp, builtin=False)
    assert [s.name for s in found] == ["pdf-instrumenty", "vtoroy-skill"], found

    # отключённый скилл не виден модели (по машинному имени скилла/папки)
    only = load_skills(tmp, disabled=["PDF-Instrumenty"], builtin=False)
    assert [s.name for s in only] == ["vtoroy-skill"], only

    loaded = load_skills(tmp, builtin=False)[0]
    assert loaded.description == "Склейка и разбиение PDF"
    assert loaded.files == ["helper.py"], loaded.files
    assert "Шаг 1" in loaded.body or "1. Открыть" in loaded.body

    # пустая папка / несуществующая — без исключений (без встроенных)
    empty = tmp / "empty_dir"
    empty.mkdir()
    assert load_skills(empty, builtin=False) == []
    assert load_skills(tmp / "nope", builtin=False) == []

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

    # пустая папка без встроенных скиллов — блока нет, а не пустая заглушка
    cfg.skills.enabled = True
    cfg.skills.dir = str(tmp / "nope")
    assert "Скиллы (формат" in build_system_prompt(cfg, ToolRegistry())
    print("ok  промпт: блок скиллов / отключение / только встроенные")


def test_skill_commands(tmp: Path) -> None:
    """Команды скиллов для автодополнения: по одной «/имя» на скилл."""
    from nemus.core.skill_commands import parse_skill_commands

    folder = tmp / "cmd-skills"
    save_skill("build-tools", "Сборка проекта", "Шаг 1. `npx hyperframes render .`", folder)
    save_skill("dup-tools", "Дубль команды", "Шаг 1.", folder)

    commands = parse_skill_commands(load_skills(folder, builtin=False))
    titles = [c.title for c in commands]
    assert titles == ["/build-tools", "/dup-tools"], titles
    assert commands[0].text == "/build-tools ", commands[0]
    assert commands[0].description == "Сборка проекта", commands[0]
    assert commands[0].skill == "build-tools", commands[0]
    assert commands[0].name == "build-tools", commands[0]

    # скилл с названием «/x» не даёт двойного слэша и не дублируется
    assert all(not item.title.startswith("//") for item in commands), titles
    assert len(set(titles)) == len(titles), titles

    # отключённый скилл не даёт команд
    off = parse_skill_commands(
        load_skills(folder, disabled=["build-tools"], builtin=False)
    )
    assert [c.title for c in off] == ["/dup-tools"], [c.title for c in off]
    print("ok  команды скиллов: «/имя» с описанием, без дублей и отключённых")


def test_skill_highlights() -> None:
    """Справка по скиллу берёт разделы инструкции, а не заголовки из кода."""
    from nemus.core.skills import skill_highlights

    body = (
        "# Название скилла\n\n"
        "## Железные правила\n\n"
        "```bash\n## это комментарий в коде, не раздел\n```\n\n"
        "### Подраздел\n\n"
        "## Железные правила\n"
    )
    items = skill_highlights(body)
    assert items == ["Железные правила", "Подраздел"], items
    assert skill_highlights("просто текст без заголовков") == []
    # ограничение количества разделов
    many = "\n".join(f"## Раздел {i}" for i in range(20))
    assert len(skill_highlights(many, limit=5)) == 5
    print("ok  справка по скиллу: разделы без кода, без дублей, с лимитом")


def test_slash_command_prompt() -> None:
    """Модель знает, что «/имя» — вызов скилла, а не путь."""
    prompt = build_system_prompt(Config(), ToolRegistry())
    assert "КОМАНДЫ /СКИЛЛ" in prompt, prompt[-800:]
    assert "load_skill" in prompt, prompt[-800:]
    print("ok  в промпте есть правило вызова скилла через «/имя»")


def test_builtin_skills(tmp: Path) -> None:
    """Встроенные скиллы из пакета работают без установки и не мешают своим."""
    from nemus.core.skills import BUILTIN_SKILLS_DIR

    names = [s.name for s in load_skills(tmp)]
    assert "code-craft" in names, names
    assert len(names) == len(set(names)), names
    assert "code-craft" not in [s.name for s in load_skills(tmp, builtin=False)]

    # отключение встроенного скилла работает как у любого другого
    assert "code-craft" not in [s.name for s in load_skills(tmp, disabled=["code-craft"])]

    # свой скилл с тем же именем перекрывает встроенный
    own = tmp / "own-skills"
    save_skill("code-craft", "Своя версия", "Своё тело.", own)
    merged = {s.name: s for s in load_skills(own)}
    assert merged["code-craft"].description == "Своя версия", merged["code-craft"]
    assert merged["code-craft"].path.parent == own, merged["code-craft"].path

    # сам встроенный скилл на месте и описывает брейншторм
    body = (BUILTIN_SKILLS_DIR / "code-craft" / "SKILL.md").read_text(encoding="utf-8")
    assert "брейншторм" in body.lower(), body[:200]
    assert "D) Свой вариант: _________" in body, body[:400]
    assert "(рекомендую)" in body, body[:400]

    prompt = build_system_prompt(_cfg_for(tmp), ToolRegistry())
    assert "- code-craft:" in prompt, prompt
    print("ok  встроенный скилл: подхват, отключение, перекрытие своим")


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
    long_desc = "Склейка PDF-файлов и сборка отчётов по документам. " * 8
    save_skill(
        "pdf-tools",
        long_desc,
        "# PDF\n\n## 1. Склейка страниц\n\n## 2. Сводка отчёта\n",
        tmp,
    )
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel

    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])
    dlg = SettingsDialog(core=None)
    try:
        assert hasattr(dlg, "chk_skills_enabled"), "вкладка скиллов не построена"
        assert dlg.chk_skills_enabled.isChecked() == dlg.config.skills.enabled

        # вкладка читает папку из конфига — подставляем тестовую и обновляем
        dlg.config.skills.dir = str(tmp)
        dlg.config.skills.disabled = ["vtoroy-skill"]
        dlg._skills_refresh()
        assert set(dlg._skill_checks) == {
            "build-tools",  # вложенный репозиторий cmd-skills/…
            "code-craft",
            "dup-tools",
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
            "build-tools",
            "code-craft",
            "dup-tools",
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

        # строки списка: описание с переносом, а не одной длинной строкой
        row = dlg._skill_rows["pdf-instrumenty"]
        assert row.lbl_desc.wordWrap(), "описание скилла не переносится по словам"
        assert set(dlg._skill_rows) == set(dlg._skill_checks), "строки и галочки разошлись"

        # клик по строке (не по квадратику) переключает скилл
        row.box.setChecked(True)
        row.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(4, 4),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert not row.box.isChecked(), "клик по строке не снял галочку"
        assert row.property("state") == "off", "выключенный скилл не потускнел"

        # клик по краткому описанию раскрывает подробности прямо в строке
        detail_row = dlg._skill_rows["pdf-tools"]
        assert detail_row.detail.isHidden(), "подробности открыты без клика"
        assert detail_row.lbl_desc.text().startswith("▸ "), detail_row.lbl_desc.text()
        was_on = detail_row.box.isChecked()
        QTest.mouseClick(detail_row.lbl_desc, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert not detail_row.detail.isHidden(), "клик по описанию не раскрыл подробности"
        assert detail_row.lbl_desc.text().startswith("▾ "), detail_row.lbl_desc.text()
        assert detail_row.box.isChecked() == was_on, "клик по описанию переключил скилл"
        texts = [lbl.text() for lbl in detail_row.detail.findChildren(QLabel)]
        assert any("Склейка PDF-файлов" in t for t in texts), texts
        assert any("В инструкции" in t and "1. Склейка страниц" in t for t in texts), texts
        assert any(t.startswith("Путь:") for t in texts), texts
        # подробности не шире строки и не распирают вкладку
        dlg.resize(940, 640)
        dlg.show()
        app.processEvents()
        page = dlg._skills_page.widget()
        viewport = dlg._skills_page.viewport().width()
        assert detail_row.detail.minimumSizeHint().width() <= viewport, (
            f"подробности требуют {detail_row.detail.minimumSizeHint().width()} px "
            f"при доступных {viewport}"
        )
        assert page.minimumSizeHint().width() <= viewport, "раскрытые подробности распёрли вкладку"
        QTest.mouseClick(detail_row.lbl_desc, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert detail_row.detail.isHidden(), "повторный клик не свернул подробности"
        assert detail_row.lbl_desc.text().startswith("▸ "), detail_row.lbl_desc.text()
        assert detail_row.box.isChecked() == was_on, "сворачивание переключило скилл"

        # длинное описание сокращается, полное остаётся в подсказке
        from nemus.gui.dialogs import _short_text

        long_desc = "Очень длинное описание скилла с подробностями. " * 40
        short = _short_text(long_desc)
        assert len(short) <= 181, len(short)
        assert short.endswith("…"), short[-20:]

        # вкладка не распирается по ширине: контент влезает в окно
        dlg.resize(940, 640)
        dlg.show()
        app.processEvents()
        page = dlg._skills_page.widget()
        viewport = dlg._skills_page.viewport().width()
        assert viewport > 0, "вкладка скиллов не получила ширину"
        assert page.minimumSizeHint().width() <= viewport, (
            f"контент вкладки шире окна: {page.minimumSizeHint().width()} > {viewport}"
        )
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


def test_plugin_commands() -> None:
    """Команды установленного плагина работают как «/плагин:команда».

    Немуся не исполняет хуки плагина — только его скиллы и команды.
    """
    from nemus.core.skills import format_skills_block

    root = Path(tempfile.mkdtemp(prefix="nemus_plugin_cmd_"))
    try:
        plugin = root / "kit"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "Kit Tools", "version": "1.0.0"}', encoding="utf-8"
        )
        (plugin / "commands").mkdir()
        (plugin / "commands" / "hello.md").write_text(
            "---\ndescription: Здоровается\nargument-hint: <имя>\n---\n\n"
            "Поздоровайся с $ARGUMENTS.\n",
            encoding="utf-8",
        )
        (plugin / "commands" / "git").mkdir()
        (plugin / "commands" / "git" / "commit.md").write_text(
            "Сделай коммит.\n", encoding="utf-8"
        )
        # файл с commands рядом, но без манифеста — не команда плагина
        stray = root / "notes" / "commands"
        stray.mkdir(parents=True)
        (stray / "lonely.md").write_text("---\ndescription: Чужое\n---\n\nТело.\n")

        skills = {s.name: s for s in load_skills(root, builtin=False)}
        # имя плагина нормализуется в slug: «Kit Tools» → kit-tools
        assert "kit-tools:hello" in skills, sorted(skills)
        assert "kit-tools:git:commit" in skills, sorted(skills)
        assert "lonely" not in skills and "notes:lonely" not in skills, sorted(skills)

        hello = skills["kit-tools:hello"]
        assert hello.kind == "command", hello.kind
        assert hello.file == "hello.md", hello.file
        assert hello.description.startswith("Здоровается") and "<имя>" in hello.description
        assert "$ARGUMENTS" in hello.body and "подставь" in hello.body, hello.body
        commit = skills["kit-tools:git:commit"]
        assert commit.description == "Сделай коммит.", commit.description

        # отключение — по полному имени или по имени команды
        off = load_skills(root, disabled=["kit-tools:hello"], builtin=False)
        assert "kit-tools:hello" not in {s.name for s in off}
        off = load_skills(root, disabled=["commit"], builtin=False)
        assert "kit-tools:git:commit" not in {s.name for s in off}

        # команда загружается по полному имени, но не по папке «commands»
        found_cmd = get_skill("kit-tools:hello", root)
        assert found_cmd is not None and found_cmd.kind == "command", found_cmd
        assert get_skill("commands", root) is None

        # команда не удаляется как скилл и описана в промпте отдельной строкой
        assert not remove_skill("kit-tools:hello", root), "команда удалилась как скилл"
        assert (plugin / "commands" / "hello.md").is_file()
        block = format_skills_block(load_skills(root, builtin=False), root)
        assert "Команды установленных плагинов" in block, block

        # справка по команде идёт тем же «/имя»
        cfg = _cfg_for(root)
        assert "- kit-tools:hello:" in build_system_prompt(cfg, ToolRegistry())
        print("ok  команды плагинов: «/плагин:команда», $ARGUMENTS, без удаления как скилл")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    tmp = _TMP
    try:
        test_slugify()
        test_parse_skill()
        test_save_and_load(tmp)
        test_nested_repo_layout()
        test_plugin_commands()
        test_builtin_skills(tmp)
        test_skill_commands(tmp)
        test_skill_highlights()
        test_slash_command_prompt()
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
