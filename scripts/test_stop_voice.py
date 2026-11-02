"""Регресс: кнопка «Стоп» должна быстро гасить голосовой ввод и отменять распознавание."""

from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import get_config  # noqa: E402
from nemus.voice.manager import VoiceManager  # noqa: E402

SR = 16000
BLOCK = 480


class FakeStream:
    """Микрофон-заглушка: громкий «голос», чтение быстро возвращает блок."""

    def __init__(self, samplerate=SR, channels=1, dtype="int16", blocksize=BLOCK, **kw):
        self.blocksize = blocksize

    def start(self):
        return self

    def stop(self):
        pass

    def close(self):
        pass

    def read(self, frames):
        t = time.time() * 200.0
        x = (np.sin(np.arange(frames) * 0.05 + t) * 8000).astype(np.int16)
        time.sleep(0.005)  # имитация задержки устройства, но быстро
        return x, None


def main() -> None:
    import nemus.voice.audio as audio_mod

    audio_mod.sd.InputStream = FakeStream

    cfg = get_config()
    cfg.voice.wake_word.enabled = False  # идём сразу в цикл записи

    transcribe_calls = []

    from nemus.voice import stt as stt_mod

    stt_mod.STTEngine.transcribe = lambda self, audio: transcribe_calls.append(1) or "тест"
    stt_mod.STTEngine.warmup = lambda self: True

    from nemus.voice import tts as tts_mod

    tts_mod.TTSEngine.__init__ = lambda self, *a, **k: setattr(self, "_noop", True)
    tts_mod.TTSEngine.stop = lambda self: None
    tts_mod.TTSEngine.speak = lambda self, *a, **k: None

    texts = []
    vm = VoiceManager(
        config=cfg,
        on_text=lambda t: texts.append(t),
        on_state=lambda s: None,
        on_amplitude=lambda a: None,
    )
    # подавляем шумодав, чтобы не грузить rnnoise-ресемплер в тесте
    from nemus.voice.noise import BuiltinNoiseReducer

    vm.cleaner = BuiltinNoiseReducer(sample_rate=SR)

    assert vm.start(), "не удалось запустить голосовой ввод"

    # даём циклу войти в запись (VAD поймает «голос» из FakeStream)
    deadline = time.time() + 2.0
    while time.time() < deadline and not vm.vad.speaking:
        time.sleep(0.02)
    assert vm.vad.speaking, "VAD не перешёл в режим речи"

    t0 = time.time()
    vm.stop()
    dt = time.time() - t0

    assert vm._thread is None or not vm._thread.is_alive(), "поток не остановился"
    assert dt < 1.0, f"stop() завис на {dt:.2f}s"
    assert not transcribe_calls, f"распознавание не отменено: {transcribe_calls} вызовов"
    assert not texts, f"текст ушёл в агент после Стоп: {texts}"

    print(f"ok  stop() за {dt*1000:.0f}ms, поток погас, распознавание отменено")
    print("STOP BUTTON TEST PASSED")


if __name__ == "__main__":
    main()
