"""Скиллы в формате Claude Agent Skills (``SKILL.md``).

Скилл — это папка с файлом ``SKILL.md``: YAML-frontmatter (``name``,
``description``) плюс пошаговая инструкция и любые вспомогательные файлы
(скрипты, шаблоны), которые лежат рядом. Формат совместим с Claude Code и
другими агентами: те же папки читаются из ``.agents/skills``.

Для модели скиллы отдаются «с прогрессивным раскрытием»: в системном промпте
только название и описание, полный текст — по запросу инструмента
``load_skill``. Новые скиллы создаются инструментом ``create_skill``.

Часть скиллов встроена и едет вместе с приложением (``nemus/skills``): они
работают сразу после установки, их можно отключить в настройках, а свой
скилл с тем же именем в ``.agents/skills`` перекрывает встроенный.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from nemus.config import PROJECT_ROOT
from nemus.utils.logger import logger

#: папка со скиллами (кросстульный стандарт, понимается и другими агентами)
DEFAULT_SKILLS_DIR = ".agents/skills"

#: встроенные скиллы: едут внутри пакета и работают сразу после установки;
#: одноимённый скилл из пользовательской папки их перекрывает
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

#: имя файла скилла
SKILL_FILE = "SKILL.md"

#: манифест установленного плагина (вместе с папкой плагина)
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"

#: папка команд плагина: ``commands/<имя>.md`` → ``/плагин:имя``
PLUGIN_COMMANDS_DIR = "commands"

#: YAML-frontmatter в начале файла: --- ... ---
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)[ \t]*---[ \t]*\n?", re.S)

#: имя скилла: строчные латиница/цифры и дефисы (требование формата)
SLUG_RE = re.compile(r"[^a-z0-9-]+")

#: транслитерация кириллицы — имена папок должны быть латиницей
_CYR: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "i", "є": "e", "ґ": "g",
}


def slugify(text: str) -> str:
    """Превращает название скилла в допустимое имя папки (``[a-z0-9-]``).

    «Работа с PDF» → ``rabota-s-pdf``. Пустой результат — ошибка:
    в имени должны быть буквы или цифры.
    """
    raw = (text or "").strip().lower().replace("ё", "е")
    out: list[str] = []
    for ch in raw:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch in _CYR:
            out.append(_CYR[ch])
        else:
            out.append("-")
    slug = SLUG_RE.sub("-", "".join(out))
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:64]
    if not slug:
        raise ValueError(
            "Название скилла должно содержать буквы или цифры — "
            "из него складывается имя папки."
        )
    return slug


@dataclass
class Skill:
    """Один найденный на диске скилл."""

    #: машинное имя (папка и поле ``name`` во frontmatter)
    name: str
    #: короткое описание — единственно, что попадает в системный промпт
    description: str
    #: папка скилла (в ней лежит SKILL.md и вспомогательные файлы)
    path: Path
    #: текст SKILL.md без frontmatter
    body: str = ""
    #: разрешённые инструменты из frontmatter (``allowed-tools``)
    allowed_tools: list[str] = field(default_factory=list)
    #: вспомогательные файлы рядом со SKILL.md (пути относительно папки)
    files: list[str] = field(default_factory=list)
    #: файл с текстом относительно ``path``: SKILL.md или команда плагина
    file: str = SKILL_FILE
    #: ``skill`` — обычный скилл, ``command`` — команда плагина
    kind: str = "skill"

    def read(self) -> str:
        """Полное содержимое файла (frontmatter + инструкция)."""
        return self.skill_file.read_text(encoding="utf-8")

    @property
    def skill_file(self) -> Path:
        """Файл с текстом скилла или команды."""
        return self.path / self.file


def skills_root(directory: str | Path | None = None) -> Path:
    """Абсолютная папка со скиллами (относительные пути — от корня проекта)."""
    raw = str(directory or DEFAULT_SKILLS_DIR).strip() or DEFAULT_SKILLS_DIR
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def parse_skill(text: str) -> tuple[dict[str, Any], str]:
    """Разбирает SKILL.md: возвращает (frontmatter, текст без него)."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError("нет YAML-frontmatter (файл должен начинаться с «---»)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"сломанный YAML во frontmatter: {exc}") from None
    if not isinstance(meta, dict):
        raise ValueError("frontmatter должен быть словарём «ключ: значение»")
    return meta, text[match.end():].strip()


