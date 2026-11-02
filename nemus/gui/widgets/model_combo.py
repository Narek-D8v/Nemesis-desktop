"""Комбо моделей LLM: выпадающий попап с поиском и фильтрами.

Вместо стандартного списка — собственный попап под полем: строка поиска
(подстрока, без учёта регистра), фильтры «Все / Бесплатные / Платные»,
счётчик найденных и список моделей с бейджами. Открывается по стрелке,
Alt+↓ и при начале набора текста в поле.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QSortFilterProxyModel, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nemus.gui import i18n
from nemus.gui.theme import (
    ACCENT_PRIMARY,
    BG_ELEVATED,
    BG_HOVER,
    BORDER_LIGHT,
    FONT_FAMILY,
    TEXT,
    TEXT_DIM,
)

#: id модели (без бейджа) — в данных элемента combo
ROLE_MODEL = int(Qt.ItemDataRole.UserRole)
#: флаг «бесплатная» (bool) — для фильтра в попапе
ROLE_FREE = ROLE_MODEL + 1

#: режимы фильтра (индексы совпадают с порядком кнопок)
MODES: tuple[str, ...] = ("all", "free", "paid")

_POPUP_QSS = f"""
QWidget {{
    background: {BG_ELEVATED};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 10px;
    font-family: {FONT_FAMILY};
    color: {TEXT};
}}
QLineEdit#ModelSearch {{
    background: {BG_HOVER};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 13px;
}}
QLineEdit#ModelSearch:focus {{ border-color: {ACCENT_PRIMARY}; }}
QPushButton#ModelFilter {{
    background: transparent;
    border: 1px solid {BORDER_LIGHT};
    border-radius: 8px;
    padding: 4px 10px;
    color: {TEXT_DIM};
    font-size: 12px;
}}
QPushButton#ModelFilter:checked {{
    border-color: {ACCENT_PRIMARY};
    color: {ACCENT_PRIMARY};
}}
QListView#ModelList {{
    background: transparent;
    border: none;
    outline: none;
    font-size: 13px;
}}
QListView#ModelList::item {{
    padding: 5px 8px;
    border-radius: 6px;
}}
QListView#ModelList::item:hover {{ background: {BG_HOVER}; }}
QListView#ModelList::item:selected {{ background: {BG_HOVER}; color: {TEXT}; }}
QLabel#ModelCount {{ color: {TEXT_DIM}; font-size: 12px; }}
"""


class _ModelsProxy(QSortFilterProxyModel):
    """Фильтрует список по строке поиска и режиму все/бесплатные/платные."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._query = ""
        self._mode = "all"

    @property
    def mode(self) -> str:
        return self._mode

    def set_query(self, text: str) -> None:
        self._query = (text or "").strip().lower()
        self.invalidateFilter()

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in MODES else "all"
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        if self._query:
            text = str(model.data(index, Qt.ItemDataRole.DisplayRole) or "").lower()
            if self._query not in text:
                return False
        free = bool(model.data(index, ROLE_FREE))
        if self._mode == "free" and not free:
            return False
        if self._mode == "paid" and free:
            return False
        return True


