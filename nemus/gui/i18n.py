"""Язык интерфейса Немуси (ru / en).

Строки читаются через :func:`tr` при сборке виджетов; смена языка применяется
при следующем запуске (после ``set_language`` из конфига).
"""

from __future__ import annotations

from typing import Any

#: доступные языки интерфейса: (код, подпись для выпадающего списка)
LANGUAGES: list[tuple[str, str]] = [
    ("ru", "Русский"),
    ("en", "English"),
]

_STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        # сайдбар / окно
        "sidebar.sub": "ГОЛОСОВОЙ АССИСТЕНТ",
        "btn.listen": "🎤 Слушать",
        "btn.listen_stop": "⏹️ Стоп",
        "btn.realtime": "⚡ Realtime",
        "btn.realtime_stop": "⏹️ Остановить",
        "btn.clear": "🗑️ Очистить",
        "btn.settings": "⚙️ Настройки",
        "chat.header": "Диалог",
        "chat.placeholder": "Напишите сообщение или команду…",
        "chat.welcome": (
            "Немуся готова к работе! 🚀\n"
            "Напишите сообщение или нажмите **Слушать** для голосового ввода."
        ),
        "chat.cleared": "Диалог очищен.",
        "chat.no_core": "Ядро ассистента не подключено.",
        "model.prefix": "модель: {model}",
        "model.demo": "демо-режим",
        # состояния
        "state.idle": "Готова",
        "state.listening": "Слушаю…",
        "state.thinking": "Думаю…",
        "state.speaking": "Говорю…",
        "state.error": "Ошибка",
        # карточки чата
        "role.you": "ВЫ",
        "role.you_letter": "В",
        "role.system": "СИСТЕМА",
        "role.error": "ОШИБКА",
        "role.tool": "ИНСТРУМЕНТ",
        "tool.result": "Результат **{name}**: {status}",
        "tool.ok": "✅ ок",
        "tool.fail": "❌ ошибка",
        # трей
        "tray.tooltip": "Немуся — ассистент",
        "tray.show": "Показать",
        "tray.quit": "Выход",
        # приложение
        "app.local_mode": "Локальный режим диалога (офлайн).",
        "app.local_failed": "Не удалось запустить локальный режим диалога.",
        "app.local_no_net": "Нет связи с облаком — локальный режим диалога.",
        "app.realtime_off_local": (
            "Realtime недоступен (нет интернета или ключа), локальный режим "
            "отключён."
        ),
        "app.voice_error": "Голосовой модуль недоступен: {exc}",
        "app.listen_failed": (
            "Не удалось запустить голосовой ввод (нет wake word модели/микрофона)."
        ),
        "app.realtime_state": "Realtime: {msg}",
        "app.lang_restart": "Язык интерфейса изменён — перезапустите приложение.",
        # настройки
        "set.title": "Настройки",
        "set.sub": "ПРОВАЙДЕР · ГОЛОС · ИНСТРУМЕНТЫ",
        "set.window": "Немуся — настройки",
        "tab.llm": "LLM",
        "tab.voice": "Голос",
        "tab.spotify": "Spotify",
        "tab.tools": "Инструменты/Безопасность",
        "tab.about": "О программе",
        "btn.cancel": "Отмена",
        "btn.save": "Сохранить",
        "llm.name": "Имя ассистента",
        "llm.provider": "Провайдер по умолчанию",
        "llm.model": "Модель по умолчанию",
        "llm.temp": "Температура",
        "llm.max_tokens": "Max tokens",
        "llm.keys": "API-ключи (хранятся в OS keyring)",
        "llm.key_for": "Ключ для {name}",
        "lang.group": "Язык",
        "lang.reply": "Язык ответов",
        "lang.ui": "Язык интерфейса",
        "lang.reply_tip": "Авто — отвечать на языке собеседника.",
        "lang.ui_tip": "Применяется после перезапуска приложения.",
        "lang.opt.auto": "Авто (язык собеседника)",
        "lang.opt.ru": "Русский",
        "lang.opt.en": "English",
        "voice.wake_enable": "Включить активацию по слову",
        "voice.wake": "Wake word",
        "voice.engine": "Движок",
        "voice.words": "Слова (через запятую)",
        "voice.rt_group": "Режим реального времени (непрерывный голосовой диалог)",
        "voice.rt_enable": "Доступен (кнопка Realtime в окне)",
        "voice.rt_provider": "Провайдер (local — офлайн)",
        "voice.rt_gemini": "Модель Gemini Live",
        "voice.rt_openai": "Модель OpenAI Realtime",
        "voice.rt_voice": "Голос OpenAI",
        "voice.rt_audio": "Передавать и воспроизводить аудио",
        "voice.rt_wake": "Запускать диалог по слову активации (Немуся)",
        "voice.rt_fallback": "Без интернета/ключа — локальный режим (офлайн)",
        "voice.barge_in": (
            "Прерывать голосом (иначе микрофон молчит, пока говорит ассистент)"
        ),
        "spotify.enable": "Включить управление Spotify",
        "spotify.info": (
            "Создайте приложение на developer.spotify.com/dashboard. В разделе "
            "Redirect URIs добавьте ровно адрес из поля ниже (кнопка «Копировать»), "
            "нажмите Add, затем Save. ВАЖНО: Spotify запрещает «localhost» — нужен "
            "именно 127.0.0.1, без завершающего слеша. Потом вставьте Client ID и "
            "Client Secret и нажмите «Войти в Spotify». Для управления "
            "воспроизведением нужен Spotify Premium."
        ),
        "spotify.device_hint": "напр. «Мой компьютер»",
        "spotify.copy": "Копировать",
        "spotify.signin": "Войти в Spotify (откроется браузер)",
        "spotify.device": "Устройство по умолчанию",
        "spotify.check_uri": "Проверьте Redirect URI: ",
        "spotify.need_ids": "Сначала введите Client ID и Client Secret.",
        "spotify.opening": (
            "Открываю браузер… Войдите в Spotify и подтвердите доступ."
        ),
        "spotify.copied": (
            "Скопировано: {text} — вставьте это в Redirect URIs на Spotify "
            "Dashboard, нажмите Add и Save."
        ),
        "spotify.ok": "Готово! Вход выполнен, токен сохранён. Нажмите «Сохранить».",
        "spotify.fail": "Не удалось: {msg}",
        "tools.perm_mode": "Режим разрешений",
        "tools.auto_allow": "Авто-разрешать",
        "tools.workspace": "Рабочая папка",
        "tools.workspace_hint": "Папка для собственных скриптов и файлов ассистента.",
        "about.text": (
            "<b>Немуся (Nemesis)</b> — голосовой ИИ-ассистент для Windows.<br>"
            "Версия {version} · Python 3.13 · GUI PySide6<br><br>"
            "<b>Возможности</b><br>"
            "• Диалог с LLM через LiteLLM: Gemini, OpenAI, Anthropic, OpenRouter, Ollama<br>"
            "• Инструменты: файлы, оболочка, Python, веб-поиск, запуск приложений,<br>"
            "  скриншоты и управление экраном (cv2 + Windows OCR), Spotify,<br>"
            "  память на SQLite, плагины и саморасширение<br>"
            "• Голос: wake word «Немуся» (Vosk), распознавание faster-whisper,<br>"
            "  синтез речи edge-tts, подавление шума (RNNoise/Krisp)<br>"
            "• Режим реального времени: Gemini Live, OpenAI Realtime<br>"
            "  или локальный офлайн-конвейер STT → LLM → TTS<br><br>"
            "Лицензия: GNU GPLv3+<br>"
            "Автор: <a href=\"https://github.com/Narek-D8v\">Narek Haroyan (Narek-D8v)</a><br>"
            "Исходный код: <a href=\"https://github.com/Narek-D8v/Nemesis-desktop\">"
            "github.com/Narek-D8v/Nemesis-desktop</a>"
        ),
        # подтверждение
        "confirm.window": "Немуся — запрос разрешения",
        "confirm.title": "Требуется подтверждение",
        "confirm.tool": "Инструмент: <b>{name}</b>",
        "confirm.risk": "●  Уровень риска: {risk}",
        "confirm.params": "ПАРАМЕТРЫ",
        "confirm.allow": "Разрешить",
        "confirm.deny": "Отклонить",
        "risk.safe": "Безопасно",
        "risk.medium": "Внимание",
        "risk.dangerous": "Опасно",
    },
    "en": {
        "sidebar.sub": "VOICE ASSISTANT",
        "btn.listen": "🎤 Listen",
        "btn.listen_stop": "⏹️ Stop",
        "btn.realtime": "⚡ Realtime",
        "btn.realtime_stop": "⏹️ Stop",
        "btn.clear": "🗑️ Clear",
        "btn.settings": "⚙️ Settings",
        "chat.header": "Chat",
        "chat.placeholder": "Type a message or command…",
        "chat.welcome": (
            "Немуся is ready to go! 🚀\n"
            "Type a message or press **Listen** for voice input."
        ),
        "chat.cleared": "Chat cleared.",
        "chat.no_core": "Assistant core is not connected.",
        "model.prefix": "model: {model}",
        "model.demo": "demo mode",
        "state.idle": "Ready",
        "state.listening": "Listening…",
        "state.thinking": "Thinking…",
        "state.speaking": "Speaking…",
        "state.error": "Error",
        "role.you": "YOU",
        "role.you_letter": "Y",
        "role.system": "SYSTEM",
        "role.error": "ERROR",
        "role.tool": "TOOL",
        "tool.result": "Result **{name}**: {status}",
        "tool.ok": "✅ ok",
        "tool.fail": "❌ error",
        "tray.tooltip": "Немуся — assistant",
        "tray.show": "Show",
        "tray.quit": "Quit",
        "app.local_mode": "Offline local dialog mode.",
        "app.local_failed": "Failed to start the local dialog mode.",
        "app.local_no_net": "Cloud unreachable — local dialog mode.",
        "app.realtime_off_local": (
            "Realtime unavailable (no internet or API key), local mode disabled."
        ),
        "app.voice_error": "Voice module unavailable: {exc}",
        "app.listen_failed": (
            "Failed to start voice input (no wake word model / microphone)."
        ),
        "app.realtime_state": "Realtime: {msg}",
        "app.lang_restart": "UI language changed — restart the app.",
        "set.title": "Settings",
        "set.sub": "PROVIDER · VOICE · TOOLS",
        "set.window": "Немуся — settings",
        "tab.llm": "LLM",
        "tab.voice": "Voice",
        "tab.spotify": "Spotify",
        "tab.tools": "Tools/Security",
        "tab.about": "About",
        "btn.cancel": "Cancel",
        "btn.save": "Save",
        "llm.name": "Assistant name",
        "llm.provider": "Default provider",
        "llm.model": "Default model",
        "llm.temp": "Temperature",
        "llm.max_tokens": "Max tokens",
        "llm.keys": "API keys (stored in the OS keyring)",
        "llm.key_for": "Key for {name}",
        "lang.group": "Language",
        "lang.reply": "Reply language",
        "lang.ui": "Interface language",
        "lang.reply_tip": "Auto — answer in the user's language.",
        "lang.ui_tip": "Applied after the app is restarted.",
        "lang.opt.auto": "Auto (user's language)",
        "lang.opt.ru": "Russian",
        "lang.opt.en": "English",
        "voice.wake_enable": "Enable wake word activation",
        "voice.wake": "Wake word",
        "voice.engine": "Engine",
        "voice.words": "Words (comma separated)",
        "voice.rt_group": "Realtime mode (continuous voice dialog)",
        "voice.rt_enable": "Available (Realtime button in the window)",
        "voice.rt_provider": "Provider (local — offline)",
        "voice.rt_gemini": "Gemini Live model",
        "voice.rt_openai": "OpenAI Realtime model",
        "voice.rt_voice": "OpenAI voice",
        "voice.rt_audio": "Send and play audio",
        "voice.rt_wake": "Start the dialog on the wake word (Немуся)",
        "voice.rt_fallback": "No internet/key — local offline mode",
        "voice.barge_in": (
            "Allow voice interruption (otherwise the mic stays silent while "
            "the assistant speaks)"
        ),
        "spotify.enable": "Enable Spotify control",
        "spotify.info": (
            "Create an app at developer.spotify.com/dashboard. In the Redirect "
            "URIs section add exactly the address below (use the Copy button), "
            "press Add and then Save. IMPORTANT: Spotify forbids \"localhost\" — "
            "use 127.0.0.1 with no trailing slash. Then paste the Client ID and "
            "Client Secret and press Sign in. Playback control needs Spotify "
            "Premium."
        ),
        "spotify.device_hint": "e.g. “My PC”",
        "spotify.copy": "Copy",
        "spotify.signin": "Sign in to Spotify (browser opens)",
        "spotify.device": "Default device",
        "spotify.check_uri": "Check the Redirect URI: ",
        "spotify.need_ids": "Enter the Client ID and Client Secret first.",
        "spotify.opening": (
            "Opening the browser… Log in to Spotify and approve access."
        ),
        "spotify.copied": (
            "Copied: {text} — paste it into Redirect URIs on the Spotify "
            "Dashboard, then press Add and Save."
        ),
        "spotify.ok": "Done! Signed in and token saved. Press Save.",
        "spotify.fail": "Failed: {msg}",
        "tools.perm_mode": "Permission mode",
        "tools.auto_allow": "Auto-allow",
        "tools.workspace": "Workspace folder",
        "tools.workspace_hint": (
            "Folder for the assistant's own scripts and files."
        ),
        "about.text": (
            "<b>Немуся (Nemesis)</b> — a voice AI assistant for Windows.<br>"
            "Version {version} · Python 3.13 · GUI PySide6<br><br>"
            "<b>Features</b><br>"
            "• LLM chat via LiteLLM: Gemini, OpenAI, Anthropic, OpenRouter, Ollama<br>"
            "• Tools: files, shell, Python, web search, app launching,<br>"
            "  screenshots &amp; screen control (cv2 + Windows OCR), Spotify,<br>"
            "  SQLite memory, plugins and self-extension<br>"
            "• Voice: “Nemyusya” wake word (Vosk), faster-whisper STT,<br>"
            "  edge-tts synthesis, noise suppression (RNNoise/Krisp)<br>"
            "• Realtime mode: Gemini Live, OpenAI Realtime<br>"
            "  or the local offline STT → LLM → TTS pipeline<br><br>"
            "License: GNU GPLv3+<br>"
            "Author: <a href=\"https://github.com/Narek-D8v\">Narek Haroyan (Narek-D8v)</a><br>"
            "Source code: <a href=\"https://github.com/Narek-D8v/Nemesis-desktop\">"
            "github.com/Narek-D8v/Nemesis-desktop</a>"
        ),
        "confirm.window": "Немуся — permission request",
        "confirm.title": "Confirmation required",
        "confirm.tool": "Tool: <b>{name}</b>",
        "confirm.risk": "●  Risk level: {risk}",
        "confirm.params": "PARAMETERS",
        "confirm.allow": "Allow",
        "confirm.deny": "Deny",
        "risk.safe": "Safe",
        "risk.medium": "Caution",
        "risk.dangerous": "Dangerous",
    },
}

