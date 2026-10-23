"""GUI-вкладка TTS: три движка, голоса, сохранение/загрузка выбора.

Запуск:  venv\\Scripts\\python.exe scripts\\test_tts_tab.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _wait_flag(app, predicate, timeout: float = 20.0) -> bool:
    """Крутит event loop, пока predicate не станет истиной (или не выйдет таймаут)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841 — держим ссылку
    dlg = SettingsDialog(None, core=None)

    # --- три движка с машинными id
    ids = [dlg.cb_tts_engine.itemData(i) for i in range(dlg.cb_tts_engine.count())]
    assert ids == ["edge-tts", "openai", "system"], ids
    print(f"  движки: {ids}")

    # --- openai: фиксированный список, сеть не нужна
    dlg.cb_tts_engine.setCurrentIndex(ids.index("openai"))
    app.processEvents()
    voices = [dlg.cb_tts_voice.itemData(i) for i in range(dlg.cb_tts_voice.count())]
    assert "alloy" in voices and len(voices) >= 10, voices
    assert "openai" in dlg._tts_full_by_engine
    # локальный фильтр языка не должен скрывать голоса без локали
    assert dlg.cb_tts_lang.count() == 1, "у openai не должно быть локалей"
    print(f"  openai: {len(voices)} голосов, фильтр локалей пуст")

    # --- system: локальный список голосов Windows (фоновый воркер)
    dlg.cb_tts_engine.setCurrentIndex(ids.index("system"))
    assert dlg._voices_worker is not None
    ok = _wait_flag(app, lambda: "system" in dlg._tts_full_by_engine)
    assert ok, "список системных голосов не загрузился"
    voices = [dlg.cb_tts_voice.itemData(i) for i in range(dlg.cb_tts_voice.count())]
    assert voices, "системные голоса не подгрузились"
    print(f"  system: голоса {voices}")

    # --- возврат к edge-tts: голоса показываются сразу (базовый набор из кэша),
    # полный облачный список докручивается в фоне, если есть интернет
    dlg.cb_tts_engine.setCurrentIndex(ids.index("edge-tts"))
    app.processEvents()
    voices = [dlg.cb_tts_voice.itemData(i) for i in range(dlg.cb_tts_voice.count())]
    assert "ru-RU-DmitryNeural" in voices, voices
    worker = dlg._voices_worker
    if worker is not None and worker.isRunning():
        _wait_flag(app, lambda: not worker.isRunning(), timeout=30.0)
        app.processEvents()
    full = "edge-tts" in dlg._tts_full_by_engine
    print(f"  edge-tts: базовый набор показан, полный список загружен: {full}")

    # --- сохранение/загрузка: движок и голос доезжают до конфига
    dlg.cb_tts_engine.setCurrentIndex(ids.index("openai"))
    app.processEvents()
    dlg.cb_tts_voice.setCurrentIndex(
        dlg.cb_tts_voice.findData("nova")
    )
    dlg._save()
    assert dlg.config.voice.tts.engine == "openai"
    assert dlg.config.voice.tts.voice == "nova"

    dlg.config.voice.tts.engine = "edge-tts"
    dlg.config.voice.tts.voice = "ru-RU-SvetlanaNeural"
    dlg._load()
    assert dlg._tts_engine_id() == "edge-tts"
    assert str(dlg.cb_tts_voice.currentData() or "") == "ru-RU-SvetlanaNeural"
    print("  сохранение/загрузка движка и голоса: ок")

    print("\nTTS TAB TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
