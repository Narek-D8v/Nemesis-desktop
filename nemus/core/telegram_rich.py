"""Безопасный rich Markdown и rich-кнопки для Bot API 10.3.

Ответ модели не должен уметь внедрять HTML: rich-сообщения понимают теги,
поэтому весь текст, кроме кода, экранируется. Наши кнопки добавляются уже
после очистки отдельной строкой ``<tg-button-row>``.

Модуль чистый: ни сети, ни Telegram, ни конфига — только строки и лимиты.
"""

from __future__ import annotations

import html
import re

#: предел rich-сообщения, символов UTF-8
RICH_LIMIT = 32768

#: данные callback-кнопки: 1–64 байта UTF-8
BUTTON_DATA_LIMIT = 64

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


class RichTextTooLong(ValueError):
    """Текст не помещается в одно rich-сообщение — нужен старый HTML-путь."""


def _extract_code(text: str) -> tuple[str, list[str]]:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    prepared = _FENCE.sub(
        lambda m: keep(f"```{m.group(1)}\n{m.group(2)}```"), text
    )
    prepared = _INLINE_CODE.sub(lambda m: keep(f"`{m.group(1)}`"), prepared)
    return prepared, stash


def _restore(text: str, stash: list[str]) -> str:
    return _PLACEHOLDER.sub(lambda m: stash[int(m.group(1))], text)


def prepare_rich_markdown(text: str) -> str:
    """Готовит Markdown к rich-сообщению, не пропуская HTML модели."""
    raw = text or ""
    if not raw:
        return ""
    prepared, stash = _extract_code(raw)
    prepared = prepared.replace("<", "&lt;")
    out = _restore(prepared, stash)
    if len(out) > RICH_LIMIT:
        raise RichTextTooLong(
            f"rich-сообщение длиннее {RICH_LIMIT} символов"
        )
    return out


def rich_button_rows(rows: list[list[tuple[str, str]]]) -> str:
    """Собирает безопасные rich-кнопки из пар «подпись, callback»."""
    blocks: list[str] = []
    index = 0
    for row in rows or []:
        buttons: list[str] = []
        for label, data in row:
            raw_data = str(data)
            size = len(raw_data.encode("utf-8"))
            if not 1 <= size <= BUTTON_DATA_LIMIT:
                raise ValueError(
                    f"callback_data должен быть 1–{BUTTON_DATA_LIMIT} байт UTF-8"
                )
            if index == 0:
                style = "primary"
            elif raw_data == "ux:help":
                style = "link"
            else:
                style = "secondary"
            safe_label = (
                str(label).replace("&", "&amp;").replace("<", "&lt;")
            )
            buttons.append(
                '<tg-button type="callback_data" '
                f'data="{html.escape(raw_data, quote=True)}" '
                f'style="{style}">{safe_label}'
                "</tg-button>"
            )
            index += 1
        if buttons:
            blocks.append(
                '<tg-button-row align="center">'
                + "".join(buttons)
                + "</tg-button-row>"
            )
    return "\n".join(blocks)


def build_rich_markdown(
    text: str, rows: list[list[tuple[str, str]]] | None = None
) -> str:
    """Безопасный текст плюс наши кнопки; лимит проверяется ещё раз."""
    out = prepare_rich_markdown(text)
    if rows:
        buttons = rich_button_rows(rows)
        out = f"{out}\n\n{buttons}" if out else buttons
        if len(out) > RICH_LIMIT:
            raise RichTextTooLong(
                f"rich-сообщение длиннее {RICH_LIMIT} символов"
            )
    return out


__all__ = [
    "BUTTON_DATA_LIMIT",
    "RICH_LIMIT",
    "RichTextTooLong",
    "build_rich_markdown",
    "prepare_rich_markdown",
    "rich_button_rows",
]