_lang: str = "ru"


def available_languages() -> list[tuple[str, str]]:
    """Список языков интерфейса для выпадающего списка."""
    return list(LANGUAGES)


def current_language() -> str:
    """Текущий код языка интерфейса (``ru``/``en``)."""
    return _lang


def set_language(lang: str | None) -> None:
    """Переключает язык интерфейса (неизвестные значения игнорируются)."""
    global _lang
    code = (lang or "").strip().lower().split("-")[0]
    if code in _STRINGS:
        _lang = code


def normalize_language(lang: str | None) -> str:
    """Нормализует значение из конфига: ``ru``/``en`` (иначе ``ru``)."""
    code = (lang or "").strip().lower().split("-")[0]
    return code if code in _STRINGS else "ru"


def language_label(lang: str) -> str:
    """Подпись языка для интерфейса."""
    return dict(LANGUAGES).get(lang, lang)


def tr(key: str, **params: Any) -> str:
    """Возвращает строку текущего языка (fallback — русский, затем ключ)."""
    table = _STRINGS.get(_lang, _STRINGS["ru"])
    template = table.get(key) or _STRINGS["ru"].get(key) or key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


__all__ = [
    "LANGUAGES",
    "available_languages",
    "current_language",
    "set_language",
    "normalize_language",
    "language_label",
    "tr",
]
