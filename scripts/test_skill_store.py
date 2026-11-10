"""Тесты магазина скиллов: каталог GitHub, установка, окно с карточками.

Сеть не нужна: подменяем методы GitHub-клиента и гоняем всё на фейковом
репозитории. Запуск:  venv\\Scripts\\python.exe scripts\\test_skill_store.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.skill_store import (  # noqa: E402
    PLUGIN_DIR,
    PLUGIN_FILE,
    GitHubClient,
    StoreError,
    StoreSkill,
    install,
    install_plugin,
    install_plugin_local,
    install_skill_local,
    installed_names,
    installed_plugins,
    is_installed,
    is_plugin_installed,
    origin_of,
    recommended,
    recommended_plugins,
    remove_plugin,
    repo_plugins,
    repo_skills,
    search,
    search_plugins,
    update,
    update_installed,
    update_plugin,
)
from nemus.core.skills import (  # noqa: E402
    SKILL_FILE,
    load_skills,
    remove_skill,
    skill_plugin,
)

PDF_SKILL = """---
name: pdf-tools
description: Склеивает и режет PDF-файлы
---

# PDF

## 1. Склейка
"""

EXCEL_SKILL = """---
name: excel-report
description: Собирает отчёт в Excel
---

# Excel
"""

REVIEW_SKILL = """---
name: review
description: Ревью кода по чек-листу
---

# Ревью
"""

LOCAL_SKILL = """---
name: local-skill
description: Скилл из плагина на диске
---

# Локальный
"""

LOCAL_COMMAND = """---
description: Здоровается из локального плагина
---

