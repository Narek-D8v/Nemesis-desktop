"""Тёмная тема Немуси: палитра редизайна 2025, галочка чекбокса и QSS."""

from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

from nemus.core.state import AssistState

# ===========================================================================
#  1. Палитра
# ===========================================================================

BG = "#08090d"
BG_SECONDARY = "#0e1016"
BG_PANEL = "#0e1016"
BG_ELEVATED = "#151821"
BG_HOVER = "#1b1f2b"
BG_ACTIVE = "#222736"

BORDER = "#1c2130"
BORDER_LIGHT = "#2a3042"
BORDER_STRONG = "#3a4256"

TEXT = "#e9edf5"
TEXT_DIM = "#8b96aa"
TEXT_DIMMER = "#565f73"

ACCENT_PRIMARY = "#7cb6ff"
ACCENT_PRIMARY_DIM = "#4a8fe0"
ACCENT_SECONDARY = "#b79cff"
ACCENT_SUCCESS = "#5eeaa5"
ACCENT_WARNING = "#f5c34a"
ACCENT_DANGER = "#ff7575"

# Совместимость со старыми именами
ACCENT = ACCENT_PRIMARY
ACCENT_DIM = ACCENT_PRIMARY_DIM

STATE_COLORS: dict[AssistState, tuple[str, str]] = {
    AssistState.IDLE: ("#0d1a2b", "#7cb6ff"),
    AssistState.LISTENING: ("#0d2419", "#5eeaa5"),
    AssistState.THINKING: ("#2a2110", "#f5c34a"),
    AssistState.SPEAKING: ("#241533", "#b79cff"),
    AssistState.ERROR: ("#2a1010", "#ff7575"),
}

FONT_FAMILY = "Inter, SF Pro Display, Segoe UI, -apple-system, sans-serif"
FONT_FAMILY_MONO = "JetBrains Mono, SF Mono, Fira Code, Consolas, monospace"


def _families(family_list: str) -> str:
    """`Inter, Segoe UI` -> `"Inter", "Segoe UI"` (корректный CSS-список)."""
    return ", ".join(f'"{f.strip()}"' for f in family_list.split(",") if f.strip())


# ===========================================================================
#  2. Чекбокс — генерируем галочку в pixmap
# ===========================================================================

_CHECK_PATH: str | None = None


def _check_icon_path() -> str:
    """Рисует галочку один раз и кладёт во временный файл (для QSS)."""
    global _CHECK_PATH
    if _CHECK_PATH and os.path.exists(_CHECK_PATH):
        return _CHECK_PATH

    path = os.path.join(tempfile.gettempdir(), "nemus_check.png")
    try:
        pm = QPixmap(28, 28)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#0a0d14"), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(7, 14), QPointF(12, 19))
        p.drawLine(QPointF(12, 19), QPointF(21, 9))
        p.end()
        pm.save(path, "PNG")
    except Exception:  # noqa: BLE001 — без QApplication картинка не создастся
        pass
    _CHECK_PATH = path
    return path


# ===========================================================================
#  3. QSS
# ===========================================================================


