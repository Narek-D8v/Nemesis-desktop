"""История диалога: карточки с аватарами, разметка, стриминг."""

from __future__ import annotations

import html
import os
import re
import tempfile
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPen, QPixmap
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
        # внешние ссылки открываем сами — иначе не перехватывается клик по
        # заголовку блока мыслей (свернуть/развернуть)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._on_anchor_clicked)
        self._blocks: list[str] = []
        self._stream_idx: int | None = None
        self._stream_buf: str = ""
        self._stream_name: str = "Немуся"
        #: текущий блок «мыслей» (скрытые рассуждения модели)
        self._thought_idx: int | None = None
        self._thought_buf: str = ""
        self._thought_dirty = False
        self._last_thought_render = 0.0
        #: id блоков мыслей и их свёрнутое состояние / текст
        self._thought_seq = 0
        self._thought_id: int | None = None
        self._thought_collapsed: dict[int, bool] = {}
        self._thought_texts: dict[int, str] = {}
        #: троттлинг перерисовки при стриминге (см. stream_delta)
        self._stream_dirty = False
        self._last_stream_render = 0.0

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
        self.thought_end()
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
        self.thought_end()
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
        self.thought_end()
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
        self.thought_end()
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
        self.thought_end()
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
        self._stream_dirty = False
        self._thought_idx = None
        self._thought_buf = ""
        self._thought_dirty = False
        self._thought_id = None
        self._thought_collapsed.clear()
        self._thought_texts.clear()
        self.setHtml("")

    # ---------------------------------------------------------- thoughts
    def _thought_block(self, body: str, tid: int, collapsed: bool) -> str:
        """Блок «мыслей» модели — визуально отделён от ответа.

        Приглушённый фон, пунктирная рамка, курсив и подпись: так видно,
        что Немуся просто думает, а не отвечает. Заголовок — ссылка:
        клик сворачивает/разворачивает блок, чтобы не засорять чат.
        """
        chevron = "▶" if collapsed else "▼"
        header = (
            f'<a href="thought:{tid}" style="color: {TEXT_DIM}; '
            f'text-decoration: none; font-weight: 700; font-size: 10px; '
            f'letter-spacing: 1.1px;">'
            f'{chevron} 🧠 {i18n.tr("role.thought")}</a>'
        )
        if collapsed:
            preview = (
                '<div style="margin-top: 3px; color: ' + TEXT_DIM
                + '; font-size: 11px;">…</div>'
            )
            body_html = ""
        else:
            preview = ""
            dim_html = render_markdown(body).replace(
                f"color: {TEXT};", f"color: {TEXT_DIM};"
            )
            body_html = (
                f'<div style="margin-top: 3px; font-style: italic; '
                f'font-size: 11.5px; line-height: 1.5;">{dim_html}</div>'
            )
        return f"""
        <!--TH{tid}-->
        <div style="margin: 8px 0 8px 34px; padding: 7px 12px;
                    background: {BG}; border: 1px dashed {BORDER};
                    border-left: 3px solid {TEXT_DIM}; border-radius: 10px;">
            {header}
            {preview}
            {body_html}
        </div>
        """

    def _on_anchor_clicked(self, url: QUrl) -> None:
        """Клик по ссылке в чате: сворачивание мыслей или внешняя ссылка."""
        if url.scheme() == "thought":
            key = url.host() or url.path().lstrip("/")
            try:
                tid = int(key)
            except ValueError:
                return
            self._toggle_thought(tid)
            return
        if url.isValid():
            QDesktopServices.openUrl(url)

    def _toggle_thought(self, tid: int) -> None:
        """Сворачивает/разворачивает конкретный блок мыслей по его id."""
        collapsed = not self._thought_collapsed.get(tid, False)
        self._thought_collapsed[tid] = collapsed
        body = self._thought_texts.get(tid, "")
        if self._thought_id == tid and self._thought_idx is not None:
            # блок ещё пишется — обновляем активный буфер
            self._thought_buf = body
        new_html = self._thought_block(body, tid, collapsed)
        marker = f"<!--TH{tid}-->"
        for i, block in enumerate(self._blocks):
            if marker in block:
                self._blocks[i] = new_html
                break
        self._render()

    def thought_start(self) -> None:
        """Открывает блок мыслей (обычно стартует сам через thought_delta).

        По умолчанию блок свёрнут — чат не засоряется, показать мысли
        можно кликом по заголовку.
        """
        if self._thought_idx is not None:
            return
        self._thought_buf = ""
        self._thought_dirty = False
        self._last_thought_render = 0.0
        self._thought_seq += 1
        self._thought_id = self._thought_seq
        self._thought_texts[self._thought_id] = ""
        self._thought_collapsed[self._thought_id] = True
        self._thought_idx = len(self._blocks)
        self._blocks.append(self._thought_block("", self._thought_id, True))
        self._render()

    def thought_delta(self, text: str) -> None:
        """Добавляет фрагмент рассуждений модели в блок мыслей."""
        if not text:
            return
        if self._thought_idx is None:
            self.thought_start()
        assert self._thought_idx is not None
        self._thought_buf += text
        now = time.monotonic()
        if now - self._last_thought_render < self.STREAM_INTERVAL:
            if not self._thought_dirty:
                self._thought_dirty = True
                QTimer.singleShot(
                    int(self.STREAM_INTERVAL * 1000), self._flush_thought
                )
            return
        self._thought_dirty = False
        self._last_thought_render = now
        self._thought_texts[self._thought_id] = self._thought_buf
        collapsed = self._thought_collapsed.get(self._thought_id, False)
        self._blocks[self._thought_idx] = self._thought_block(
            self._thought_buf, self._thought_id, collapsed
        )
        self._render()

    def _flush_thought(self) -> None:
        """Дорисовывает накопленный буфер мыслей (таймер после паузы)."""
        if self._thought_idx is None or not self._thought_dirty:
            return
        self._thought_dirty = False
        self._last_thought_render = time.monotonic()
        self._thought_texts[self._thought_id] = self._thought_buf
        collapsed = self._thought_collapsed.get(self._thought_id, False)
        self._blocks[self._thought_idx] = self._thought_block(
            self._thought_buf, self._thought_id, collapsed
        )
        self._render()

    def thought_end(self) -> None:
        """Закрывает блок мыслей (пустой блок убирается)."""
        if self._thought_idx is None:
            return
        idx = self._thought_idx
        tid = self._thought_id
        self._thought_dirty = False
        if not self._thought_buf.strip():
            self._blocks.pop(idx)
            if tid is not None:
                self._thought_texts.pop(tid, None)
                self._thought_collapsed.pop(tid, None)
            if self._stream_idx is not None and self._stream_idx > idx:
                self._stream_idx -= 1
        else:
            self._thought_texts[tid] = self._thought_buf
            collapsed = self._thought_collapsed.get(tid, False)
            self._blocks[idx] = self._thought_block(self._thought_buf, tid, collapsed)
        self._thought_idx = None
        self._thought_buf = ""
        self._thought_id = None
        self._render()

    # -------------------------------------------------------------- streaming
    #: минимальный интервал полной перерисовки чата при стриминге, сек.
    #: на каждый токен setHtml(весь чат) даёт O(n²) и «заморозку» окна
    #: на длинных ответах (презентации, скрипты) — держим ~10 fps.
    STREAM_INTERVAL = 0.1

    def stream_start(self, name: str = "Немуся") -> None:
        """Начинает потоковую передачу сообщения."""
        if self._stream_idx is not None:
            return
        self.thought_end()  # мысли всегда предшествуют ответу
        self._stream_name = name
        self._stream_buf = ""
        self._stream_dirty = False
        self._last_stream_render = 0.0
        self._stream_idx = len(self._blocks)
        self._blocks.append(self._assistant_block(name, ""))
        self._render()

    def stream_delta(self, text: str) -> None:
        """Добавляет текст в потоковое сообщение (перерисовка не чаще 10 раз/с)."""
        if not text:
            return
        if self._stream_idx is None:
            self.stream_start()
        assert self._stream_idx is not None
        self._stream_buf += text
        now = time.monotonic()
        if now - self._last_stream_render < self.STREAM_INTERVAL:
            # копим буфер дёшево, отрисовку догонит таймер
            if not self._stream_dirty:
                self._stream_dirty = True
                QTimer.singleShot(
                    int(self.STREAM_INTERVAL * 1000), self._flush_stream
                )
            return
        self._stream_dirty = False
        self._last_stream_render = now
        self._blocks[self._stream_idx] = self._assistant_block(
            self._stream_name, self._stream_buf
        )
        self._render()

    def _flush_stream(self) -> None:
        """Дорисовывает накопленный буфер (таймер после пропущенной отрисовки)."""
        if self._stream_idx is None or not self._stream_dirty:
            return
        self._stream_dirty = False
        self._last_stream_render = time.monotonic()
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
            # поток не открывался (например, сразу пришёл финальный текст)
            if final_text.strip():
                self.thought_end()
                block = self._assistant_block(self._stream_name, final_text)
                self._add(block)
            return
        if final_text.strip():
            self._stream_buf = final_text
        self._stream_dirty = False
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
