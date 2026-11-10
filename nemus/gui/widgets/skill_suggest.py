"""Подсказки команд ``/`` (скиллов) в поле ввода промпта.

Пользователь набирает ``/`` — под полем раскрывается список скиллов с
описаниями; ``/cod`` сужает его до ``/code-craft``. Выбор подставляет
команду вместо набранного слова, остальной текст промпта остаётся на месте:
``«сделай отчёт /cod»`` → ``«сделай отчёт /code-craft »``.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QModelIndex, QObject, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QCompleter, QLineEdit

from nemus.core.skill_commands import SLASH_PREFIX, SkillCommand
from nemus.gui.theme import ACCENT_PRIMARY, BG_ELEVATED, BORDER, TEXT

_POPUP_QSS = f"""
QAbstractItemView {{
    background: {BG_ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: 0;
    selection-background-color: {ACCENT_PRIMARY};
}}
QAbstractItemView::item {{
    padding: 7px 10px;
    border-radius: 6px;
    color: {TEXT};
}}
QAbstractItemView::item:selected {{
    background: {ACCENT_PRIMARY};
    color: #0a0d14;
}}
QToolTip {{
    color: {TEXT};
    background: {BG_ELEVATED};
    border: 1px solid {BORDER};
    padding: 6px 8px;
}}
"""

#: последнее слово строки — то, что пользователь сейчас дописывает
_LAST_TOKEN_RE = re.compile(r"(\S*)\Z")


def last_token(text: str) -> str:
    """Последнее слово перед курсором (пусто, если ввод кончается пробелом)."""
    if not text or text[-1].isspace():
        return ""
    match = _LAST_TOKEN_RE.search(text)
    return match.group(1) if match else ""


class SkillSuggester(QObject):
    """Автодополнение слэш-команд скиллов в поле ввода.

    Срабатывает только на слово, начинающееся с ``/``: обычный текст промпта
    подсказок не вызывает, а команду можно набрать посреди фразы.
    """

    def __init__(self, line_edit: QLineEdit, parent: QObject | None = None) -> None:
        super().__init__(parent or line_edit)
        self._edit = line_edit
        self._commands: list[SkillCommand] = []

        self._model = QStandardItemModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setMaxVisibleItems(8)
        self._completer.setWidget(line_edit)
        self._completer.activated[QModelIndex].connect(self._on_activated)

        popup = self._completer.popup()
        popup.setStyleSheet(_POPUP_QSS)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.installEventFilter(self)
        #: попап открыт нами (isVisible на некоторых платформах запаздывает)
        self._popup_open = False

        line_edit.textEdited.connect(self._on_typed)

    # --------------------------------------------------------------- публичное
    def set_commands(self, commands: list[SkillCommand]) -> None:
        """Заменяет набор команд (после перечитывания скиллов)."""
        self._commands = list(commands)
        self._model.clear()
        for command in self._commands:
            item = QStandardItem(command.title)
            item.setData(command.text, Qt.ItemDataRole.UserRole)
            item.setToolTip(
                f"{command.description}\nскилл: {command.skill}\nвставит: {command.text}"
                if command.description
                else f"скилл: {command.skill}"
            )
            self._model.appendRow(item)

    def current_token(self) -> str:
        """Последнее слово перед курсором (то, что сейчас дописывается)."""
        return last_token(self._edit.text()[: self._edit.cursorPosition()])

    def matches(self, text: str | None = None) -> list[SkillCommand]:
        """Команды под набранное слово: только если оно начинается с ``/``."""
        token = self.current_token() if text is None else last_token(text)
        if not token.startswith(SLASH_PREFIX):
            return []
        needle = token[len(SLASH_PREFIX):].lower()
        found: list[SkillCommand] = []
        for command in self._commands:
            name = command.name.lower()
            if needle and needle not in name:
                continue
            found.append(command)
        # сначала те, что начинаются с введённого, потом остальные вхождения
        found.sort(key=lambda c: (not c.name.lower().startswith(needle), c.name.lower()))
        return found

    def apply(self, command: SkillCommand) -> None:
        """Подставляет команду вместо недописанного слова, сохраняя остальной ввод."""
        text = self._edit.text()
        pos = self._edit.cursorPosition()
        start = self._token_start(text, pos)
        head = text[:start]
        tail = text[pos:].lstrip()
        insert = command.text
        self._hide_popup()
        if tail:
            gap = "" if insert.endswith(" ") else " "
            new_text = f"{head}{insert}{gap}{tail}"
            cursor = len(head) + len(insert)
        elif insert.endswith(" "):
            new_text = head + insert
            cursor = len(new_text)
        else:
            new_text = f"{head}{insert} "
            cursor = len(new_text)
        self._edit.setText(new_text)
        self._edit.setCursorPosition(cursor)
        self._edit.setFocus()

    def popup_visible(self) -> bool:
        """Открыт ли список команд (Enter тогда выбирает, а не отправляет)."""
        return self._popup_open

    def _hide_popup(self) -> None:
        """Скрывает список и снимает признак открытого попапа."""
        self._popup_open = False
        self._completer.popup().hide()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        """Попап закрылся сам (Escape, выбор, фокус) — признак снимаем."""
        if event.type() == QEvent.Type.Hide and obj is self._completer.popup():
            self._popup_open = False
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- служебное
    @staticmethod
    def _token_start(text: str, pos: int) -> int:
        """Начало недописанного слова перед позицией курсора."""
        match = _LAST_TOKEN_RE.search(text[:pos])
        return match.start(1) if match else pos

    def _on_typed(self, _text: str) -> None:
        """Показывает попап команд, когда последнее слово начинается с ``/``."""
        token = self.current_token()
        if not self._commands or not token.startswith(SLASH_PREFIX):
            self._hide_popup()
            return
        if not self.matches(token):
            self._hide_popup()
            return
        self._completer.setCompletionPrefix(token[len(SLASH_PREFIX):])
        rect = self._edit.rect()
        rect.setWidth(self._edit.width())
        self._completer.complete(rect)
        popup = self._completer.popup()
        popup.setMinimumWidth(max(340, self._edit.width()))
        model = popup.model()
        self._popup_open = model is not None and model.rowCount() > 0
        if self._popup_open and model is not None:
            popup.setCurrentIndex(model.index(0, 0))

    def _on_activated(self, index: QModelIndex) -> None:
        """Выбор команды мышью или Enter."""
        text = index.data(Qt.ItemDataRole.UserRole)
        if not text:
            return
        command = next(
            (item for item in self._commands if item.text == str(text)),
            None,
        )
        if command is not None:
            self.apply(command)


__all__ = ["SkillSuggester", "last_token"]
