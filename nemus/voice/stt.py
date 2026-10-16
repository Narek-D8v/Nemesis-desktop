"""Распознавание речи (STT) через faster-whisper (локально, офлайн)."""

from __future__ import annotations

import os
import threading
from typing import Any

import numpy as np

from nemus.utils.logger import logger


class STTEngine:
    """Лениво загружаемая модель faster-whisper с прогревом и быстрым decode."""

    def __init__(
        self, model: str = "small", language: str = "ru", device: str = "auto"
    ) -> None:
        self.model_size = model
        self.language = language
        self.device = device
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            device = self.device
            compute = "int8"
            if device in ("auto", ""):
                try:
                    import ctranslate2  # noqa: F401

                    device = (
                        "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                    )
                except Exception:  # noqa: BLE001
                    device = "cpu"
            if device == "cuda":
                compute = "float16"
            cpu_threads = max(1, (os.cpu_count() or 4) - 1)
            logger.info(
                f"Загружаю STT-модель '{self.model_size}' на {device} ({compute}), "
                f"потоков CPU: {cpu_threads}…"
            )
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute,
                cpu_threads=cpu_threads,
            )
            return self._model

    def warmup(self) -> bool:
        """Заранее грузит модель, чтобы первая команда обрабатывалась быстро."""
        try:
            self._load()
            logger.info("STT-модель прогрета.")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Не удалось прогреть STT: {exc}")
            return False

    def transcribe(self, audio: np.ndarray | bytes) -> str:
        """Распознаёт PCM-аудио (int16 @16кГц или float32) в текст."""
        if isinstance(audio, bytes):
            audio = np.frombuffer(audio, dtype=np.int16)
        if audio.size == 0:
            return ""
        # faster-whisper ожидает float32 0..1
        if audio.dtype == np.int16:
            arr = audio.astype(np.float32) / 32768.0
        else:
            arr = audio.astype(np.float32)
        model = self._load()
        segments, _info = model.transcribe(
            arr,
            language=self.language,
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
            temperature=0.0,
            initial_prompt="Это короткая голосовая команда ассистенту.",
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
