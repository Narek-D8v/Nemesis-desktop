"""Офлайн-тесты режима реального времени (без сети и API-ключей)."""

from __future__ import annotations

import asyncio
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
from nemus.voice.realtime.audio_io import resample_pcm16  # noqa: E402


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
    cb = RealtimeCallbacks()
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
    assert {t["function"]["name"] for t in sess["tools"]} == {"test_safe", "test_danger"}
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
    win.close()
    print("ok  GUI кнопка Realtime + сигналы")


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

    setup = session._setup_message()
    assert setup["model"] == "models/gemini-3.8-live"
    detection = setup["realtimeInputConfig"]["automaticActivityDetection"]
    assert detection["silenceDurationMs"] == 400
    print("ok  Gemini Live: setup")

    # Диагностика задержки: после паузы 0.35 с фиксируется конец речи
    session._mic_spoke = True
    session._voice_ts = time.monotonic() - 0.5
    session._track_voice(b"\x00\x00" * 480)
    assert session._speech_end_ts is not None
    session._track_voice(b"\x10\x27" * 480)
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
