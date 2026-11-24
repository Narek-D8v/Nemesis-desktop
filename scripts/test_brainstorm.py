"""Проверка разбора брейншторма на inline-кнопки Telegram."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.core.brainstorm import (  # noqa: E402
    build_answer,
    build_answers,
    choice_lines,
    parse_choices,
    parse_grilling_choices,
)

SAMPLE = """Поняла задачу: видео о блинах.
Перед стартом уточню ключевые параметры:
1. Какого формата сделать видео?
A) Горизонтальный 16:9 (рекомендую)
B) Вертикальный 9:16
C) Квадратный 1:1
D) Свой вариант: _________

2. Какой движок использовать?
A) HyperFrames (рекомендую)
B) Remotion
C) Сравнить оба

3. Какая структура у ролика?
A) Полный туториал (рекомендую)
B) Быстрый обзор
D) Свой вариант: _________

Ответь коротко: «1A, 2A, 3A» или «всё A».
"""


def main() -> int:
    groups = parse_choices(SAMPLE)
    assert len(groups) == 3, groups
    assert groups[0].title == "Какого формата сделать видео?", groups[0].title
    assert [option.label for option in groups[0].options] == ["A", "B", "C", "D"]
    assert groups[0].options[-1].custom is True
    assert groups[0].options[-1].text == "Свой вариант"
    assert groups[1].title == "Какой движок использовать?", groups[1].title
    assert [option.custom for option in groups[1].options] == [False] * 3

    answer = build_answer(groups, {0: 0, 1: 2, 2: 0})
    assert answer == "1A, 2C, 3A", answer

    custom_groups = parse_choices(SAMPLE)
    selected = {0: 3, 1: 1, 2: 0}
    custom = {0: "Вертикальный 4:5 для Telegram"}
    lines = choice_lines(custom_groups, selected, custom)
    assert lines[0].endswith("D) Вертикальный 4:5 для Telegram"), lines[0]
    text = build_answers(custom_groups, selected, custom)
    assert "Выбор: 1D, 2B, 3A" in text, text
    assert "Вертикальный 4:5 для Telegram" in text, text
    assert "приступай к работе" in text, text

    assert parse_choices("Один вопрос:\nA) да\nB) нет") == []

    grilling = """❓ Q1 - Главный месседж и тон видео: Какой эмоциональный и содержательный акцент мы делаем — пугающая медицинская статистика или вдохновляющий рассказ?
➡️ Фокус на наглядной медицинской статистике и фактах о восстановлении.

❓ Q2 - Целевая аудитория и язык: На кого рассчитан ролик — на подростков или на курильщиков со стажем?
➡️ Универсальная аудитория с акцентом на научные факты.

❓ Q3 - Технический движок: Какую технологию рендеринга используем — HyperFrames или Remotion?
➡️ HyperFrames.
"""
    fallback = parse_grilling_choices(grilling)
    assert len(fallback) == 3, fallback
    assert fallback[0].title.startswith("Какой эмоциональный"), fallback[0].title
    assert fallback[0].title.endswith("?"), fallback[0].title
    assert [option.label for option in fallback[0].options] == ["A", "D"]
    assert "рекомендую" in fallback[0].options[0].text, fallback[0].options[0]
    assert fallback[0].options[1].custom is True
    print(
        "ok  брейншторм: 3 вопроса, свой вариант, ответ 1D, 2B, 3A; "
        "запасной разбор «❓ Q1»"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