class ModelPopup(QWidget):
    """Попап под полем модели: поиск + фильтры + список + счётчик."""

    def __init__(self, combo: "ModelComboBox") -> None:
        super().__init__(None, Qt.WindowType.Popup)
        self._combo = combo
        self._mode = "all"
        self.setObjectName("ModelPopup")
        self.setStyleSheet(_POPUP_QSS)
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("ModelSearch")
        self.search.setPlaceholderText(i18n.tr("llm.search_ph"))
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        labels = ("llm.filter_all", "llm.filter_free", "llm.filter_paid")
        for mode, key in zip(MODES, labels, strict=True):
            button = QPushButton(i18n.tr(key))
            button.setObjectName("ModelFilter")
            button.setCheckable(True)
            button.setChecked(mode == "all")
            button.toggled.connect(
                lambda checked, m=mode: self.set_mode(m) if checked else None
            )
            self._group.addButton(button)
            self._buttons[mode] = button
            filters.addWidget(button)
        filters.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("ModelCount")
        filters.addWidget(self.lbl_count)
        layout.addLayout(filters)

        self.list = QListView()
        self.list.setObjectName("ModelList")
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.setAlternatingRowColors(False)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.list, 1)

        self._proxy = _ModelsProxy(self)
        self.list.setModel(self._proxy)
        self.search.textChanged.connect(self._apply)
        self.list.clicked.connect(self._activate)
        self.list.doubleClicked.connect(self._activate)
        self.search.installEventFilter(self)
        self.list.installEventFilter(self)

    # ------------------------------------------------------------- публичное
    def set_search(self, text: str) -> None:
        """Задать строку поиска (обновляет список и счётчик)."""
        self.search.setText(text)

    def set_mode(self, mode: str) -> None:
        """Режим фильтра: all | free | paid."""
        mode = mode if mode in MODES else "all"
        for name, button in self._buttons.items():
            button.blockSignals(True)
            button.setChecked(name == mode)
            button.blockSignals(False)
        self._mode = mode
        self._apply()

    def rows(self) -> int:
        """Сколько моделей видно после фильтра."""
        return self._proxy.rowCount()

    def prepare(self, sync_typed: bool = False) -> None:
        """Синхронизация с combo перед показом: модель, запрос, позиция.

        ``sync_typed=True`` — подхватить набранный в поле текст как запрос
        поиска (при открытии по стрелке поиск остаётся прежним).
        """
        combo = self._combo
        if self._proxy.sourceModel() is not combo.model():
            self._proxy.setSourceModel(combo.model())
        index = combo.currentIndex()
        if sync_typed:
            typed = ""
            if index < 0 or (
                combo.currentText().strip() != combo.itemText(index).strip()
            ):
                typed = combo.currentText().strip()
            if typed != self.search.text():
                self.search.setText(typed)
        self._apply()

        top_left = combo.mapToGlobal(QPoint(0, combo.height()))
        width = max(combo.width(), 360)
        height = min(440, max(200, self.rows() * 26 + 104))
        screen = QGuiApplication.screenAt(top_left) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = min(max(top_left.x(), area.left()), area.right() - width)
            y = top_left.y()
            if y + height > area.bottom():
                y = combo.mapToGlobal(QPoint(0, 0)).y() - height
            y = max(y, area.top())
        else:
            x, y = top_left.x(), top_left.y()
        self.setGeometry(x, y, width, height)

        if index >= 0:
            proxy_index = self._proxy.mapFromSource(combo.model().index(index, 0))
            if proxy_index.isValid():
                self.list.setCurrentIndex(proxy_index)
                self.list.scrollTo(
                    proxy_index, QAbstractItemView.ScrollHint.PositionAtCenter
                )
            else:
                self.list.clearSelection()
        elif self.rows():
            self.list.setCurrentIndex(self._proxy.index(0, 0))
        else:
            self.list.clearSelection()
        self.search.setFocus(Qt.FocusReason.PopupFocusReason)

    # -------------------------------------------------------------- внутреннее
    def _apply(self, *_: object) -> None:
        self._proxy.set_query(self.search.text())
        self._proxy.set_mode(self._mode)
        self.lbl_count.setText(i18n.tr("llm.model_count", n=self.rows()))

    def _activate(self, index: QModelIndex) -> None:
        if not index.isValid():
            if self.rows():
                index = self._proxy.index(0, 0)
            else:
                return
        source = self._proxy.mapToSource(index)
        if not source.isValid():
            return
        self._combo.setCurrentIndex(source.row())
        self._combo.hidePopup()

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self._combo.hidePopup()
                return True
            if obj is self.search:
                if key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
                    if self.rows() and not self.list.currentIndex().isValid():
                        self.list.setCurrentIndex(self._proxy.index(0, 0))
                    self.list.setFocus(Qt.FocusReason.TabFocusReason)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._activate(self.list.currentIndex())
                    return True
            elif obj is self.list:
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._activate(self.list.currentIndex())
                    return True
                if key == Qt.Key.Key_Up and self.list.currentIndex().row() <= 0:
                    self.search.setFocus(Qt.FocusReason.TabFocusReason)
                    return True
        return super().eventFilter(obj, event)


class ModelComboBox(QComboBox):
    """Комбо моделей со своим попапом (поиск и фильтры) вместо списка Qt."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setDuplicatesEnabled(False)
        completer = self.completer()
        completer.setCompletionMode(QCompleter.CompletionMode.InlineCompletion)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setWrapAround(False)
        #: попап с поиском/фильтрами (открывается вместо списка по умолчанию)
        self.popup = ModelPopup(self)
        self._sync_typed = False
        self.lineEdit().textEdited.connect(self._on_typed)

    def _on_typed(self, _text: str) -> None:
        self._sync_typed = True
        if not self.popup.isVisible():
            self.showPopup()

    def showPopup(self) -> None:  # noqa: D102
        self.popup.prepare(sync_typed=self._sync_typed)
        self._sync_typed = False
        self.popup.show()
        self.popup.raise_()

    def hidePopup(self) -> None:  # noqa: D102
        if self.popup.isVisible():
            self.popup.hide()
        super().hidePopup()


__all__ = ["MODES", "ROLE_FREE", "ROLE_MODEL", "ModelComboBox", "ModelPopup"]