Поздоровайся с $ARGUMENTS!
"""


class FakeGitHub(GitHubClient):
    """GitHub «из коробки»: словарь репозиториев путь → текст файла."""

    def __init__(
        self,
        repos: dict[str, dict[str, str]],
        found: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(cache_path=None)  # тесты не должны трогать кэш проекта
        self.repos = repos
        self.found = found or {}
        self.read_calls: list[str] = []
        self.fail_on: str = ""

    def repo_info(self, repo: str) -> dict[str, object]:
        if repo not in self.repos:
            raise StoreError("Репозиторий не найден.")
        return {"ref": "main", "stars": 42, "description": f"Скиллы {repo}"}

    def tree(self, repo: str, ref: str) -> list[dict[str, object]]:
        if repo not in self.repos:
            raise StoreError("Репозиторий не найден.")
        return [{"path": path, "type": "blob"} for path in sorted(self.repos[repo])]

    def read_bytes(self, repo: str, ref: str, path: str) -> bytes:
        self.read_calls.append(path)
        if path == self.fail_on:
            raise StoreError("Нет связи с GitHub — проверьте интернет.")
        try:
            return self.repos[repo][path].encode("utf-8")
        except KeyError:
            raise StoreError("Скилл или репозиторий не найден на GitHub.") from None

    def search_repos(self, query: str, limit: int = 6) -> list[str]:
        # запросы вида «pdf topic:claude-skills» ищутся по первому слову
        for key, repos in self.found.items():
            if query.startswith(key):
                return repos[:limit]
        return []


def _client() -> FakeGitHub:
    return FakeGitHub(
        {
            "acme/skills": {
                "README.md": "# Скиллы",
                "skills/pdf/SKILL.md": PDF_SKILL,
                "skills/pdf/scripts/merge.py": "print('merge')\n",
                "skills/pdf/reference.md": "ссылки\n",
                "skills/excel/SKILL.md": EXCEL_SKILL,
                "skills/excel/reference.md": "ссылки\n",
                "node_modules/pkg/SKILL.md": "---\nname: junk\ndescription: мусор\n---\n",
                ".claude/skills/hidden/SKILL.md": "---\nname: hidden\ndescription: нет\n---\n",
            },
            "anthropics/skills": {"pdf/SKILL.md": PDF_SKILL},
            "obra/superpowers": {"skills/brainstorm/SKILL.md": EXCEL_SKILL},
            # пакет плагинов: у каждого плагина свои скиллы, имена могут совпадать
            "acme/pack": {
                ".claude-plugin/marketplace.json": (
                    '{"plugins": [{"name": "tools", "source": "./plugins/tools"},'
                    ' {"name": "other", "source": "./plugins/other"}]}'
                ),
                "plugins/tools/.claude-plugin/plugin.json": '{"name": "tools"}',
                "plugins/tools/skills/review/SKILL.md": REVIEW_SKILL,
                "plugins/tools/skills/pdf/SKILL.md": PDF_SKILL,
                "plugins/other/.claude-plugin/plugin.json": '{"name": "other"}',
                "plugins/other/skills/review/SKILL.md": REVIEW_SKILL.replace(
                    "по чек-листу", "второй пары глаз"
                ),
                # команды и агент плагина: у Немуси команды становятся «/имя»
                "plugins/tools/commands/hello.md": (
                    "---\ndescription: Здоровается\nargument-hint: <имя>\n---\n\n"
                    "Поздоровайся с $ARGUMENTS."
                ),
                "plugins/tools/commands/git/commit.md": (
                    "---\ndescription: Коммитит изменения\n---\n\nСделай коммит.\n"
                ),
                "plugins/tools/agents/helper.md": (
                    "---\nname: helper\n---\n\nПомогает с ревью.\n"
                ),
            },
            # маркетплейс плагинов — витрина части «Плагины»
            "anthropics/claude-code": {
                ".claude-plugin/marketplace.json": (
                    '{"plugins": [{"name": "example", "source": "./plugins/example",'
                    ' "description": "Пример плагина", "version": "0.1.0"}]}'
                ),
                "plugins/example/.claude-plugin/plugin.json": (
                    '{"name": "example", "version": "0.1.0"}'
                ),
                "plugins/example/commands/hello.md": (
                    "---\ndescription: Здоровается\n---\n\nПоздоровайся с $ARGUMENTS."
                ),
                "plugins/example/agents/helper.md": (
                    "---\nname: helper\n---\n\nАгент.\n"
                ),
            },
        },
        found={"pdf": ["acme/skills"], "pack": ["acme/pack"]},
    )


def test_catalog() -> None:
    client = _client()
    skills = repo_skills(client, "acme/skills")
    names = [s.name for s in skills]
    assert names == ["excel-report", "pdf-tools"], names
    pdf = next(s for s in skills if s.name == "pdf-tools")
    assert pdf.description == "Склеивает и режет PDF-файлы", pdf.description
    assert pdf.files[0] == "SKILL.md", pdf.files
    assert "scripts/merge.py" in pdf.files, pdf.files
    assert "reference.md" in pdf.files, pdf.files
    assert pdf.stars == 42 and pdf.source == "acme/skills · ★ 42", pdf.source
    assert pdf.url.endswith("/tree/main/skills/pdf"), pdf.url
    assert pdf.slug == "pdf-tools"
    print("ok  каталог: скиллы репозитория, файлы, скрытые и node_modules отсеяны")


def test_search_and_recommended() -> None:
    client = _client()
    # найденный репозиторий отдаёт все свои скиллы, а не только совпавшие
    found = search(client, "pdf")
    assert {s.name for s in found} == {"pdf-tools", "excel-report"}, found
    assert search(client, "") == []
    assert search(client, "ничего") == []

    # рекомендуемые: закреплённые репозитории подмешиваются в случайную выборку
    rec = recommended(client, sample=10)
    assert {s.repo for s in rec} <= {"acme/skills", "anthropics/skills", "obra/superpowers"}
    assert {s.name for s in rec} >= {"pdf-tools", "excel-report"}, rec
    assert len(recommended(client, sample=1)) == 1

    broken = FakeGitHub({})
    try:
        recommended(broken)
        raise AssertionError("пустой каталог должен давать StoreError")
    except StoreError:
        pass
    print("ok  поиск по репозиториям и случайные рекомендуемые")


def test_install(tmp: Path) -> None:
    client = _client()
    skill = next(s for s in repo_skills(client, "acme/skills") if s.name == "pdf-tools")
    assert not is_installed(skill, tmp)

    installed = install(skill, tmp, client)
    assert installed.name == "pdf-tools", installed.name
    assert (tmp / "pdf-tools" / "SKILL.md").is_file()
    assert (tmp / "pdf-tools" / "scripts" / "merge.py").read_text(encoding="utf-8") == (
        "print('merge')\n"
    ), "вспомогательные файлы скилла не скопировались"
    assert not (tmp / ".pdf-tools.installing").exists(), "не убрана временная папка"
    assert installed_names(tmp) == {"pdf-tools"}
    assert is_installed(skill, tmp)
    assert [s.name for s in load_skills(tmp, builtin=False)] == ["pdf-tools"]

    # повторная установка не затирает свою версию
    try:
        install(skill, tmp, client)
        raise AssertionError("повторная установка должна падать")
    except StoreError as exc:
        assert "уже установлен" in str(exc), exc

    # обрыв скачивания не оставляет полусобранный скилл
    client.fail_on = "skills/excel/reference.md"
    excel = next(s for s in repo_skills(client, "acme/skills") if s.name == "excel-report")
    try:
        install(excel, tmp, client)
        raise AssertionError("обрыв скачивания должен падать")
    except StoreError:
        pass
    assert not (tmp / "excel-report").exists(), "битый скилл попал в папку скиллов"
    assert not (tmp / ".excel-report.installing").exists(), "осталась временная папка"
    print("ok  установка: файлы, повтор, обрыв скачивания")


def test_remove(tmp: Path) -> None:
    client = _client()
    skill = next(s for s in repo_skills(client, "acme/skills") if s.name == "pdf-tools")
    install(skill, tmp, client)

    # чужое имя и отсутствующая папка не удаляют установленный скилл
    assert not remove_skill("excel-report", tmp)
    assert not remove_skill("", tmp)
    assert not remove_skill("pdf-tools", tmp.parent / "nope")
    assert (tmp / "pdf-tools" / "SKILL.md").is_file()

    assert remove_skill("pdf-tools", tmp), "скилл не удалился"
    assert not (tmp / "pdf-tools").exists(), "папка скилла осталась на диске"
    assert installed_names(tmp) == set()
    assert not remove_skill("pdf-tools", tmp), "повторное удаление должно быть False"
    print("ok  удаление: папка целиком, чужое имя и повтор — без ошибок")


def test_pack(tmp: Path) -> None:
    """Набор плагинов: имена из разных плагинов — разные скиллы."""
    client = _client()
    skills = repo_skills(client, "acme/pack")
    by_path = {s.path: s for s in skills}
    assert by_path["plugins/tools/skills/review"].plugin == "tools"
    assert by_path["plugins/other/skills/review"].plugin == "other"
    # манифесты не попали в файлы скиллов
    assert ".claude-plugin" not in " ".join(by_path["plugins/tools/skills/review"].files)
    # лимит репозитория не забивается одним плагином набора
    pair = repo_skills(client, "acme/pack", limit=2)
    assert {s.plugin for s in pair} == {"tools", "other"}, [s.plugin for s in pair]
    # кэш первого запроса не должен скрывать остальной набор
    client.forget_skills("acme/pack")
    assert len(repo_skills(client, "acme/pack", limit=1)) == 1
    grown = repo_skills(client, "acme/pack", limit=3)
    assert len(grown) == 3, "догрузка набора упирается в кэш первого запроса"

    # витрина не схлопывает одинаковые имена из разных плагинов
    found = search(client, "pack", limit_repos=1)
    reviews = [s for s in found if s.name == "review"]
    assert {s.plugin for s in reviews} == {"tools", "other"}, reviews
    assert len({s.key for s in found}) == len(found), "карточки-двойники"

    first = by_path["plugins/tools/skills/review"]
    second = by_path["plugins/other/skills/review"]
    assert not is_installed(first, tmp) and not is_installed(second, tmp)
    install(first, tmp, client)
    assert is_installed(first, tmp) and not is_installed(second, tmp), (
        "галочка перепутала плагины с одинаковым именем"
    )
    install(second, tmp, client)
    assert (tmp / "review").is_dir() and (tmp / "other-review").is_dir(), (
        "одинаковые имена затёрли друг друга"
    )
    names = {s.name for s in load_skills(tmp, builtin=False)}
    assert names == {"review", "other-review"}, names
    origin = origin_of(tmp, "other-review")
    assert origin is not None and origin["path"] == "plugins/other/skills/review", origin
    # обновление находит скилл набора по запомненному пути и переименование не сбивает
    _, changed = update_installed("other-review", tmp, client)
    assert not changed, "одинаковый скилл из набора считается обновлением"
    assert (tmp / "other-review" / "SKILL.md").read_text(encoding="utf-8").startswith(
        "---"
    )

    # скилл из набора грузится с любой вложенности (plugins/<п>/skills/<с>)
    nested = tmp.parent / "skills_nested"
    lint = nested / "pack" / "plugins" / "tools" / "skills" / "lint"
    lint.mkdir(parents=True)
    (lint / "SKILL.md").write_text(
        REVIEW_SKILL.replace("name: review", "name: lint"), encoding="utf-8"
    )
    assert "lint" in {s.name for s in load_skills(nested, builtin=False)}, (
        "вложенный скилл плагина не найден"
    )
    print("ok  набор плагинов: имена из разных плагинов и установка без затирания")


def test_update(tmp: Path) -> None:
    client = _client()
    skill = next(s for s in repo_skills(client, "acme/skills") if s.name == "pdf-tools")
    install(skill, tmp, client)
    assert origin_of(tmp, "pdf-tools") == {
        "repo": "acme/skills",
        "path": "skills/pdf",
        "name": "pdf-tools",
        "local": "",
        "kind": "skill",
    }, origin_of(tmp, "pdf-tools")

    # без изменений на GitHub папка не трогается
    _, changed = update_installed("pdf-tools", tmp, client)
    assert not changed, "одинаковое содержимое не должно считаться обновлением"

    # новая версия в репозитории заменяет файлы целиком
    client.repos["acme/skills"]["skills/pdf/SKILL.md"] = PDF_SKILL.replace(
        "Склеивает и режет PDF-файлы", "Склеивает, режет и поворачивает PDF"
    )
    client.repos["acme/skills"]["skills/pdf/scripts/merge.py"] = "print('v2')\n"
    updated, changed = update_installed("pdf-tools", tmp, client)
    assert changed, "новая версия не применилась"
    assert "поворачивает" in updated.description, updated.description
    disk = tmp / "pdf-tools"
    assert (disk / "scripts" / "merge.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert not list(disk.parent.glob(".pdf-tools.*")), "остались временные папки"

    # скилл без запомненного источника и исчезнувший с GitHub — понятные ошибки
    manual = tmp / "manual-skill"
    manual.mkdir()
    (manual / "SKILL.md").write_text(PDF_SKILL, encoding="utf-8")
    try:
        update_installed("manual-skill", tmp, client)
        raise AssertionError("скилл без источника не должен обновляться")
    except StoreError as exc:
        assert "скачан" in str(exc).lower(), exc

    gone = StoreSkill(
        name="gone", description="", repo="acme/skills", path="skills/gone", ref="main"
    )
    try:
        update(gone, tmp, client)
        raise AssertionError("неустановленный скилл не должен обновляться")
    except StoreError as exc:
        assert "не установлен" in str(exc), exc
    print("ok  обновление: свежая версия, замена файлов, ошибки источника")


def test_install_guards(tmp: Path) -> None:
    client = _client()
    # путь наружу из папки скилла
    bad = StoreSkill(
        name="evil",
        description="плохой",
        repo="acme/skills",
        path="skills/pdf",
        ref="main",
        files=("SKILL.md", "../../../escape.txt"),
    )
    try:
        install(bad, tmp, client)
        raise AssertionError("путь с «..» должен отклоняться")
    except StoreError as exc:
        assert "путь" in str(exc).lower(), exc
    assert not (tmp.parent / "escape.txt").exists(), "файл утек за пределы папки скиллов"

    # скилл без SKILL.md
    empty = StoreSkill(
        name="empty", description="", repo="acme/skills", path="", ref="main", files=("README.md",)
    )
    try:
        install(empty, tmp, client)
        raise AssertionError("скилл без SKILL.md не должен ставиться")
    except StoreError as exc:
        assert "SKILL.md" in str(exc), exc
    print("ok  защита: пути наружу, отсутствие SKILL.md")


def test_plugins(tmp: Path) -> None:
    """Плагины целиком: каталог, установка пакетом, команды «/плагин:команда»."""
    client = _client()
    plugins = repo_plugins(client, "acme/pack")
    by_name = {p.name: p for p in plugins}
    assert set(by_name) == {"tools", "other"}, by_name

    tools = by_name["tools"]
    assert tools.path == "plugins/tools", tools.path
    assert (tools.skills, tools.commands, tools.agents) == (2, 2, 1), (
        tools.skills,
        tools.commands,
        tools.agents,
    )
    assert tools.files[0] == f"{PLUGIN_DIR}/{PLUGIN_FILE}", tools.files[:2]
    assert "commands/hello.md" in tools.files, tools.files
    assert "commands/git/commit.md" in tools.files, tools.files
    # манифест самого маркетплейса — не файл плагина
    assert f"{PLUGIN_DIR}/marketplace.json" not in tools.files
    assert not is_plugin_installed(tools, tmp)

    # поиск и витрина плагинов
    found = search_plugins(client, "pack", limit_repos=1)
    assert {p.name for p in found} == {"tools", "other"}, found
    assert search_plugins(client, "") == []
    rec = recommended_plugins(client)
    assert [p.name for p in rec] == ["example"], rec
    assert rec[0].version == "0.1.0" and rec[0].commands == 1, rec[0]

    # установка целым пакетом: скиллы, команды и агенты на месте
    assert install_plugin(tools, tmp, client) == "tools"
    disk = tmp / "tools"
    assert (disk / PLUGIN_DIR / PLUGIN_FILE).is_file()
    assert (disk / "commands" / "hello.md").is_file()
    assert (disk / "agents" / "helper.md").is_file()
    assert (disk / "skills" / "review" / "SKILL.md").is_file()
    assert not (tmp / ".tools.installing").exists()
    assert is_plugin_installed(tools, tmp)
    origin = origin_of(tmp, "tools")
    assert origin is not None and origin["kind"] == "plugin", origin
    # скилл внутри этого плагина сам по себе не считается установленным
    packed = next(s for s in repo_skills(client, "acme/pack") if s.path == tools.path + "/skills/review")
    assert not is_installed(packed, tmp), "плагин выдал себя за отдельный скилл"

    skills = {s.name: s for s in load_skills(tmp, builtin=False)}
    assert "review" in skills, sorted(skills)
    command = skills.get("tools:hello")
    assert command is not None and command.kind == "command", sorted(skills)
    assert "подставь" in command.body.lower(), command.body
    assert command.description.startswith("Здоровается") and "<имя>" in command.description
    assert "tools:git:commit" in skills, sorted(skills)

    # скилл внутри плагина — часть пакета: отдельно не удаляется и не обновляется
    assert skill_plugin(disk / "skills" / "review", tmp) == "tools"
    assert skill_plugin(disk, tmp) == "tools"
    assert skill_plugin(tmp, tmp) == ""
    assert not remove_skill("review", tmp), "скилл вырезан из установленного плагина"
    assert (disk / "skills" / "review" / "SKILL.md").is_file()

    installed = installed_plugins(tmp)
    assert [p.folder for p in installed] == ["tools"], installed
    assert (installed[0].skills, installed[0].commands, installed[0].agents) == (2, 2, 1)
    assert installed[0].tracked

    # обновление по запомненному источнику
    client.repos["acme/pack"]["plugins/tools/commands/hello.md"] = (
        "---\ndescription: Здоровается дважды\n---\n\nПоздоровайся с $ARGUMENTS!"
    )
    _, changed = update_plugin("tools", tmp, client)
    assert changed, "новая версия плагина не применилась"
    assert "дважды" in (disk / "commands" / "hello.md").read_text(encoding="utf-8")
    _, changed = update_plugin("tools", tmp, client)
    assert not changed, "одинаковый плагин считается обновлением"

    # удаление: папка целиком и запись источника
    assert remove_plugin("tools", tmp), "плагин не удалился"
    assert not disk.exists(), "папка плагина осталась на диске"
    assert origin_of(tmp, "tools") is None
    assert installed_plugins(tmp) == []
    assert not remove_plugin("tools", tmp), "повторное удаление должно быть False"

    try:
        update_plugin("tools", tmp, client)
        raise AssertionError("неустановленный плагин не должен обновляться")
    except StoreError as exc:
        assert "скачан" in str(exc).lower(), exc
    print("ok  плагины: каталог, установка пакетом, команды «/плагин:команда», обновление и удаление")


def test_local_plugin(tmp: Path) -> None:
    """Плагин из папки и .zip: установка, запись источника, обновление."""
    source = tmp / "src" / "local-pack"
    (source / PLUGIN_DIR).mkdir(parents=True)
    (source / PLUGIN_DIR / PLUGIN_FILE).write_text(
        json.dumps(
            {
                "name": "Local Pack",
                "version": "1.0.0",
                "description": "Плагин с диска",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (source / "skills" / "local-skill").mkdir(parents=True)
    (source / "skills" / "local-skill" / SKILL_FILE).write_text(
        LOCAL_SKILL, encoding="utf-8"
    )
    (source / "commands").mkdir()
    (source / "commands" / "hi.md").write_text(LOCAL_COMMAND, encoding="utf-8")
    # служебные каталоги в плагин не тащим
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("x", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "big.js").write_text("x", encoding="utf-8")

    root = tmp / "skills"
    folder, changed = install_plugin_local(source, root)
    assert (folder, changed) == ("local-pack", True), (folder, changed)
    disk = root / "local-pack"
    assert (disk / PLUGIN_DIR / PLUGIN_FILE).is_file()
    assert not (disk / ".git").exists() and not (disk / "node_modules").exists()
    origin = origin_of(root, "local-pack")
    assert origin is not None and origin["kind"] == "plugin", origin
    assert origin["local"] == str(source.resolve()) and not origin["repo"], origin
    installed = installed_plugins(root)
    assert installed[0].local == str(source.resolve()), installed[0]
    assert not installed[0].tracked, "локальный плагин не должен тянуться с GitHub"
    names = {s.name for s in load_skills(root, builtin=False)}
    assert {"local-skill", "local-pack:hi"} <= names, sorted(names)

    try:
        install_plugin_local(source, root)
        raise AssertionError("повторная установка должна требовать replace")
    except StoreError as exc:
        assert "уже установлен" in str(exc), exc

    # обновление из того же источника: правка в папке доезжает до установленного
    (source / "skills" / "local-skill" / SKILL_FILE).write_text(
        LOCAL_SKILL.replace("# Локальный", "# Локальный правленый"),
        encoding="utf-8",
    )
    _, changed = update_plugin("local-pack", root)
    assert changed, "правка локального источника не применилась"
    assert "правленый" in (disk / "skills" / "local-skill" / SKILL_FILE).read_text(
        encoding="utf-8"
    )
    assert not update_plugin("local-pack", root)[1], "одинаковый плагин считается обновлением"

    # архив, где плагин лежит в папке-обёртке
    archive_path = tmp / "zip-pack.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "zip-pack-main/.claude-plugin/plugin.json",
            json.dumps({"name": "zip-pack", "version": "2.0"}),
        )
        archive.writestr(
            "zip-pack-main/skills/zip-skill/SKILL.md",
            "---\nname: zip-skill\ndescription: Скилл из архива\n---\n\n# Zip\n",
        )
        archive.writestr("zip-pack-main/notes/readme.txt", "ok")
    folder, changed = install_plugin_local(archive_path, root)
    assert (folder, changed) == ("zip-pack", True), (folder, changed)
    assert (root / "zip-pack" / "notes" / "readme.txt").is_file()
    assert "zip-skill" in {s.name for s in load_skills(root, builtin=False)}
    assert origin_of(root, "zip-pack")["local"] == str(archive_path.resolve())

    # архив без манифеста и архив с двумя плагинами
    bad = tmp / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("readme.txt", "нет манифеста")
    two = tmp / "two.zip"
    with zipfile.ZipFile(two, "w") as archive:
        for name in ("a", "b"):
            archive.writestr(
                f"{name}/.claude-plugin/plugin.json",
                json.dumps({"name": name}),
            )
    for path, needle in ((bad, "нет плагина"), (two, "несколько плагинов")):
        try:
            install_plugin_local(path, root)
            raise AssertionError(f"{path.name} не должен ставиться")
        except StoreError as exc:
            assert needle in str(exc).lower(), exc

    assert remove_plugin("local-pack", root) and remove_plugin("zip-pack", root)
    assert installed_plugins(root) == [], "локальные плагины остались после удаления"
    assert origin_of(root, "local-pack") is None
    print("ok  плагин из файла: папка, .zip, источник, обновление, ошибки архивов")


def test_local_skill(tmp: Path) -> None:
    """Скилл из папки и .zip: установка, запись источника, обновление."""
    source = tmp / "src" / "local-skill"
    source.mkdir(parents=True)
    (source / SKILL_FILE).write_text(
        LOCAL_SKILL.replace("local-skill", "Local Skill"), encoding="utf-8"
    )
    (source / "scripts").mkdir()
    (source / "scripts" / "helper.py").write_text("print(1)\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("x", encoding="utf-8")

    root = tmp / "skills"
    skill, changed = install_skill_local(source, root)
    assert (skill.name, changed) == ("Local Skill", True), (skill.name, changed)
    disk = root / "local-skill"
    assert (disk / SKILL_FILE).is_file() and (disk / "scripts" / "helper.py").is_file()
    assert not (disk / ".git").exists()
    origin = origin_of(root, "local-skill")
    assert origin is not None and origin["kind"] == "skill", origin
    assert origin["local"] == str(source.resolve()) and not origin["repo"], origin

    try:
        install_skill_local(source, root)
        raise AssertionError("повторная установка должна требовать replace")
    except StoreError as exc:
        assert "уже установлен" in str(exc), exc

    # обновление из того же источника
    (source / SKILL_FILE).write_text(
        LOCAL_SKILL.replace("local-skill", "Local Skill").replace(
            "# Локальный", "# Локальный правленый"
        ),
        encoding="utf-8",
    )
    _, changed = update_installed("Local Skill", root)
    assert changed, "правка локального скилла не применилась"
    assert "правленый" in (disk / SKILL_FILE).read_text(encoding="utf-8")
    assert not update_installed("Local Skill", root)[1], (
        "одинаковый скилл считается обновлением"
    )

    # архив: скилл внутри вложенной папки, лишнее рядом не тащим
    archive_path = tmp / "one-skill.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "repo-main/skills/ziped/SKILL.md",
            "---\nname: Zipped Skill\ndescription: Скилл из архива\n---\n\n# Zip\n",
        )
        archive.writestr("repo-main/skills/ziped/extra.txt", "ok")
        archive.writestr("repo-main/README.md", "не скилл")
    skill, changed = install_skill_local(archive_path, root)
    assert (skill.name, changed) == ("Zipped Skill", True), (skill.name, changed)
    assert (root / "zipped-skill" / "extra.txt").is_file()
    assert not (root / "zipped-skill" / "README.md").exists()
    assert origin_of(root, "zipped-skill")["local"] == str(archive_path.resolve())

    # архив без SKILL.md и архив с двумя скиллами
    bad = tmp / "bad-skill.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("readme.txt", "нет скилла")
    two = tmp / "two-skills.zip"
    with zipfile.ZipFile(two, "w") as archive:
        for name in ("a", "b"):
            archive.writestr(
                f"{name}/SKILL.md", f"---\nname: {name}\n---\n\n# {name}\n"
            )
    for path, needle in ((bad, "нет skill.md"), (two, "несколько скиллов")):
        try:
            install_skill_local(path, root)
            raise AssertionError(f"{path.name} не должен ставиться")
        except StoreError as exc:
            assert needle in str(exc).lower(), exc

    assert remove_skill("Local Skill", root), "локальный скилл не удалился"
    assert not disk.exists()
    print("ok  скилл из файла: папка, .zip, источник, обновление, ошибки архивов")


def _cards(layout) -> list:
    """Виджеты-карточки в раскладке (без заглушек и растяжек)."""
    widgets = []
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if widget is not None and widget.objectName() == "StoreCard":
            widgets.append(widget)
    return widgets


def _headers(layout) -> list[str]:
    """Заголовки групп в раскладке «Скачанных»."""
    texts = []
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if widget is not None and widget.objectName() == "StoreGroup":
            texts.append(widget.text())
    return texts


def _wait(app, predicate, timeout: float = 10.0) -> None:
    """Ждёт выполнения условия, прокачивая события Qt (воркеры в фоне)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("фоновая операция не завершилась за отведённое время")


