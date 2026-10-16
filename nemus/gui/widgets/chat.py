"""История диалога: карточки с аватарами, разметка, стриминг."""

from __future__ import annotations

import html
import os
import re
import tempfile

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QTextBrowser, QWidget

from nemus.gui import i18n
from nemus.gui.theme import (
    ACCENT_DANGER,
    ACCENT_PRIMARY,
    ACCENT_PRIMARY_DIM,
    ACCENT_SECONDARY,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    BG,
    BG_ACTIVE,
    BG_ELEVATED,
    BORDER,
    FONT_FAMILY,
    FONT_FAMILY_MONO,
    TEXT,
    TEXT_DIM,
)
from nemus.gui.theme import (
    BG_ELEVATED as _CODE_BG,
)

_BLOCK_END = "</div>"

_AVATAR_SIZE = 34
_AVATAR_CACHE: dict[str, str] = {}


def _avatar_path(letter: str, color_a: str, color_b: str) -> str:
    """Круглый аватар с буквой (генерируется один раз во временный файл)."""
    key = f"{letter}_{color_a}_{color_b}".replace("#", "")
    path = _AVATAR_CACHE.get(key)
    if path and os.path.exists(path):
        return path

    path = os.path.join(tempfile.gettempdir(), f"nemus_av_{key}.png")
    try:
        pm = QPixmap(_AVATAR_SIZE, _AVATAR_SIZE)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, _AVATAR_SIZE, _AVATAR_SIZE)
        grad.setColorAt(0, QColor(color_a))
        grad.setColorAt(1, QColor(color_b))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawEllipse(0, 0, _AVATAR_SIZE, _AVATAR_SIZE)
        p.setPen(QPen(QColor("#0a0d14")))
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        p.setFont(font)
        p.drawText(
            0, 0, _AVATAR_SIZE, _AVATAR_SIZE,
            Qt.AlignmentFlag.AlignCenter,
            letter.upper(),
        )
        p.end()
        pm.save(path, "PNG")
    except Exception:  # noqa: BLE001 — без QApplication картинка не создастся
        pass
    _AVATAR_CACHE[key] = path
    return path


def _avatar_html(letter: str, color_a: str, color_b: str) -> str:
    """HTML-тег аватара (локальный файл, Qt грузит его в документ)."""
    path = _avatar_path(letter, color_a, color_b).replace("\\", "/")
    src = QUrl.fromLocalFile(path).toString()
    return (
        f'<img src="{src}" width="{_AVATAR_SIZE}" height="{_AVATAR_SIZE}" '
        f'style="border-radius: {_AVATAR_SIZE // 2}px;"/>'
    )


def render_markdown(text: str) -> str:
    """Преобразует markdown в HTML: код, заголовки, курсив, ссылки."""
    escaped = html.escape(text)

    # Блоки кода — изымаем до прочей разметки, чтобы не ломать их символы
    code_blocks: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    escaped = re.sub(r"```[^\n]*\n(.*?)```", _stash, escaped, flags=re.DOTALL)
    escaped = re.sub(r"```(.*?)```", _stash, escaped, flags=re.DOTALL)

    # Заголовки
    escaped = re.sub(
        r"^### (.+)$",
        rf'<h3 style="color: {ACCENT_PRIMARY}; font-size: 15px; '
        rf'margin: 6px 0 4px 0;">\1</h3>',
        escaped,
        flags=re.MULTILINE,
    )
    escaped = re.sub(
        r"^## (.+)$",
        rf'<h2 style="color: {ACCENT_PRIMARY}; font-size: 17px; '
        rf'margin: 8px 0 4px 0;">\1</h2>',
        escaped,
        flags=re.MULTILINE,
    )
    escaped = re.sub(
        r"^# (.+)$",
        rf'<h1 style="color: {ACCENT_PRIMARY}; font-size: 19px; '
        rf'margin: 8px 0 6px 0;">\1</h1>',
        escaped,
        flags=re.MULTILINE,
    )

    # Жирный текст
    escaped = re.sub(
        r"\*\*(.+?)\*\*",
        rf'<b style="color: {ACCENT_PRIMARY}; font-weight: 700;">\1</b>',
        escaped,
    )
    escaped = re.sub(
        r"__(.+?)__",
        rf'<b style="color: {ACCENT_PRIMARY}; font-weight: 700;">\1</b>',
        escaped,
    )

    # Курсив (после жирного, чтобы не срезать **)
    escaped = re.sub(
        r"\*([^*\n]+)\*",
        rf'<i style="color: {TEXT_DIM};">\1</i>',
        escaped,
    )
    escaped = re.sub(
        r"(?<![\w])_([^_\n]+)_(?![\w])",
        rf'<i style="color: {TEXT_DIM};">\1</i>',
        escaped,
    )

    # Код в строке
    escaped = re.sub(
        r"`([^`]+)`",
        rf'<code style="background: {_CODE_BG}; color: {ACCENT_PRIMARY}; '
        rf"padding: 2px 6px; border-radius: 4px; font-family: {FONT_FAMILY_MONO}; "
        rf'font-size: 12px;">\1</code>',
        escaped,
    )

    # Ссылки
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^\)]+)\)",
        rf'<a href="\2" style="color: {ACCENT_PRIMARY}; text-decoration: none; '
        rf'border-bottom: 1px solid {ACCENT_PRIMARY_DIM};">\1</a>',
        escaped,
    )

    # Возвращаем блоки кода
    def _restore(m: re.Match[str]) -> str:
        code = code_blocks[int(m.group(1))]
        return (
            f'<pre style="background: {_CODE_BG}; color: {TEXT_DIM}; '
            f"padding: 12px; border-radius: 8px; border-left: 3px solid "
            f"{ACCENT_PRIMARY}; font-family: {FONT_FAMILY_MONO}; font-size: 12px; "
            f'margin: 6px 0;">{code}</pre>'
        )

    escaped = re.sub(r"\x00CB(\d+)\x00", _restore, escaped)

    # Переносы строк
    escaped = escaped.replace("\n", "<br>")

    return f'<div style="line-height: 1.6; color: {TEXT};">{escaped}{_BLOCK_END}'


