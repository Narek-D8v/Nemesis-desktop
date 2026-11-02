"""Сквозная проверка realtime-сессии приложения на синтезированной речи.

Подменяет микрофон и динамик заглушками: «речь» подаётся блоками по 60 мс,
как из настоящего микрофона, а ответ модели только подсчитывается (без звука).
Проверяет реальный setup (промпт + инструменты), задержку «конец речи → первый
звук» и диагностическую запись в лог.

Запуск:
    venv\\Scripts\\python.exe -X utf8 scripts\\realtime_end_to_end_check.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PHRASE = "Расскажи коротко что ты умеешь"
PCM = b""          # заполняется перед стартом сессии
CHUNK_MS = 60


class FakeMic:
    """Вместо микрофона отдаёт заранее синтезированную речь + тишину."""

    def __init__(self, sample_rate, on_chunk, block_ms=CHUNK_MS, cleaner=None):
        self.sample_rate = sample_rate
        self.on_chunk = on_chunk
        self.blocksize = int(sample_rate * block_ms / 1000)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        chunk = self.blocksize * 2
        speech_chunks = max(1, len(PCM) // chunk)
        data = PCM[: speech_chunks * chunk] + b"\x00" * chunk * 30
        for index, offset in enumerate(range(0, len(data) - chunk, chunk)):
            if not self._running:
                break
            self.on_chunk(data[offset : offset + chunk])
            if index == speech_chunks - 1:
                speech_end[0] = time.monotonic()
            time.sleep(CHUNK_MS / 1000)

    def stop(self) -> None:
        self._running = False


class FakeSpeaker:
    """Вместо звука запоминает момент первого чанка ответа."""

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.chunks = 0
        self.is_playing = False

    def start(self) -> bool:
        return True

    def play(self, data: bytes) -> None:
        self.chunks += 1
        self.is_playing = True
        if speech_end[0] and first_play[0] is None:
            first_play[0] = time.monotonic()
        if self.chunks == 1:
            print(f"  [динамик] пошёл звук, {len(data)} байт")

    def clear(self) -> None:
        self.is_playing = False

    def stop(self) -> None:
        self.is_playing = False


speech_end: list[float] = [0.0]
first_play: list[float | None] = [None]


def main() -> int:
    import argparse

    from realtime_latency_probe import synth_speech

    import nemus.voice.realtime.gemini_live as gl
    from nemus.config import load_config
    from nemus.core.agent import build_system_prompt
    from nemus.core.factory import build_core
    from nemus.voice.realtime.base import RealtimeCallbacks, RealtimeState

    parser = argparse.ArgumentParser()
    parser.add_argument("--phrase", default=PHRASE)
    parser.add_argument("--wait", type=float, default=30.0)
    args = parser.parse_args()

    cfg = load_config()
    core = build_core(cfg)
    prompt = build_system_prompt(cfg, core.registry)
    rc = cfg.voice.realtime

    global PCM
    PCM = asyncio.run(synth_speech(args.phrase))
    print(f"фраза: {len(PCM) / 2 / 16000:.2f} с | {args.phrase!r}")
    print(f"модель: {rc.gemini_model}")

    gl.MicStreamer = FakeMic
    gl.SpeakerPlayer = FakeSpeaker

    ready = threading.Event()
    assistant_lines: list[str] = []
    result: dict[str, str] = {}

    def on_state(state: RealtimeState) -> None:
        print(f"  [состояние] {state.value}")
        if state in (RealtimeState.READY, RealtimeState.LISTENING):
            ready.set()

    def on_user(text: str) -> None:
        result["user"] = text
        print(f"  [услышала] {text}")

    def on_assistant(text: str) -> None:
        result["assistant"] = text
        assistant_lines.append(text)
        print(f"  [ответ] {text}")

    def on_error(message: str) -> None:
        print(f"  [ошибка] {message}")

    session = gl.GeminiLiveSession(
        api_key=cfg.llm.providers["gemini"].api_key,
        model=rc.gemini_model,
        audio=True,
        voice=rc.gemini_voice,
        language="ru-RU",
        callbacks=RealtimeCallbacks(
            on_state=on_state,
            on_user_text=on_user,
            on_assistant_text=on_assistant,
            on_error=on_error,
        ),
        registry=core.registry,
        permissions=core.permissions,
        system_prompt=prompt,
        barge_in=cfg.voice.barge_in,
        noise_reducer=None,
    )

    try:
        if not session.start():
            print("не удалось запустить сессию")
            return 1
        if not ready.wait(30):
            print("сессия не перешла в готовность")
            return 1
        time.sleep(args.wait)

        if first_play[0] and speech_end[0]:
            latency = (first_play[0] - speech_end[0]) * 1000
            print(f"конец речи -> первый звук: {latency:.0f} мс")
            print(f"LATENCY_MS={latency:.0f}")
        else:
            print("ответа не дождались")
        print(f"распознала: {result.get('user')!r}")
        print(f"сказала: {result.get('assistant')!r}")
        print(f"реплик всего: {len(assistant_lines)}")
        return 0 if assistant_lines else 1
    finally:
        session.stop()
        time.sleep(1.5)
        closer = getattr(core, "close", None)
        if callable(closer):
            closer()


if __name__ == "__main__":
    raise SystemExit(main())
