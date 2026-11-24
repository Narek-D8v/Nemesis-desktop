"""Разбор брейншторма на варианты A/B/C для inline-кнопок Telegram.

Модель просят писать вопросы в строгом виде:

    1. Какого формата сделать видео?
    A) горизонтальный (рекомендую)
    B) вертикальный
    C) квадратный
    D) Свой вариант: _________

Здесь этот текст превращается в группы выборов. Telegram показывает их по
одному: ответ на каждый вопрос сразу открывает следующий. «Свой вариант»
не становится готовым ответом — бот просит пользователя ввести текст.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_OPTION_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\d+\s*[.)]\s*)?\**"
    r"([A-Da-dА-Га-г])\**\s*[).:]\s+(.+)$"
)
_HEADING_RE = re.compile(r"^\s*\**\s*\d+\s*[).:]\s*(.+?)\s*\**\s*$")
_GRILLING_QUESTION_RE = re.compile(
    r"^\s*❓\s*\**\s*Q\s*(\d+)\s*\**\s*[-–—:.]?\s*(.+)$",
    re.IGNORECASE,
)
_GRILLING_RECOMMEND_RE = re.compile(r"^\s*➡️\s*(.+)$")
_CUSTOM_RE = re.compile(
    r"свой\s+вариант|сво[йя]\s+вариант|другой\s+вариант|"
    r"custom|other|^_+$",
    re.IGNORECASE,
)


@dataclass
class ChoiceOption:
    label: str
    text: str
    custom: bool = False


@dataclass
class ChoiceGroup:
    title: str
    options: list[ChoiceOption] = field(default_factory=list)


def parse_choices(text: str) -> list[ChoiceGroup]:
    """Возвращает 2+ группы вопросов с вариантами или пустой список.

    Одна группа — это обычный вопрос, а не брейншторм: кнопки для неё не
    нужны. Вариант «Свой вариант» остаётся в группе с флагом ``custom``,
    чтобы бот мог попросить текст ответа вместо простого нажатия.
    """
    groups: list[ChoiceGroup] = []
    current: ChoiceGroup | None = None
    heading = ""

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _OPTION_RE.match(line)
        if match:
            label = match.group(1).upper()
            option_text = match.group(2).strip().strip("*_ ")
            is_custom = bool(_CUSTOM_RE.search(option_text))
            if current is None:
                current = ChoiceGroup(heading or f"Вопрос {len(groups) + 1}")
            elif label == "A" or any(
                option.label == label for option in current.options
            ):
                groups.append(current)
                current = ChoiceGroup(heading or f"Вопрос {len(groups) + 1}")
            current.options.append(
                ChoiceOption(
                    label,
                    "Свой вариант" if is_custom else option_text,
                    custom=is_custom,
                )
            )
            heading = ""
            continue
        # заголовок следующего вопроса; строку «Ответь коротко…» не берём
        if "ответь" in line.lower():
            continue
        title = _HEADING_RE.sub(r"\1", line).strip().rstrip(":：")
        if title:
            heading = title[:120]

    if current is not None:
        groups.append(current)

    useful = [group for group in groups if len(group.options) >= 2]
    if len(useful) < 2:
        return []
    return useful


def parse_grilling_choices(text: str) -> list[ChoiceGroup]:
    """Страховка от формата «❓ Q1 … ➡️ рекомендация» из чужих скиллов.

    Некоторые установленные скиллы учат отвечать раундом `❓ Q1` и своей
    рекомендацией вместо вариантов A/B/C. В Telegram такой ответ нельзя
    показать кнопками, поэтому рекомендованный ответ становится вариантом A,
    а «Свой вариант» — вариантом D. Это запасной путь: штатный формат —
    обычные вопросы с A/B/C/D.
    """
    groups: list[ChoiceGroup] = []
    current: ChoiceGroup | None = None

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        question = _GRILLING_QUESTION_RE.match(line)
        if question:
            header = question.group(2).replace("**", "").strip()
            title = header
            parts = re.split(r"[:：]", header, maxsplit=1)
            if len(parts) == 2:
                body = parts[1].strip()
                # в формате «Q1 - Тема: Какой …?» настоящий вопрос — после двоеточия
                if body.endswith(("?", "？")):
                    title = body
            current = ChoiceGroup(title.rstrip(":：").strip()[:200])
            groups.append(current)
            continue
        recommend = _GRILLING_RECOMMEND_RE.match(line)
        if recommend and current is not None:
            answer = recommend.group(1).replace("**", "").strip()
            if answer:
                if "рекомендую" not in answer.lower():
                    answer += " (рекомендую)"
                current.options.append(ChoiceOption("A", answer))

    for group in groups:
        if any(option.label == "A" for option in group.options):
            group.options.append(
                ChoiceOption("D", "Свой вариант", custom=True)
            )

    useful = [group for group in groups if group.options]
    return useful if len(useful) >= 2 else []


def build_answer(
    groups: list[ChoiceGroup], selected: dict[int, int]
) -> str:
    """Собирает короткий ответ «1A, 2B» по выбранным кнопкам."""
    parts: list[str] = []
    for index, group in enumerate(groups):
        option_index = selected.get(index)
        if option_index is None or not (0 <= option_index < len(group.options)):
            continue
        parts.append(f"{index + 1}{group.options[option_index].label}")
    return ", ".join(parts)


def choice_lines(
    groups: list[ChoiceGroup],
    selected: dict[int, int],
    custom: dict[int, str] | None = None,
) -> list[str]:
    """Нумерованные ответы с полным текстом вариантов для сводки."""
    custom = custom or {}
    lines: list[str] = []
    for index, group in enumerate(groups):
        option_index = selected.get(index)
        if option_index is None or not (0 <= option_index < len(group.options)):
            continue
        option = group.options[option_index]
        if option.custom:
            answer = custom.get(index, "").strip() or option.text
            value = f"{option.label}) {answer}"
        else:
            value = f"{option.label}) {option.text}"
        sep = "" if group.title.endswith(("?", "!", ".")) else ":"
        lines.append(f"{index + 1}. {group.title}{sep} {value}")
    return lines


def build_answers(
    groups: list[ChoiceGroup],
    selected: dict[int, int],
    custom: dict[int, str] | None = None,
) -> str:
    """Сводка ответов, которую бот отправляет агенту после опроса."""
    lines = choice_lines(groups, selected, custom)
    short = build_answer(groups, selected)
    return "\n".join(
        [
            "Ответы пользователя на вопросы брейншторма:",
            f"Выбор: {short}" if short else "",
            "",
            *lines,
            "",
            "Если критичных данных не хватает — задай следующий раунд, не "
            "выдумывай. Если всё есть — приступай к работе по этим ответам.",
        ]
    ).strip()
