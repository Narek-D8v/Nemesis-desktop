"""Smoke-тест голосового цикла: TTS → STT (round-trip). Требует интернет.

Запуск:  venv\\Scripts\\python.exe scripts\\test_voice.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from nemus.voice.stt import STTEngine  # noqa: E402
from nemus.voice.tts import TTSEngine  # noqa: E402


def main() -> int:
    phrase = "Привет, я Немуся."
    print(f"Фраза: {phrase}")

    tts = TTSEngine(voice="ru-RU-DmitryNeural")
    mp3 = tts.synthesize(phrase)
    print(f"MP3: {mp3.stat().st_size} байт")

    audio24, sr = tts._decode(mp3)
    # ресемплинг 24k → 16k для whisper
    idx = np.round(np.arange(0, len(audio24) - 1, sr / 16000)).astype(int)
    a16 = audio24[idx]
    mp3.unlink(missing_ok=True)

    stt = STTEngine(model="tiny", language="ru", device="cpu")
    text = stt.transcribe((a16 * 32767).astype(np.int16))
    print(f"Распознано: {text!r}")

    if len(text) < 3:
        print("Пустая расшифровка")
        return 1
    print("\nVOICE SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
