"""Неблокирующая очистка микрофонного сигнала: ВЧ-фильтр + шумовые ворота.

Задача — убрать фоновый гул и шум, чтобы серверный VAD (Gemini Live /
OpenAI Realtime) и классический энергетический VAD не принимали их за речь.

Оценка уровня шума — «минимум за окно» (minimum statistics): шумовой пол
берётся как самый тихий блок за последние ~2 секунды. Это устойчиво к
постоянному шуму (вентилятор, гул) и не «съедает» речь, т.к. между словами
всегда есть тихие участки.
"""

from __future__ import annotations

import collections
import math

import numpy as np

from nemus.voice.params import BLOCK_MS, SAMPLE_RATE


class AudioCleaner:
    """Фильтрует шум: одно-полюсный ВЧ-фильтр + шумовые ворота.

    Ворота имеют гистерезис и «послесвечение» (hangover), чтобы не резать
    тихие слоги внутри слова, а усиление меняется плавно внутри блока —
    без щелчков.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        highpass_hz: float = 80.0,
        min_rms: float = 0.003,
        gate_ratio: float = 2.5,
        max_threshold: float = 0.05,
        hangover_ms: int = 120,
        floor_window_ms: int = 2000,
        block_ms: int = BLOCK_MS,
    ) -> None:
        self.sample_rate = sample_rate
        self.highpass_hz = highpass_hz
        self.min_rms = min_rms
        self.gate_ratio = gate_ratio
        self.max_threshold = max_threshold
        self.hangover_blocks = max(1, int(round(hangover_ms / block_ms)))
        self._window = max(1, int(round(floor_window_ms / block_ms)))

        self._history: collections.deque[float] = collections.deque(maxlen=self._window)
        self._open = False
        self._quiet_blocks = 0
        self._gain = 0.0

        # коэффициент одно-полюсного ВЧ-фильтра
        self._hp_a = 1.0 / (1.0 + 2.0 * math.pi * highpass_hz / sample_rate)

    # ----------------------------------------------------------------- filter
    def _highpass(self, x: np.ndarray) -> np.ndarray:
        a = self._hp_a
        y = np.empty_like(x)
        y_prev = 0.0
        x_prev = 0.0
        for i in range(x.shape[0]):
            xi = x[i]
            yi = a * (y_prev + xi - x_prev)
            y[i] = yi
            y_prev = yi
            x_prev = xi
        return y

    def reset(self) -> None:
        """Сбрасывает состояние (например, при старте новой сессии)."""
        self._history.clear()
        self._open = False
        self._quiet_blocks = 0
        self._gain = 0.0

    # ------------------------------------------------------------------ process
    def clean(self, block: np.ndarray) -> np.ndarray:
        """Возвращает int16-блок той же длины, подавив фоновый шум."""
        if block.size == 0:
            return block
        x = block.astype(np.float32) / 32768.0
        if self.highpass_hz > 0:
            x = self._highpass(x)

        rms = float(np.sqrt(np.mean(x * x)))

        # шумовой пол — минимум за окно (до добавления текущего блока)
        floor = min(self._history) if self._history else self.min_rms
        threshold = min(max(self.min_rms, floor * self.gate_ratio), self.max_threshold)

        if rms >= threshold:
            self._open = True
            self._quiet_blocks = 0
        else:
            self._quiet_blocks += 1
            if self._quiet_blocks >= self.hangover_blocks:
                self._open = False

        self._history.append(rms)

        target = 1.0 if self._open else 0.0
        ramp = np.linspace(self._gain, target, num=x.shape[0], dtype=np.float32)
        self._gain = target

        y = x * ramp
        return np.clip(y * 32768.0, -32768, 32767).astype(np.int16)