def _helper_files(folder: Path) -> list[str]:
    """Вспомогательные файлы скилла (кроме SKILL.md), с относительными путями."""
    names: list[str] = []
    try:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.name != SKILL_FILE:
                names.append(path.relative_to(folder).as_posix())
    except OSError:
        return names
    return names


def _allowed_tools(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("allowed-tools", meta.get("allowed_tools", []))
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _scan_skills(root: Path, disabled: set[str]) -> list[Skill]:
    """Все скиллы одной папки на любой вложенности.

    Так работают наборы и плагины, положенные целиком:
    ``пакет/plugins/<плагин>/skills/<скилл>/SKILL.md`` тоже находится.
    Внутрь папки, где уже есть ``SKILL.md``, скан не спускается: это сам
    скилл и его файлы, а не скиллы внутри скилла. Скрытые папки и
    ``node_modules`` пропускаются, битые файлы — с предупреждением в лог.
    ``disabled`` — имена, которые не отдаются модели.
    """
    if not root.is_dir():
        return []
    skills: list[Skill] = []
    seen: set[str] = set()
    # любая вложенность — наборы и плагины кладут скиллы глубже
    candidates = sorted(
        path
        for path in root.rglob(SKILL_FILE)
        if not any(
            part.startswith(".") or part == "node_modules"
            for part in path.relative_to(root).parts[:-1]
        )
    )

    def inside_skill(path: Path) -> bool:
        """Папка скилла цельная: его примеры и вложенные документы — не скиллы."""
        folder = path.parent.parent
        while folder != root and root in folder.parents:
            if (folder / SKILL_FILE).is_file():
                return True
            folder = folder.parent
        return False

    candidates = [path for path in candidates if not inside_skill(path)]
    for skill_file in candidates:
        folder = skill_file.parent
        try:
            meta, body = parse_skill(skill_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"Скилл «{folder.name}» пропущен: {exc}")
            continue
        name = str(meta.get("name") or folder.name).strip() or folder.name
        if name.lower() in disabled or folder.name.lower() in disabled:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        description = str(meta.get("description") or "").strip()
        if not description:
            description = next(
                (ln.strip() for ln in body.splitlines() if ln.strip()), ""
            )
        skills.append(
            Skill(
                name=name,
                description=description[:300],
                path=folder,
                body=body,
                allowed_tools=_allowed_tools(meta),
                files=_helper_files(folder),
            )
        )
    return skills


def _command_body(body: str) -> str:
    """Поясняет шаблон команды: ``$ARGUMENTS`` — это задача пользователя."""
    if "$ARGUMENTS" not in body and "$1" not in body:
        return body
    return (
        f"{body}\n\n"
        "$ARGUMENTS (а также $1, $2, …) — это текст задачи из сообщения "
        "пользователя: подставь его сюда, а не пересказывай шаблон."
    )


def _scan_plugin_commands(root: Path, disabled: set[str]) -> list[Skill]:
    """Команды установленных плагинов — тоже ``/имя`` для Немуси.

    Плагин Claude Code кладёт их в ``commands/*.md``, а имя превращается в
    ``<плагин>:<команда>``: иначе ``/review`` из двух плагинов путался бы.
    Подпапки дают вложенное имя (``commands/git/commit.md`` → ``git:commit``).
    Агенты и хуки плагина не регистрируются: это его внутренняя механика.
    """
    if not root.is_dir():
        return []
    found: list[Skill] = []
    seen: set[str] = set()
    manifests = sorted(
        path
        for path in root.rglob(PLUGIN_MANIFEST)
        if not any(
            part.startswith(".") or part == "node_modules"
            for part in path.relative_to(root).parts[:-2]
        )
    )
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plugin_dir = manifest.parent.parent
        raw_name = data.get("name") if isinstance(data, dict) else ""
        raw_name = str(raw_name or plugin_dir.name).strip() or plugin_dir.name
        try:
            plugin = slugify(raw_name)
        except ValueError:
            plugin = plugin_dir.name.lower()
        commands_dir = plugin_dir / PLUGIN_COMMANDS_DIR
        if not commands_dir.is_dir():
            continue
        for path in sorted(commands_dir.rglob("*.md")):
            rel = path.relative_to(commands_dir)
            command = ":".join(rel.with_suffix("").parts)
            if not command:
                continue
            label = f"{plugin}:{command}"
            key = label.lower()
            short = command.split(":")[-1].lower()
            if key in seen or key in disabled or short in disabled:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(f"Команда плагина «{label}» пропущена: {exc}")
                continue
            try:
                meta, body = parse_skill(text)
            except ValueError:
                meta, body = {}, text.strip()
            description = str(meta.get("description") or "").strip()
            if not description:
                description = next(
                    (ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()),
                    "",
                )
            hint = str(
                meta.get("argument-hint", meta.get("argument_hint", "")) or ""
            ).strip()
            if hint:
                description = f"{description} ({hint})" if description else hint
            seen.add(key)
            found.append(
                Skill(
                    name=label,
                    description=description[:300],
                    path=commands_dir,
                    body=_command_body(body),
                    file=rel.as_posix(),
                    kind="command",
                )
            )
    return found


def load_skills(
    directory: str | Path | None = None,
    disabled: Iterable[str] = (),
    builtin: bool = True,
) -> list[Skill]:
    """Находит все скиллы ``<dir>/*/SKILL.md`` плюс встроенные (``nemus/skills``).

    Встроенные скиллы идут первыми; одноимённый скилл из пользовательской
    папки перекрывает встроенный (своя версия важнее поставленной).
    ``builtin=False`` — только пользовательская папка.
    """
    root = skills_root(directory)
    off = {str(item).strip().lower() for item in disabled if str(item).strip()}
    merged: dict[str, Skill] = {}
    if builtin and root.resolve() != BUILTIN_SKILLS_DIR.resolve():
        for skill in _scan_skills(BUILTIN_SKILLS_DIR, off):
            merged[skill.name.lower()] = skill
    for skill in _scan_skills(root, off):
        merged[skill.name.lower()] = skill
    # команды плагинов не должны перекрывать настоящие скиллы с тем же именем
    for command in _scan_plugin_commands(root, off):
        merged.setdefault(command.name.lower(), command)
    return sorted(merged.values(), key=lambda s: s.name.lower())


def skill_plugin(
    path: str | Path, directory: str | Path | None = None
) -> str:
    """Имя плагина, внутри которого лежит скилл (пусто — обычный скилл).

    Плагин ставится одной папкой: удалять или обновлять скилл изнутри неё
    нельзя — иначе пакет развалится. Поэтому магазин спрашивает название
    плагина и прячет у таких скиллов «⟳» и «×».
    """
    root = skills_root(directory).resolve()
    folder = Path(path).resolve()
    if folder != root and root not in folder.parents:
        return ""
    current = folder
    while True:
        manifest = current / PLUGIN_MANIFEST
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            name = data.get("name") if isinstance(data, dict) else ""
            return str(name or current.name).strip() or current.name
        if current == root or root not in current.parents:
            return ""
        current = current.parent


def get_skill(name: str, directory: str | Path | None = None,
              disabled: Iterable[str] = ()) -> Skill | None:
    """Ищет скилл по имени (без учёта регистра, по имени папки)."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for skill in load_skills(directory, disabled=disabled):
        if skill.name.lower() == wanted:
            return skill
        # папку сравниваем только у скиллов: у всех команд папка «commands»
        if skill.kind == "skill" and skill.path.name.lower() == wanted:
            return skill
    return None


def remove_skill(name: str, directory: str | Path | None = None) -> bool:
    """Удаляет папку пользовательского скилла целиком (встроенные не трогает).

    Возвращает ``True``, если скилл найден и папка удалена. Удаляем только
    внутри папки скиллов: символические ссылки и пути наружу отсекаются.
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return False
    base = skills_root(directory).resolve()
    for skill in load_skills(directory, builtin=False):
        if skill.kind != "skill":
            continue
        if skill.name.lower() != wanted and skill.path.name.lower() != wanted:
            continue
        folder = skill.path.resolve()
        if base not in folder.parents:
            logger.warning(f"Скилл «{name}» лежит вне папки скиллов — не удаляю")
            return False
        plugin = skill_plugin(folder, directory)
        if plugin:
            logger.warning(
                f"Скилл «{name}» — часть плагина «{plugin}»: удалять его отдельно нельзя"
            )
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()
    return False


def save_skill(
    name: str,
    description: str,
    content: str,
    directory: str | Path | None = None,
) -> Skill:
    """Создаёт (или перезаписывает) скилл на диске и возвращает его.

    Имя нормализуется в slug — оно же становится именем папки.
    """
    slug = slugify(name)
    desc = (description or "").strip()
    body = (content or "").strip()
    if not desc:
        raise ValueError("Нужно описание скилла (description): по нему модель "
                         "понимает, когда его применять.")
    if not body:
        raise ValueError("Скилл пуст — нужен текст инструкции.")

    folder = skills_root(directory) / slug
    folder.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(
        {"name": slug, "description": desc},
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    (folder / SKILL_FILE).write_text(
        f"---\n{front}\n---\n\n{body}\n", encoding="utf-8"
    )
    logger.info(f"Скилл «{slug}» сохранён: {folder}")
    return Skill(
        name=slug,
        description=desc,
        path=folder,
        body=body,
        files=_helper_files(folder),
    )


#: заголовок markdown в инструкции скилла
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")


def skill_highlights(body: str, limit: int = 8) -> list[str]:
    """Заголовки инструкции — краткий состав того, что умеет скилл.

    Заголовки внутри блоков кода не считаются; заголовок-название документа
    (первый H1) пропускается — он дублирует имя скилла.
    """
    items: list[str] = []
    in_fence = False
    seen_title = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if len(match.group(1)) == 1 and not seen_title:
            seen_title = True
            continue
        title = match.group(2).strip()
        if title and title not in items:
            items.append(title)
        if len(items) >= limit:
            break
    return items


#: Подсказки для детерминированного выбора скилла: русские и английские
#: корни, которые встречаются в реальных просьбах. Скилл с описанием на
#: английском иначе не находится по запросу «сделай презентацию».
SKILL_TRIGGERS: dict[str, tuple[str, ...]] = {
    "pptx-design-styles": (
        "презентац", "слайд", "колода", "powerpoint", "pptx", "deck", "slide",
    ),
    "slideshow": ("слайдшоу", "slideshow", "слайд-шоу"),
    "superimage-generator": (
        "нарису", "картин", "изображен", "рисунок", "чиби", "chibi",
        "аватар", "портрет", "стилиз", "image", "art", "фото",
    ),
    "frontend-design": (
        "сайт", "лендинг", "интерфейс", "фронтенд", "frontend", "html",
        "css", "react", "веб-страниц", "web page",
    ),
    "hyperframes": (
        "видео", "ролик", "клип", "анимац", "титр", "video", "motion",
    ),
    "brainstorming": (
        "брейншторм", "мозговой штурм", "штурм идей", "brainstorm", "придумай идеи",
    ),
    "algorithmic-art": ("генеративн", "algorithmic", "p5.js", "арт-объект"),
    "canvas-design": ("постер", "плакат", "афиш", "обложк", "canvas"),
    "brand-guidelines": ("бренд", "фирменн", "brand"),
    "pdf-reader": ("pdf", "пдф"),
    "zip-handler": ("zip", "архив"),
    "mcp-builder": ("mcp", "mcp-сервер"),
    "skill-creator": ("создай скилл", "новый скилл", "skill"),
}


def match_skills(
    query: str, skills: Iterable[Skill], limit: int = 2
) -> list[Skill]:
    """Выбирает скиллы под запрос по ключевым словам (без участия модели).

    Это страховка от «модель не догадалась вызвать load_skill»: для явных тем
    (презентация, картинка, видео) инструкция подгружается в промпт сразу.
    Совпадения по имени скилла и по триггерам складываются; возвращаются
    лучшие ``limit`` штук без слабых совпадений.
    """
    text = (query or "").lower().replace("ё", "е")
    if not text.strip():
        return []
    scored: list[tuple[int, Skill]] = []
    for skill in skills:
        name = skill.name.lower()
        score = 0
        if name in text:
            score += 10
        for trigger in SKILL_TRIGGERS.get(name, ()):
            if trigger in text:
                score += 5
                break
        if score:
            scored.append((score, skill))
    # при равном счёте — короткое имя стабильнее, затем алфавит
    scored.sort(key=lambda item: (-item[0], len(item[1].name), item[1].name))
    return [skill for _score, skill in scored[: max(0, limit)]]


def explicit_skill(query: str, skills: Iterable[Skill]) -> Skill | None:
    """Скилл из команды ``/имя`` в начале сообщения (или ``None``)."""
    text = (query or "").lstrip()
    if not text.startswith("/"):
        return None
    command = text[1:].split(maxsplit=1)[0].strip().lower()
    if not command:
        return None
    for skill in skills:
        if skill.name.lower() == command:
            return skill
        if skill.kind == "skill" and skill.path.name.lower() == command:
            return skill
    return None


def format_skills_block(
    skills: list[Skill], directory: str | Path | None = None
) -> str:
    """Блок для системного промпта: краткие описания + как ими пользоваться."""
    if not skills:
        return ""
    root = skills_root(directory)
    commands = [skill for skill in skills if skill.kind == "command"]
    lines = [
        f"Скиллы (формат Claude Agent Skills, папка {root}, плюс встроенные): "
        f"{len(skills) - len(commands)} шт.",
    ]
    if commands:
        lines.append(
            f"Команды установленных плагинов (вызов тот же — /имя): "
            f"{len(commands)} шт."
        )
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description or 'без описания'}")
    lines.append(
        "Список исчерпывающий и обновляется при каждом запросе: скиллов, которых "
        "здесь нет, не существует. Когда задача попадает под описание скилла — "
        "сначала вызови load_skill(имя): там полная инструкция и вспомогательные "
        "файлы (пути к ним есть в ответе). Если под задачу подходят несколько "
        "скиллов (одна тема или смежные темы) — загрузи ВСЕ их до начала работы "
        "и применяй совместно. Если подходящего скилла нет, но задача будет "
        "повторяться — сохрани свой через create_skill."
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "BUILTIN_SKILLS_DIR",
    "DEFAULT_SKILLS_DIR",
    "PLUGIN_COMMANDS_DIR",
    "PLUGIN_MANIFEST",
    "SKILL_FILE",
    "Skill",
    "SKILL_TRIGGERS",
    "explicit_skill",
    "format_skills_block",
    "get_skill",
    "match_skills",
    "load_skills",
    "parse_skill",
    "save_skill",
    "skill_highlights",
    "skill_plugin",
    "skills_root",
    "slugify",
]