def test_gui(tmp: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

    from nemus.gui.skill_store import SkillStoreDialog

    app = QApplication.instance() or QApplication([])
    dlg = SkillStoreDialog(None, directory=tmp, client=_client())
    signals: list[str] = []
    dlg.installed.connect(signals.append)
    dlg.show()
    try:
        # витрина: карточки с «+» вместо пустого экрана
        _wait(app, lambda: bool(_cards(dlg._rec_box)))
        cards = [
            b
            for b in dlg.findChildren(QPushButton)
            if b.objectName() == "StoreInstall"
        ]
        assert cards, "в витрине нет кнопок установки"
        assert all(b.text() == "+" for b in cards), [b.text() for b in cards]
        assert _cards(dlg._rec_box), "карточки витрины не построились"

        # карточка скилла из плагина подписывает плагин
        from PySide6.QtWidgets import QLabel

        packed = repo_skills(dlg._client, "acme/pack")
        plugin_card = dlg._card(next(s for s in packed if s.plugin == "other"))
        labels = [w.text() for w in plugin_card.findChildren(QLabel)]
        assert any("плагин other" in text for text in labels), labels
        plugin_card.deleteLater()

        # витрина не обрезана: конец списка догружает ещё карточки,
        # а когда GitHub больше ничего не дал — подсказывает поиск
        for _ in range(12):
            if "через поиск" in dlg.status.text():
                break
            before = len(_cards(dlg._rec_box))
            dlg._load_more_recommended()
            _wait(
                app,
                lambda b=before: len(_cards(dlg._rec_box)) > b
                or "через поиск" in dlg.status.text(),
                timeout=20,
            )
        assert "через поиск" in dlg.status.text(), (
            f"догрузка витрины не дошла до конца: {dlg.status.text()}"
        )

        # фильтр по репозиторию и порядок «по новизне»
        assert dlg.repo_filter.count() > 1, "фильтр не знает ни одного репозитория"
        repo = str(dlg.repo_filter.itemData(1))
        dlg.repo_filter.setCurrentIndex(1)
        app.processEvents()
        assert _cards(dlg._rec_box), "фильтр по репозиторию опустошил витрину"
        assert all(s.repo == repo for s in dlg._rec_view), dlg._rec_view

        dlg.sort_mode.setCurrentIndex(1)  # по новизне
        app.processEvents()
        assert dlg._rec_sort == "new", dlg._rec_sort
        assert all(s.repo == repo for s in dlg._rec_view), "фильтр слетел при сортировке"
        dates = [(s.updated, -s.stars) for s in dlg._rec_view]
        assert dates == sorted(dates, reverse=True), dates

        dlg.repo_filter.setCurrentIndex(0)  # снова все репозитории
        dlg.sort_mode.setCurrentIndex(0)
        app.processEvents()
        assert len(dlg._rec_view) == len(dlg._rec_pool), "фильтр не снялся"

        # поиск по запросу
        dlg.search_input.setText("pdf")
        dlg._run_search()
        _wait(app, lambda: bool(_cards(dlg._search_box)))
        assert dlg.tabs.currentIndex() == dlg.tab_search, "поиск не открыл свой раздел"

        # результаты поиска не обрезаны: конец списка догружает карточки
        for _ in range(12):
            if "уточните запрос" in dlg.status.text():
                break
            before = len(_cards(dlg._search_box))
            dlg._load_more_search()
            _wait(
                app,
                lambda b=before: len(_cards(dlg._search_box)) > b
                or "уточните запрос" in dlg.status.text(),
                timeout=20,
            )
        assert "уточните запрос" in dlg.status.text(), (
            f"догрузка поиска не дошла до конца: {dlg.status.text()}"
        )

        # плюсик ставит скилл и сразу показывает его в «Скачанных»
        pdf_key = next(k for k in dlg._buttons if k.endswith("pdf"))
        pdf_btn = dlg._buttons[pdf_key][0]
        assert pdf_btn.text() == "+", pdf_btn.text()
        QTest.mouseClick(pdf_btn, Qt.MouseButton.LeftButton)
        _wait(app, lambda: (tmp / "pdf-tools" / "SKILL.md").is_file())
        _wait(app, lambda: "установлен" in dlg.status.text())
        assert pdf_btn.text() == "✓" and not pdf_btn.isEnabled(), "кнопка не стала галочкой"
        assert signals == ["pdf-tools"], signals
        assert dlg.tabs.currentIndex() == dlg.tab_installed, "не открылись «Скачанные»"
        assert "1" in dlg.tabs.tabText(dlg.tab_installed), dlg.tabs.tabText(dlg.tab_installed)
        assert _cards(dlg._installed_box), "установленный скилл не попал в раздел"
        assert _headers(dlg._installed_box) == ["Самостоятельные скиллы (1)"], _headers(
            dlg._installed_box
        )
        assert any(
            w.text().startswith("источник:")
            for c in _cards(dlg._installed_box)
            for w in c.findChildren(QLabel)
        ), "у установленного скилла нет пометки источника"

        # «⟳» на карточке скачанного скилла подтягивает свежую версию
        # кнопки берём из карты окна: старые карточки после пересборки ещё живы
        upd_btn = dlg._update_buttons.get("pdf-tools")
        assert upd_btn is not None and upd_btn.objectName() == "StoreUpdate", (
            "на карточке скачанного нет кнопки обновления"
        )
        updated_signals: list[str] = []
        dlg.updated.connect(updated_signals.append)
        # скилл мог приехать из любого рекомендованного репозитория — правим именно его
        origin = origin_of(tmp, "pdf-tools")
        assert origin is not None, "источник установки не запомнился"
        dlg._client.repos[origin["repo"]][f"{origin['path']}/SKILL.md"] = PDF_SKILL.replace(
            "Склеивает и режет PDF-файлы", "Склеивает и режет PDF из магазина"
        )
        QTest.mouseClick(upd_btn, Qt.MouseButton.LeftButton)
        _wait(app, lambda: "обновлён" in dlg.status.text(), timeout=20)
        assert updated_signals == ["pdf-tools"], updated_signals
        assert "магазина" in (tmp / "pdf-tools" / "SKILL.md").read_text(encoding="utf-8")
        # повторный клик — уже свежая версия, сигнала нет
        QTest.mouseClick(dlg._update_buttons["pdf-tools"], Qt.MouseButton.LeftButton)
        _wait(app, lambda: "уже свежий" in dlg.status.text(), timeout=20)
        assert updated_signals == ["pdf-tools"], updated_signals

        # «×» на карточке скачанного скилла удаляет папку целиком
        remove_btns = [
            b
            for b in dlg._installed_box.parentWidget().findChildren(QPushButton)
            if b.objectName() == "StoreRemove"
        ]
        assert remove_btns, "на карточке скачанного нет кнопки удаления"

        import nemus.gui.skill_store as store_mod

        class _YesBox:
            """Заглушка QMessageBox: подтверждение без модального окна."""

            StandardButton = QMessageBox.StandardButton

            @staticmethod
            def question(*args, **kwargs) -> QMessageBox.StandardButton:
                return QMessageBox.StandardButton.Yes

        original = store_mod.QMessageBox
        store_mod.QMessageBox = _YesBox
        removed_signals: list[str] = []
        dlg.removed.connect(removed_signals.append)
        try:
            QTest.mouseClick(remove_btns[0], Qt.MouseButton.LeftButton)
        finally:
            store_mod.QMessageBox = original
        _wait(app, lambda: not (tmp / "pdf-tools").exists())
        assert removed_signals == ["pdf-tools"], removed_signals
        assert "удалён" in dlg.status.text(), dlg.status.text()
        assert not _cards(dlg._installed_box), "карточка удалённого скилла осталась"
        assert "0" in dlg.tabs.tabText(dlg.tab_installed), dlg.tabs.tabText(dlg.tab_installed)
        # витрина снова предлагает установить этот скилл
        assert pdf_btn.text() == "+" and pdf_btn.isEnabled(), pdf_btn.text()

        # старая карточка, удалённая Qt, не ломает галочку при пересборке витрины
        from PySide6.QtCore import QCoreApplication, QEvent

        stale = QPushButton("+")
        stale.setObjectName("StoreInstall")
        stale.deleteLater()
        dlg._buttons.setdefault(pdf_key, []).append(stale)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        dlg._mark_installed(pdf_key, True)  # прежде — RuntimeError: объект уже удалён
        assert stale not in dlg._buttons[pdf_key], "мёртвая кнопка осталась в реестре"

        # скилл из локальной папки: установка, источник, обновление и удаление
        skill_src = tmp.parent / "gui-skill-src"
        skill_src.mkdir(parents=True)
        (skill_src / SKILL_FILE).write_text(
            LOCAL_SKILL.replace("local-skill", "GUI Skill"), encoding="utf-8"
        )
        dlg._install_local_skill(str(skill_src))
        _wait(app, lambda: (tmp / "gui-skill" / SKILL_FILE).is_file())
        _wait(app, lambda: "GUI Skill" in dlg.status.text())
        assert dlg.tabs.currentIndex() == dlg.tab_installed
        gui_labels = [
            w.text() for c in _cards(dlg._installed_box) for w in c.findChildren(QLabel)
        ]
        assert any("GUI Skill" in text for text in gui_labels), gui_labels
        assert any(
            text.startswith("источник:") and str(skill_src.resolve()) in text
            for text in gui_labels
        ), gui_labels
        local_skill_origin = origin_of(tmp, "gui-skill")
        assert local_skill_origin is not None and local_skill_origin["kind"] == "skill"

        (skill_src / SKILL_FILE).write_text(
            LOCAL_SKILL.replace("local-skill", "GUI Skill").replace(
                "# Локальный", "# Локальный правленый"
            ),
            encoding="utf-8",
        )
        QTest.mouseClick(dlg._update_buttons["gui skill"], Qt.MouseButton.LeftButton)
        _wait(app, lambda: "обновлён" in dlg.status.text(), timeout=20)
        assert "правленый" in (tmp / "gui-skill" / SKILL_FILE).read_text(
            encoding="utf-8"
        )

        gui_remove = next(
            b
            for card in _cards(dlg._installed_box)
            for b in card.findChildren(QPushButton)
            if b.objectName() == "StoreRemove"
            and any("GUI Skill" == label.text() for label in card.findChildren(QLabel))
        )
        original = store_mod.QMessageBox
        store_mod.QMessageBox = _YesBox
        try:
            QTest.mouseClick(gui_remove, Qt.MouseButton.LeftButton)
        finally:
            store_mod.QMessageBox = original
        _wait(app, lambda: not (tmp / "gui-skill").exists())
        assert origin_of(tmp, "gui-skill") is None, "источник удалённого скилла остался"

        # карточки не распирают окно по ширине
        dlg.resize(880, 640)
        app.processEvents()
        page = dlg._rec_box.parentWidget()
        assert page.minimumSizeHint().width() <= 880, page.minimumSizeHint().width()

        # магазин из двух частей: «Скиллы» и «Плагины» целиком
        assert dlg.sections.count() == 2, dlg.sections.count()
        assert "Скиллы" in dlg.sections.tabText(dlg.section_skills)
        assert "Плагины" in dlg.sections.tabText(dlg.section_plugins)
        dlg.sections.setCurrentIndex(dlg.section_plugins)
        _wait(app, lambda: bool(_cards(dlg._prec_box)))

        # поиск внутри части «Плагины»
        dlg.plugin_search_input.setText("pack")
        dlg._run_plugin_search()
        _wait(app, lambda: bool(_cards(dlg._psearch_box)))
        assert dlg.plugin_tabs.currentIndex() == dlg.plugin_search_tab, (
            "поиск не открыл свою вкладку плагинов"
        )
        assert dlg._plugin_search_pool, "поиск плагинов ничего не вернул"
        dlg.plugin_tabs.setCurrentIndex(dlg.plugin_rec_tab)

        plugin = next(iter(dlg._p_cards.values()))
        plugin_btn = dlg._p_buttons[plugin.key][0]
        QTest.mouseClick(plugin_btn, Qt.MouseButton.LeftButton)
        _wait(app, lambda: (tmp / plugin.slug).is_dir(), timeout=20)
        _wait(app, lambda: plugin_btn.text() == "✓")
        assert dlg.plugin_tabs.currentIndex() == dlg.plugin_installed_tab, (
            "не открылись установленные плагины"
        )
        assert _cards(dlg._pinstalled_box), "плагин не попал в «Установленные»"
        assert any(
            w.text().startswith("источник:")
            for c in _cards(dlg._pinstalled_box)
            for w in c.findChildren(QLabel)
        ), "у установленного плагина нет пометки источника"
        names = {s.name for s in load_skills(tmp, builtin=False)}
        assert f"{plugin.slug}:hello" in names, sorted(names)

        p_remove = [
            b
            for b in dlg._pinstalled_box.parentWidget().findChildren(QPushButton)
            if b.objectName() == "StoreRemove"
        ]
        assert p_remove, "на карточке плагина нет удаления"
        original = store_mod.QMessageBox
        store_mod.QMessageBox = _YesBox
        try:
            QTest.mouseClick(p_remove[0], Qt.MouseButton.LeftButton)
        finally:
            store_mod.QMessageBox = original
        _wait(app, lambda: not (tmp / plugin.slug).exists())
        assert installed_plugins(tmp) == [], "плагин остался после удаления"
        assert not _cards(dlg._pinstalled_box), "карточка плагина осталась"

        # плагин из локальной папки: установка, запись источника и обновление «⟳»
        source = tmp.parent / "gui-local-plugin"
        (source / PLUGIN_DIR).mkdir(parents=True)
        (source / PLUGIN_DIR / PLUGIN_FILE).write_text(
            json.dumps({"name": "gui-local", "version": "1.0"}),
            encoding="utf-8",
        )
        (source / "skills" / "gui-skill").mkdir(parents=True)
        (source / "skills" / "gui-skill" / SKILL_FILE).write_text(
            LOCAL_SKILL.replace("local-skill", "gui-skill"), encoding="utf-8"
        )
        dlg._install_local_plugin(str(source))
        _wait(app, lambda: (tmp / "gui-local" / PLUGIN_DIR / PLUGIN_FILE).is_file())
        _wait(app, lambda: "gui-local" in dlg.status.text())
        assert dlg.plugin_tabs.currentIndex() == dlg.plugin_installed_tab
        # скилл из плагина — во второй группе «Скачанных», без своих «⟳»/«×»
        assert _headers(dlg._installed_box) == ["В составе плагинов (1)"], _headers(
            dlg._installed_box
        )
        member_cards = _cards(dlg._installed_box)
        assert member_cards, "скилл из плагина не попал в «Скачанные»"
        assert not [
            b
            for card in member_cards
            for b in card.findChildren(QPushButton)
            if b.objectName() in ("StoreUpdate", "StoreRemove")
        ], "у скилла из плагина появились свои «⟳»/«×»"
        local_cards = _cards(dlg._pinstalled_box)
        assert local_cards, "локальный плагин не попал в «Установленные»"
        local_plugin = next(p for p in installed_plugins(tmp) if p.folder == "gui-local")
        assert local_plugin.local == str(source.resolve()), local_plugin
        assert not local_plugin.tracked, "локальный плагин не должен тянуться с GitHub"
        assert "gui-local" in dlg._p_update_buttons, "у локального плагина нет «⟳»"

        (source / "skills" / "gui-skill" / SKILL_FILE).write_text(
            LOCAL_SKILL.replace("local-skill", "gui-skill").replace(
                "# Локальный", "# Локальный правленый"
            ),
            encoding="utf-8",
        )
        QTest.mouseClick(dlg._p_update_buttons["gui-local"], Qt.MouseButton.LeftButton)
        _wait(app, lambda: "обновлён" in dlg.status.text(), timeout=20)
        assert "правленый" in (
            tmp / "gui-local" / "skills" / "gui-skill" / SKILL_FILE
        ).read_text(encoding="utf-8")

        local_remove = next(
            b
            for card in _cards(dlg._pinstalled_box)
            for b in card.findChildren(QPushButton)
            if b.objectName() == "StoreRemove"
            and any("gui-local" == label.text() for label in card.findChildren(QLabel))
        )
        original = store_mod.QMessageBox
        store_mod.QMessageBox = _YesBox
        try:
            QTest.mouseClick(local_remove, Qt.MouseButton.LeftButton)
        finally:
            store_mod.QMessageBox = original
        _wait(app, lambda: not (tmp / "gui-local").exists())
        assert installed_plugins(tmp) == [], "локальный плагин остался после удаления"
        assert origin_of(tmp, "gui-local") is None
    finally:
        dlg.close()
        dlg.deleteLater()
    print(
        "ok  GUI: карточки, поиск, установка по «+» и из файла, обновление по «⟳», "
        "удаление по «×», «Скачанные», разделы «Скиллы»/«Плагины»"
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="nemus_store_"))
    try:
        test_catalog()
        test_search_and_recommended()
        test_install(tmp / "skills1")
        test_remove(tmp / "skills_rm")
        test_update(tmp / "skills_upd")
        test_install_guards(tmp / "skills2")
        test_pack(tmp / "skills_pack")
        test_plugins(tmp / "plugins")
        test_local_plugin(tmp / "local_plugins")
        test_local_skill(tmp / "local_skills")
        test_gui(tmp / "skills3")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nSKILL STORE TESTS PASSED")


if __name__ == "__main__":
    main()
