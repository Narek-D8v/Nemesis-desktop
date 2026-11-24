"""Офлайн-тесты режима реального времени (без сети и API-ключей)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from nemus.config import get_config  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.tools.base import RiskLevel, Tool, ToolResult  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402
from nemus.voice.realtime import (  # noqa: E402
    RealtimeCallbacks,
    RealtimeManager,
    RealtimeState,
)
from nemus.voice.realtime.audio_io import SpeakerPlayer, resample_pcm16  # noqa: E402


class _SafeTool(Tool):
    name = "test_safe"
    description = "безопасный тестовый инструмент"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}
    risk = RiskLevel.SAFE

    def execute(self, x: str = "", **_: object) -> ToolResult:
        return ToolResult.success(f"получено: {x}")


class _DangerTool(Tool):
    name = "test_danger"
    description = "опасный тестовый инструмент"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.DANGEROUS

    def execute(self, **_: object) -> ToolResult:
        return ToolResult.success("выполнено опасное действие")


def test_resample() -> None:
    # 1 кГц синус на 16 кГц -> 24 кГц: частота та же, длина растёт
    rate, t = 16000, 0.1
    n = int(rate * t)
    x = np.sin(2 * np.pi * 440 * np.arange(n) / rate)
    pcm = (x * 32767).astype(np.int16).tobytes()
    out = resample_pcm16(pcm, 16000, 24000)
    expected = int(n * 24000 / 16000)
    assert abs(len(out) // 2 - expected) <= 2, f"длина {len(out)//2} != ~{expected}"
    assert resample_pcm16(pcm, 16000, 16000) == pcm, "идентичный ресемплинг"
    assert resample_pcm16(b"", 16000, 24000) == b"", "пустой ввод"
    print("ok  resample_pcm16")


def test_manager_requires_key() -> None:
    config = get_config(reload=True)
    os.environ.pop("GEMINI_API_KEY", None)
    from nemus.config import ProviderConfig

    config.llm.providers.setdefault("gemini", ProviderConfig())
    config.llm.providers["gemini"].api_key = ""
    config.voice.realtime.provider = "gemini"
    errors: list[str] = []
    cb = RealtimeCallbacks(on_error=errors.append)
    registry = ToolRegistry()
    registry.register(_SafeTool())
    registry.register(_DangerTool())
    mgr = RealtimeManager(config, registry, PermissionManager(config), cb)
    started = mgr.start()
    assert started is False, "старт без ключа должен быть отклонён"
    assert errors and "ключ" in errors[0].lower(), f"нет ошибки: {errors}"
    assert mgr.active is False
    print("ok  старт без ключа отклонён:", errors[0])


def test_provider_messages() -> None:
    from nemus.voice.realtime.gemini_live import GeminiLiveSession
    from nemus.voice.realtime.openai_realtime import OpenAIRealtimeSession

    registry = ToolRegistry()
    registry.register(_SafeTool())
    registry.register(_DangerTool())
    tool_events: list[tuple[str, dict, str, str]] = []
    cb = RealtimeCallbacks(on_tool=lambda *a: tool_events.append(a))  # type: ignore[arg-type]
    perms = PermissionManager(get_config())

    g = GeminiLiveSession(
        api_key="dummy",
        model="gemini-2.5-flash-native-audio-latest",
        audio=True,
        callbacks=cb,
        registry=registry,
        permissions=perms,
        system_prompt="Ты — Немуся.",
    )
    setup = g._setup_message()
    assert setup["model"].startswith("models/")
    instr = setup["systemInstruction"]["parts"][0]["text"]
    assert instr.startswith("Ты — Немуся.")
    assert "русском" in instr
    assert "НЕ НАЧИНАЙ" in instr, "нет антигаллюцинационной инструкции"
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}
    assert (
        setup["generationConfig"]["speechConfig"]["voiceConfig"][
            "prebuiltVoiceConfig"
        ]["voiceName"]
        == "Aoede"
    )
    decls = setup["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in decls} == {"test_safe", "test_danger"}
    assert decls[0]["parameters"]["type"] == "object"
    print("ok  gemini setup:", len(decls), "объявлений инструментов")

    # вызовы инструментов должны уходить в GUI-колбэк (иначе в чате пусто)
    g._emit_tool("run_python", {"code": "print(1)"}, "start")
    g._emit_tool("run_python", {"code": "print(1)"}, "ok", "1")
    assert [e[2] for e in tool_events] == ["start", "ok"], tool_events
    assert tool_events[0][0] == "run_python" and tool_events[1][3] == "1"
    print("ok  realtime: вызовы инструментов транслируются в GUI-колбэк")

    o = OpenAIRealtimeSession(
        api_key="dummy",
        model="gpt-realtime",
        voice="marin",
        audio=True,
        callbacks=cb,
        registry=registry,
        permissions=perms,
    )
    upd = o._session_update()
    assert upd["type"] == "session.update"
    sess = upd["session"]
    assert sess["input_audio_format"] == "pcm16"
    assert sess["voice"] == "marin"
    assert "НЕ НАЧИНАЙ" in sess["instructions"], "нет антигаллюцинационной инструкции"
    assert {t["function"]["name"] for t in sess["tools"]} == {"test_safe", "test_danger"}
    o._emit_tool("test_safe", {}, "start")
    assert [e[2] for e in tool_events][-1] == "start", tool_events
    print("ok  openai session.update:", len(sess["tools"]), "инструментов")


def test_permission_gate() -> None:
    config = get_config(reload=True)
    config.tools.permission_mode = "smart"
    cb = RealtimeCallbacks()
    registry = ToolRegistry()
    safe, danger = _SafeTool(), _DangerTool()
    registry.register(safe)
    registry.register(danger)
    perms = PermissionManager(config)

    answers = [False]

    def confirm(tool, args) -> bool:
        return answers[0]

    from nemus.voice.realtime.base import BaseRealtimeSession

    class _Stub(BaseRealtimeSession):
        async def _run(self) -> None:
            pass

        async def _close(self) -> None:
            pass

        async def _send_text(self, text: str) -> None:
            pass

    stub = _Stub(cb, registry, perms, confirm=confirm)
    assert stub._execute_tool("test_safe", {"x": "1"}).content == "получено: 1"
    assert stub._execute_tool("test_missing", {}).ok is False

    answers[0] = False
    res = stub._execute_tool("test_danger", {})
    assert res.ok is False and "отклонил" in res.content, res.content

    answers[0] = True
    res = stub._execute_tool("test_danger", {})
    assert res.ok is True, res.content
    print("ok  PermissionManager гейт (allow/deny/ask)")


def test_gui_button() -> None:
    from PySide6.QtWidgets import QApplication

    from nemus.gui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])  # noqa: F841 — держим ссылку
    win = MainWindow(None, "Тест")
    assert hasattr(win, "realtime_btn")
    assert hasattr(win, "realtime_toggled")

    seen: list[bool] = []
    win.realtime_toggled.connect(seen.append)
    win._on_realtime_clicked(True)
    assert seen == [True], seen
    assert "Остановить" in win.realtime_btn.text(), win.realtime_btn.text()

    win.set_realtime(True)
    assert win.listen_btn.isEnabled() is False
    win.set_realtime(False)
    assert win.listen_btn.isEnabled() is True
    win.show_realtime_user("привет")
    win.show_realtime_assistant("здравствуй")
    assert "привет" in win.chat.toHtml()

    # карточка и статус инструмента из realtime-сессии
    win.show_realtime_tool("run_python", {"code": "print(1)"}, "start")
    win.show_realtime_tool("run_python", {"code": "print(1)"}, "ok", "1")
    win.show_realtime_tool("web_search", {"q": "x"}, "fail", "сеть недоступна")
    chat_text = win.chat.toPlainText()
    assert "RUN_PYTHON" in chat_text, chat_text[-300:]
    assert "сеть недоступна" in chat_text, chat_text[-300:]
    win.close()
    print("ok  GUI кнопка Realtime + сигналы + карточки инструментов")


def test_bridge_confirm_threading() -> None:
    from PySide6.QtWidgets import QApplication

    from nemus.app import RealtimeBridge
    from nemus.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(None, "Тест")
    bridge = RealtimeBridge(win, win)

    from nemus.gui import dialogs

    dialogs.ConfirmDialog.ask = staticmethod(lambda tool, args, parent: True)
    results: list[bool] = []

    def worker() -> None:
        results.append(bridge.confirm(_DangerTool(), {"y": 2}))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = 0
    while not results and deadline < 50:
        app.processEvents()
        threading.Event().wait(0.1)
        deadline += 1
    t.join(timeout=2)
    assert results == [True], results
    win.close()
    print("ok  подтверждение из фонового потока через Qt-сигнал")


def test_settings_dialog() -> None:
    from PySide6.QtWidgets import QApplication

    from nemus.gui.dialogs import SettingsDialog

    _app = QApplication.instance() or QApplication([])  # noqa: F841 — держим ссылку
    dlg = SettingsDialog(None)
    assert dlg.cb_rt_provider.currentText() in {"gemini", "openai"}
    assert dlg.edit_rt_gemini.text() != ""
    assert dlg.edit_rt_openai.text() != ""
    cfg = dlg.config.voice.realtime
    # значения совпадают с загруженным конфигом
    assert dlg.cb_rt_provider.currentText() == cfg.provider
    assert dlg.chk_rt_audio.isChecked() == cfg.audio
    assert dlg.chk_rt_aec.isChecked() == cfg.aec
    # рабочая папка для собственных скриптов
    assert dlg.edit_workspace.text() == dlg.config.tools.workspace
    dlg.close()
    print("ok  SettingsDialog: вкладка realtime заполнена")
    print("ok  SettingsDialog: рабочая папка заполнена")

    # системная инструкция знает о возможности писать/запускать код
    from nemus.core.agent import build_system_prompt

    prompt = build_system_prompt(dlg.config, _registry_for_prompt())
    assert "write_file" in prompt and "run_python" in prompt and "create_tool" in prompt
    assert dlg.config.tools.workspace in prompt
    print("ok  system prompt: про скрипты/файлы/workspace")


def _registry_for_prompt():
    from nemus.core.tools.all import all_builtin_tools
    from nemus.core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register_many(all_builtin_tools())
    return reg


def test_aec() -> None:
    from nemus.voice.echo import PlaybackReference, create_echo_canceller

    ref = PlaybackReference(16000, max_ms=100)
    ref.append(np.arange(100, dtype=np.int16))
    latest = ref.latest(50)
    assert np.array_equal(latest, np.arange(50, 100, dtype=np.int16)), latest
    player = SpeakerPlayer(24000, reference_rate=16000)
    block = np.full(480, 1000, dtype=np.int16)
    for _ in range(3):
        player._append_reference(block)
    assert np.array_equal(player.reference.latest(960), block.repeat(2))
    print("ok  AEC: опорный сигнал динамика")

    aec = create_echo_canceller(16000, delay_ms=40)
    assert aec is not None, "pywebrtc-audio не установлен"
    rng = np.random.default_rng(1)
    far = (rng.standard_normal(16000 * 3) * 3000).astype(np.int16)
    near = np.zeros_like(far)
    near[800:] = (far[:-800] * 0.35).astype(np.int16)
    out = np.concatenate(
        [aec.process(near[i : i + 960], far[i : i + 960]) for i in range(0, len(far), 960)]
    )
    near_rms = float(np.sqrt(np.mean(near[16000 * 2 :].astype(np.float64) ** 2)))
    out_rms = float(np.sqrt(np.mean(out[16000 * 2 :].astype(np.float64) ** 2)))
    assert out_rms < near_rms * 0.5, (near_rms, out_rms)
    print(f"ok  AEC: эхо подавлено ({near_rms:.0f} -> {out_rms:.0f} RMS)")


def test_gemini_audio_and_latency() -> None:
    from nemus.voice.realtime.gemini_live import GeminiLiveSession

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    config = get_config(reload=True)
    seen: list[RealtimeState] = []
    session = GeminiLiveSession(
        api_key="test",
        model="gemini-3.8-live",
        registry=ToolRegistry(),
        permissions=PermissionManager(config),
        callbacks=RealtimeCallbacks(on_state=seen.append),
        system_prompt="тест",
    )
    ws = _FakeWs()
    session._ws = ws
    asyncio.run(session._send_audio(b"\x01\x00" * 480))
    payload = ws.sent[0]["realtimeInput"]
    assert payload["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert "mediaChunks" not in payload, "устаревшее поле игнорируется новыми моделями"
    print("ok  Gemini Live: микрофон уходит в поле audio")

    # Keepalive: после паузы уходит тишина, сразу повторно — не уходит
    session._last_send_ts = 0.0
    asyncio.run(session._send_keepalive())
    silence = base64.b64decode(ws.sent[-1]["realtimeInput"]["audio"]["data"])
    assert silence == b"\x00\x00" * 1600
    sent = len(ws.sent)
    asyncio.run(session._send_keepalive())
    assert len(ws.sent) == sent, "keepalive не должен спамить при свежем аудио"
    print("ok  Gemini Live: keepalive от idle-таймаута")

    setup = session._setup_message()
    assert setup["model"] == "models/gemini-3.8-live"
    detection = setup["realtimeInputConfig"]["automaticActivityDetection"]
    assert detection["silenceDurationMs"] == 600
    assert detection["startOfSpeechSensitivity"] == "START_SENSITIVITY_LOW"
    assert detection["prefixPaddingMs"] == 300
    print("ok  Gemini Live: setup")

    # Эхо динамика не уходит в модель, а громкое перебивание — уходит
    class _FakeSpeaker:
        is_playing = True
        output_rms = 3000.0

        def __init__(self) -> None:
            self.cleared = False

        def clear(self) -> None:
            self.cleared = True

    speaker = _FakeSpeaker()
    session.barge_in = True
    assert session._echo_guard(b"echo", 500.0, 100.0, speaker, None) is None
    assert not speaker.cleared
    assert session._echo_guard(b"one", 4000.0, 100.1, speaker, None) is None
    assert session._echo_guard(b"two", 4000.0, 100.35, speaker, None) == [b"one", b"two"]
    assert speaker.cleared
    print("ok  Gemini Live: эхо не запускает новый ход")

    # Гейт тишины: тишину с самого начала не шлём (галлюцинации модели),
    # хвост после звука — шлём, затянувшуюся тишину — режем
    assert session._gate_audio(0.0, 100.0) is False
    assert session._gate_audio(1000.0, 101.0) is True
    assert session._gate_audio(0.0, 101.5) is True
    assert session._gate_audio(0.0, 103.0) is False
    session._last_loud_ts = 0.0
    print("ok  Gemini Live: гейт тишины")

    # Диагностика задержки: после паузы 0.35 с фиксируется конец речи
    session._mic_spoke = True
    session._voice_ts = time.monotonic() - 0.5
    session._track_voice(0.0, time.monotonic())
    assert session._speech_end_ts is not None
    session._track_voice(10000.0, time.monotonic())
    assert session._speech_end_ts is None
    print("ok  Gemini Live: замер конца речи")

    # Статус «Думаю…» не должен дублироваться на каждый аудио-чанк
    session._emit_state(RealtimeState.THINKING)
    session._emit_state(RealtimeState.THINKING)
    session._emit_state(RealtimeState.LISTENING)
    assert seen == [RealtimeState.THINKING, RealtimeState.LISTENING], seen
    print("ok  Gemini Live: статусы без дублей")


def main() -> int:
    failures = 0
    for test in (
        test_resample,
        test_manager_requires_key,
        test_provider_messages,
        test_permission_gate,
        test_gui_button,
        test_bridge_confirm_threading,
        test_settings_dialog,
        test_aec,
        test_gemini_audio_and_latency,
    ):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    print(f"\nИтог: {'FAILURES=' + str(failures) if failures else 'ВСЕ ТЕСТЫ ПРОШЛИ'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
