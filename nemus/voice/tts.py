"""Синтез речи (TTS) через edge-tts с плавным очередным воспроизведением."""

from __future__ import annotations

import asyncio
import queue
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from nemus.utils.logger import logger

_SENTENCE_RE = re.compile(r"[^.!?…\n]+[.!?…\n]?", re.UNICODE)


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    """Дробит текст на фразы, чтобы речь начиналась быстрее и не рвалась."""
    parts: list[str] = []
    for match in _SENTENCE_RE.finditer(text):
        piece = match.group().strip()
        if not piece:
            continue
        if len(piece) > max_len:
            for i in range(0, len(piece), max_len):
                parts.append(piece[i : i + max_len])
        else:
            parts.append(piece)
    return parts or ([text.strip()] if text.strip() else [])


class TTSEngine:
    """Синтезирует и воспроизводит речь строго последовательно (без наложений)."""

    def __init__(
        self, voice: str = "ru-RU-DmitryNeural", rate: str = "+0%", volume: str = "+0%"
    ) -> None:
        self.voice = voice
        self.rate = rate
        self.volume = volume

        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        self._stop_flag = threading.Event()
        self._speaking = threading.Event()
        self._play_lock = threading.Lock()

    # ------------------------------------------------------------- synthesize
    def synthesize(self, text: str) -> Path:
        """Синтезирует текст в временный mp3 и возвращает путь."""
        import edge_tts

        tmp = Path(tempfile.gettempdir()) / f"nemus_tts_{uuid.uuid4().hex}.mp3"
        communicate = edge_tts.Communicate(
            text, self.voice, rate=self.rate, volume=self.volume
        )
        asyncio.run(communicate.save(str(tmp)))
        return tmp

    # ---------------------------------------------------------------- playback
    @staticmethod
    def _decode(path: Path) -> tuple[Any, int]:
        import av
        import numpy as np

        container = av.open(str(path))
        stream = container.streams.audio[0]
        sample_rate = stream.rate or 24000
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        chunks: list[Any] = []
        for frame in container.decode(stream):
            frame.pts = None
            for resampled in resampler.resample(frame):
                arr = resampled.to_ndarray()
                if arr.ndim == 2:
                    arr = arr[0]
                chunks.append(arr)
        container.close()
        if not chunks:
            return np.zeros(0, dtype=np.float32), sample_rate
        return np.concatenate(chunks), sample_rate

    # ------------------------------------------------------------------ worker
    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True, name="tts")
        self._worker.start()

    def _loop(self) -> None:
        while self._running:
            try:
                text = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if text is None:
                break
            try:
                with self._play_lock:
                    self._play_text(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"TTS сбой: {exc}")

    # --------------------------------------------------------------- speaking
    def _play_text(self, text: str) -> None:
        import sounddevice as sd

        self._stop_flag.clear()
        sentences = split_sentences(text)
        if not sentences:
            return

        current: Path | None = None
        try:
            self._speaking.set()
            for i, sentence in enumerate(sentences):
                if self._stop_flag.is_set():
                    break
                # синтез текущего предложения (первое — сразу, дальше предзагрузка)
                if current is None:
                    current = self.synthesize(sentence)
                # предзагрузка следующего предложения параллельно воспроизведению
                nxt: dict[str, Any] = {}
                if i + 1 < len(sentences):
                    nxt["path"] = self.synthesize(sentences[i + 1])
                audio, sample_rate = self._decode(current)
                if audio.size and not self._stop_flag.is_set():
                    sd.play(audio, sample_rate)
                    sd.wait()
                current.unlink(missing_ok=True)
                current = nxt.get("path")
                if self._stop_flag.is_set():
                    if current is not None:
                        Path(current).unlink(missing_ok=True)
                    current = None
                    break
        finally:
            self._speaking.clear()
            if current is not None:
                Path(current).unlink(missing_ok=True)

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    # ------------------------------------------------------------------ speak
    def speak(self, text: str, block: bool = False) -> None:
        """Ставит текст в очередь (block=False) или озвучивает сразу (block=True)."""
        if not text.strip():
            return
        if block:
            with self._play_lock:
                self._play_text(text)
            return
        self._ensure_worker()
        self._queue.put(text)

    def stop(self) -> None:
        """Обрывает текущую речь и очищает очередь (barge-in)."""
        self._stop_flag.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        self._speaking.clear()

    def shutdown(self) -> None:
        self.stop()
        self._running = False
        self._queue.put(None)
