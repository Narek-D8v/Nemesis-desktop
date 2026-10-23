"""Запись звука и энергетический VAD."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from nemus.voice.params import BLOCK_MS, SAMPLE_RATE


@dataclass
class AudioConfig:
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    block_ms: int = BLOCK_MS  # размер блока в мс

    @property
    def blocksize(self) -> int:
        return int(self.sample_rate * self.block_ms / 1000)


class EnergyVAD:
    """Простой энергетический детектор речи (по RMS)."""

    def __init__(
        self,
        threshold: float = 0.015,
        silence_ms: int = 1200,
        min_speech_ms: int = 300,
    ) -> None:
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self._speaking = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._min_blocks = int(min_speech_ms / 30)

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, block: np.ndarray, block_ms: int) -> str:
        """Обрабатывает блок, возвращает событие: 'start'|'end'|None."""
        energy = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
        is_speech = energy >= self.threshold

        if is_speech:
            self._silence_ms = 0
            if not self._speaking:
                self._speech_ms += block_ms
                if self._speech_ms >= self.min_speech_ms:
                    self._speaking = True
                    return "start"
            return None

        if self._speaking:
            self._speech_ms = 0
            self._silence_ms += block_ms
            if self._silence_ms >= self.silence_ms:
                self._speaking = False
                self._silence_ms = 0
                return "end"
        else:
            self._speech_ms = 0
        return None

    def reset(self) -> None:
        self._speaking = False
        self._speech_ms = 0
        self._silence_ms = 0


class Recorder:
    """Записывает звук с микрофона через sounddevice.

    Потокобезопасно: ``open()``/``close()`` могут вызываться одновременно из
    GUI-потока (VoiceManager.stop) и фонового цикла записи (_loop). Раньше
    двойной вызов ``Stream.close()`` приводил к STATUS_HEAP_CORRUPTION
    (0xC0000374) в PortAudio — вылет всего приложения.
    """

    def __init__(self, config: AudioConfig | None = None) -> None:
        self.config = config or AudioConfig()
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

    def read_block(self) -> np.ndarray:
        """Блокирующее чтение одного блока звука."""
        stream = self._stream
        if stream is None:
            raise RuntimeError("Поток не открыт. Используй open().")
        block, _ = stream.read(self.config.blocksize)
        return np.asarray(block).reshape(-1)

    def open(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            stream = sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                blocksize=self.config.blocksize,
            )
            stream.start()
            self._stream = stream

    def close(self) -> None:
        # «Отписываем» поток под локом: только один поток дойдёт до
        # stop()/close(), повторный вызов станет no-op.
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Recorder":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
