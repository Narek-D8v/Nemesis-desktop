"""Тест движков синтеза речи: диспетчер, openai-голоса, системный Windows SAPI.

Без сети и API-ключей: openai проверяется на уровне диспетчера, системный —
реальным (офлайн) синтезом через PowerShell.

Запуск:  venv\\Scripts\\python.exe scripts\\test_tts_engines.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.voice.tts import (  # noqa: E402
    ENGINES,
    OPENAI_VOICES,
    TTSEngine,
    list_engine_voices,
    resolve_openai_key,
    split_sentences,
    synthesize_text,
)


def test_dispatch_unknown_engine() -> None:
    try:
        synthesize_text("bogus", "привет", "v", "+0%", "+0%")
    except ValueError as exc:
        assert "Неизвестный движок" in str(exc)
    else:
        raise AssertionError("Неизвестный движок должен давать ValueError")
    # сам движок при этом деградирует на edge-tts, а не падает
    engine = TTSEngine(engine="bogus")
    assert engine.engine == "edge-tts"
    print("  диспетчер: неизвестный движок → ValueError, TTSEngine → edge-tts")


def test_openai_no_key() -> None:
    try:
        synthesize_text("openai", "привет", "alloy", "+0%", "+0%", api_key="")
    except RuntimeError as exc:
        assert "API-ключ" in str(exc)
    else:
        raise AssertionError("openai без ключа должен давать RuntimeError")
    print("  openai без ключа: понятная ошибка")


def test_openai_voices_static() -> None:
    voices = list_engine_voices("openai")
    assert len(voices) == len(OPENAI_VOICES) >= 10
    assert all(v.get("ShortName") for v in voices)
    assert "alloy" in {v["ShortName"] for v in voices}
    print(f"  openai: фиксированный список из {len(voices)} голосов")


def test_resolve_openai_key() -> None:
    import types

    providers = {"openai": types.SimpleNamespace(api_key=" sk-test ")}
    assert resolve_openai_key(providers) == "sk-test"
    assert resolve_openai_key({}) == ""
    assert resolve_openai_key(None) == ""
    print("  resolve_openai_key: ок")


def test_system_voices_and_synth() -> None:
    if not sys.platform.startswith("win"):
        print("  системный движок: пропущено (не Windows)")
        return
    voices = list_engine_voices("system")
    print(f"  системные голоса Windows: {len(voices)} — {[v['ShortName'] for v in voices][:4]}")
    assert all(v.get("ShortName") for v in voices)
    voice = voices[0]["ShortName"] if voices else ""
    wav = synthesize_text("system", "Привет, я Немуся!", voice, "+0%", "+0%")
    size = wav.stat().st_size
    print(f"  WAV: {size} байт (голос: {voice or 'по умолчанию'})")
    assert size > 1000, "системный синтез вернул пустой файл"
    wav.unlink(missing_ok=True)


def test_system_engine_normalizes() -> None:
    engine = TTSEngine(engine="system")
    assert engine.engine == "system"
    assert engine.synthesize.__self__ is engine
    print("  TTSEngine(engine='system'): ок")


def test_split_sentences() -> None:
    parts = split_sentences("Привет! Как дела? Нормально.")
    assert parts == ["Привет!", "Как дела?", "Нормально."]
    print("  split_sentences: ок")


def main() -> int:
    assert ENGINES == ("edge-tts", "openai", "system")
    tests = [
        test_dispatch_unknown_engine,
        test_openai_no_key,
        test_openai_voices_static,
        test_resolve_openai_key,
        test_system_voices_and_synth,
        test_system_engine_normalizes,
        test_split_sentences,
    ]
    for test in tests:
        test()
    print("\nTTS ENGINES TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
