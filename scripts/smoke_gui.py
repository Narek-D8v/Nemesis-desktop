"""Smoke-тест GUI ↔ агент (offscreen, фиктивный LLM).

Запуск:  set QT_QPA_PLATFORM=offscreen
         venv\\Scripts\\python.exe scripts\\smoke_gui.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import Agent  # noqa: E402
from nemus.core.factory import Core  # noqa: E402
from nemus.core.llm import LLMClient, StreamEvent  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, ToolCall  # noqa: E402
from nemus.core.tools.basic import basic_tools  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402
from nemus.gui.main_window import MainWindow  # noqa: E402


class FakeLLM(LLMClient):
    model = "fake/model"
    provider = "fake"

    def __init__(self, config):  # noqa: D107
        self.config = config
        self._step = 0

    def stream(self, messages, tools=None, **overrides):
        self._step += 1
        if self._step == 1:
            yield StreamEvent(kind="content", text="Считаю… ")
            call = ToolCall(id="c1", name="calculate", arguments={"expression": "7*6"})
            yield StreamEvent(
                kind="final",
                response=LLMResponse(content="Считаю… ", tool_calls=[call]),
            )
        else:
            yield StreamEvent(kind="content", text="Получается 42.")
            yield StreamEvent(
                kind="final", response=LLMResponse(content="Получается 42.")
            )


def build_fake_core():
    cfg = get_config()
    registry = ToolRegistry()
    registry.register_many(basic_tools())
    llm = FakeLLM(cfg)
    agent = Agent(cfg, llm, registry, PermissionManager(cfg))  # type: ignore[arg-type]
    return Core(
        config=cfg,
        llm=llm,
        registry=registry,
        permissions=PermissionManager(cfg),
        agent=agent,
        tools=list(registry),
    )


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    core = build_fake_core()
    window = MainWindow(core)
    window.show()

    window.submit_text("Сколько будет 7*6?")
    # пока агент думает, в чате должен быть статус «Думаю…», а не «Слушаю…»
    from nemus.gui import i18n

    assert i18n.tr("state.thinking") in window.state_label.text(), (
        window.state_label.text()
    )
    print("ok  статус в чате: Думаю…")

    result = {"ok": False}

    def check() -> None:
        if window._worker is None:
            text = window.chat.toPlainText()
            result["ok"] = "42" in text and "calculate" in text
            print("chat text:\n", text)
            app.quit()

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(200)
    QTimer.singleShot(15000, app.quit)
    app.exec()

    assert result["ok"], "GUI не показал результат агента"
    assert i18n.tr("state.idle") in window.state_label.text(), (
        window.state_label.text()
    )
    print("ok  статус после ответа: Готова")
    print("\nGUI SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
