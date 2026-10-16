"""Waveform-индикатор состояния (редизайн 2025): бегущие полосы + реакция на голос."""

from __future__ import annotations

import math
import random

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from nemus.core.state import AssistState
from nemus.gui.theme import STATE_COLORS


class WaveformWidget(QWidget):
    """Аудио-волновая панель состояния: полосы живут по таймеру 30 FPS.

    Профиль полос зависит от :class:`AssistState`, а реальная громкость
    микрофона (``set_amplitude``) масштабирует высоту в режимах
    «слушаю/говорю».
    """

    BAR_COUNT = 33

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(120)
        self.setMinimumWidth(220)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._state = AssistState.IDLE
        self._phase = 0.0
        self._amplitude = 0.0
        self._target_amplitude = 0.0
        self._levels = [0.0] * self.BAR_COUNT
        self._rng = random.Random(7)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------ API
    def set_state(self, state: AssistState) -> None:
        """Устанавливает состояние (меняет профиль волн)."""
        if state == self._state:
            return
        self._state = state
        if state not in (AssistState.LISTENING, AssistState.SPEAKING):
            self._target_amplitude = 0.0

    @property
    def state(self) -> AssistState:
        return self._state

    def set_amplitude(self, value: float) -> None:
        """Громкость микрофона 0..1 — полосы реагируют на голос."""
        self._target_amplitude = max(0.0, min(1.0, value))

    # ------------------------------------------------------------ анимация
    def _tick(self) -> None:
        self._phase += 0.12
        self._amplitude += (self._target_amplitude - self._amplitude) * 0.3
        amp = self._amplitude

        state = self._state
        phase = self._phase
        n = self.BAR_COUNT
        for i in range(n):
            if state == AssistState.IDLE:
                target = 0.12 + 0.05 * math.sin(phase * 0.7 + i * 0.35)
            elif state == AssistState.LISTENING:
                flicker = 0.10 * self._rng.random()
                ring = 0.55 + 0.45 * math.sin(phase * 1.6 + i * 0.55)
                target = 0.16 + 0.75 * amp * ring + flicker * (0.3 + amp)
            elif state == AssistState.THINKING:
                wave = 0.5 + 0.5 * math.sin(phase * 3.2 - i * 0.45)
                target = 0.16 + 0.55 * wave
            elif state == AssistState.SPEAKING:
                flicker = 0.18 * self._rng.random()
                ring = 0.45 + 0.55 * math.sin(phase * 4.2 + i * 0.7)
                target = 0.22 + 0.68 * amp * abs(ring) + flicker + 0.15 * amp
            else:  # ERROR — резкие скачки
                spike = self._rng.random()
                target = 0.18 + (0.6 if spike > 0.72 else 0.1) * spike

            # сглаживание
            self._levels[i] += (min(1.0, target) - self._levels[i]) * 0.35

        self.update()

    # --------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_color, accent_color = STATE_COLORS.get(
            self._state, STATE_COLORS[AssistState.IDLE]
        )
        accent = QColor(accent_color)
        base = QColor(bg_color)

        w, h = self.width(), self.height()
        n = self.BAR_COUNT
        gap = 4
        bar_w = max(3.0, (w - gap * (n - 1)) / n)
        max_h = h - 16.0
        top = 8.0

        pen = QPen(accent)
        pen.setWidthF(bar_w)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)

        for i in range(n):
            level = self._levels[i]
            x = i * (bar_w + gap) + bar_w / 2.0
            bar_h = max(bar_w, max_h * level)
            y0 = top + (max_h - bar_h) / 2.0
            y1 = y0 + bar_h
            painter.setOpacity(0.35 + 0.65 * level)
            painter.drawLine(int(x), int(y0), int(x), int(y1))

        # тонкая опорная линия в цвет фона панели
        painter.setOpacity(0.9)
        painter.setPen(QPen(base.darker(140), 1))
        painter.drawLine(8, int(h / 2), w - 8, int(h / 2))

        painter.end()


# Совместимость: старый орб — теперь та же волновая панель.
OrbWidget = WaveformWidget

__all__ = ["WaveformWidget", "OrbWidget"]
