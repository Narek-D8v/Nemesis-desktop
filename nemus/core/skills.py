"""Скиллы в формате Claude Agent Skills (``SKILL.md``).

Скилл — это папка с файлом ``SKILL.md``: YAML-frontmatter (``name``,
``description``) плюс пошаговая инструкция и любые вспомогательные файлы
(скрипты, шаблоны), которые лежат рядом. Формат совместим с Claude Code и
другими агентами: те же папки читаются из ``.agents/skills``.

Для модели скиллы отдаются «с прогрессивным раскрытием»: в системном промпте
только название и описание, полный текст — по запросу инструмента
``load_skill``. Новые скиллы создаются инструментом ``create_skill``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from nemus.config import PROJECT_ROOT
from nemus.utils.logger import logger

#: папка со скиллами (кросстульный стандарт, понимается и другими агентами)
DEFAULT_SKILLS_DIR = ".agents/skills"

#: имя файла скилла
SKILL_FILE = "SKILL.md"

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

    def read(self) -> str:
        """Полное содержимое SKILL.md (frontmatter + инструкция)."""
        return self.skill_file.read_text(encoding="utf-8")

    @property
    def skill_file(self) -> Path:
        return self.path / SKILL_FILE


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


def load_skills(
    directory: str | Path | None = None, disabled: Iterable[str] = ()
) -> list[Skill]:
    """Находит все скиллы ``<dir>/*/SKILL.md``.

    Битые файлы пропускаются с предупреждением в лог. ``disabled`` — имена,
    которые не отдаются модели (отключение в настройках).
    """
    root = skills_root(directory)
    if not root.is_dir():
        return []
    off = {str(item).strip().lower() for item in disabled if str(item).strip()}
    skills: list[Skill] = []
    seen: set[str] = set()
    # первый уровень (<папка>/SKILL.md) и вложенный (например
    # hyperframes/skills/<имя>/SKILL.md — репозитории со скиллами,
    # положенные в .agents/skills целиком)
    candidates = sorted(root.glob(f"*/{SKILL_FILE}")) + sorted(
        root.glob(f"*/*/*/{SKILL_FILE}")
    )
    for skill_file in candidates:
        rel = skill_file.relative_to(root)
        # скрытые копии (.claude/skills/…, .agents/skills/…) не дублируем
        if any(
            part.startswith(".") or part == "node_modules"
            for part in rel.parts[:-1]
        ):
            continue
        folder = skill_file.parent
        try:
            meta, body = parse_skill(skill_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"Скилл «{folder.name}» пропущен: {exc}")
            continue
        name = str(meta.get("name") or folder.name).strip() or folder.name
        if name.lower() in off or folder.name.lower() in off:
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
    skills.sort(key=lambda s: s.name.lower())
    return skills


def get_skill(name: str, directory: str | Path | None = None,
              disabled: Iterable[str] = ()) -> Skill | None:
    """Ищет скилл по имени (без учёта регистра, по имени папки)."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for skill in load_skills(directory, disabled=disabled):
        if skill.name.lower() == wanted or skill.path.name.lower() == wanted:
            return skill
    return None


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


def format_skills_block(
    skills: list[Skill], directory: str | Path | None = None
) -> str:
    """Блок для системного промпта: краткие описания + как ими пользоваться."""
    if not skills:
        return ""
    root = skills_root(directory)
    lines = [
        f"Скиллы (формат Claude Agent Skills, папка {root}): {len(skills)} шт.",
    ]
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
    "DEFAULT_SKILLS_DIR",
    "SKILL_FILE",
    "Skill",
    "format_skills_block",
    "get_skill",
    "load_skills",
    "parse_skill",
    "save_skill",
    "skills_root",
    "slugify",
]