class ChatView(QTextBrowser):
    """Карточки сообщений с аватарами.

    Сообщения складываются в блоки и отрисовываются разом — стриминг
    обновляет один блок вместо сообщения на каждый токен.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ChatView")
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self._blocks: list[str] = []
        self._stream_idx: int | None = None
        self._stream_buf: str = ""
        self._stream_name: str = "Немуся"

        font = QFont()
        font.setFamilies([f.strip() for f in FONT_FAMILY.split(",") if f.strip()])
        font.setPointSize(12)
        self.setFont(font)

    # ------------------------------------------------------------- rendering
    def _render(self) -> None:
        self.setHtml("".join(self._blocks))
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add(self, block: str) -> None:
        self._blocks.append(block)
        self._render()

    @staticmethod
    def _card(
        avatar: str,
        name: str,
        body: str,
        card_bg: str,
        accent: str,
        name_color: str,
        indent: bool = False,
    ) -> str:
        margin = "        " if indent else ""
        return f"""
{margin}<table style="margin: 12px 0; border-spacing: 0; width: 100%;"><tr>
{margin}<td style="vertical-align: top; padding: 4px 10px 0 0; width: 34px;">
{margin}    {avatar}
{margin}</td>
{margin}<td style="vertical-align: top;">
{margin}    <div style="background: {card_bg}; border: 1px solid {BORDER};
{margin}                border-left: 3px solid {accent}; border-radius: 12px;
{margin}                padding: 10px 14px;">
{margin}        <span style="color: {name_color}; font-weight: 700; font-size: 12px;">
{margin}            {html.escape(name)}
{margin}        </span>
{margin}        <div style="margin-top: 4px; color: {TEXT}; line-height: 1.6;">
{margin}            {body}
{margin}        </div>
{margin}    </div>
{margin}</td></tr></table>
"""

    # -------------------------------------------------------------- messages
    def add_user(self, text: str) -> None:
        """Добавляет сообщение пользователя."""
        block = self._card(
            _avatar_html(i18n.tr("role.you_letter"), ACCENT_PRIMARY, ACCENT_PRIMARY_DIM),
            i18n.tr("role.you"),
            render_markdown(text),
            "#101826",
            ACCENT_PRIMARY,
            ACCENT_PRIMARY,
        )
        self._add(block)

    def add_assistant(self, text: str, name: str = "Немуся") -> None:
        """Добавляет сообщение ассистента."""
        letter = (name.strip() or "Н")[0]
        block = self._card(
            _avatar_html(letter, ACCENT_SECONDARY, ACCENT_PRIMARY),
            name,
            render_markdown(text),
            BG_ELEVATED,
            ACCENT_SECONDARY,
            ACCENT_SECONDARY,
            indent=True,
        )
        self._add(block)

    @staticmethod
    def _assistant_block(name: str, body: str, indent: bool = False) -> str:
        letter = (name.strip() or "Н")[0]
        return ChatView._card(
            _avatar_html(letter, ACCENT_SECONDARY, ACCENT_PRIMARY),
            name,
            render_markdown(body),
            BG_ELEVATED,
            ACCENT_SECONDARY,
            ACCENT_SECONDARY,
            indent=indent,
        )

    def add_system(self, text: str) -> None:
        """Добавляет системное сообщение."""
        block = f"""
        <div style="margin: 10px 0; padding: 9px 12px;
                    background: rgba(94, 234, 165, 0.08);
                    border-radius: 9px; border: 1px solid {ACCENT_SUCCESS};">
            <span style="color: {ACCENT_SUCCESS}; font-weight: 600; font-size: 11px;">
                {i18n.tr("role.system")}
            </span>
            <div style="margin-top: 4px; color: {TEXT_DIM}; font-size: 12px;">
                {render_markdown(text)}
            </div>
        </div>
        """
        self._add(block)

    def add_error(self, text: str) -> None:
        """Добавляет сообщение об ошибке."""
        block = f"""
        <div style="margin: 10px 0; padding: 9px 12px;
                    background: rgba(255, 117, 117, 0.08);
                    border-radius: 9px; border: 1px solid {ACCENT_DANGER};">
            <span style="color: {ACCENT_DANGER}; font-weight: 600; font-size: 11px;">
                {i18n.tr("role.error")}
            </span>
            <div style="margin-top: 4px; color: {TEXT_DIM}; font-size: 12px;">
                {render_markdown(text)}
            </div>
        </div>
        """
        self._add(block)

    def add_tool_card(
        self, tool_name: str, args: dict, result: str | None = None, ok: bool = True
    ) -> None:
        """Добавляет карточку инструмента."""
        args_html = "".join(
            f'<div style="margin: 4px 0; color: {TEXT_DIM}; font-size: 11px;">'
            f'<span style="color: {TEXT}; font-weight: 600;">{html.escape(str(k))}:</span> '
            f'<code style="background: {BG}; color: {ACCENT_PRIMARY}; padding: 2px 4px; '
            f'border-radius: 3px; font-family: {FONT_FAMILY_MONO};">{html.escape(str(v))}</code></div>'
            for k, v in args.items()
        )
        status = ""
        if result is not None:
            color = ACCENT_SUCCESS if ok else ACCENT_DANGER
            status = (
                f'<div style="color:{color};font-size:12px;margin-top:6px;">'
                f"→ {html.escape(result[:500])}</div>"
            )

        block = f"""
        <div style="margin: 12px 0; padding: 10px 14px; background: {BG_ACTIVE};
                    border-radius: 11px; border: 1px solid {BORDER};
                    border-left: 3px solid {ACCENT_WARNING};">
            <span style="color: {ACCENT_WARNING}; font-weight: 700; font-size: 11px;
                         letter-spacing: 1.2px;">
                {i18n.tr("role.tool")} · {html.escape(tool_name.upper())}
            </span>
            <div style="margin-top: 8px;">
                {args_html}
            </div>
            {status}
        </div>
        """
        self._add(block)

    def clear_chat(self) -> None:
        """Очищает чат."""
        self._blocks.clear()
        self._stream_idx = None
        self._stream_buf = ""
        self.setHtml("")

    # -------------------------------------------------------------- streaming
    def stream_start(self, name: str = "Немуся") -> None:
        """Начинает потоковую передачу сообщения."""
        if self._stream_idx is not None:
            return
        self._stream_name = name
        self._stream_buf = ""
        self._stream_idx = len(self._blocks)
        self._blocks.append(self._assistant_block(name, ""))
        self._render()

    def stream_delta(self, text: str) -> None:
        """Добавляет текст в потоковое сообщение."""
        if not text:
            return
        if self._stream_idx is None:
            self.stream_start()
        assert self._stream_idx is not None
        self._stream_buf += text
        self._blocks[self._stream_idx] = self._assistant_block(
            self._stream_name, self._stream_buf
        )
        self._render()

    def stream_end(self, final_text: str = "") -> None:
        """Заканчивает потоковую передачу (при пустом потоке — вставляет текст).

        Непустой ``final_text`` всегда заменяет потоковый буфер: так в чате
        остаётся чистый финальный ответ без черновиков и рассуждений.
        """
        if self._stream_idx is None:
            return
        if final_text.strip():
            self._stream_buf = final_text
        if not self._stream_buf.strip():
            # пустой блок не показываем
            self._blocks.pop(self._stream_idx)
        else:
            self._blocks[self._stream_idx] = self._assistant_block(
                self._stream_name, self._stream_buf
            )
        self._stream_idx = None
        self._stream_buf = ""
        self._render()


__all__ = ["ChatView", "render_markdown"]
