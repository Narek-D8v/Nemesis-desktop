"""Тест каталога моделей: фолбэк, парсеры провайдеров и combo в настройках.

Запуск:  venv\\Scripts\\python.exe scripts\\test_model_catalog.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.core.providers.models import (  # noqa: E402
    STATIC_MODELS,
    list_models,
    parse_gemini,
    parse_ollama,
    parse_openrouter,
    sort_models,
)
from nemus.core.providers.registry import PROVIDER_SPECS  # noqa: E402


def test_static_catalog() -> None:
    for provider in PROVIDER_SPECS:
        models = STATIC_MODELS.get(provider)
        assert models, f"у провайдера {provider} нет статического списка"
        for name, free in models:
            assert name and isinstance(free, bool), (provider, name, free)
        ordered = sort_models(models)
        assert ordered == sort_models(ordered), provider  # стабильная сортировка
        flags = [free for _, free in ordered]
        assert flags == sorted(flags, reverse=True), (
            f"у {provider} бесплатные не сверху: {flags}"
        )
        assert len({n for n, _ in ordered}) == len(ordered), f"дубли у {provider}"
    print("ok  статический каталог: есть у всех провайдеров, бесплатные сверху")


def test_parsers() -> None:
    openrouter = parse_openrouter(
        {
            "data": [
                {"id": "google/gemma:free", "pricing": {"prompt": "0"}},
                {"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0.00015"}},
                {"id": "meta/llama", "pricing": {}},  # без цены — платная
                {"id": ""},  # мусор пропускается
            ]
        }
    )
    assert openrouter == [
        ("openrouter/google/gemma:free", True),
        ("openrouter/openai/gpt-4o-mini", False),
        ("openrouter/meta/llama", False),
    ], openrouter

    gemini = parse_gemini(
        {
            "models": [
                {"name": "models/gemini-2.0-flash",
                 "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/embedding-001",
                 "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-2.5-pro"},  # без списка методов — годится
            ]
        }
    )
    assert gemini == [
        ("gemini/gemini-2.0-flash", True),
        ("gemini/gemini-2.5-pro", True),
    ], gemini

    ollama = parse_ollama({"models": [{"name": "llama3.2:latest"}, {"name": ""}]})
    assert ollama == [("ollama/llama3.2:latest", True)], ollama
    print("ok  парсеры: openrouter (free/цена), gemini (методы), ollama (имена)")


def test_fallback_without_network() -> None:
    import nemus.core.providers.models as models_mod

    original = models_mod._http_json

    def offline(*_args: object, **_kwargs: object):
        raise OSError("нет сети")

    models_mod._http_json = offline  # type: ignore[assignment]
    try:
        for provider in PROVIDER_SPECS:
            models, live = list_models(provider)
            assert not live, provider
            assert models == sort_models(STATIC_MODELS.get(provider, [])), provider

        models, live = list_models("unknown-provider")
        assert models == [] and not live
    finally:
        models_mod._http_json = original  # type: ignore[assignment]
    print("ok  фолбэк: без сети отдаётся статический каталог")


def test_dialog_combo() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QCompleter

    import nemus.gui.dialogs as dialogs
    from nemus.gui import i18n
    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    original = dialogs.list_models
    dialogs.list_models = lambda provider, cfg=None: (
        sort_models(STATIC_MODELS.get(provider, [])),
        False,
    )
    dlg = SettingsDialog(None, core=None)
    try:
        assert dlg.cb_model.isEditable()
        assert dlg.cb_model.count() > 0, "при открытии список моделей пуст"

        # начальная модель из конфига выбрана и бейдж не попадает в данные
        current = dlg._current_model()
        assert current and " — " not in current, current

        # смена провайдера: combo перезаполняется моделями нового провайдера
        dlg.cb_provider.setCurrentText("ollama")
        worker = dlg._models_worker
        assert worker is not None, "фоновый список моделей не запущен"
        assert worker.wait(8000), "фоновый список моделей не завершился"
        for _ in range(3):
            QApplication.processEvents()

        expected = sort_models(STATIC_MODELS["ollama"])
        assert dlg.cb_model.count() == len(expected), (
            dlg.cb_model.count(),
            expected,
        )
        for index, (model, free) in enumerate(expected):
            assert dlg.cb_model.itemData(index) == model
            badge = i18n.tr("llm.free") if free else i18n.tr("llm.paid")
            assert dlg.cb_model.itemText(index).endswith(f" — {badge}")

        # сохранение: у выбранного пункта — id без бейджа
        dlg.cb_model.setCurrentIndex(0)
        assert dlg._current_model() == expected[0][0]
        # ручной ввод тоже не содержит бейджа
        dlg.cb_model.setEditText("my/custom-model — " + i18n.tr("llm.free"))
        assert dlg._current_model() == "my/custom-model"

        # попап с поиском и фильтрами: открыт, ищет по подстроке, фильтрует
        dlg.cb_provider.setCurrentText("openrouter")
        worker = dlg._models_worker
        assert worker is not None and worker.wait(8000)
        for _ in range(3):
            QApplication.processEvents()
        full = sort_models(STATIC_MODELS["openrouter"])
        combo = dlg.cb_model
        assert combo.count() == len(full), (combo.count(), full)
        free = [model for model, is_free in full if is_free]

        completer = combo.completer()
        assert completer.completionMode() == QCompleter.CompletionMode.InlineCompletion
        assert completer.caseSensitivity() == Qt.CaseSensitivity.CaseInsensitive

        combo.setCurrentIndex(0)  # без набранного текста — поиск пустой
        combo.showPopup()
        popup = combo.popup
        assert popup.isVisible(), "попап не открылся"
        assert popup.rows() == len(full), (popup.rows(), len(full))
        assert popup.lbl_count.text(), "счётчик найденных не подписан"

        popup.set_search("llama")
        assert popup.rows() == 1, popup.rows()
        popup.set_search("")
        assert popup.rows() == len(full)

        popup.set_mode("free")
        assert popup.rows() == len(free), (popup.rows(), free)
        popup.set_mode("paid")
        assert popup.rows() == len(full) - len(free), popup.rows()
        popup.set_mode("all")
        assert popup.rows() == len(full)

        # выбор пункта пишет id без бейджа и закрывает попап
        proxy_index = popup.list.model().index(0, 0)
        popup.list.clicked.emit(proxy_index)
        assert not popup.isVisible(), "попап не закрылся после выбора"
        assert dlg._current_model() == full[0][0], dlg._current_model()
    finally:
        dlg.done(0)  # дожидается фонового потока
        dialogs.list_models = original
    print("ok  GUI: combo моделей, бейджи, смена провайдера, сохранение без бейджа")
    print("ok  GUI: попап с поиском, фильтрами и счётчиком")


def test_dialog_os() -> None:
    """Настройка ОС в GUI: семейство + дистрибутив и подсказка агенту в промпте."""
    from PySide6.QtWidgets import QApplication

    from nemus.config import get_config
    from nemus.core.agent import build_system_prompt
    from nemus.core.tools.registry import ToolRegistry
    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    dlg = SettingsDialog(None, core=None)
    cfg = get_config(reload=True)
    old_family, old_details = cfg.assistant.os_family, cfg.assistant.os_details
    try:
        assert hasattr(dlg, "cb_os_family"), "нет комбо ОС"
        assert hasattr(dlg, "edit_os_details"), "нет поля уточнения ОС"
        ids = [dlg.cb_os_family.itemData(i) for i in range(dlg.cb_os_family.count())]
        assert ids == ["auto", "windows", "linux", "macos"], ids
        # при открытии выбрано значение из конфига
        assert dlg.cb_os_family.currentData() == old_family, dlg.cb_os_family.currentData()
        assert dlg.edit_os_details.text() == old_details

        # пользователь указал Linux + дистрибутив — ровно то читает _save
        dlg._select_by_data(dlg.cb_os_family, "linux")
        dlg.edit_os_details.setText("Ubuntu 24.04")
        assert str(dlg.cb_os_family.currentData() or "auto") == "linux"
        assert dlg.edit_os_details.text().strip() == "Ubuntu 24.04"

        # и это уходит в системный промпт агенту
        cfg.assistant.os_family = "linux"
        cfg.assistant.os_details = "Ubuntu 24.04"
        prompt = build_system_prompt(cfg, ToolRegistry())
        assert "Linux — Ubuntu 24.04" in prompt, prompt
    finally:
        cfg.assistant.os_family = old_family
        cfg.assistant.os_details = old_details
        dlg.done(0)
    print("ok  GUI: ОС в настройках (семейство + дистрибутив) и подсказка в промпте")


def test_dialog_limits() -> None:
    """Лимиты агента в GUI: спиннеры шагов/времени, значения из конфига."""
    from PySide6.QtWidgets import QApplication

    from nemus.config import get_config
    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    dlg = SettingsDialog(None, core=None)
    cfg = get_config(reload=True)
    old_steps, old_budget = cfg.tools.max_steps, cfg.tools.run_budget_seconds
    try:
        assert hasattr(dlg, "spin_max_steps"), "нет спиннера шагов"
        assert hasattr(dlg, "spin_time_budget"), "нет спиннера времени"
        # значения пришли из конфига
        assert dlg.spin_max_steps.value() == old_steps, dlg.spin_max_steps.value()
        assert dlg.spin_time_budget.value() == old_budget, dlg.spin_time_budget.value()
        # диапазоны не дают поставить 0 или вечность
        assert dlg.spin_max_steps.minimum() >= 5 and dlg.spin_max_steps.maximum() <= 1000
        assert dlg.spin_time_budget.minimum() >= 30
        # пользователь меняет — ровно это читает _save
        dlg.spin_max_steps.setValue(250)
        dlg.spin_time_budget.setValue(1800)
        assert dlg.spin_max_steps.value() == 250
        assert dlg.spin_time_budget.value() == 1800
    finally:
        cfg.tools.max_steps = old_steps
        cfg.tools.run_budget_seconds = old_budget
        dlg.done(0)
    print("ok  GUI: лимиты агента (шаги и время) настраиваются")


def main() -> int:
    test_static_catalog()
    test_parsers()
    test_fallback_without_network()
    test_dialog_combo()
    test_dialog_os()
    test_dialog_limits()
    print("\nMODEL CATALOG TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
