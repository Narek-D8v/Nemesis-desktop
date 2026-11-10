"""Команды из скиллов: ``/имя-скилла`` для поля ввода промпта.

Каждый скилл — это команда ``/имя``: набрав ``/`` в поле ввода, пользователь
видит список скиллов с описаниями и подставляет нужный. Такие же ссылки
``/hyperframes-cli``, ``/media-use`` встречаются в самих инструкциях — это и
есть команды скиллов, отдельного реестра не нужно: источник один, папка
скиллов.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from nemus.core.skills import Skill

#: с этого символа начинаются команды скиллов
SLASH_PREFIX = "/"

#: сколько команд собирать со всех скиллов (попап всё равно короткий)
MAX_COMMANDS = 300


@dataclass(frozen=True)
class SkillCommand:
    """Одна команда-подсказка: что вставить в поле ввода и откуда она."""

    #: текст, который подставляется в поле ввода
    text: str
    #: короткое название в списке подсказок
    title: str
    #: пояснение (описание скилла)
    description: str
    #: имя скилла-источника (без слэша)
    skill: str
    #: ``skill`` — команда вызова скилла
    kind: str = "skill"

    @property
    def name(self) -> str:
        """Имя команды без слэша: ``/code-craft`` → ``code-craft``."""
        return self.title.lstrip(SLASH_PREFIX)


def parse_skill_commands(
    skills: Iterable[Skill], limit: int = MAX_COMMANDS
) -> list[SkillCommand]:
    """Собирает команды ``/имя`` из скиллов (по одной на скилл, без дублей).

    Команды идут в порядке имён, а не установки: так в попапе они читаются
    предсказуемо. ``limit`` защищает от папки с сотнями скиллов.
    """
    commands: list[SkillCommand] = []
    seen: set[str] = set()
    for skill in sorted(skills, key=lambda item: item.name.lower()):
        name = (skill.name or "").strip().lstrip(SLASH_PREFIX)
        if not name:
            continue
        title = f"{SLASH_PREFIX}{name}"
        if title.lower() in seen:
            continue
        seen.add(title.lower())
        commands.append(
            SkillCommand(
                text=f"{title} ",
                title=title,
                description=skill.description or "",
                skill=name,
                kind=skill.kind,
            )
        )
        if len(commands) >= limit:
            break
    return commands


__all__ = ["MAX_COMMANDS", "SLASH_PREFIX", "SkillCommand", "parse_skill_commands"]
