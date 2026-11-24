"""Акустическое эхоподавление (WebRTC AEC3) для голосовых сессий.

Микрофон слышит не только пользователя, но и динамик. AEC вычитает из
микрофонного сигнала точную копию того, что уже ушло в динамик, поэтому
ассистент перестаёт слышать сам себя.

Модуль необязателен: если ``pywebrtc-audio`` не установлен или не может
создать фильтр, приложение продолжает работать с энергетическим echo-guard.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from nemus.utils.logger import logger

_warned = False


class PlaybackReference:
    """Кольцевой буфер реально выведенного в динамик сигнала.

    Записывается из аудио-колбэка вывода, читается из аудио-колбэка микрофона.
    Хранит последние ``max_ms`` миллисекунд, чтобы AEC всегда мог получить
    опорный сигнал той же длины, что и микрофонный блок.
    """

    def __init__(self, sample_rate: int, max_ms: int = 1000) -> None:
        self.sample_rate = sample_rate
        self._capacity = max(1, int(sample_rate * max_ms / 1000))
        self._buf = np.zeros(self._capacity, dtype=np.int16)
        self._pos = 0
        self._filled = 0
        self._lock = threading.Lock()

    def append(self, samples: np.ndarray) -> None:
        """Добавляет блок, который только что был отдан в динамик."""
        arr = np.asarray(samples, dtype=np.int16).reshape(-1)
        if arr.size == 0:
            return
        with self._lock:
            n = arr.size
            if n >= self._capacity:
                self._buf[:] = arr[-self._capacity :]
                self._pos = 0
                self._filled = self._capacity
                return
            end = self._pos + n
            if end <= self._capacity:
                self._buf[self._pos : end] = arr
            else:
                first = self._capacity - self._pos
                self._buf[self._pos :] = arr[:first]
                self._buf[: end - self._capacity] = arr[first:]
            self._pos = end % self._capacity
            self._filled = min(self._capacity, self._filled + n)

    def latest(self, n: int) -> np.ndarray:
        """Возвращает последние ``n`` отсчётов; недостающее дополняет нулями."""
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        with self._lock:
            out = np.zeros(n, dtype=np.int16)
            take = min(n, self._filled)
            if take == 0:
                return out
            start = (self._pos - take) % self._capacity
            if start + take <= self._capacity:
                out[-take:] = self._buf[start : start + take]
            else:
                first = self._capacity - start
                out[-take : -take + first] = self._buf[start:]
                out[-take + first :] = self._buf[: take - first]
            return out

    def clear(self) -> None:
        with self._lock:
            self._buf.fill(0)
            self._pos = 0
            self._filled = 0


def create_echo_canceller(sample_rate: int, delay_ms: int = 40) -> Any | None:
    """Создаёт WebRTC AEC3 или возвращает ``None``, если пакет недоступен."""
    global _warned

    try:
        from pywebrtc_audio import EchoCanceller
    except Exception as exc:  # noqa: BLE001
        if not _warned:
            _warned = True
            logger.warning(
                f"AEC недоступен ({exc}); остаётся энергетический echo-guard. "
                "Установите pywebrtc-audio."
            )
        return None

    try:
        return EchoCanceller(
            sample_rate=sample_rate,
            num_channels=1,
            stream_delay_ms=max(0, int(delay_ms)),
        )
    except Exception as exc:  # noqa: BLE001
        if not _warned:
            _warned = True
            logger.warning(f"Не удалось включить AEC: {exc}")
        return None
