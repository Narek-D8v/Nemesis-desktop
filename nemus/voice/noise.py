"""Подавление шума микрофона: встроенный фильтр, RNNoise или Krisp SDK.

Предоставляет единый интерфейс ``NoiseReducer.clean(block) -> block``
(int16 -> int16), который подставляется вместо ``AudioCleaner.clean``.

* ``BuiltinNoiseReducer`` — обёртка над существующим ``AudioCleaner``
  (ВЧ-фильтр + шумовые ворота). Работает всегда, без внешних зависимостей.
* ``RNNoiseReducer`` — локальный AI-шумодав RNNoise (BSD, CPU, без API и
  интернета). Рекомендуемая замена Krisp. Работает на 48 кГц, поэтому
  вход ресемплится 16k->48k и обратно (через PyAV).
* ``KrispNoiseReducer`` — коммерческий Krisp VIVA SDK (AI-подавление шума
  и голосовой изоляции). Требует нативный SDK ``krisp_audio``, API-ключ
  лицензии и модель ``.kef``. Если SDK/ключ/модель недоступны — фабрика
  откатывается на встроенный фильтр (с предупреждением в логе).

RNNoise обрабатывает фиксированные кадры 480 отсчётов, Krisp — кадры
``frame_ms``; входные блоки буферизуются и собираются обратно.
"""

from __future__ import annotations

import collections
import threading
from typing import Any

import numpy as np

from nemus.config import NoiseConfig
from nemus.utils.logger import logger
from nemus.voice.audio_dsp import AudioCleaner
from nemus.voice.params import BLOCK_MS, SAMPLE_RATE


class NoiseReducer:
    """Единый интерфейс подавления шума (int16 -> int16)."""

    name = "base"

    def reset(self) -> None:  # noqa: B027
        """Сбрасывает внутреннее состояние (при старте новой сессии)."""

    def clean(self, block: np.ndarray) -> np.ndarray:
        """Возвращает int16-блок той же длины, подавив шум."""
        return block

    def close(self) -> None:  # noqa: B027
        """Освобождает ресурсы (сессии SDK и т.п.)."""


class BuiltinNoiseReducer(NoiseReducer):
    """Встроенный фильтр: ВЧ + шумовые ворота (AudioCleaner)."""

    name = "builtin"

    def __init__(self, sample_rate: int = SAMPLE_RATE, block_ms: int = BLOCK_MS) -> None:
        self._cleaner = AudioCleaner(sample_rate=sample_rate, block_ms=block_ms)

    def reset(self) -> None:
        self._cleaner.reset()

    def clean(self, block: np.ndarray) -> np.ndarray:
        return self._cleaner.clean(block)


class OffNoiseReducer(NoiseReducer):
    """Без обработки (provider=off)."""

    name = "off"