def qss() -> str:
    """Возвращает таблицу стилей приложения (редизайн 2025)."""
    check = _check_icon_path().replace("\\", "/")
    families = _families(FONT_FAMILY)
    return f"""
    * {{ margin: 0; padding: 0; outline: 0; }}

    QWidget {{
        background: {BG};
        color: {TEXT};
        font-family: {families};
        font-size: 13px;
    }}

    QLabel {{ background: transparent; color: {TEXT}; }}

    /* Заголовки */
    #HeaderLabel {{
        font-size: 19px;
        font-weight: 700;
        letter-spacing: -0.2px;
        color: {TEXT};
        background: transparent;
    }}
    #SubLabel {{
        font-size: 11px;
        color: {TEXT_DIMMER};
        background: transparent;
        letter-spacing: 0.3px;
    }}
    #SectionLabel {{
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.8px;
        color: {TEXT_DIMMER};
        background: transparent;
    }}
    #StateLabel {{
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.4px;
        background: transparent;
    }}
    #ModelLabel {{
        font-size: 11px;
        color: {TEXT_DIMMER};
        background: transparent;
    }}

    /* Карточки / панели */
    #Sidebar {{
        background: {BG_SECONDARY};
        border: 1px solid {BORDER};
        border-radius: 18px;
    }}
    #Card {{
        background: {BG_SECONDARY};
        border: 1px solid {BORDER};
        border-radius: 16px;
    }}
    #ChatView {{
        background: {BG_SECONDARY};
        border: 1px solid {BORDER};
        border-radius: 18px;
        padding: 6px 10px;
    }}
    #Divider {{
        background: {BORDER};
        border: none;
        max-height: 1px;
        min-height: 1px;
    }}

    /* Ввод */
    QLineEdit, QSpinBox, QComboBox, QTextEdit {{
        background: {BG_ELEVATED};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 10px;
        padding: 9px 12px;
        selection-background-color: {ACCENT_PRIMARY_DIM};
        selection-color: #ffffff;
        font-size: 13px;
    }}
    QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QTextEdit:hover {{
        border-color: {BORDER_LIGHT};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
        border: 1px solid {ACCENT_PRIMARY};
    }}

    /* Пилюля ввода */
    #InputPill {{
        background: {BG_ELEVATED};
        border: 1px solid {BORDER};
        border-radius: 24px;
    }}
    #InputPill[active="true"] {{
        border: 1px solid {ACCENT_PRIMARY};
    }}
    QLineEdit#PillInput {{
        background: transparent;
        border: none;
        padding: 12px 4px 12px 18px;
        font-size: 14px;
        color: {TEXT};
    }}
    QPushButton#SendButton {{
        background: {ACCENT_PRIMARY};
        color: #0a0d14;
        border: none;
        border-radius: 18px;
        font-size: 16px;
        font-weight: 800;
        padding: 0;
        min-height: 0;
    }}
    QPushButton#SendButton:hover {{ background: #96c4ff; }}
    QPushButton#SendButton:pressed {{ background: {ACCENT_PRIMARY_DIM}; }}
    QPushButton#SendButton:disabled {{
        background: {BG_ACTIVE};
        color: {TEXT_DIMMER};
    }}

    /* QComboBox */
    QComboBox::drop-down {{ border: none; width: 26px; background: transparent; }}
    QComboBox::down-arrow {{ image: none; }}
    QComboBox QAbstractItemView {{
        background: {BG_ELEVATED};
        border: 1px solid {BORDER_LIGHT};
        border-radius: 10px;
        padding: 6px;
        color: {TEXT};
        outline: 0;
    }}
    QComboBox QAbstractItemView::item {{
        padding: 8px 10px;
        border-radius: 6px;
        min-height: 22px;
    }}
    QComboBox QAbstractItemView::item:selected {{
        background: {ACCENT_PRIMARY_DIM};
        color: #ffffff;
    }}

    /* Кнопки */
    QPushButton {{
        background: {BG_ELEVATED};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 11px;
        padding: 9px 16px;
        min-height: 20px;
        font-size: 13px;
        font-weight: 500;
    }}
    QPushButton:hover {{ background: {BG_HOVER}; border-color: {BORDER_LIGHT}; }}
    QPushButton:pressed {{ background: {BG_ACTIVE}; }}
    QPushButton:disabled {{
        color: {TEXT_DIMMER};
        background: {BG_SECONDARY};
        border-color: {BORDER};
    }}

    QPushButton#Primary {{
        background: {ACCENT_PRIMARY};
        color: #0a0d14;
        border: none;
        border-radius: 12px;
        padding: 12px 20px;
        font-size: 13px;
        font-weight: 700;
    }}
    QPushButton#Primary:hover {{ background: #96c4ff; }}
    QPushButton#Primary:pressed {{ background: {ACCENT_PRIMARY_DIM}; }}
    QPushButton#Primary:disabled {{ background: {BG_ACTIVE}; color: {TEXT_DIMMER}; }}
    QPushButton#Primary:checked {{ background: {ACCENT_PRIMARY_DIM}; color: #ffffff; }}

    QPushButton#Ghost {{
        background: transparent;
        border: 1px solid {BORDER};
        color: {TEXT_DIM};
        font-weight: 500;
    }}
    QPushButton#Ghost:hover {{
        border-color: {ACCENT_PRIMARY};
        color: {ACCENT_PRIMARY};
        background: rgba(124, 182, 255, 0.06);
    }}
    QPushButton#Ghost:checked {{
        border-color: {ACCENT_PRIMARY};
        color: {ACCENT_PRIMARY};
        background: rgba(124, 182, 255, 0.14);
    }}

    QPushButton#Danger {{
        background: rgba(255, 117, 117, 0.10);
        border: 1px solid rgba(255, 117, 117, 0.55);
        color: {ACCENT_DANGER};
    }}
    QPushButton#Danger:hover {{ background: rgba(255, 117, 117, 0.20); }}

    /* GroupBox */
    QGroupBox {{
        background: {BG_SECONDARY};
        border: 1px solid {BORDER};
        border-radius: 14px;
        margin-top: 18px;
        padding: 18px 16px 16px 16px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 16px;
        padding: 0 8px;
        color: {TEXT_DIM};
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.6px;
        text-transform: uppercase;
    }}

    /* CheckBox */
    QCheckBox {{ spacing: 10px; background: transparent; color: {TEXT}; font-size: 13px; }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border: 1.5px solid {BORDER_LIGHT};
        border-radius: 5px;
        background: {BG_ELEVATED};
    }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT_PRIMARY}; }}
    QCheckBox::indicator:checked {{
        background: {ACCENT_PRIMARY};
        border-color: {ACCENT_PRIMARY};
        image: url({check});
    }}

    /* Скроллбары */
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 6px 3px 6px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER_LIGHT};
        border-radius: 4px;
        min-height: 36px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {BORDER_STRONG}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}

    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 0 6px 3px 6px;
    }}
    QScrollBar::handle:horizontal {{
        background: {BORDER_LIGHT};
        border-radius: 4px;
        min-width: 36px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {BORDER_STRONG}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    /* Диалоги */
    QDialog {{ background: {BG}; }}

    /* ToolTip */
    QToolTip {{
        background: {BG_ELEVATED};
        color: {TEXT};
        border: 1px solid {BORDER_LIGHT};
        border-radius: 6px;
        padding: 6px 8px;
        font-size: 12px;
    }}
    """


__all__ = [
    "BG",
    "BG_SECONDARY",
    "BG_ELEVATED",
    "BG_PANEL",
    "BG_HOVER",
    "BG_ACTIVE",
    "BORDER",
    "BORDER_LIGHT",
    "BORDER_STRONG",
    "TEXT",
    "TEXT_DIM",
    "TEXT_DIMMER",
    "ACCENT_PRIMARY",
    "ACCENT_PRIMARY_DIM",
    "ACCENT_SECONDARY",
    "ACCENT_SUCCESS",
    "ACCENT_WARNING",
    "ACCENT_DANGER",
    "ACCENT",
    "ACCENT_DIM",
    "STATE_COLORS",
    "FONT_FAMILY",
    "FONT_FAMILY_MONO",
    "qss",
    "_families",
    "_check_icon_path",
]
