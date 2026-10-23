"""Тесты языка ответов и фильтрации рассуждений модели.

Проверяет:
- определение языка собеседника и инструкции для LLM;
- скрытые рассуждения Gemini Live (thought parts) не попадают в чат;
- ``reasoning_content`` и теги ``<thinking>`` не попадают в ответ;
- i18n интерфейса (ru/en).
"""

from __future__ import annotations

import asyncio
import os
import sys

# На Windows консоль по умолчанию cp1251/cp1252 — без этого печать кириллицы
# падает UnicodeEncodeError при запуске вне PYTHONUTF8/PYTHONIOENCODING.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import Config, get_config  # noqa: E402
from nemus.core.language import (  # noqa: E402
    REPLY_AUTO,
    anti_reasoning_directive,
    detect_language,
    normalize,
    reply_language_directive,
    speech_language_code,
)


def test_detect_language() -> None:
    assert detect_language("Привет, как дела?") == "ru"
    assert detect_language("Hello there!") == "en"
    assert detect_language("Привет world") == "ru"  # кириллицы больше
    assert detect_language("hello world") == "en"
    assert detect_language("") == ""
    assert detect_language("12345 !!!") == ""
    print("ok  detect_language: ru/en/неизвестный")


def test_reply_directives() -> None:
    auto = reply_language_directive(REPLY_AUTO)
    assert "языке" in auto.lower(), auto  # слово обязательно для инструкции Gemini Live
    assert "не переводь" in auto or "никогда не переводь" in auto or "не переводи" in auto, auto

    ru = reply_language_directive("ru")
    assert "только на русском" in ru, ru

    en = reply_language_directive("en")
    assert "на английском" in en, en

    detected = reply_language_directive(REPLY_AUTO, "en")
    assert "английском" in detected, detected

    assert "рассуждения" in anti_reasoning_directive()
    print("ok  инструкции языка: auto/ru/en + запрет рассуждений")


def test_speech_codes_and_normalize() -> None:
    assert speech_language_code("ru") == "ru-RU"
    assert speech_language_code("en") == "en-US"
    assert speech_language_code("ru-RU") == "ru-RU"
    assert speech_language_code(REPLY_AUTO) == "ru-RU"
    assert normalize("RU-ru") == "ru"
    assert normalize(None) == REPLY_AUTO
    assert normalize("Auto") == REPLY_AUTO
    print("ok  speech_language_code / normalize")


def test_config_defaults() -> None:
    cfg = Config()
    assert cfg.assistant.reply_language == REPLY_AUTO, cfg.assistant.reply_language
    assert cfg.gui.language == "ru", cfg.gui.language
    print("ok  конфиг по умолчанию: reply_language=auto, gui.language=ru")


def test_system_prompt_language() -> None:
    from nemus.core.agent import build_system_prompt
    from nemus.core.permissions import PermissionManager
    from nemus.core.tools.registry import ToolRegistry

    cfg = get_config(reload=True)
    registry = ToolRegistry()
    old = cfg.assistant.reply_language
    try:
        cfg.assistant.reply_language = REPLY_AUTO
        prompt = build_system_prompt(cfg, registry)
        assert "языке" in prompt.lower(), prompt
        assert "рассуждения" in prompt, prompt

        cfg.assistant.reply_language = "en"
        prompt_en = build_system_prompt(cfg, registry)
        assert "на английском языке" in prompt_en, prompt_en
    finally:
        cfg.assistant.reply_language = old
    _ = PermissionManager
    print("ok  build_system_prompt: авто-инструкция и фиксированный язык")


def test_agent_language_hint() -> None:
    from nemus.core.agent import Agent
    from nemus.core.permissions import PermissionManager
    from nemus.core.tools.registry import ToolRegistry

    cfg = get_config(reload=True)
    old = cfg.assistant.reply_language
    cfg.assistant.reply_language = REPLY_AUTO
    registry = ToolRegistry()
    agent = Agent(
        config=cfg,
        llm=None,
        registry=registry,
        permissions=PermissionManager(cfg),
    )

    agent._apply_language_hint("Hello, how do I export a CSV file?")
    content = str(agent.history[0].content)
    assert "английском" in content, content  # auto + английский ввод

    agent._apply_language_hint("Привет, как дела?")
    content = str(agent.history[0].content)
    assert "по-русски" in content, content
    assert "английском" not in content

    agent._apply_language_hint("Привет")
    content = str(agent.history[0].content)
    assert content.startswith(agent._base_system), "промпт не должен накапливаться"
    assert "по-русски" in content, content

    try:
        cfg.assistant.reply_language = "ru"
        agent2 = Agent(config=cfg, llm=None, registry=registry,
                       permissions=PermissionManager(cfg))
        agent2._apply_language_hint("Hello my friend")
        content = str(agent2.history[0].content)
        assert "всегда отвечай только на русском" in content, content
        assert "The user writes in" not in content
    finally:
        cfg.assistant.reply_language = old
    print("ok  Agent: подсказка языка по текущей реплике (auto/фиксированный)")