class RNNoiseReducer(NoiseReducer):
    """Локальный AI-шумодав RNNoise (48 кГц, кадр 480). Без API и интернета.

    Входные блоки (любая частота) ресемплятся в 48 кГц через PyAV,
    обрабатываются покадрово нативным RNNoise и ресемплятся обратно.
    Падает с RuntimeError, если нативная библиотека недоступна.
    """

    name = "rnnoise"
    TARGET_RATE = 48000

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        from nemus.voice import rnnoise_native as rn

        if not rn.available():
            raise RuntimeError(
                "Библиотека RNNoise не найдена "
                f"({rn.dll_path()}). Положите rnnoise.dll в nemus/voice/_rnnoise/."
            )
        self._den = rn.Denoiser()
        self._frame = self._den.frame_size
        self.sample_rate = sample_rate

        try:
            import av
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"PyAV недоступен для ресемплинга: {exc}") from exc
        self._av = av
        self._up = av.AudioResampler(format="s16", layout="mono", rate=self.TARGET_RATE)
        self._down = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)

        self._up_buf: list[int] = []  # int16 @ 48k
        self._out_buf: list[int] = []  # int16 @ sample_rate (denoised)

    def _resample(self, resampler: Any, arr: np.ndarray, in_rate: int) -> np.ndarray:
        if arr.size == 0:
            return np.zeros(0, dtype=np.int16)
        frame = self._av.AudioFrame.from_ndarray(
            np.ascontiguousarray(arr, dtype=np.int16).reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = in_rate
        out = resampler.resample(frame)
        parts = [f.to_ndarray().reshape(-1).astype(np.int16) for f in out]
        if not parts:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(parts)

    def reset(self) -> None:
        self._up_buf.clear()
        self._out_buf.clear()

    def clean(self, block: np.ndarray) -> np.ndarray:
        if block.size == 0:
            return block
        n = block.shape[0]

        up = self._resample(self._up, block, self.sample_rate)
        self._up_buf.extend(int(v) for v in up)

        denoised: list[np.ndarray] = []
        while len(self._up_buf) >= self._frame:
            frame = np.array(self._up_buf[: self._frame], dtype=np.int16)
            del self._up_buf[: self._frame]
            out, _prob = self._den.process(frame)
            denoised.append(out)
        if denoised:
            down = self._resample(self._down, np.concatenate(denoised), self.TARGET_RATE)
            self._out_buf.extend(int(v) for v in down)

        take = self._out_buf[:n]
        del self._out_buf[:n]
        y = np.array(take, dtype=np.int16)
        if y.shape[0] < n:
            y = np.pad(y, (0, n - y.shape[0]))
        return y

    def close(self) -> None:
        try:
            self._den.close()
        except Exception:  # noqa: BLE001
            pass
        # PyAV-ресемплеры держат нативные ресурсы; освобождаем их здесь,
        # чтобы они не разбирались позже сборщиком мусора (это роняло процесс).
        self._up = None
        self._down = None


class KrispNoiseReducer(NoiseReducer):
    """Krisp VIVA SDK (коммерческий AI-шумодав). Инициализирует SDK один раз
    на процесс, создаёт NC-сессию и обрабатывает фиксированные кадры.

    Падает с RuntimeError, если SDK/ключ/модель недоступны (перехватывается
    фабрикой).
    """

    name = "krisp"
    _global_inited: bool = False

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        api_key: str = "",
        model_path: str = "",
        frame_ms: int = 10,
        suppression: float = 100.0,
    ) -> None:
        try:
            import krisp_audio  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Krisp SDK (krisp_audio) не установлен. Скачайте SDK с "
                "https://krisp.ai/developers и установите python-биндинги."
            ) from exc

        if not api_key:
            raise RuntimeError("Не задан API-ключ Krisp (voice.noise.krisp_api_key).")
        if not model_path:
            raise RuntimeError("Не задан путь к модели Krisp (voice.noise.krisp_model_path).")

        self._krisp = krisp_audio
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.suppression = float(suppression)
        self._frame_samples = max(1, int(round(sample_rate * frame_ms / 1000)))
        self._buffer: collections.deque[float] = collections.deque()
        self._out: list[np.ndarray] = []
        self._lock = threading.Lock()

        if not KrispNoiseReducer._global_inited:
            try:
                krisp_audio.globalInit(api_key, self._log, krisp_audio.LogLevel.Off)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Krisp globalInit не удался: {exc}") from exc
            KrispNoiseReducer._global_inited = True

        self._nc = self._create_session(model_path, frame_ms)

    # ------------------------------------------------------------------ session
    def _create_session(self, model_path: str, frame_ms: int) -> Any:
        k = self._krisp
        cfg = k.NcSessionConfig()
        rate = self._rate_enum()
        frame = self._frame_enum(frame_ms)
        self._set(cfg, ("inputSampleRate", "inputSampleRate"), rate)
        self._set(cfg, ("outputSampleRate", "outputSampleRate"), rate)
        self._set(cfg, ("inputFrameDuration", "inputFrameDuration"), frame)
        model_info = getattr(k, "ModelInfo", None)
        if model_info is not None:
            info = model_info()
            self._set(info, ("path", "modelPath"), model_path)
            self._set(cfg, ("modelInfo", "model"), info)
        create = getattr(k.NcFloat, "create", None)
        if create is not None:
            return create(cfg)
        return k.NcFloat(cfg)

    def _rate_enum(self) -> Any:
        k = self._krisp
        table = {
            8000: "Sr8000Hz",
            16000: "Sr16000Hz",
            24000: "Sr24000Hz",
            32000: "Sr32000Hz",
            44100: "Sr44100Hz",
            48000: "Sr48000Hz",
        }
        return getattr(k.SamplingRate, table.get(self.sample_rate, "Sr16000Hz"))

    def _frame_enum(self, frame_ms: int) -> Any:
        k = self._krisp
        name = {10: "Fd10ms", 15: "Fd15ms", 20: "Fd20ms", 30: "Fd30ms", 32: "Fd32ms"}.get(
            frame_ms, "Fd10ms"
        )
        return getattr(k.FrameDuration, name)

    @staticmethod
    def _set(obj: Any, names: tuple[str, ...], value: Any) -> None:
        for n in names:
            if hasattr(obj, n):
                try:
                    setattr(obj, n, value)
                    return
                except Exception:  # noqa: BLE001
                    continue

    @staticmethod
    def _log(_level: Any, _message: str) -> None:
        return

    # ------------------------------------------------------------------ process
    def reset(self) -> None:
        self._buffer.clear()
        self._out.clear()

    def clean(self, block: np.ndarray) -> np.ndarray:
        if block.size == 0:
            return block
        x = block.astype(np.float32) / 32768.0
        self._buffer.extend(x.tolist())

        frame = self._frame_samples
        processed: list[np.ndarray] = []
        buf = self._buffer
        while len(buf) >= frame:
            frame_arr = np.array([buf.popleft() for _ in range(frame)], dtype=np.float32)
            out = self._process_frame(frame_arr)
            processed.append(np.asarray(out, dtype=np.float32))

        if not processed:
            return np.zeros_like(block)
        y = np.concatenate(processed)
        if y.shape[0] < block.shape[0]:
            y = np.pad(y, (0, block.shape[0] - y.shape[0]))
        else:
            y = y[: block.shape[0]]
        return np.clip(y * 32768.0, -32768, 32767).astype(np.int16)

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        with self._lock:
            nc = self._nc
            if nc is None:
                return frame  # сессия уже закрыта — пропускаем без гонки
            proc = getattr(nc, "process", None)
            if proc is None:
                proc = nc.processFloat
            return proc(frame, self.suppression)

    def close(self) -> None:
        # Идемпотентно и потокобезопасно: stop() (GUI) и finally фонового
        # цикла могут звать close() одновременно — двойной destroy сессии
        # (нативный SDK) портит кучу (0xC0000374).
        with self._lock:
            nc, self._nc = self._nc, None
        if nc is None:
            return
        for attr in ("destroy", "close", "delete"):
            fn = getattr(nc, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                break


# ---------------------------------------------------------------------- factory
def create_noise_reducer(
    cfg: NoiseConfig, sample_rate: int = SAMPLE_RATE, block_ms: int = BLOCK_MS
) -> NoiseReducer:
    """Создаёт редюсер шума по конфигу; при любой ошибке провайдера — откат
    на встроенный фильтр (или off, если так задано)."""
    provider = (cfg.provider or "builtin").lower()

    if provider == "off":
        return OffNoiseReducer()

    if provider == "rnnoise":
        try:
            reducer = RNNoiseReducer(sample_rate=sample_rate)
            logger.info("Шумодав: RNNoise активен (локально, без API)")
            return reducer
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"RNNoise недоступен ({exc}); использую встроенный фильтр")
            return BuiltinNoiseReducer(sample_rate=sample_rate, block_ms=block_ms)

    if provider == "krisp":
        try:
            reducer = KrispNoiseReducer(
                sample_rate=sample_rate,
                api_key=cfg.krisp_api_key,
                model_path=cfg.krisp_model_path,
                frame_ms=cfg.krisp_frame_ms,
                suppression=cfg.krisp_suppression,
            )
            logger.info("Шумодав: Krisp SDK активен")
            return reducer
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Krisp недоступен ({exc}); использую встроенный фильтр")
            return BuiltinNoiseReducer(sample_rate=sample_rate, block_ms=block_ms)

    return BuiltinNoiseReducer(sample_rate=sample_rate, block_ms=block_ms)
