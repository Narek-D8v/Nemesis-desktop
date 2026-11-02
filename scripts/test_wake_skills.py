"""Тесты навыков слова активации и его обработки в приложении.

Запуск:  venv\\Scripts\\python.exe scripts\\test_wake_skills.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.app import NemusApp  # noqa: E402
from nemus.config import WakeSkillConfig, get_config  # noqa: E402
from nemus.voice.wake_skills import (  # noqa: E402
    build_skills,
    match_skill,
    normalize_phrase,
)
from nemus.voice.wakeword import DEFAULT_WAKE_WORDS, normalize_words  # noqa: E402


def test_normalize_phrase() -> None:
    assert normalize_phrase("  Который ЧАС?!  ") == "который час"
    assert normalize_phrase("Открой YouTube.") == "открой youtube"
    assert normalize_phrase("ёлка,  и   пауза") == "елка и пауза"
    assert normalize_phrase("") == ""
    print("ok  normalize_phrase")


def test_build_and_match() -> None:
    cfg = SimpleNamespace(
        skills=[
            WakeSkillConfig(
                name="time",
                phrases=["который час", "сколько времени"],
                tool="get_time",
            ),
            WakeSkillConfig(
                name="youtube",
                phrases=["открой ютуб"],
                tool="open_url",
                arguments={"url": "https://www.youtube.com"},
            ),
            WakeSkillConfig(name="broken", phrases=[], tool="get_time"),
            WakeSkillConfig(name="no_tool", phrases=["привет"], tool=""),
        ]
    )
    skills = build_skills(cfg)
    assert [s.name for s in skills] == ["time", "youtube"]

    assert match_skill("Который час?", skills).name == "time"
    assert match_skill("немуся, открой ютуб пожалуйста", skills).name == "youtube"
    assert match_skill("включи музыку", skills) is None
    assert match_skill("", skills) is None
    print("ok  build_skills / match_skill")


def test_default_skills_from_config() -> None:
    cfg = get_config(reload=True)
    skills = build_skills(cfg.voice.wake_word)
    assert skills, "в конфиге по умолчанию должны быть навыки"
    time_skill = match_skill("сколько времени", skills)
    assert time_skill is not None and time_skill.tool == "get_time"
    assert time_skill.arguments == {}
    youtube = match_skill("открой youtube", skills)
    assert youtube is not None and youtube.arguments["url"].startswith("https://")
    print(f"ok  навыки из конфига: {[s.name for s in skills]}")


def test_normalize_wake_words() -> None:
    assert normalize_words(["Немуся", "немесис", "НЕМУСЯ"]) == ["немуся", "немесис"]
    assert normalize_words(["ёлка"]) == ["елка"]
    broken = normalize_words(["������", "", "   "])
    assert broken == list(DEFAULT_WAKE_WORDS)
    assert normalize_words([]) == list(DEFAULT_WAKE_WORDS)
    print("ok  normalize_words")


def _fake_app(*, wake_only: bool, wake_activates: bool = True):
    app = object.__new__(NemusApp)
    app.realtime = None
    app.voice = SimpleNamespace(wake_only=wake_only)
    app.config = SimpleNamespace(
        voice=SimpleNamespace(
            realtime=SimpleNamespace(
                wake_activates=wake_activates,
                provider="local",
                offline_fallback=True,
            )
        )
    )
    app._session_from_wake = False
    calls: list[str] = []

    def _start_voice() -> bool:
        calls.append("start_voice")
        return True

    app._stop_wake_only = lambda: calls.append("stop_wake_only")
    app._stop_voice = lambda: calls.append("stop_voice")
    app._start_wake_only = lambda: calls.append("start_wake_only")
    app._start_realtime = lambda: calls.append("start_realtime")
    app._start_local_live = lambda: calls.append("start_local_live")
    app._start_voice = _start_voice
    app.window = SimpleNamespace(
        chat=SimpleNamespace(add_system=lambda *_: None, add_error=lambda *_: None),
        listen_btn=SimpleNamespace(setChecked=lambda *_: None, setText=lambda *_: None),
    )
    return app, calls


def test_wake_from_wake_only_starts_dialog() -> None:
    """Регрессия: wake-only менеджер не должен блокировать обработку wake word."""
    app, calls = _fake_app(wake_only=True)
    NemusApp._on_wake_detected(app)
    assert calls == ["stop_wake_only", "start_local_live"], calls
    assert app._session_from_wake is True
    print("ok  wake word из фонового режима запускает диалог")


def test_wake_while_already_listening_is_noop() -> None:
    app, calls = _fake_app(wake_only=False)
    NemusApp._on_wake_detected(app)
    assert calls == [], calls
    print("ok  повторное срабатывание при активном прослушивании игнорируется")


def test_wake_without_dialog_starts_listening() -> None:
    app, calls = _fake_app(wake_only=True, wake_activates=False)
    NemusApp._on_wake_detected(app)
    assert calls == ["stop_wake_only", "start_voice"], calls
    assert app._session_from_wake is False
    print("ok  wake word без диалога включает запись команды")


def main() -> int:
    test_normalize_phrase()
    test_build_and_match()
    test_default_skills_from_config()
    test_normalize_wake_words()
    test_wake_from_wake_only_starts_dialog()
    test_wake_while_already_listening_is_noop()
    test_wake_without_dialog_starts_listening()
    print("\nWAKE SKILLS TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
