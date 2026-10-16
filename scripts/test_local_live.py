"""Проверка локального аналога Realtime: офлайн-диалог STT → агент → TTS."""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SR = 16000
BLOCK = 480


class FakeStream:
    """Скриптовый микрофон: 15 громких блоков, затем тишина (одна реплика)."""

    def __init__(self, **kw):
        self._n = 0

    def start(self):
        return self

    def stop(self):
        pass

    def close(self):
        pass

    def read(self, frames):
        self._n += 1
        loud = self._n <= 15
        amp = 4000 if loud else 0
        x = (np.sin(np.arange(frames) * 0.1) * amp).astype(np.int16)
        time.sleep(0.002)
        return x, None


def main() -> None:
    import nemus.voice.audio as audio_mod

    audio_mod.sd.InputStream = FakeStream

    from nemus.config import get_config
    from nemus.voice import local_live as ll

    # --- заглушки тяжёлых зависимостей ---
    class FakeSTT:
        def __init__(self, *a, **k):
            pass

        def warmup(self):
            return True

        def transcribe(self, audio):
            return "привет" if getattr(audio, "size", 0) else ""

    class FakeTTS:
        def __init__(self, *a, **k):
            self._speaking = False

        def speak(self, text, block=False):
            pass

        def stop(self):
            pass

        @property
        def is_speaking(self):
            return False

    class FakeAgent:
        def __init__(self, *a, **k):
            pass

        def run(self, text):
            yield SimpleNamespace(kind="final", text=f"ответ: {text}", error="")

    ll.STTEngine = FakeSTT
    ll.TTSEngine = FakeTTS
    ll.Agent = FakeAgent

    cfg = get_config()
    cfg.voice.noise.provider = "off"
    cfg.voice.barge_in = False

    users = []
    replies = []
    states = []
    errors = []

    from nemus.core.permissions import PermissionManager
    from nemus.core.tools.registry import ToolRegistry
    from nemus.voice.realtime.base import RealtimeCallbacks, RealtimeState

    callbacks = RealtimeCallbacks(
        on_state=lambda s: states.append(s),
        on_user_text=lambda t: users.append(t),
        on_assistant_text=lambda t: replies.append(t),
        on_error=lambda m: errors.append(m),
    )

    session = ll.LocalLiveSession(
        config=cfg,
        callbacks=callbacks,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        confirm=None,
        llm=None,
        memory=None,
    )
    assert session.start(), "сессия не запустилась"

    deadline = time.time() + 6.0
    while time.time() < deadline and not (users and replies):
        time.sleep(0.05)

    session.stop()
    assert not errors, f"ошибки сессии: {errors}"
    assert users == ["привет"], f"распознанный текст не пришёл: {users}"
    assert replies and replies[0].startswith("ответ:"), f"ответ не озвучен: {replies}"
    assert RealtimeState.CLOSED in states, "сессия не закрылась корректно"
    assert not session._running
    print(f"ok  user={users} assistant={replies}")
    print("LOCAL LIVE TEST PASSED")


if __name__ == "__main__":
    main()
