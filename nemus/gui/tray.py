"""Системный трей: показать окно и выход."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from nemus.gui import i18n
from nemus.gui.theme import ACCENT_PRIMARY, ACCENT_SECONDARY


def _make_icon() -> QIcon:
    """Красивая градиентная иконка-орб для трея (как у окна)."""
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    gradient = QRadialGradient(32, 32, 32)
    gradient.setColorAt(0, QColor(ACCENT_PRIMARY))
    gradient.setColorAt(1, QColor(ACCENT_SECONDARY))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, 64, 64)
    painter.end()
    return QIcon(pix)


class Tray(QSystemTrayIcon):
    """Трей-иконка: показать окно, выход."""

    show_window = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_make_icon(), parent)
        self.setToolTip(i18n.tr("tray.tooltip"))

        menu = QMenu()
        show_action = menu.addAction(i18n.tr("tray.show"))
        show_action.triggered.connect(self.show_window.emit)
        menu.addSeparator()
        quit_action = menu.addAction(i18n.tr("tray.quit"))
        quit_action.triggered.connect(self.quit_requested.emit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window.emit()


__all__ = ["Tray"]
