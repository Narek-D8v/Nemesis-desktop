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
        "app.realtime_start_failed": (
            "Не удалось перезапустить голосовой режим: {msg}. Текстовый режим "
            "работает как прежде."
        ),
        "app.settings_failed": (
            "Не удалось полностью применить настройки: {msg}. Остальные "
            "параметры сохранены."
        ),
        "app.lang_restart": "Язык интерфейса изменён — перезапустите приложение.",
        # настройки
        "set.title": "Настройки",
        "set.sub": "ПРОВАЙДЕР · ГОЛОС · ИНСТРУМЕНТЫ",
        "set.window": "Немуся — настройки",
        "tab.llm": "LLM",
        "tab.voice": "Голос",
        "tab.tts": "Озвучка (TTS)",
        "tab.mcp": "MCP",
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
        # озвучка (TTS)
        "tts.engine": "Движок синтеза",
        "tts.engine_edge_tts": "Edge TTS (облачный, бесплатный)",
        "tts.engine_openai": "OpenAI TTS (нужен API-ключ)",
        "tts.engine_system": "Системный (Windows, офлайн)",
        "tts.lang": "Язык (локаль)",
        "tts.all_langs": "Все языки",
        "tts.voice": "Голос",
        "tts.rate": "Скорость речи",
        "tts.volume": "Громкость",
        "tts.hint": (
            "Edge TTS — сотни голосов на десятках языков (нужен интернет). "
            "OpenAI TTS — естественная речь, требует API-ключ openai. "
            "Системный — офлайн-синтез Windows, работает без интернета."
        ),
        "tts.load": "Обновить список голосов",
        "tts.preview": "Прослушать",
        "tts.preview_text": "Привет, я Немуся!",
        "tts.loading": "Загружаю список голосов…",
        "tts.loaded": "Загружено голосов: {n}.",
        "tts.load_failed": (
            "Не удалось загрузить список голосов (нет интернета или доступа?) — "
            "показан базовый набор."
        ),
        "tts.preview_failed": "Прослушивание не удалось: {msg}",
        # MCP
        "mcp.enable": "Включить MCP-серверы",
        "mcp.refresh": "Обновлять инструменты каждые",
        "mcp.unit_s": "{n} с",
        "mcp.unit_min": "{n} мин",
        "mcp.unit_h": "{n} ч",
        "mcp.unit_day": "{n} д",
        "mcp.refresh_tip_period": (
            "Как часто фоново перечитывать инструменты работающих серверов."
        ),
        "mcp.hint": (
            "Внешние серверы (Node.js/Python) добавляют Немусе сторонние "
            "инструменты. Изменения применяются после «Сохранить»."
        ),
        "mcp.help": (
            "MCP (Model Context Protocol) — внешние серверы с дополнительными "
            "инструментами для Немуси: работа с файлами, интернетом, базами данных "
            "и т. д.\nЧтобы добавить сервер: выберите готовый пример или впишите "
            "команду сами, затем «Проверить» и «Сохранить».\nГотовые серверы: "
            "https://github.com/modelcontextprotocol/servers"
        ),
        "mcp.presets": "Примеры:",
        "mcp.presets_tip": (
            "Готовые серверы от Model Context Protocol: "
            "github.com/modelcontextprotocol/servers"
        ),
        "mcp.preset_add": "Добавить",
        "mcp.preset_add_tip": "Добавить выбранный пример в список серверов",
        "mcp.import": "Импорт…",
        "mcp.import_tip": (
            "Импортировать серверы из mcp_config.json (формат Claude Desktop)"
        ),
        "mcp.import_done": "Импортировано серверов: {n}.",
        "mcp.import_none": "В файле нет серверов (секция mcpServers пуста).",
        "mcp.import_fail": "Не удалось прочитать файл: {msg}",
        "mcp.tools_count": "{n} инстр.",
        "mcp.preset.filesystem": "Файлы и папки",
        "mcp.preset.everything": "Тестовый набор",
        "mcp.preset.memory": "Память ассистента",
        "mcp.preset.fetch": "Чтение веб-страниц",
        "mcp.preset.git": "Работа с git",
        "mcp.preset.github": "GitHub (нужен токен)",
        "mcp.preset.time": "Дата и время",
        "mcp.empty": (
            "Серверов пока нет. Выберите готовый пример сверху или добавьте "
            "пустую карточку и заполните её вручную."
        ),
        "mcp.name": "Название",
        "mcp.nameless": "Без названия",
        "mcp.name_hint": "Например: Файлы и папки",
        "mcp.cmd": "Команда",
        "mcp.cmd_ph": "npx / uvx / node / python",
        "mcp.cmd_empty": "команда не задана",
        "mcp.cmd_tip": "Чем запускать: npx (Node.js), uvx (Python), node, python…",
        "mcp.args": "Аргументы",
        "mcp.args_ph": "-y @modelcontextprotocol/server-filesystem <путь>",
        "mcp.cwd": "Рабочая папка",
        "mcp.env": "Переменные окружения",
        "mcp.env_hint": "Каждая переменная с новой строки: KEY=значение",
        "mcp.server_enable": "Сервер включён",
        "mcp.server_enable_tip": (
            "Выключенный сервер остаётся в списке, но не запускается."
        ),
        "mcp.add": "Добавить сервер",
        "mcp.add_tip": "Добавить пустую карточку и заполнить поля вручную",
        "mcp.delete": "Удалить",
        "mcp.delete_tip": "Удалить этот сервер из списка",
        "mcp.expand_tip": "Показать или скрыть настройки сервера",
        "mcp.probe": "Проверить",
        "mcp.probe_tip": (
            "Запустить сервер, прочитать его инструменты и отключиться. "
            "Ничего не сохраняет."
        ),
        "mcp.refresh_tools": "Обновить",
        "mcp.refresh_tip": (
            "Перечитать инструменты уже работающих серверов (после правок "
            "сначала нажмите «Сохранить»)."
        ),
        "mcp.need_cmd": "Укажите команду запуска сервера.",
        "mcp.probe_running": "Проверяю подключение к серверу…",
        "mcp.probe_ok": "Ок. Инструменты: {tools}",
        "mcp.probe_fail": "Ошибка подключения: {msg}",
        "mcp.tools_tip": "Инструменты: {tools}",
        "mcp.fail_tip": "Ошибка: {msg}",
        "mcp.need_pkg": (
            "Пакет «mcp» не установлен — интеграция отключена (pip install mcp)."
        ),
        "mcp.status_no_core": "MCP-менеджер не инициализирован.",
        "mcp.status_on": "Активных MCP-инструментов: {n}.",
        "mcp.status_off": "MCP выключен или серверы не заданы.",
        "mcp.no_tools": "(серверы отвечают, инструментов нет)",
        "mcp.refresh_running": "Перечитываю инструменты серверов…",
        "mcp.refresh_done": "Обновлено. Инструменты: {tools}",
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
        "app.realtime_start_failed": (
            "Failed to restart the voice mode: {msg}. Text mode works as usual."
        ),
        "app.settings_failed": (
            "Could not fully apply the settings: {msg}. The rest were saved."
        ),
        "app.lang_restart": "UI language changed — restart the app.",
        "set.title": "Settings",
        "set.sub": "PROVIDER · VOICE · TOOLS",
        "set.window": "Немуся — settings",
        "tab.llm": "LLM",
        "tab.voice": "Voice",
        "tab.tts": "Voice (TTS)",
        "tab.mcp": "MCP",
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
        # TTS
        "tts.engine": "Synthesis engine",
        "tts.engine_edge_tts": "Edge TTS (cloud, free)",
        "tts.engine_openai": "OpenAI TTS (needs an API key)",
        "tts.engine_system": "System (Windows, offline)",
        "tts.lang": "Language (locale)",
        "tts.all_langs": "All languages",
        "tts.voice": "Voice",
        "tts.rate": "Speech rate",
        "tts.volume": "Volume",
        "tts.hint": (
            "Edge TTS — hundreds of voices in dozens of languages (internet "
            "required). OpenAI TTS — natural speech, needs the openai API key. "
            "System — offline Windows synthesis, no internet needed."
        ),
        "tts.load": "Refresh voice list",
        "tts.preview": "Preview",
        "tts.preview_text": "Hi, I am Nemus!",
        "tts.loading": "Loading the voice list…",
        "tts.loaded": "Voices loaded: {n}.",
        "tts.load_failed": (
            "Could not fetch the voice list (offline or no access?) — showing "
            "the default set."
        ),
        "tts.preview_failed": "Preview failed: {msg}",
        # MCP
        "mcp.enable": "Enable MCP servers",
        "mcp.refresh": "Refresh tools every",
        "mcp.unit_s": "{n} s",
        "mcp.unit_min": "{n} min",
        "mcp.unit_h": "{n} h",
        "mcp.unit_day": "{n} d",
        "mcp.refresh_tip_period": (
            "How often to re-read tools of the running servers in the background."
        ),
        "mcp.hint": (
            "External servers (Node.js/Python) give Nemus extra tools. "
            "Changes apply after you press Save."
        ),
        "mcp.help": (
            "MCP (Model Context Protocol) — external servers that provide extra "
            "tools for Nemus: file access, the web, databases, and more.\n"
            "To add a server: pick a preset or type a command yourself, then "
            "press «Test» and «Save». Ready-made servers: "
            "https://github.com/modelcontextprotocol/servers"
        ),
        "mcp.presets": "Presets:",
        "mcp.presets_tip": (
            "Ready-made servers from Model Context Protocol: "
            "github.com/modelcontextprotocol/servers"
        ),
        "mcp.preset_add": "Add",
        "mcp.preset_add_tip": "Add the selected preset to the server list",
        "mcp.import": "Import…",
        "mcp.import_tip": (
            "Import servers from mcp_config.json (Claude Desktop format)"
        ),
        "mcp.import_done": "Imported servers: {n}.",
        "mcp.import_none": "No servers in the file (mcpServers is empty).",
        "mcp.import_fail": "Failed to read the file: {msg}",
        "mcp.tools_count": "{n} tools",
        "mcp.preset.filesystem": "Files & folders",
        "mcp.preset.everything": "Test suite",
        "mcp.preset.memory": "Assistant memory",
        "mcp.preset.fetch": "Web page reader",
        "mcp.preset.git": "Git repositories",
        "mcp.preset.github": "GitHub (token required)",
        "mcp.preset.time": "Date & time",
        "mcp.empty": (
            "No servers yet. Pick a preset above or add an empty card and fill "
            "it in yourself."
        ),
        "mcp.name": "Name",
        "mcp.nameless": "Untitled",
        "mcp.name_hint": "e.g. Files & folders",
        "mcp.cmd": "Command",
        "mcp.cmd_ph": "npx / uvx / node / python",
        "mcp.cmd_empty": "no command set",
        "mcp.cmd_tip": "What to launch with: npx (Node.js), uvx (Python), node, python…",
        "mcp.args": "Arguments",
        "mcp.args_ph": "-y @modelcontextprotocol/server-filesystem <path>",
        "mcp.cwd": "Working directory",
        "mcp.env": "Environment variables",
        "mcp.env_hint": "One variable per line: KEY=value",
        "mcp.server_enable": "Server enabled",
        "mcp.server_enable_tip": (
            "A disabled server stays in the list but is not launched."
        ),
        "mcp.add": "Add server",
        "mcp.add_tip": "Add an empty card and fill in the fields manually",
        "mcp.delete": "Delete",
        "mcp.delete_tip": "Remove this server from the list",
        "mcp.expand_tip": "Show or hide the server settings",
        "mcp.probe": "Test",
        "mcp.probe_tip": (
            "Launch the server, read its tools and disconnect. Saves nothing."
        ),
        "mcp.refresh_tools": "Refresh",
        "mcp.refresh_tip": (
            "Re-read tools of the running servers (press «Save» after edits "
            "first)."
        ),
        "mcp.need_cmd": "Set the server command.",
        "mcp.probe_running": "Testing the server connection…",
        "mcp.probe_ok": "OK. Tools: {tools}",
        "mcp.probe_fail": "Connection error: {msg}",
        "mcp.tools_tip": "Tools: {tools}",
        "mcp.fail_tip": "Error: {msg}",
        "mcp.need_pkg": (
            "The «mcp» package is not installed — integration disabled "
            "(pip install mcp)."
        ),
        "mcp.status_no_core": "MCP manager is not initialized.",
        "mcp.status_on": "Active MCP tools: {n}.",
        "mcp.status_off": "MCP is disabled or no servers configured.",
        "mcp.no_tools": "(servers respond, no tools)",
        "mcp.refresh_running": "Re-reading server tools…",
        "mcp.refresh_done": "Refreshed. Tools: {tools}",
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
