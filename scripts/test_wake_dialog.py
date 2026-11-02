"""Проверка: слово активации вызывает on_wake (а не одиночную запись)."""

from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SR = 16000
BLOCK = 480


class FakeStream:
    def __init__(self, **kw):
        pass

    def start(self):
        return self

    def stop(self):
        pass

    def close(self):
        pass

    def read(self, frames):
        t = time.time() * 300.0
        x = (np.sin(np.arange(frames) * 0.1 + t) * 6000).astype(np.int16)
        time.sleep(0.003)
        return x, None


def main() -> None:
    import nemus.voice.audio as audio_mod

    audio_mod.sd.InputStream = FakeStream

    from nemus.config import get_config
    from nemus.voice import manager as mgr

    # --- заглушка wake word: срабатывает один раз ---
    class FakeWake:
        fired = False

        def __init__(self, *a, **k):
            pass

        def feed(self, block) -> bool:
            if not FakeWake.fired:
                FakeWake.fired = True
                return True
            return False

        def reset(self):
            pass

    mgr.VoskWakeWord = FakeWake
    mgr.model_available = lambda *a, **k: True

    cfg = get_config()
    cfg.voice.wake_word.enabled = True
    cfg.voice.wake_word.engine = "vosk"
    cfg.voice.noise.provider = "off"

    woke = threading.Event()
    recorded = []

    vm = mgr.VoiceManager(
        config=cfg,
        on_text=lambda t: recorded.append(t),
        on_state=lambda s: None,
        on_amplitude=lambda a: None,
        on_wake=woke.set,
    )
    # заглушки STT/TTS, чтобы не грузить модели/звук
    vm.stt.warmup = lambda: True
    vm.stt.transcribe = lambda audio: recorded.append(("stt", len(audio))) or "x"
    vm.tts.stop = lambda: None

    assert vm.start(), "не удалось запустить голосовой ввод"
    assert woke.wait(3.0), "on_wake не был вызван по слову активации"
    vm.stop()

    # одиночное распознавание запускаться не должно — управление ушло в on_wake
    assert not any(isinstance(r, tuple) for r in recorded), f"сработала обычная запись: {recorded}"
    print("ok  wake word -> on_wake (диалог), обычная запись не запускалась")

    # --- без on_wake: старое поведение (запись команды) ---
    FakeWake.fired = False
    recorded2 = []
    vm2 = mgr.VoiceManager(
        config=cfg,
        on_text=lambda t: recorded2.append(t),
        on_state=lambda s: None,
        on_amplitude=lambda a: None,
        on_wake=None,
    )
    vm2.stt.warmup = lambda: True
    vm2.stt.transcribe = lambda audio: recorded2.append(("stt", len(audio))) or "команда"
    vm2.tts.stop = lambda: None
    assert vm2.start()
    deadline = time.time() + 8.0
    while time.time() < deadline and not any(isinstance(r, tuple) for r in recorded2):
        time.sleep(0.05)
    vm2.stop()
    assert any(isinstance(r, tuple) for r in recorded2), "без on_wake запись команды не сработала"
    print("ok  без on_wake -> обычная запись команды (обратная совместимость)")

    print("WAKE->DIALOG TEST PASSED")


if __name__ == "__main__":
    main()
