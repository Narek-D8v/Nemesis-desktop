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
_QUOTE_LINE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^:?-{3,}:?$")



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


def _table_cells(line: str) -> list[str]:
    match = _TABLE_ROW.match(line)
    if match is None:
        return []
    return [cell.strip() for cell in match.group(1).split("|")]


def _cell_align(cell: str) -> str | None:
    left = cell.startswith(":")
    right = cell.endswith(":")
    if left and right:
        return "center"
    if left:
        return "left"
    if right:
        return "right"
    return None


def _plain_cell(cell: str) -> bool:
    """В HTML-таблице Markdown не парсится — не теряем разметку ячеек."""
    return not any(marker in cell for marker in ("**", "__", "`", "[", "]", "<", "\\"))


def _compact_tables(text: str) -> str:
    """Простые GFM-таблицы превращает в компактные `<table>` Bot API 10.3.

    Сложные таблицы (ссылки, код, жирный текст) остаются Markdown: внутри
    HTML-блока они потеряли бы форматирование.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        header = _table_cells(lines[index])
        separator = _table_cells(lines[index + 1]) if index + 1 < len(lines) else []
        aligns = [_cell_align(cell) for cell in separator]
        valid = (
            len(header) >= 2
            and len(header) == len(separator)
            and all(_TABLE_SEP.match(cell) for cell in separator)
            and all(_plain_cell(cell) for cell in header)
        )
        if not valid:
            out.append(lines[index])
            index += 1
            continue
        rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines):
            cells = _table_cells(lines[cursor])
            if len(cells) != len(header):
                break
            if not all(_plain_cell(cell) for cell in cells):
                break
            rows.append(cells)
            cursor += 1
        if not rows:
            out.append(lines[index])
            index += 1
            continue

        def cell(
            tag: str, value: str, column: int, aligns: list[str | None] = aligns
        ) -> str:
            align = aligns[column]
            attr = f' align="{align}"' if align else ""
            return f"<{tag}{attr}>{value}</{tag}>"

        html_rows = [
            "<tr>"
            + "".join(cell("th", value, column) for column, value in enumerate(header))
            + "</tr>"
        ]
        html_rows.extend(
            "<tr>"
            + "".join(cell("td", value, column) for column, value in enumerate(row))
            + "</tr>"
            for row in rows
        )
        out.append(
            '<table bordered striped compact>'
            + "".join(html_rows)
            + "</table>"
        )
        index = cursor
    return "\n".join(out)


def _expandable_quotes(text: str) -> str:
    """Длинные цитаты сворачивает в expandable-blockquote Bot API 10.3.

    Короткие цитаты остаются обычным Markdown: внутри HTML-блока цитаты
    Markdown не парсится, поэтому порог нужен, чтобы не терять `**bold**`.
    """
    lines = text.split("\n")
    out: list[str] = []
    group: list[str] = []
    original: list[str] = []

    def flush() -> None:
        if not group:
            return
        content = "<br>".join(group).strip()
        if len(group) >= 4 or len(content) >= 240:
            out.append(f"<blockquote expandable>{content}</blockquote>")
        else:
            out.extend(original)
        group.clear()
        original.clear()

    for line in lines:
        match = _QUOTE_LINE.match(line)
        if match:
            group.append(match.group(1))
            original.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def prepare_rich_markdown(text: str) -> str:
    """Готовит Markdown к rich-сообщению, не пропуская HTML модели."""
    raw = text or ""
    if not raw:
        return ""
    prepared, stash = _extract_code(raw)
    prepared = prepared.replace("<", "&lt;")
    prepared = _compact_tables(prepared)
    prepared = _expandable_quotes(prepared)
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
