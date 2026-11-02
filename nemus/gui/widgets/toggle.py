"""Компактный тумблер: пилюля с бегунком, рисуется кодом — без картинок."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from nemus.gui.theme import (
    ACCENT_PRIMARY,
    BG_ACTIVE,
    BG_SECONDARY,
    BORDER_LIGHT,
    TEXT,
    TEXT_DIM,
)


class ToggleSwitch(QAbstractButton):
    """Тумблер вместо чекбокса: слева пилюля, справа подпись.

    В отличие от чекбокса это именно «выключатель»: положение бегунка и цвет
    пилюли следуют ``isChecked()``, отключение (setEnabled) гасит цвета,
    сохраняя состояние.
    """

    PILL_W = 40
    PILL_H = 22
    GAP = 10

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.PILL_H)

    def sizeHint(self) -> QSize:  # noqa: D102
        width = self.PILL_W
        if self.text():
            width += self.GAP + self.fontMetrics().horizontalAdvance(self.text())
        return QSize(width, self.PILL_H)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: D102, ARG002
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isEnabled() and self.isChecked()

        pill = QRectF(0, 0, self.PILL_W, self.PILL_H)
        p.setBrush(QColor(ACCENT_PRIMARY) if on else QColor(BG_ACTIVE))
        if on or not self.isEnabled():
            p.setPen(Qt.PenStyle.NoPen)
        else:
            p.setPen(QPen(QColor(BORDER_LIGHT), 1.5))
        p.drawRoundedRect(pill, self.PILL_H / 2, self.PILL_H / 2)

        radius = self.PILL_H / 2 - 3
        cx = pill.right() - radius - 3 if on else pill.left() + radius + 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(TEXT if on else (TEXT_DIM if self.isEnabled() else BG_SECONDARY)))
        p.drawEllipse(QPointF(cx, pill.center().y()), radius, radius)

        if self.text():
            p.setPen(QColor(TEXT if self.isEnabled() else TEXT_DIM))
            p.drawText(
                QRectF(self.PILL_W + self.GAP, 0, self.width(), self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.text(),
            )
