"""Локальный аналог режима реального времени (офлайн-диалог).

Непрерывный голосовой диалог без облака: микрофон → VAD → локальный STT →
агент (LLM + инструменты) → локальный TTS. Используется, когда нет интернета
или API-ключа, а также как самостоятельный режим (``provider: local``).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import numpy as np

from nemus.config import Config
from nemus.core.agent import Agent
from nemus.core.permissions import PermissionManager
from nemus.core.tools.base import Tool
from nemus.core.tools.registry import ToolRegistry
from nemus.utils.logger import logger
from nemus.voice.audio import AudioConfig, EnergyVAD, Recorder
from nemus.voice.noise import create_noise_reducer
from nemus.voice.params import SAMPLE_RATE, VAD_SILENCE_MS, VAD_THRESHOLD
from nemus.voice.realtime.base import RealtimeCallbacks, RealtimeState
from nemus.voice.stt import STTEngine, build_hotwords
from nemus.voice.tts import TTSEngine, resolve_openai_key

#: порог RMS для перебивания речи ассистента (заметно выше порога VAD)
_BARGE_RMS = 0.05


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


class LocalLiveSession:
    """Офлайн-сессия непрерывного диалога (локальный аналог Realtime).

    Публичный интерфейс совпадает с облачными realtime-сессиями
    (``start``/``stop``/``send_text``), поэтому приложение управляет ими одинаково.
    """

    provider = "local"
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        *,
        config: Config,
        callbacks: RealtimeCallbacks,
        registry: ToolRegistry,
        permissions: PermissionManager,
        confirm: Callable[[Tool, dict[str, Any]], bool] | None = None,
        system_prompt: str = "",
        barge_in: bool = True,
        llm: Any = None,
        memory: Any = None,
    ) -> None:
        self.config = config
        self.cb = callbacks
        self.registry = registry
        self.permissions = permissions
        self.confirm = confirm
        self.system_prompt = system_prompt
        self.barge_in = barge_in
        self.llm = llm
        self.memory = memory

        self._thread: threading.Thread | None = None
        self._running = False
        self._recorder: Recorder | None = None
        self._cleaner: Any = None
        self._stt: STTEngine | None = None
        self._tts: TTSEngine | None = None
        self._agent: Agent | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="local-live")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        # закрываем поток — это разблокирует блокирующий read()
        try:
            if self._recorder is not None:
                self._recorder.close()
        except Exception:  # noqa: BLE001
            pass
        if self._tts is not None:
            self._tts.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._close_cleaner()

    def send_text(self, text: str) -> None:
        """Внешний текстовый ввод (из GUI) — обрабатывается как реплика."""
        if not text.strip() or not self._running:
            return
        threading.Thread(
            target=self._respond, args=(text,), daemon=True, name="local-live-respond"
        ).start()

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        cfg = self.config
        audio_cfg = AudioConfig(sample_rate=self.sample_rate)
        self._recorder = Recorder(audio_cfg)
        self._cleaner = create_noise_reducer(
            cfg.voice.noise, sample_rate=self.sample_rate, block_ms=audio_cfg.block_ms
        )
        self._tts = TTSEngine(
            voice=cfg.voice.tts.voice,
            rate=cfg.voice.tts.rate,
            volume=cfg.voice.tts.volume,
            engine=cfg.voice.tts.engine,
            model=cfg.voice.tts.openai_model,
            api_key=resolve_openai_key(cfg.llm.providers),
        )
        self._stt = STTEngine(
            model=cfg.voice.stt.model,
            language=cfg.voice.stt.language,
            device=cfg.voice.stt.device,
            beam_size=cfg.voice.stt.beam_size,
            hotwords=build_hotwords(cfg),
        )
        self._agent = Agent(
            config=cfg,
            llm=self.llm,
            registry=self.registry,
            permissions=self.permissions,
            confirm=self.confirm,
            memory=self.memory,
        )

        try:
            self._recorder.open()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Локальный диалог: нет доступа к микрофону: {exc}")
            self._emit_error(f"Нет доступа к микрофону: {exc}")
            self._running = False
            self._emit_state(RealtimeState.CLOSED)
            return

        threading.Thread(target=self._stt.warmup, daemon=True, name="local-stt-warmup").start()
        self._emit_state(RealtimeState.READY)
        vad = EnergyVAD(threshold=VAD_THRESHOLD, silence_ms=VAD_SILENCE_MS)

        try:
            while self._running:
                self._emit_state(RealtimeState.LISTENING)
                audio = self._capture_turn(vad)
                if not self._running:
                    break
                if audio.size == 0:
                    continue
                self._emit_state(RealtimeState.THINKING)
                text = self._stt.transcribe(audio) if self._stt else ""
                if not text:
                    continue
                logger.info(f"Локальный диалог, распознано: {text}")
                self._emit_user_text(text)
                self._respond(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Сбой локального диалога")
            self._emit_error(str(exc))
        finally:
            self._running = False
            try:
                if self._recorder is not None:
                    self._recorder.close()
            except Exception:  # noqa: BLE001
                pass
            self._close_cleaner()
            self._emit_state(RealtimeState.CLOSED)

    # -------------------------------------------------------------- turn I/O
    def _capture_turn(self, vad: EnergyVAD) -> np.ndarray:
        """Записывает одну реплику до паузы. Пустой массив — речи не было."""
        assert self._recorder is not None
        vad.reset()
        blocks: list[np.ndarray] = []
        block_ms = self._recorder.config.block_ms
        max_wait_blocks = max(1, int(4000 / block_ms))  # ждём начала речи до 4 с
        while self._running:
            block = self._recorder.read_block()
            block = self._cleaner.clean(block)
            # пока ассистент говорит — только следим за перебиванием
            if self._tts is not None and self._tts.is_speaking:
                if self.barge_in and _rms(block) >= _BARGE_RMS:
                    self._tts.stop()
                    vad.reset()
                    blocks = []
                continue
            blocks.append(block)
            event = vad.feed(block, block_ms)
            if event == "end":
                break
            if not vad.speaking and len(blocks) > max_wait_blocks:
                blocks = []
                break
            if len(blocks) > 1500:  # защита от «зависшего» звука
                break
        if not self._running or not blocks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(blocks)

    def _respond(self, text: str) -> None:
        """Прогоняет текст через агента и озвучивает финальный ответ."""
        if self._agent is None:
            return
        final = ""
        try:
            for event in self._agent.run(text):
                if not self._running:
                    return
                if event.kind == "final":
                    final = event.text or final
                elif event.kind == "error":
                    self._emit_error(event.error or "Ошибка агента")
                    return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Локальный диалог: сбой агента")
            self._emit_error(str(exc))
            return

        if not final.strip():
            return
        self._emit_assistant_text(final)
        self._emit_state(RealtimeState.SPEAKING)
        if self._tts is not None:
            self._tts.speak(final, block=False)
            # дождаться старта речи (макс 0.5 с), чтобы захват не поймал хвост паузы
            deadline = time.monotonic() + 0.5
            while self._running and not self._tts.is_speaking and time.monotonic() < deadline:
                time.sleep(0.02)

    # --------------------------------------------------------------- helpers
    def _close_cleaner(self) -> None:
        close = getattr(self._cleaner, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _emit_state(self, state: RealtimeState) -> None:
        if self.cb.on_state:
            try:
                self.cb.on_state(state)
            except Exception:  # noqa: BLE001
                pass

    def _emit_user_text(self, text: str) -> None:
        if text and self.cb.on_user_text:
            self.cb.on_user_text(text)

    def _emit_assistant_text(self, text: str) -> None:
        if text and self.cb.on_assistant_text:
            self.cb.on_assistant_text(text)

    def _emit_error(self, message: str) -> None:
        logger.warning(f"Локальный диалог: {message}")
        if self.cb.on_error:
            self.cb.on_error(message)
