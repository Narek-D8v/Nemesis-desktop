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
from nemus.voice.params import SAMPLE_RATE, VAD_SILENCE_MS, VAD_THRESHOLD
from nemus.voice.stt import STTEngine, build_hotwords
from nemus.voice.tts import TTSEngine, resolve_openai_key
from nemus.voice.wakeword import (
    VoskWakeWord,
    download_model,
    model_available,
    normalize_words,
)

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
        wake_only: bool = False,
    ) -> None:
        self.config = config
        self.on_text = on_text
        self.on_state = on_state
        self.on_amplitude = on_amplitude
        self.on_wake = on_wake
        self._wake_only = wake_only

        audio_cfg = AudioConfig()
        self.recorder = Recorder(audio_cfg)
        self.cleaner = create_noise_reducer(
            config.voice.noise, sample_rate=audio_cfg.sample_rate, block_ms=audio_cfg.block_ms
        )
        self.vad = EnergyVAD(threshold=VAD_THRESHOLD, silence_ms=VAD_SILENCE_MS)
        self.stt = None
        if not wake_only:
            self.stt = STTEngine(
                model=config.voice.stt.model,
                language=config.voice.stt.language,
                device=config.voice.stt.device,
                beam_size=config.voice.stt.beam_size,
                hotwords=build_hotwords(config),
            )
        self.tts = None
        if not wake_only:
            tts_cfg = config.voice.tts
            self.tts = TTSEngine(
                voice=tts_cfg.voice,
                rate=tts_cfg.rate,
                volume=tts_cfg.volume,
                engine=tts_cfg.engine,
                model=tts_cfg.openai_model,
                api_key=resolve_openai_key(config.llm.providers),
            )

        self._thread: threading.Thread | None = None
        self._running = False
        self._wakeword: VoskWakeWord | None = None
        self._last_wake = 0.0

    @property
    def wake_only(self) -> bool:
        """True — менеджер только слушает слово активации (без записи команд)."""
        return self._wake_only

    # ------------------------------------------------------------------- state
    def start(self) -> bool:
        """Запускает фоновый цикл. Возвращает False, если не удалось."""
        if self._running:
            return True
        if not self._init_wakeword():
            return False
        if self._wake_only and self._wakeword is None:
            logger.warning("Wake-слушатель не нужен: модель слова активации недоступна.")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # прогрев STT в фоне — первая команда не будет «тормозить»
        if self.stt is not None:
            threading.Thread(target=self.stt.warmup, daemon=True, name="stt-warmup").start()
        logger.info("Голосовой ввод запущен." if not self._wake_only else "Wake-слушатель запущен.")
        return True

    def stop(self) -> None:
        self._running = False
        # сначала закрываем поток — это разблокирует блокирующий read()
        # в фоновом потоке, и join() завершается мгновенно (без зависания UI)
        try:
            self.recorder.close()
        except Exception:  # noqa: BLE001
            pass
        if self.tts is not None:
            self.tts.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        # Разрываем цикл ссылок VoiceManager <-> Thread: иначе объект никогда
        # не освободится обычным счётчиком ссылок и попадёт под cyclic GC,
        # который может abort'ить процесс при разборе нативных ресурсов.
        self._thread = None
        close = getattr(self.cleaner, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("Голосовой ввод остановлен.")

    def speak(self, text: str, block: bool = False) -> None:
        """Озвучивает текст (для ответов ассистента)."""
        if self.tts is None:
            return
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
            if cfg.engine == "vosk":
                cfg.words = normalize_words(cfg.words)
            logger.info(
                f"Wake word слушает: {', '.join(cfg.words)} (модель {model_path})"
            )
            self._wakeword = VoskWakeWord(
                model_path, cfg.words, sample_rate=SAMPLE_RATE
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
                    # В режиме wake_only после срабатывания не нужно сразу
                    # запускать запись команды — app сам запустит Realtime.
                    if self._wake_only:
                        continue
                # В wake_only режиме без wakeword нечего делать — просто ждём
                if self._wake_only:
                    continue
                self._record_command()
        except Exception:  # noqa: BLE001
            logger.exception("Сбой голосового цикла")
        finally:
            self.recorder.close()
            try:
                self.cleaner.close()
            except Exception:  # noqa: BLE001
                pass
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
