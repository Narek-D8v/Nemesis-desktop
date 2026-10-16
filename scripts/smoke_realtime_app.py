"""Smoke-тест интеграции режима реального времени с NemusApp (offscreen, без сети).

Запуск:  set QT_QPA_PLATFORM=offscreen
         venv\\Scripts\\python.exe scripts\\smoke_realtime_app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

from PySide6.QtCore import QTimer  # noqa: E402


def main() -> int:
    from nemus.app import NemusApp
    from nemus.config import get_config
    from nemus.core.state import AssistState

    # гарантируем отсутствие ключей, чтобы старт был без сети
    cfg = get_config(reload=True)
    for provider in cfg.llm.providers.values():
        provider.api_key = ""

    app = NemusApp([])
    window = app.window

    checks: list[str] = []

    def on_error(msg: str) -> None:
        checks.append(msg)

    app.rt_bridge.error.connect(on_error)

    # 1) включение realtime без ключа -> мягкая ошибка, кнопка возвращается
    window._on_realtime_clicked(True)

    def step2() -> None:
        assert app.realtime is None, "менеджер должен быть None после сбоя"
        assert window.realtime_btn.isChecked() is False, "кнопка должна сброситься"
        assert checks and "ключ" in checks[0].lower(), f"нет ошибки: {checks}"
        assert window.listen_btn.isEnabled() is True, "кнопка голоса должна вернуться"
        print("ok  отказ без ключа обработан:", checks[0])

        # 2) обмен текстом через realtime не требуется; проверяем чат и состояние
        window.show_realtime_user("тестовое сообщение")
        window.show_realtime_assistant("тестовый ответ")
        assert "тестовое сообщение" in window.chat.toHtml()
        assert "тестовый ответ" in window.chat.toHtml()
        window.set_state(AssistState.IDLE)
        print("ok  realtime-транскрипты в чате")

        # 3) выключение (marginal) не падает
        window._on_realtime_clicked(False)
        print("ok  выключение realtime")

        app.qapp.quit()

    QTimer.singleShot(600, step2)
    QTimer.singleShot(8000, app.qapp.quit)
    code = app.qapp.exec()

    assert code == 0, f"код выхода {code}"
    print("\nREALTIME APP SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
