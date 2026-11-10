"""Форматирование ответов для Telegram: Markdown → HTML.

Telegram понимает ограниченный набор HTML-тегов, поэтому ответ модели
переводится в них: ``**жирный**`` → ``<b>``, ``> цитата`` → ``<blockquote>``,
`` `код` `` → ``<code>``, ``` ```блок``` ``` → ``<pre>``, ``||тайна||`` →
``<tg-spoiler>``, ``[текст](url)`` → ``<a>``. Всё остальное экранируется,
чтобы модель не сломала сообщение случайным ``<`` или ``&``.

Длинный ответ режется на части заранее, до форматирования: так теги не
разрываются между сообщениями.
"""

from __future__ import annotations

import html
import re

#: предел одного сообщения Telegram
TEXT_LIMIT = 4000
#: с запасом под HTML-теги исходный кусок берём короче
CHUNK_LIMIT = 3200

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_UNDERLINE = re.compile(r"(?<!_)__(?!_)(.+?)(?<!_)__(?!_)", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_SPOILER = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def split_message(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Режет длинный текст на сообщения, не разрывая слова и строки."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def _href(url: str) -> str:
    """URL для атрибута href: кавычки не должны закрыть тег.

    Текст к этому моменту уже экранирован, поэтому ``&`` повторно не трогаем.
    """
    return url.replace('"', "%22")


def _fence_html(match: re.Match[str]) -> str:
    lang = (match.group(1) or "").strip()
    # перевод строки перед закрывающими ``` — часть разметки, а не кода
    code = html.escape(match.group(2).rstrip("\n"), quote=False)
    cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
    return f"<pre><code{cls}>{code}</code></pre>"


def _blockquotes(text: str) -> str:
    """Строки ``&gt; …`` собирает в один Telegram-цитатный блок."""
    lines = text.split("\n")
    out: list[str] = []
    quoted: list[str] = []

    def flush() -> None:
        if quoted:
            out.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            quoted.clear()

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("&gt;"):
            content = stripped[len("&gt;") :]
            if content.startswith(" "):
                content = content[1:]
            quoted.append(content)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def _tables(text: str) -> str:
    """Подряд идущие строки таблицы — моноширинным блоком (ровные столбцы)."""
    lines = text.split("\n")
    out: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if len(table) >= 2:
            out.append("<pre>" + "\n".join(table) + "</pre>")
        else:
            out.extend(table)
        table.clear()

    for line in lines:
        if _TABLE_LINE.match(line):
            table.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def to_html(text: str) -> str:
    """Переводит Markdown-ответ в HTML, который понимает Telegram."""
    if not text:
        return ""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # код вынимаем до экранирования: внутри него разметка не работает
    prepared = _FENCE.sub(lambda m: keep(_fence_html(m)), text)
    prepared = _INLINE_CODE.sub(
        lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"),
        prepared,
    )

    out = html.escape(prepared, quote=False)
    out = _LINK.sub(
        lambda m: f'<a href="{_href(m.group(2))}">{m.group(1)}</a>', out
    )
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _UNDERLINE.sub(r"<u>\1</u>", out)
    out = _STRIKE.sub(r"<s>\1</s>", out)
    out = _SPOILER.sub(r"<tg-spoiler>\1</tg-spoiler>", out)
    out = _ITALIC_STAR.sub(r"<i>\1</i>", out)
    out = _ITALIC_UNDER.sub(r"<i>\1</i>", out)
    out = _HEADING.sub(r"<b>\1</b>", out)
    out = _blockquotes(out)
    out = _tables(out)
    return _PLACEHOLDER.sub(lambda m: stash[int(m.group(1))], out)


def strip_html(text: str) -> str:
    """Убирает теги — запасной вариант, если Telegram не принял разметку."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def html_chunks(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Готовые HTML-сообщения: режем исходник, потом форматируем каждый кусок.

    Если после разметки кусок всё же длиннее лимита Telegram, отдаём его
    обычным текстом — лучше без форматирования, чем с оборванным тегом.
    """
    chunks: list[str] = []
    for raw in split_message(text, limit=limit):
        formatted = to_html(raw)
        chunks.append(formatted if len(formatted) <= TEXT_LIMIT else raw)
    return chunks


__all__ = [
    "CHUNK_LIMIT",
    "TEXT_LIMIT",
    "html_chunks",
    "split_message",
    "strip_html",
    "to_html",
]
