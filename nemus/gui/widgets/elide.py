"""Подпись с сокращением: длинный текст не растягивает раскладку."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """QLabel, который режется многоточием и не требует всей ширины текста.

    Обычный QLabel минимум по ширине равен полной длине текста: длинное имя
    модели или команда MCP-сервера распирали соседние панели, а в сайдбаре
    с фиксированной шириной текст просто обрезался посреди слова. Полный
    текст всегда лежит в подсказке.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        super().setText(text)
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self.setToolTip(text)
        self._refit()

    def full_text(self) -> str:
        """Текст до сокращения (для подсказки и тестов)."""
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """По ширине подпись может сжаться до нуля (текст сократится)."""
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refit()

    def _refit(self) -> None:
        width = self.width()
        if width <= 0:
            text = self._full
        else:
            text = self.fontMetrics().elidedText(
                self._full, Qt.TextElideMode.ElideRight, max(24, width)
            )
        if text != QLabel.text(self):
            super().setText(text)


__all__ = ["ElidedLabel"]
