"""Захват микрофона и воспроизведение для realtime-сессий."""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd

from nemus.utils.logger import logger
from nemus.voice.noise import BuiltinNoiseReducer


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Пересэмплировка моно PCM16 линейной интерполяцией."""
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_out = max(1, int(round(len(samples) * dst_rate / src_rate)))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    out = np.interp(x_new, x_old, samples)
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """Moно PCM16 -> float32 в диапазоне [-1, 1]."""
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def float32_to_pcm16(arr: np.ndarray) -> bytes:
    """float32 [-1, 1] -> mono PCM16."""
    return np.clip(arr * 32768.0, -32768, 32767).astype(np.int16).tobytes()


class MicStreamer:
    """Захват моно-аудио с микрофона в отдельном потоке.

    Каждый блок передаётся в ``on_chunk`` (PCM16 байты) — колбэк вызывается
    из потока звуковой карты, поэтому сам колбэк должен быть потокобезопасным.
    """

    def __init__(
        self,
        sample_rate: int,
        on_chunk,
        block_ms: int = 60,
        cleaner=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.on_chunk = on_chunk
        self.blocksize = max(1, int(sample_rate * block_ms / 1000))
        self.cleaner = (
            cleaner
            if cleaner is not None
            else BuiltinNoiseReducer(sample_rate, block_ms=block_ms)
        )
        self._stream: sd.InputStream | None = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        if self._running:
            return True

        def _callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                logger.debug(f"Mic status: {status}")
            try:
                data = np.asarray(indata, dtype=np.int16).reshape(-1)
                if self.cleaner is not None:
                    data = self.cleaner.clean(data)
                self.on_chunk(data.tobytes())
            except Exception:  # noqa: BLE001
                pass

        try:
            with self._lock:
                if self._stream is not None:
                    return True
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=self.blocksize,
                    callback=_callback,
                )
                stream.start()
                self._stream = stream
            self._running = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Не удалось открыть микрофон: {exc}")
            return False

    def stop(self) -> None:
        """Останавливает поток. Идемпотентно и потокобезопасно.

        Раньше одновременный вызов из GUI-потока и потока сессии приводил к
        двойному ``Stream.close()`` и порче кучи PortAudio (0xC0000374).
        """
        self._running = False
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        close = getattr(self.cleaner, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


class SpeakerPlayer:
    """Плавное воспроизведение моно PCM16 через callback-вывод sounddevice.

    Вместо по-чанковой записи (что давало обрывы/дрожание) данные складываются
    в потокобезопасный буфер, а звуковая карта сама вытягивает из него ровные
    блоки в тайминге железа. Небольшой пре-буфер гасит джиттер сети.
    """

    def __init__(self, sample_rate: int, prebuffer_ms: int = 120, block_ms: int = 20):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._stream: sd.OutputStream | None = None
        self._running = False
        self._playing = False
        self._last_data_time = 0.0
        self._blocksize = max(1, int(sample_rate * block_ms / 1000))
        self._prebuffer_bytes = max(2, int(sample_rate * prebuffer_ms / 1000)) * 2
        self.underruns = 0

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return (
                self._running
                and self._playing
                and (len(self._buf) > 0 or (time.monotonic() - self._last_data_time) < 0.15)
            )

    def start(self) -> bool:
        if self._running:
            return True
        try:
            with self._lock:
                if self._stream is not None:
                    self._running = True
                    return True
                stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=self._blocksize,
                    callback=self._callback,
                    latency="low",
                )
                stream.start()
                self._stream = stream
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Не удалось открыть динамики: {exc}")
            return False
        self._running = True
        return True

    def _callback(self, outdata, frames, time_info, status):  # noqa: ANN001
        need = frames  # в моно кадр == 1 сэмпл int16
        with self._lock:
            # если данных нет дольше 300 мс — ответ закончился, ждём пре-буфер заново
            if (
                self._playing
                and not self._buf
                and (time.monotonic() - self._last_data_time) > 0.3
            ):
                self._playing = False
            # короткий ответ: накопилось меньше пре-буфера, но новых данных нет —
            # проигрываем то, что есть
            if (
                not self._playing
                and self._buf
                and (time.monotonic() - self._last_data_time) > 0.4
            ):
                self._playing = True
            if not self._playing and len(self._buf) >= self._prebuffer_bytes:
                self._playing = True
            if not self._playing:
                outdata[:, 0] = 0
                return
            take = min(len(self._buf) // 2, need)
            if take <= 0:
                outdata[:, 0] = 0
                self.underruns += 1
                return
            raw = bytes(self._buf[: take * 2])
            del self._buf[: take * 2]
            data = np.frombuffer(raw, dtype=np.int16)
            outdata[:take, 0] = data
            if take < need:
                outdata[take:, 0] = 0
                self.underruns += 1

    def play(self, chunk: bytes) -> None:
        if not self._running or not chunk:
            return
        with self._lock:
            self._buf += chunk
            self._last_data_time = time.monotonic()
            if not self._playing and len(self._buf) >= self._prebuffer_bytes:
                self._playing = True

    def clear(self) -> None:
        """Очищает буфер (прерывание ответа) — следующий ответ заново пре-буферится."""
        with self._lock:
            self._buf.clear()
            self._playing = False

    def stop(self) -> None:
        """Останавливает вывод. Идемпотентно и потокобезопасно (см. MicStreamer.stop)."""
        self._running = False
        with self._lock:
            stream, self._stream = self._stream, None
            self._buf.clear()
            self._playing = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
