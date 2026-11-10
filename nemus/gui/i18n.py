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
        # брейншторм: кликабельные варианты A/B/C/D
        "bm.title": "БРЕЙНШТОРМ · ВЫБЕРИТЕ ВАРИАНТЫ",
        "bm.hint": "клик по варианту выбирает его; можно ответить и текстом: «1A, 2B»",
        "bm.recommended": "рекомендую",
        "bm.custom": "Свой вариант",
        "bm.custom_empty": "Свой вариант: нажмите, чтобы ввести",
        "bm.custom_title": "Свой вариант",
        "bm.custom_hint": "Введите свой вариант для этого пункта:",
        "bm.send": "Отправить выбор",
        "bm.send_default": "Отправить рекомендованное",
        "bm.sent": "Выбор отправлен",
        "bm.frozen": "Выбор не отправлен — диалог продолжен",
        # справка по скиллу: команда «/имя» без задачи
        "help.title": "СПРАВКА · {name}",
        "help.files": "вспомогательные файлы: {n}",
        "help.hint": "Напиши «{cmd} <задача>» — и я возьмусь за работу по этому скиллу.",
        "help.unknown": "Скилла {cmd} нет.",
        "help.available": "Доступные команды: {list}",
        # диалоги (сессии)
        "sessions.button": "🗂 {title}",
        "sessions.window": "Диалоги",
        "sessions.count": "{n} сообщений · {date}",
        "sessions.hint": "Открыть список диалогов",
        "sessions.disabled": "История выключена",
        "sessions.new": "＋ Новый диалог",
        "sessions.new_short": "＋ Новый",
        "sessions.rename": "Переименовать диалог",
        "sessions.rename_one": "✏️ Переименовать…",
        "sessions.rename_prompt": "Название диалога:",
        "sessions.renamed": "Диалог переименован: {title}",
        "sessions.delete": "Удалить диалог",
        "sessions.delete_one": "🗑 Удалить…",
        "sessions.confirm_delete": (
            "Удалить диалог «{title}»? Все его сообщения будут потеряны."
        ),
        "sessions.deleted": "Диалог удалён.",
        "sessions.showing": "Диалог: {title}",
        "sessions.busy": "Дождитесь ответа — потом переключим диалог.",
        "model.prefix": "модель: {model}",
        "model.demo": "демо-режим",
        "remind.fired": "⏰ Напоминание: {text}",
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
        "role.thought": "Мысли · просто думаю, это не ответ",
        # ОС в настройках (подсказка агенту)
        "llm.os_group": "Система (ОС)",
        "os.family": "ОС",
        "os.opt.auto": "Определить автоматически",
        "os.opt.windows": "Windows",
        "os.opt.linux": "Linux",
        "os.opt.macos": "macOS",
        "os.details": "Дистрибутив / версия",
        "os.tip": (
            "Подсказка агенту: какой системы вы работаете. Для Linux укажите "
            "дистрибутив (Ubuntu 24.04, Fedora 40…), для Windows — версию "
            "(Windows 11 Pro). Пусто = определить автоматически."
        ),
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
        "tab.telegram": "Telegram",
        "tab.tools": "Инструменты",
        "tab.about": "О программе",
        "tab.skills": "Скиллы",
        # Telegram-бот
        "tg.enable": "Включить Telegram-бота",
        "tg.enable_tip": (
            "Писать Немусе в Telegram и получать ответы и файлы прямо в чат"
        ),
        "tg.hint": (
            "Бот выполняет команды на этом компьютере, поэтому слушает только "
            "указанные ID: случайный человек не получит доступ к ноутбуку. "
            "Свой ID можно узнать у @userinfobot. Без токена и хотя бы одного ID "
            "бот не запустится."
        ),
        "tg.token": "Токен бота",
        "tg.users": "ID пользователей",
        "tg.users_tip": (
            "Кому разрешено писать боту — через запятую. Другим бот ответит «Доступ запрещён»."
        ),
        "tg.allow_groups": "Отвечать в группах",
        "tg.transcribe": "Распознавать голосовые сообщения",
        "tg.max_file": "Максимальный файл",
        "tg.mb": "МБ",
        "tg.check": "Проверить токен",
        "tg.botfather": "Открыть @BotFather",
        "tg.checking": "Проверяю токен…",
        "tg.check_ok": "Токен верный: @{username}",
        "tg.check_fail": "Токен не подошёл: {msg}",
        "tg.need_token": "Сначала вставьте токен от @BotFather.",
        "tg.status_running": "Бот @{username} слушает Telegram.",
        "tg.status_error": "Бот не запустился: {msg}",
        "tg.status_off": "Бот выключен — включите галочку и сохраните настройки.",
        "tg.autostart_group": "Демон без окна",
        "tg.autostart": "Запускать Telegram-бота при входе в Windows",
        "tg.autostart_tip": (
            "В автозагрузке Windows появится ярлык «Немуся Telegram». Он "
            "запускает бота после входа в систему даже с закрытым "
            "приложением, а задача NemusTelegram перезапускает его при падении."
        ),
        "tg.autostart_on": (
            "Автозапуск включён: ярлык «Немуся Telegram» и задача "
            "NemusTelegram активны."
        ),
        "tg.autostart_off": "Автозапуск выключен.",
        "tg.autostart_pending_on": "Автозапуск включится после сохранения настроек.",
        "tg.autostart_pending_off": "Автозапуск выключится после сохранения настроек.",
        "tg.autostart_fail": "Не удалось изменить автозапуск: {msg}",
        "tg.autostart_unsupported": "Автозапуск доступен только в Windows.",
        "tg.howto": (
            "Напишите боту текст, пришлите файл, фото или голосовое — задача "
            "выполнится здесь, а результат и готовые файлы бот пришлёт в чат. "
            "Видео можно попросить превратить в круг: «сделай из этого видео кружок»."
        ),
        # скиллы (формат Claude Agent Skills)
        "skills.enable": "Включить скиллы",
        "skills.enable_tip": (
            "Скиллы в формате Claude Agent Skills: модель видит названия и "
            "описания, а полный текст подгружает сама."
        ),
        "skills.toggle_tip": "Включить или выключить скилл {name}",
        "skills.refresh": "Обновить",
        "skills.open_folder": "Открыть папку",
        "skills.open_folder_tip": "Где лежат скиллы на диске",
        "skills.hint": (
            "Скилл — папка с файлом SKILL.md: название, описание и пошаговая "
            "инструкция (плюс свои скрипты рядом). Модель видит в промпте только "
            "названия и описания, а полный текст подгружает сама, когда задача "
            "подходит под описание. Формат совместим с Claude Code — те же папки "
            "читают и другие агенты. Свой скилл модель создаёт инструментом "
            "create_skill."
        ),
        "skills.folder": "Папка",
        "skills.empty": (
            "Скиллов нет. Создайте папку .agents/skills/<имя>/ с файлом SKILL.md "
            "(шапка из name и description между «---») или попросите Немусю "
            "сохранить навык через create_skill."
        ),
        "skills.no_desc": "без описания",
        "skills.more": "Нажмите на описание — будет подробнее",
        "skills.in_instruction": "В инструкции:",
        "skills.tools": "Инструменты:",
        "skills.path": "Путь:",
        "skills.count": "Найдено скиллов: {n}. Снятие галочки прячет скилл от модели.",
        # магазин скиллов
        "btn.store": "Магазин",
        "store.button_tip": "Магазин: готовые скиллы и плагины с GitHub в один клик",
        "store.title": "Магазин",
        "store.section.skills": "Скиллы",
        "store.section.plugins": "Плагины",
        "store.plugin_hint": (
            "Плагины Claude Code целиком: скиллы, команды и агенты внутри. "
            "Скиллы работают как обычные, а команды вызываются так: «/плагин:команда»."
        ),
        "store.plugin_search_placeholder": "Поиск плагинов на GitHub: review, git, tests…",
        "store.plugin_loading": "Загружаем плагины с GitHub…",
        "store.plugin_rec_empty": (
            "Не удалось загрузить плагины. Проверьте интернет и нажмите «Обновить»."
        ),
        "store.plugin_installed_empty": (
            "Плагинов пока нет. Выберите подходящий в «Рекомендуемых» или найдите поиском."
        ),
        "store.plugin_parts": "скиллов: {skills} · команд: {commands} · агентов: {agents}",
        "store.version": "v{version}",
        "store.plugin_install": "Установить плагин «{name}» целиком",
        "store.plugin_installing": "Скачиваем плагин «{name}»…",
        "store.plugin_installed_status": (
            "Плагин «{name}» установлен: его скиллы и команды уже доступны."
        ),
        "store.plugin_install_fail": "Не удалось установить плагин «{name}»: {msg}",
        "store.plugin_update_tip": "Обновить плагин «{name}» с GitHub",
        "store.plugin_remove_tip": "Удалить плагин «{name}» целиком",
        "store.plugin_member": "в составе плагина «{name}»",
        "store.plugin_local_zip": "Из .zip",
        "store.plugin_local_folder": "Из папки",
        "store.plugin_local_zip_tip": "Поставить плагин из скачанного архива целиком",
        "store.plugin_local_folder_tip": (
            "Поставить плагин из папки на диске целиком"
        ),
        "store.plugin_local_zip_title": "Выберите архив с плагином",
        "store.plugin_local_folder_title": "Выберите папку плагина",
        "store.plugin_local_filter": "Архивы плагинов (*.zip)",
        "store.plugin_local_fail": "Плагин из файла не установлен: {msg}",
        "store.local_source": "источник: {path}",
        "store.source_manual": "источник: поставлен вручную",
        "store.source_repo": "источник: {repo}",
        "store.source_repo_path": "источник: {repo} · {path}",
        "store.skill_local_zip_tip": "Поставить скилл из скачанного архива",
        "store.skill_local_folder_tip": "Поставить скилл из папки на диске",
        "store.skill_local_zip_title": "Выберите архив со скиллом",
        "store.skill_local_folder_title": "Выберите папку скилла",
        "store.skill_local_filter": "Архивы скиллов (*.zip)",
        "store.skill_local_fail": "Скилл из файла не установлен: {msg}",
        "store.hint": (
            "Готовые скиллы с GitHub: нажмите «+», и скилл встанет в вашу папку "
            "скиллов — Немуся увидит его со следующего запроса, без перезапуска."
        ),
        "store.search_placeholder": "Поиск на GitHub: pdf, excel, видео, дизайн…",
        "store.find": "Найти",
        "store.refresh": "Обновить",
        "store.refresh_tip": "Пересобрать витрину со случайной подборкой",
        "store.tab.recommended": "Рекомендуемые",
        "store.tab.search": "Поиск",
        "store.tab.installed": "Скачанные",
        "store.tab.installed_count": "Скачанные ({n})",
        "store.loading": "Загружаем с GitHub…",
        "store.search_hint": "Введите запрос — найдём скиллы в репозиториях GitHub.",
        "store.search_empty": "По запросу «{query}» ничего не нашлось. Попробуйте другое слово.",
        "store.rec_empty": "Не удалось загрузить витрину. Проверьте интернет и нажмите «Обновить».",
        "store.rec_end": "Это все скиллы, что нашлись, — остальное ищите через поиск.",
        "store.rec_loading": "Догружаем витрину с GitHub…",
        "store.filter_tip": "Показать скиллы только одного репозитория",
        "store.filter_all": "Все репозитории ({n})",
        "store.sort_tip": "Порядок витрины: по звёздам или по свежести репозитория",
        "store.sort.stars": "По звёздам",
        "store.sort.new": "По новизне",
        "store.fresh": "обновлён {date}",
        "store.search_loading": "Догружаем результаты с GitHub…",
        "store.search_end": "Это все результаты — уточните запрос, чтобы найти больше.",
        "store.files": "файлов: {n}",
        "store.plugin": "плагин {name}",
        "store.install": "Установить скилл «{name}»",
        "store.installing": "Скачиваем «{name}»…",
        "store.installed": "установлен",
        "store.installed_status": "Скилл «{name}» установлен — он уже в папке скиллов.",
        "store.install_fail": "Не удалось установить «{name}»: {msg}",
        "store.load_fail": "Не удалось прочитать папку скиллов: {msg}",
        "store.installed_empty": (
            "Пока ничего не скачано. Откройте «Рекомендуемые» или «Поиск» и "
            "нажмите «+» у подходящего скилла."
        ),
        "store.group.standalone": "Самостоятельные скиллы ({n})",
        "store.group.plugin_members": "В составе плагинов ({n})",
        "store.path": "Папка: {path}",
        "store.update_tip": "Обновить скилл «{name}» с GitHub",
        "store.updating": "Обновляем «{name}» с GitHub…",
        "store.update_done": "Скилл «{name}» обновлён до свежей версии с GitHub.",
        "store.update_fresh": "Скилл «{name}» уже свежий — менять нечего.",
        "store.update_fail": "Не удалось обновить «{name}»: {msg}",
        "store.updated_chat": "Скилл «{name}» обновлён с GitHub.",
        "store.remove_tip": "Удалить скилл «{name}»",
        "store.remove_title": "Удаление скилла",
        "store.remove_ask": (
            "Удалить скилл «{name}»? Папка скилла со всеми файлами будет удалена."
        ),
        "store.remove_done": "Скилл «{name}» удалён.",
        "store.remove_fail": "Не удалось удалить «{name}»: файлы заняты или нет прав.",
        "store.removed_chat": "«{name}» удалён из папки скиллов.",
        "store.installed_chat": (
            "«{name}» из магазина установлен — он уже в папке скиллов и "
            "доступен модели со следующего запроса."
        ),
        "store.enabled_chat": (
            "Скиллы были выключены — включил их, чтобы модель видела новые навыки."
        ),
        "skills.load_fail": "Не удалось прочитать скиллы: {msg}",
        "skills.open_fail": "Не удалось открыть папку: {msg}",
        "btn.cancel": "Отмена",
        "btn.save": "Сохранить",
        "llm.name": "Имя ассистента",
        "llm.provider": "Провайдер по умолчанию",
        "llm.model": "Модель по умолчанию",
        "llm.model_tip": (
            "Выберите модель из списка (бесплатные сверху) или впишите своё "
            "название — оно сохранится как есть. У бесплатных моделей есть "
            "лимиты запросов и суточные квоты — на долгих задачах они кончаются "
            "быстрее, чем кажется."
        ),
        "llm.free": "бесплатно",
        "llm.paid": "платно",
        "llm.search_ph": "Поиск модели…",
        "llm.filter_all": "Все",
        "llm.filter_free": "Бесплатные",
        "llm.filter_paid": "Платные",
        "llm.model_count": "{n} шт.",
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
        "voice.words_tip": (
            "Можно кириллицей или латиницей: nemusya станет «немуся». "
            "Имена, которых нет в словаре Vosk, разбиваются на похожие слова "
            "(«немуся» → «не муся»), а совсем незнакомые слушаются в "
            "open-vocabulary режиме."
        ),
        "voice.rt_group": "Режим реального времени",
        "voice.rt_group_tip": (
            "Непрерывный голосовой диалог: Gemini Live или OpenAI Realtime, "
            "локальный офлайн-режим как запасной."
        ),
        "voice.rt_enable": "Доступен (кнопка Realtime в окне)",
        "voice.rt_provider": "Провайдер (local — офлайн)",
        "voice.rt_gemini": "Модель Gemini Live",
        "voice.rt_openai": "Модель OpenAI Realtime",
        "voice.rt_voice": "Голос OpenAI",
        "voice.rt_audio": "Передавать и воспроизводить аудио",
        "voice.rt_wake": "Запускать по слову активации (Немуся)",
        "voice.rt_fallback": "Офлайн без интернета и ключа",
        "voice.barge_in": "Прерывать голосом (barge-in)",
        "voice.barge_in_tip": "Иначе микрофон молчит, пока говорит ассистент.",
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
        "tools.limits_group": "Лимиты агента",
        "tools.max_steps": "Макс. шагов за запрос",
        "tools.time_budget": "Время на запрос",
        "tools.limits_hint": (
            "Сколько шагов (вызовов инструментов) и сколько времени максимум "
            "тратится на одно сообщение. Больше — длинные задачи (установка "
            "движков, рендер видео) доводятся до конца."
        ),
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
        # brainstorm: clickable A/B/C/D options
        "bm.title": "BRAINSTORM · PICK OPTIONS",
        "bm.hint": "click an option to pick it; you can also answer in text: \"1A, 2B\"",
        "bm.recommended": "recommended",
        "bm.custom": "Custom",
        "bm.custom_empty": "Custom: click to type",
        "bm.custom_title": "Custom option",
        "bm.custom_hint": "Type your own option for this question:",
        "bm.send": "Send selection",
        "bm.send_default": "Send recommended",
        "bm.sent": "Selection sent",
        "bm.frozen": "Selection not sent — the dialog moved on",
        # skill help: "/name" command without a task
        "help.title": "HELP · {name}",
        "help.files": "helper files: {n}",
        "help.hint": "Type «{cmd} <task>» and I will work on it with this skill.",
        "help.unknown": "There is no {cmd} skill.",
        "help.available": "Available commands: {list}",
        # chats (sessions)
        "sessions.button": "🗂 {title}",
        "sessions.window": "Chats",
        "sessions.count": "{n} messages · {date}",
        "sessions.hint": "Open the chat list",
        "sessions.disabled": "History is off",
        "sessions.new": "＋ New chat",
        "sessions.new_short": "＋ New",
        "sessions.rename": "Rename chat",
        "sessions.rename_one": "✏️ Rename…",
        "sessions.rename_prompt": "Chat name:",
        "sessions.renamed": "Chat renamed: {title}",
        "sessions.delete": "Delete chat",
        "sessions.delete_one": "🗑 Delete…",
        "sessions.confirm_delete": (
            "Delete chat \"{title}\"? All of its messages will be lost."
        ),
        "sessions.deleted": "Chat deleted.",
        "sessions.showing": "Chat: {title}",
        "sessions.busy": "Wait for the reply before switching chats.",
        "model.prefix": "model: {model}",
        "remind.fired": "⏰ Reminder: {text}",
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
        "role.thought": "Thoughts · just thinking, not an answer",
        # OS in settings (hint for the agent)
        "llm.os_group": "System (OS)",
        "os.family": "OS",
        "os.opt.auto": "Detect automatically",
        "os.opt.windows": "Windows",
        "os.opt.linux": "Linux",
        "os.opt.macos": "macOS",
        "os.details": "Distro / version",
        "os.tip": (
            "Hint for the agent: which system you run. For Linux name the distro "
            "(Ubuntu 24.04, Fedora 40…), for Windows the version (Windows 11 Pro). "
            "Empty = detect automatically."
        ),
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
        "tab.telegram": "Telegram",
        "tab.tools": "Tools",
        "tab.about": "About",
        "tab.skills": "Skills",
        # Telegram bot
        "tg.enable": "Enable the Telegram bot",
        "tg.enable_tip": "Message Nemusya in Telegram and get answers and files in the chat",
        "tg.hint": (
            "The bot runs commands on this computer, so it only listens to the "
            "listed IDs: a random person cannot reach the laptop. Find your ID "
            "via @userinfobot. Without a token and at least one ID the bot will "
            "not start."
        ),
        "tg.token": "Bot token",
        "tg.users": "User IDs",
        "tg.users_tip": (
            "Who may message the bot — comma separated. Others get “Access denied”."
        ),
        "tg.allow_groups": "Reply in groups",
        "tg.transcribe": "Transcribe voice messages",
        "tg.max_file": "Maximum file",
        "tg.mb": "MB",
        "tg.check": "Check token",
        "tg.botfather": "Open @BotFather",
        "tg.checking": "Checking the token…",
        "tg.check_ok": "Token is valid: @{username}",
        "tg.check_fail": "Token rejected: {msg}",
        "tg.need_token": "Paste a token from @BotFather first.",
        "tg.status_running": "Bot @{username} is listening to Telegram.",
        "tg.status_error": "The bot did not start: {msg}",
        "tg.status_off": "The bot is off — enable it and save the settings.",
        "tg.autostart_group": "Headless daemon",
        "tg.autostart": "Start the Telegram bot at Windows sign-in",
        "tg.autostart_tip": (
            "A “Немуся Telegram” shortcut appears in Windows Startup. It starts "
            "the bot after sign-in even when the app is closed, and the "
            "NemusTelegram task restarts it if it crashes."
        ),
        "tg.autostart_on": (
            "Autostart is on: the “Немуся Telegram” shortcut and the "
            "NemusTelegram task are active."
        ),
        "tg.autostart_off": "Autostart is off.",
        "tg.autostart_pending_on": "Autostart will be enabled after saving.",
        "tg.autostart_pending_off": "Autostart will be disabled after saving.",
        "tg.autostart_fail": "Could not change autostart: {msg}",
        "tg.autostart_unsupported": "Autostart is available on Windows only.",
        "tg.howto": (
            "Send the bot text, a file, a photo or a voice message — the task "
            "runs here and the result and files come back to the chat. You can "
            "ask it to turn a video into a round video note."
        ),
        # skills (Claude Agent Skills format)
        "skills.enable": "Enable skills",
        "skills.enable_tip": (
            "Skills in Claude Agent Skills format: the model sees names and "
            "descriptions and loads the full text itself."
        ),
        "skills.toggle_tip": "Turn the skill {name} on or off",
        "skills.refresh": "Refresh",
        "skills.open_folder": "Open folder",
        "skills.open_folder_tip": "Where skills live on disk",
        "skills.hint": (
            "A skill is a folder with a SKILL.md file: name, description and a "
            "step-by-step instruction (plus your own scripts alongside). The model "
            "sees only names and descriptions in the prompt and loads the full text "
            "itself when a task matches. The format is compatible with Claude Code "
            "— other agents read the same folders. The assistant saves its own "
            "skills with the create_skill tool."
        ),
        "skills.folder": "Folder",
        "skills.empty": (
            "No skills yet. Create .agents/skills/<name>/ with a SKILL.md file "
            "(frontmatter with name and description between «---»), or ask Немуся "
            "to save a skill with create_skill."
        ),
        "skills.no_desc": "no description",
        "skills.more": "Click the description for details",
        "skills.in_instruction": "In the instructions:",
        "skills.tools": "Tools:",
        "skills.path": "Path:",
        "skills.count": "Skills found: {n}. Unchecking one hides it from the model.",
        # skill store
        "btn.store": "Store",
        "store.button_tip": "Store: ready-made skills and plugins from GitHub in one click",
        "store.title": "Store",
        "store.section.skills": "Skills",
        "store.section.plugins": "Plugins",
        "store.plugin_hint": (
            "Whole Claude Code plugins: skills, commands and agents inside. "
            "Skills work as usual, commands are called as «/plugin:command»."
        ),
        "store.plugin_search_placeholder": "Search plugins on GitHub: review, git, tests…",
        "store.plugin_loading": "Loading plugins from GitHub…",
        "store.plugin_rec_empty": (
            "Could not load plugins. Check the internet and press «Refresh»."
        ),
        "store.plugin_installed_empty": (
            "No plugins yet. Pick one in «Recommended» or search for it."
        ),
        "store.plugin_parts": "skills: {skills} · commands: {commands} · agents: {agents}",
        "store.version": "v{version}",
        "store.plugin_install": "Install plugin «{name}» as a whole",
        "store.plugin_installing": "Downloading plugin «{name}»…",
        "store.plugin_installed_status": (
            "Plugin «{name}» installed: its skills and commands are already available."
        ),
        "store.plugin_install_fail": "Could not install plugin «{name}»: {msg}",
        "store.plugin_update_tip": "Update plugin «{name}» from GitHub",
        "store.plugin_remove_tip": "Remove plugin «{name}» with all its files",
        "store.plugin_member": "part of plugin «{name}»",
        "store.plugin_local_zip": "From .zip",
        "store.plugin_local_folder": "From folder",
        "store.plugin_local_zip_tip": "Install a plugin from a downloaded archive",
        "store.plugin_local_folder_tip": "Install a plugin from a folder on disk",
        "store.plugin_local_zip_title": "Choose a plugin archive",
        "store.plugin_local_folder_title": "Choose a plugin folder",
        "store.plugin_local_filter": "Plugin archives (*.zip)",
        "store.plugin_local_fail": "Plugin from file was not installed: {msg}",
        "store.local_source": "source: {path}",
        "store.source_manual": "source: installed manually",
        "store.source_repo": "source: {repo}",
        "store.source_repo_path": "source: {repo} · {path}",
        "store.skill_local_zip_tip": "Install a skill from a downloaded archive",
        "store.skill_local_folder_tip": "Install a skill from a folder on disk",
        "store.skill_local_zip_title": "Choose a skill archive",
        "store.skill_local_folder_title": "Choose a skill folder",
        "store.skill_local_filter": "Skill archives (*.zip)",
        "store.skill_local_fail": "Skill from file was not installed: {msg}",
        "store.hint": (
            "Ready-made skills from GitHub: press «+» and the skill lands in your "
            "skills folder — Nemusya sees it from the next request, no restart."
        ),
        "store.search_placeholder": "Search GitHub: pdf, excel, video, design…",
        "store.find": "Search",
        "store.refresh": "Refresh",
        "store.refresh_tip": "Rebuild the showcase with a new random selection",
        "store.tab.recommended": "Recommended",
        "store.tab.search": "Search",
        "store.tab.installed": "Downloaded",
        "store.tab.installed_count": "Downloaded ({n})",
        "store.loading": "Loading from GitHub…",
        "store.search_hint": "Type a query — we will look for skills in GitHub repositories.",
        "store.search_empty": "Nothing found for «{query}». Try another word.",
        "store.rec_empty": "Could not load the showcase. Check the internet and press «Refresh».",
        "store.rec_end": "That's every skill we found — search for the rest.",
        "store.rec_loading": "Loading more of the showcase from GitHub…",
        "store.filter_tip": "Show skills from a single repository only",
        "store.filter_all": "All repositories ({n})",
        "store.sort_tip": "Showcase order: by stars or by repository freshness",
        "store.sort.stars": "By stars",
        "store.sort.new": "By freshness",
        "store.fresh": "updated {date}",
        "store.search_loading": "Loading more results from GitHub…",
        "store.search_end": "That's every result — refine the query to find more.",
        "store.files": "files: {n}",
        "store.plugin": "plugin {name}",
        "store.install": "Install skill «{name}»",
        "store.installing": "Downloading «{name}»…",
        "store.installed": "installed",
        "store.installed_status": "Skill «{name}» installed — it is already in the skills folder.",
        "store.install_fail": "Could not install «{name}»: {msg}",
        "store.load_fail": "Could not read the skills folder: {msg}",
        "store.installed_empty": (
            "Nothing downloaded yet. Open «Recommended» or «Search» and press "
            "«+» on a skill you like."
        ),
        "store.group.standalone": "Standalone skills ({n})",
        "store.group.plugin_members": "Inside plugins ({n})",
        "store.path": "Folder: {path}",
        "store.update_tip": "Update skill «{name}» from GitHub",
        "store.updating": "Updating «{name}» from GitHub…",
        "store.update_done": "Skill «{name}» updated to the latest GitHub version.",
        "store.update_fresh": "Skill «{name}» is already up to date.",
        "store.update_fail": "Could not update «{name}»: {msg}",
        "store.updated_chat": "Skill «{name}» was updated from GitHub.",
        "store.remove_tip": "Remove skill «{name}»",
        "store.remove_title": "Remove skill",
        "store.remove_ask": (
            "Remove skill «{name}»? The whole skill folder will be deleted."
        ),
        "store.remove_done": "Skill «{name}» removed.",
        "store.remove_fail": "Could not remove «{name}»: files are in use or access denied.",
        "store.removed_chat": "«{name}» was removed from the skills folder.",
        "store.installed_chat": (
            "«{name}» from the store is installed — it is in the skills folder "
            "and available to the model from the next request."
        ),
        "store.enabled_chat": (
            "Skills were off — I turned them on so the model can see the new skills."
        ),
        "skills.load_fail": "Failed to read skills: {msg}",
        "skills.open_fail": "Failed to open the folder: {msg}",
        "btn.cancel": "Cancel",
        "btn.save": "Save",
        "llm.name": "Assistant name",
        "llm.provider": "Default provider",
        "llm.model": "Default model",
        "llm.model_tip": (
            "Pick a model from the list (free ones first) or type your own — it "
            "will be saved as-is. Free models have request limits and daily "
            "quotas, which run out faster than you expect on long tasks."
        ),
        "llm.free": "free",
        "llm.paid": "paid",
        "llm.search_ph": "Search models…",
        "llm.filter_all": "All",
        "llm.filter_free": "Free",
        "llm.filter_paid": "Paid",
        "llm.model_count": "{n} shown",
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
        "voice.words_tip": (
            "Cyrillic or Latin: nemusya becomes “немуся”. Names missing from "
            "the Vosk dictionary are split into similar words (“немуся” → "
            "“не муся”); unknown names use open-vocabulary recognition."
        ),
        "voice.rt_group": "Realtime mode",
        "voice.rt_group_tip": (
            "Continuous voice dialog: Gemini Live or OpenAI Realtime, with the "
            "local offline mode as a fallback."
        ),
        "voice.rt_enable": "Available (Realtime button in the window)",
        "voice.rt_provider": "Provider (local — offline)",
        "voice.rt_gemini": "Gemini Live model",
        "voice.rt_openai": "OpenAI Realtime model",
        "voice.rt_voice": "OpenAI voice",
        "voice.rt_audio": "Send and play audio",
        "voice.rt_wake": "Start on the wake word (Немуся)",
        "voice.rt_fallback": "Offline without internet or key",
        "voice.barge_in": "Allow voice interruption (barge-in)",
        "voice.barge_in_tip": (
            "Otherwise the mic stays silent while the assistant speaks."
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
        "tools.limits_group": "Agent limits",
        "tools.max_steps": "Max steps per request",
        "tools.time_budget": "Time per request",
        "tools.limits_hint": (
            "How many steps (tool calls) and how much time one message may take "
            "at most. Larger values let long tasks (engine installs, video "
            "renders) finish."
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
