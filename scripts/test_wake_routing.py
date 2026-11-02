"""Проверка маршрутизации слова активации: облако vs локальный режим (offscreen).

Запуск:  set QT_QPA_PLATFORM=offscreen
         venv\\Scripts\\python.exe scripts\\test_wake_routing.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)


def main() -> int:
    from nemus.app import NemusApp
    from nemus.config import get_config

    cfg = get_config(reload=True)
    for provider in cfg.llm.providers.values():
        provider.api_key = ""

    app = NemusApp([])

    calls: list[str] = []

    # изолируем от реального звука и сети
    app._start_voice = lambda: None
    app._stop_voice = lambda: None
    app._rearm_voice = lambda: calls.append("rearm")
    app._start_realtime = lambda: calls.append("cloud")
    app._start_local_live = lambda: calls.append("local")

    rt = app.config.voice.realtime

    # 1) нет облака, fallback включён -> локальный режим
    app.realtime = None
    calls.clear()
    app._realtime_available = lambda: False
    rt.offline_fallback = True
    rt.provider = "gemini"
    app._on_wake_detected()
    assert calls == ["local"], f"ожидался local, получено {calls}"
    print("ok  нет облака + fallback -> локальный режим")

    # 2) облако доступно -> облачный realtime
    app.realtime = None
    calls.clear()
    app._realtime_available = lambda: True
    app._on_wake_detected()
    assert calls == ["cloud"], f"ожидался cloud, получено {calls}"
    print("ok  облако доступно -> cloud Realtime")

    # 3) провайдер local -> всегда локальный режим
    app.realtime = None
    calls.clear()
    rt.provider = "local"
    app._on_wake_detected()
    assert calls == ["local"], f"ожидался local, получено {calls}"
    print("ok  provider=local -> локальный режим")

    # 4) нет облака и fallback выключен -> ошибка + возврат к слову активации
    app.realtime = None
    calls.clear()
    rt.provider = "gemini"
    rt.offline_fallback = False
    app._realtime_available = lambda: False
    app._on_wake_detected()
    assert calls == ["rearm"], f"ожидался rearm, получено {calls}"
    print("ok  fallback off -> ошибка и rearm")

    app.qapp.quit()
    print("\nWAKE ROUTING TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
