"""ctypes-обёртка над RNNoise (xiph/rnnoise, BSD-3-Clause).

RNNoise — лёгкая рекуррентная нейросеть для шумоподавления речи в реальном
времени, работает локально на CPU, без API и без интернета. Нативная
библиотека лежит рядом (``nemus/voice/_rnnoise/rnnoise.dll``). Если файла
нет — сообщаем о недоступности, а фабрика шумодава откатывается на builtin.

RNNoise работает на 48 кГц, длина кадра 10 мс (480 отсчётов). Сэмплы
передаются в диапазоне int16 ([-32768, 32767]), но как float32.
"""

from __future__ import annotations

import ctypes
import os
import sys

import numpy as np

if sys.platform == "win32" or os.name == "nt":
    _DLL_NAME = "rnnoise.dll"
elif sys.platform == "darwin":
    _DLL_NAME = "librnnoise.dylib"
else:
    _DLL_NAME = "librnnoise.so"

_DLL_PATH = os.path.join(os.path.dirname(__file__), "_rnnoise", _DLL_NAME)

_lib: ctypes.CDLL | None = None


class RnnoiseUnavailable(RuntimeError):
    """RNNoise DLL недоступна."""


def dll_path() -> str:
    return _DLL_PATH


def available() -> bool:
    return os.path.exists(_DLL_PATH)


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    if not os.path.exists(_DLL_PATH):
        raise RnnoiseUnavailable(f"Библиотека RNNoise не найдена: {_DLL_PATH}")

    lib = ctypes.CDLL(_DLL_PATH)
    lib.rnnoise_create.argtypes = [ctypes.c_void_p]
    lib.rnnoise_create.restype = ctypes.c_void_p
    lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    lib.rnnoise_destroy.restype = None
    lib.rnnoise_get_frame_size.restype = ctypes.c_int
    lib.rnnoise_process_frame.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.rnnoise_process_frame.restype = ctypes.c_float
    _lib = lib
    return lib


def frame_size() -> int:
    """Размер кадра RNNoise в отсчётах (обычно 480 @ 48 кГц)."""
    return int(_load().rnnoise_get_frame_size())


class Denoiser:
    """Состояние RNNoise для одного моно-потока."""

    def __init__(self) -> None:
        lib = _load()
        self._lib = lib
        self._state: ctypes.c_void_p | None = lib.rnnoise_create(None)
        self.frame_size = int(lib.rnnoise_get_frame_size())

    def process(self, frame_int16: np.ndarray) -> tuple[np.ndarray, float]:
        """Обрабатывает один кадр (int16), возвращает (очищенный int16, вероятность речи)."""
        if self._state is None:
            raise RnnoiseUnavailable("Сессия RNNoise не инициализирована.")
        f = np.ascontiguousarray(frame_int16, dtype=np.float32)
        if f.shape[0] < self.frame_size:
            f = np.pad(f, (0, self.frame_size - f.shape[0]))
        ptr = f.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        prob = float(self._lib.rnnoise_process_frame(self._state, ptr, ptr))
        return f.astype(np.int16)[: frame_int16.shape[0]], prob

    def close(self) -> None:
        if self._state is not None:
            self._lib.rnnoise_destroy(self._state)
            self._state = None

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
