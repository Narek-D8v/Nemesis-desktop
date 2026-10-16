"""Оркестратор голосового ввода: wake word → запись → STT → колбэк."""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from nemus.config import Config
from nemus.core.state import AssistState
from nemus.utils.logger import logger
from nemus.voice.audio import AudioConfig, EnergyVAD, Recorder
from nemus.voice.audio_dsp import AudioCleaner  # noqa: F401 (реэкспорт)
from nemus.voice.noise import create_noise_reducer
from nemus.voice.stt import STTEngine
from nemus.voice.tts import TTSEngine
from nemus.voice.wakeword import VoskWakeWord, download_model, model_available

OnText = Callable[[str], None]
OnState = Callable[[AssistState], None]
OnAmplitude = Callable[[float], None] | None
OnWake = Callable[[], None] | None


class VoiceManager:
    """Управляет микрофоном, wake word, распознаванием и синтезом."""

    def __init__(
        self,
        config: Config,
        on_text: OnText,
        on_state: OnState,
        on_amplitude: OnAmplitude = None,
        on_wake: OnWake = None,
    ) -> None:
        self.config = config
        self.on_text = on_text
        self.on_state = on_state
        self.on_amplitude = on_amplitude
        self.on_wake = on_wake

        audio_cfg = AudioConfig(sample_rate=16000)
        self.recorder = Recorder(audio_cfg)
        self.cleaner = create_noise_reducer(
            config.voice.noise, sample_rate=16000, block_ms=audio_cfg.block_ms
        )
        self.vad = EnergyVAD(threshold=0.012, silence_ms=600)
        self.stt = STTEngine(
            model=config.voice.stt.model,
            language=config.voice.stt.language,
            device=config.voice.stt.device,
        )
        self.tts = TTSEngine(
            voice=config.voice.tts.voice,
            rate=config.voice.tts.rate,
            volume=config.voice.tts.volume,
        )

        self._thread: threading.Thread | None = None
        self._running = False
        self._wakeword: VoskWakeWord | None = None
        self._last_wake = 0.0

    # ------------------------------------------------------------------- state
    def start(self) -> bool:
        """Запускает фоновый цикл. Возвращает False, если не удалось."""
        if self._running:
            return True
        if not self._init_wakeword():
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # прогрев STT в фоне — первая команда не будет «тормозить»
        threading.Thread(target=self.stt.warmup, daemon=True, name="stt-warmup").start()
        logger.info("Голосовой ввод запущен.")
        return True

    def stop(self) -> None:
        self._running = False
        # сначала закрываем поток — это разблокирует блокирующий read()
        # в фоновом потоке, и join() завершается мгновенно (без зависания UI)
        try:
            self.recorder.close()
        except Exception:  # noqa: BLE001
            pass
        self.tts.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        close = getattr(self.cleaner, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("Голосовой ввод остановлен.")

    def speak(self, text: str, block: bool = False) -> None:
        """Озвучивает текст (для ответов ассистента)."""
        self.tts.speak(text, block=block)

    # ---------------------------------------------------------------- wake word
    def _init_wakeword(self) -> bool:
        cfg = self.config.voice.wake_word
        if not cfg.enabled or cfg.engine in ("off", ""):
            self._wakeword = None
            return True
        model_path = cfg.vosk_model_path
        try:
            if not model_available(model_path):
                logger.info("Модель wake word не найдена — скачиваю…")
                from nemus.config import DATA_DIR

                model_path = str(download_model(DATA_DIR / "models"))
                cfg.vosk_model_path = model_path
            self._wakeword = VoskWakeWord(
                model_path, cfg.words, sample_rate=16000
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Wake word недоступен: {exc}. Слушаю без активации.")
            self._wakeword = None
            return True

    # -------------------------------------------------------------------- loop
    def _loop(self) -> None:
        try:
            self.recorder.open()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Нет доступа к микрофону: {exc}")
            self._running = False
            return

        self.on_state(AssistState.IDLE)
        try:
            while self._running:
                if self._wakeword is not None:
                    block = self.recorder.read_block()
                    self._report_amplitude(block)
                    clean = self.cleaner.clean(block)
                    pcm = clean.astype(np.int16).tobytes()
                    if self._wakeword.feed(pcm):
                        self._handle_wake()
                else:
                    self._record_command()
        except Exception:  # noqa: BLE001
            logger.exception("Сбой голосового цикла")
        finally:
            self.recorder.close()
            self._running = False

    def _handle_wake(self) -> None:
        """Реакция на слово активации: либо диалог (on_wake), либо запись команды."""
        now = time.monotonic()
        if now - self._last_wake < 1.5:  # антидребезг
            return
        self._last_wake = now
        if self._wakeword is not None:
            self._wakeword.reset()
        if self.on_wake is not None:
            # приложение само решит: облачный Realtime или локальный диалог
            try:
                self.on_wake()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Ошибка обработки слова активации: {exc}")
        else:
            self._record_command()

    def _record_command(self) -> None:
        """Записывает речь до паузы, распознаёт и отдаёт текст."""
        self.tts.stop()  # barge-in: прерываем ответ, если пользователь заговорил
        self.on_state(AssistState.LISTENING)
        self.vad.reset()
        audio_blocks: list[np.ndarray] = []
        block_ms = self.recorder.config.block_ms
        max_wait_blocks = max(1, int(3000 / block_ms))  # ждём начала речи до 3 с
        try:
            while self._running:
                block = self.recorder.read_block()
                self._report_amplitude(block)
                block = self.cleaner.clean(block)
                audio_blocks.append(block)
                if self.vad.feed(block, block_ms) == "end":
                    break
                # не дождались речи (ложное срабатывание) — выходим
                if not self.vad.speaking and len(audio_blocks) > max_wait_blocks:
                    audio_blocks = []
                    break
                # защита от «зависшего» звука
                if len(audio_blocks) > 1000:
                    break
        finally:
            # THINKING ставим только если действительно поймали речь и ещё не остановлены
            if self._running and audio_blocks:
                self.on_state(AssistState.THINKING)

        # остановили во время записи (кнопка «Стоп») — ничего не распознаём
        if not self._running or not audio_blocks:
            self.on_state(AssistState.IDLE)
            return
        audio = np.concatenate(audio_blocks)
        # дать STT модели прогрузиться
        text = self.stt.transcribe(audio)
        if text:
            logger.info(f"Распознано: {text}")
            self.on_text(text)
        else:
            logger.info("Пустая расшифровка.")
        self.on_state(AssistState.IDLE)

    def _report_amplitude(self, block: np.ndarray) -> None:
        if self.on_amplitude is None:
            return
        rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
        self.on_amplitude(min(1.0, rms * 8.0))