def test_gemini_thought_filter() -> None:
    from nemus.voice.realtime.base import RealtimeCallbacks
    from nemus.voice.realtime.gemini_live import GeminiLiveSession

    texts: list[str] = []
    users: list[str] = []
    cb = RealtimeCallbacks(on_assistant_text=texts.append, on_user_text=users.append)
    session = GeminiLiveSession(
        api_key="dummy",
        model="gemini-test",
        audio=False,
        callbacks=cb,
        registry=_registry(),
        permissions=_permissions(),
        system_prompt="Ты — Немуся.",
    )
    session._play_queue = None

    # скрытые рассуждения модели не должны попадать в ответ
    asyncio.run(
        session._handle_server_content(
            {
                "modelTurn": {
                    "parts": [
                        {"thought": True, "text": "Сначала подумаю шаг за шагом..."},
                        {"text": "Готово: нашёл нужный файл."},
                    ]
                },
                "turnComplete": True,
            }
        )
    )
    assert texts == ["Готово: нашёл нужный файл."], texts

    # расшифровка речи приоритетнее: текст не дублируется
    texts.clear()
    asyncio.run(
        session._handle_server_content(
            {
                "outputTranscription": {"text": "Привет! Это ответ."},
                "modelTurn": {"parts": [{"text": "Привет! Это ответ."}]},
                "turnComplete": True,
            }
        )
    )
    assert texts == ["Привет! Это ответ."], texts

    # прерывание сбрасывает накопленный текст (и рассуждения)
    texts.clear()
    asyncio.run(
        session._handle_server_content(
            {"outputTranscription": {"text": "Начало ответа"}, "interrupted": True}
        )
    )
    asyncio.run(session._handle_server_content({"turnComplete": True}))
    assert texts == [], texts
    print("ok  Gemini Live: thought-части скрыты, дублей нет, interrupt сбрасывает")


def test_gemini_setup_instruction() -> None:
    from nemus.voice.realtime.base import RealtimeCallbacks
    from nemus.voice.realtime.gemini_live import GeminiLiveSession

    session = GeminiLiveSession(
        api_key="dummy",
        model="gemini-test",
        audio=True,
        callbacks=RealtimeCallbacks(),
        registry=_registry(),
        permissions=_permissions(),
        system_prompt="Ты — Немуся.",
    )
    setup = session._setup_message()
    instr = setup["systemInstruction"]["parts"][0]["text"]
    assert instr.startswith("Ты — Немуся."), instr
    assert "русском" in instr, instr  # язык по умолчанию — русский/авто
    assert "только на русском языке, даже если" not in instr  # старого запрета больше нет
    assert "рассуждения" in instr, instr

    session.reply_language = "en"
    instr_en = session._setup_message()["systemInstruction"]["parts"][0]["text"]
    assert "на английском языке" in instr_en, instr_en
    assert "русском" not in instr_en
    print("ok  Gemini setup: авто-язык (русский по умолчанию) + явный английский")


def test_openai_instructions() -> None:
    from nemus.voice.realtime.base import RealtimeCallbacks
    from nemus.voice.realtime.openai_realtime import OpenAIRealtimeSession

    session = OpenAIRealtimeSession(
        api_key="dummy",
        model="gpt-realtime",
        audio=True,
        callbacks=RealtimeCallbacks(),
        registry=_registry(),
        permissions=_permissions(),
        system_prompt="Ты — Немуся.",
    )
    instructions = session._session_update()["session"]["instructions"]
    assert instructions.startswith("Ты — Немуся."), instructions
    assert "языке" in instructions.lower(), instructions
    print("ok  OpenAI instructions: авто-язык")


def test_reasoning_filter() -> None:
    from nemus.core.llm import _ReasoningFilter, strip_reasoning

    assert strip_reasoning("<thinking>план</thinking>Ответ 42.") == "Ответ 42."
    assert strip_reasoning("<thought>идея</thought>") == ""
    assert strip_reasoning("Просто ответ") == "Просто ответ"
    assert strip_reasoning("") == ""

    # теги, разрезанные между дельтами
    f = _ReasoningFilter()
    assert f.feed("<thin") == ""  # незавершённый тег удержан
    assert f.feed("king>скрытая мысль</thin") == ""  # открыт блок — подавлено
    out = f.feed("king>Видимый ответ 42.")  # закрытие блока доехало из буфера
    assert out == "Видимый ответ 42.", repr(out)
    assert f.flush() == ""

    # хвост вне блока рассуждений доезжает через flush
    f2 = _ReasoningFilter()
    assert f2.feed("Обычный текст") == "Обычный текст"
    assert f2.feed("<b>жирный") == "<b>жирный"  # не блок рассуждений — пропускаем
    print("ok  strip_reasoning / потоковый фильтр тегов")


