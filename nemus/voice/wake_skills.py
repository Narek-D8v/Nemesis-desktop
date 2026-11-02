"""Навыки слова активации: короткие фразы → вызов инструмента.

Пользователь говорит «Немуся, который час» — локальный STT распознаёт фразу,
она сопоставляется с навыком из ``config.yaml`` (``voice.wake_word.skills``),
и инструмент выполняется сразу, без обращения к LLM. Если совпадения нет,
команда уходит обычному агенту.

Такой путь работает, когда диалог реального времени выключен или недоступен
(``wake_activates: false`` / офлайн-режим). В Realtime-сессии инструменты
вызывает сама модель.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nemus.utils.logger import logger

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class WakeSkill:
    """Один навык: набор фраз и инструмент, который нужно вызвать."""

    name: str
    phrases: tuple[str, ...]
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


def normalize_phrase(text: str) -> str:
    """Приводит фразу к сравнимому виду: регистр, ё→е, пунктуация, пробелы."""
    lowered = (text or "").strip().lower().replace("ё", "е")
    cleaned = _PUNCT.sub(" ", lowered)
    return _SPACES.sub(" ", cleaned).strip()


def build_skills(wake_word_config: Any) -> list[WakeSkill]:
    """Собирает навыки из секции ``voice.wake_word`` конфигурации."""
    skills: list[WakeSkill] = []
    seen: set[str] = set()
    for item in getattr(wake_word_config, "skills", []) or []:
        phrases = tuple(
            phrase for phrase in (normalize_phrase(p) for p in item.phrases) if phrase
        )
        tool = (item.tool or "").strip()
        if not phrases or not tool:
            logger.warning(f"Навык wake word пропущен (нет фраз или tool): {item}")
            continue
        name = (item.name or tool).strip()
        if name in seen:
            name = f"{name}_{len(skills) + 1}"
        seen.add(name)
        skills.append(
            WakeSkill(
                name=name,
                phrases=phrases,
                tool=tool,
                arguments=dict(item.arguments or {}),
            )
        )
    return skills


def match_skill(text: str, skills: list[WakeSkill]) -> WakeSkill | None:
    """Ищет навык по вхождению фразы в распознанный текст."""
    heard = normalize_phrase(text)
    if not heard:
        return None
    for skill in skills:
        if any(phrase in heard for phrase in skill.phrases):
            return skill
    return None