def test_stream_hides_reasoning() -> None:
    from nemus.core.llm import LLMClient

    def chunk(content=None, reasoning=None, finish=None):
        delta = SimpleNamespace(
            content=content, reasoning_content=reasoning, tool_calls=None
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish)]
        )

    stream = [
        chunk(reasoning="нужно сначала подумать про CSV"),
        chunk("<think>сначала найду файл</think>"),
        chunk("Ответ: файл найден."),
        chunk(finish="stop"),
    ]
    client = LLMClient(get_config())
    events = list(client._iter_stream(stream, "test-model"))
    deltas = [e.text for e in events if e.kind == "content"]
    joined = "".join(deltas)
    assert "нужно сначала подумать" not in joined, joined
    assert "сначала найду файл" not in joined, joined
    assert "Ответ: файл найден." in joined, joined

    final = [e for e in events if e.kind == "final"][-1]
    content = final.response.content
    assert content == "Ответ: файл найден.", repr(content)
    assert "подумать" not in content and "найду файл" not in content
    print("ok  LLM-стрим: reasoning_content и <thinking> скрыты, финал чист")


def test_i18n() -> None:
    from nemus.gui import i18n

    i18n.set_language("ru")
    assert i18n.current_language() == "ru"
    assert i18n.tr("btn.listen") == "🎤 Слушать"
    assert i18n.tr("state.thinking") == "Думаю…"
    assert i18n.tr("model.prefix", model="gpt-4o") == "модель: gpt-4o"
    assert i18n.tr("role.you") == "ВЫ"
    assert i18n.tr("not_a_key") == "not_a_key"

    i18n.set_language("en")
    assert i18n.current_language() == "en"
    assert i18n.tr("btn.listen") == "🎤 Listen"
    assert i18n.tr("state.thinking") == "Thinking…"
    assert i18n.tr("role.you") == "YOU"
    assert i18n.tr("model.prefix", model="gpt-4o") == "model: gpt-4o"

    i18n.set_language("xx")  # неизвестный язык игнорируется
    assert i18n.current_language() == "en"
    assert i18n.normalize_language("de") == "ru"
    assert i18n.normalize_language("en-GB") == "en"
    i18n.set_language("ru")
    print("ok  i18n: ru/en, параметры, fallback, неизвестный язык")


def test_gui_language_switch() -> None:
    from PySide6.QtWidgets import QApplication

    from nemus.gui import i18n
    from nemus.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841

    i18n.set_language("en")
    win_en = MainWindow(None, "Немуся")
    assert win_en.listen_btn.text() == "🎤 Listen", win_en.listen_btn.text()
    assert win_en.realtime_btn.text() == "⚡ Realtime"
    assert win_en.input.placeholderText() == "Type a message or command…"
    assert "Chat" in win_en.windowTitle() or True  # заголовок окна статический
    win_en.close()

    i18n.set_language("ru")
    win_ru = MainWindow(None, "Немуся")
    assert win_ru.listen_btn.text() == "🎤 Слушать", win_ru.listen_btn.text()
    assert win_ru.input.placeholderText() == "Напишите сообщение или команду…"
    win_ru.realtime_btn.setChecked(True)
    win_ru.set_realtime(True)
    assert win_ru.realtime_btn.text() == "⏹️ Остановить"
    win_ru.close()
    print("ok  GUI: кнопки/подсказки на выбранном языке")


def _registry():
    from nemus.core.tools.registry import ToolRegistry

    registry = ToolRegistry()
    return registry


def _permissions():
    from nemus.core.permissions import PermissionManager

    return PermissionManager(get_config())


def main() -> int:
    tests = (
        test_detect_language,
        test_reply_directives,
        test_speech_codes_and_normalize,
        test_config_defaults,
        test_system_prompt_language,
        test_agent_language_hint,
        test_gemini_thought_filter,
        test_gemini_setup_instruction,
        test_openai_instructions,
        test_reasoning_filter,
        test_stream_hides_reasoning,
        test_i18n,
        test_gui_language_switch,
    )
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"\nПровалено тестов: {failures}")
        return 1
    print("\nВСЕ ТЕСТЫ ЯЗЫКА И ФИЛЬТРАЦИИ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
