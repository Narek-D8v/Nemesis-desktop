"""Язык ответов: определение языка собеседника и инструкции для LLM.

Единая точка правды для всех путей (текстовый агент, Gemini Live,
OpenAI Realtime, локальный диалог): как выбирать язык ответа и как
запрещать модели показывать собственные рассуждения.
"""

from __future__ import annotations

import re

#: язык ответов: ``auto`` — язык собеседника, иначе фиксированный тег
REPLY_AUTO = "auto"

#: варианты для выпадающего списка в настройках: (код, подпись)
REPLY_CHOICES: list[tuple[str, str]] = [
    ("auto", "Авто (язык собеседника)"),
    ("ru", "Русский"),
    ("en", "English"),
]

#: названия языков (используются в подсказках модели)
LANGUAGE_NAMES: dict[str, str] = {
    "ru": "русском",
    "en": "английском",
    "de": "немецком",
    "fr": "французском",
    "es": "испанском",
    "it": "итальянском",
    "pt": "португальском",
    "uk": "украинском",
    "kk": "казахском",
    "tr": "турецком",
    "pl": "польском",
    "nl": "нидерландском",
    "zh": "китайском",
    "ja": "японском",
    "ko": "корейском",
    "ar": "арабском",
}

#: коды для распознавания/озвучки (Gemini Live speechConfig, STT, TTS)
_SPEECH_CODES: dict[str, str] = {
    "ru": "ru-RU",
    "en": "en-US",
    "de": "de-DE",
    "fr": "fr-FR",
    "es": "es-ES",
    "it": "it-IT",
    "pt": "pt-BR",
    "uk": "uk-UA",
    "kk": "kk-KZ",
    "tr": "tr-TR",
    "pl": "pl-PL",
}

_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
_LATIN = re.compile(r"[A-Za-z]")


def normalize(language: str | None) -> str:
    """Приводит значение языка к виду ``auto``/``ru``/``en``."""
    code = (language or "").strip().lower().replace("_", "-")
    if not code or code in {"auto", "any", "auto-detect"}:
        return REPLY_AUTO
    return code.split("-")[0]


#: явные маркеры языка (только однозначные буквы, чтобы не угадывать)
_LANGUAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("es", "¿¡ñ"),  # вопросительные в начале и ñ
    ("de", "ß"),
    ("pt", "õã"),  # носовые гласные
    ("fr", "ç"),  # седиль
)


def detect_language(text: str) -> str:
    """Грубо определяет язык текста: ``ru``/``en``/``es``/… или ``""``."""
    if not text:
        return ""
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if cyrillic == 0 and latin == 0:
        return ""
    if cyrillic >= latin:
        return "ru"
    lowered = text.lower()
    for code, marker in _LANGUAGE_MARKERS:
        if any(ch in lowered for ch in marker):
            return code
    return "en"


def language_name(code: str) -> str:
    """Название языка в предложном падеже (для подсказки модели)."""
    code = normalize(code)
    return LANGUAGE_NAMES.get(code, code)


def speech_language_code(language: str, default: str = "ru-RU") -> str:
    """Код языка для распознавания/озвучки (``ru`` → ``ru-RU``)."""
    code = (language or "").strip()
    if not code or code == REPLY_AUTO:
        return default
    if "-" in code:
        return code
    return _SPEECH_CODES.get(code.lower(), default)


def reply_language_directive(reply_language: str, detected: str = "") -> str:
    """Инструкция модели, на каком языке отвечать.

    ``reply_language`` — настройка (``auto``/``ru``/…), ``detected`` — язык
    последней реплики пользователя (только для режима ``auto``).
    """
    code = normalize(reply_language)
    if code != REPLY_AUTO:
        if code == "ru":
            return (
                "Язык ответов: всегда отвечай только на русском языке. "
                "Если язык собеседника явно изменился — можешь подстроиться, но "
                "по умолчанию используй русский. Никогда не переводи сообщения пользователя."
            )
        name = language_name(code)
        return (
            f"Язык ответов: всегда отвечай только на {name} языке, независимо от "
            "языка разговора. Никогда не переводи сообщения пользователя."
        )

    detected = normalize(detected) if detected else ""
    if detected and detected != REPLY_AUTO:
        if detected == "ru":
            return (
                "Определи язык последней реплики пользователя и отвечай ТОЛЬКО на нём. "
                "Пользователь говорит по-русски — отвечай по-русски. "
                "Не переходи на другой язык, даже если отдельные слова не распознаны. "
                "Никогда не переводи сообщения пользователя."
            )
        name = language_name(detected)
        return (
            f"Определи язык последней реплики пользователя и отвечай ТОЛЬКО на нём. "
            f"Пользователь говорит на {name} языке — отвечай только на {name} языке. "
            "Сразу подстраивайся под смену языка собеседника. "
            "Никогда не переводь и не пересказывай сообщения пользователя. "
            "Не переключайся на испанский, французский или другие языки по ошибке."
        )
    return (
        "Определи язык каждой реплики пользователя и отвечай ТОЛЬКО на том же языке. "
        "Если собеседник перешёл с русского на английский — немедленно отвечай по-английски. "
        "Если язык не ясен или речь распознана с ошибками — отвечай на русском. "
        "Никогда не переводь сообщения пользователя и не смешивай языки."
    )


def anti_reasoning_directive() -> str:
    """Инструкция не показывать собственные рассуждения и черновики."""
    return (
        "Никогда не выводи свои рассуждения, размышления, план или черновики — "
        "только готовый ответ пользователю. Не начинай ответ с описания того, "
        "что ты собираешься сделать."
    )


def hallucination_guard_directive() -> str:
    """Запрет начинать разговор самой и отвечать на тишину/шум.

    Гасит галлюцинации realtime-моделей: на тишине, фоновом шуме и музыке
    модель иногда придумывает вопрос (в т.ч. на другом языке) и сама начинает
    диалог — эта инструкция прямо запрещает ей это делать.
    """
    return (
        "Фон и тишина: НЕ НАЧИНАЙ разговор первым и НЕ отвечай на фоновый шум, "
        "музыку, эхо, щелчки и нечленораздельные звуки. Если речь не разборчива "
        "или ты не уверен, что к тебе обращаются, — промолчи: не задавай вопрос, "
        "ничего не предлагай и не пытайся продолжить разговор. Никогда не сочиняй "
        "реплику за собеседника и не выдумывай вопрос, которого он не задавал. "
        "Сомневаешься — молчи и жди явной речи."
    )


# ---------------------------------------------------------------------------
#  Короткие реплики, простые запросы и маркеры отказа (ru + en)
# ---------------------------------------------------------------------------

#: мгновенные ответы без модели: прямое обращение, приветствие, спасибо.
#: Ключ — нормализованная реплика (нижний регистр, без пунктуации).
QUICK_REPLIES: dict[str, str] = {
    # русский
    "немуся": "Да, я здесь. Чем помочь?",
    "немо": "Да, я здесь. Чем помочь?",
    "немесис": "Да, я здесь. Чем помочь?",
    "немуся привет": "Привет! Я здесь. Чем помочь?",
    "привет немуся": "Привет! Я здесь. Чем помочь?",
    "привет": "Привет! Чем помочь?",
    "здравствуй": "Здравствуй! Чем помочь?",
    "ты тут": "Да, я здесь.",
    "ты здесь": "Да, я здесь.",
    "немуся ты тут": "Да, я здесь.",
    "немуся ты здесь": "Да, я здесь.",
    "на связи": "Да, на связи.",
    "как дела": "Всё хорошо, спасибо! А у тебя?",
    "как ты": "Всё отлично! А как ты?",
    "спасибо": "Пожалуйста!",
    "благодарю": "Пожалуйста!",
    # english
    "hello": "Hello! I'm here. How can I help?",
    "hi": "Hi! How can I help?",
    "nemesis": "Yes, I'm here. How can I help?",
    "nemo": "Yes, I'm here. How can I help?",
    "nemesis hello": "Hi! I'm here. How can I help?",
    "hello nemesis": "Hi! I'm here. How can I help?",
    "hey nemesis": "Hi! I'm here. How can I help?",
    "hi nemesis": "Hi! I'm here. How can I help?",
    "nemesis are you there": "Yes, I'm here.",
    "are you there": "Yes, I'm here.",
    "are you here": "Yes, I'm here.",
    "you there": "Yes, I'm here.",
    "how are you": "I'm doing great! And you?",
    "thanks": "You're welcome!",
    "thank you": "You're welcome!",
}

#: маркеры задач: такая реплика не может уйти в быстрый путь без
#: инструментов (подстрока в нижнем регистре; ru + en)
SIMPLE_EXCLUDE: tuple[str, ...] = (
    # ru
    "напомн", "таймер", "будильник", "погод", "курс", "который час",
    "сколько времени", "дата", "день недели", "открой", "закрой", "запусти",
    "включи", "выключи", "громк", "найди", "покажи", "проверь", "установи",
    "скачай", "создай", "сделай", "сгенерируй", "нарисуй", "презентац",
    "видео", "картин", "фото", "файл", "папк", "скрин", "код", "скрипт",
    "ошибк", "исправь", "почини", "напиши", "расскажи", "объясни",
    "обработа", "улучш", "переделай", "придумай",
    "переведи", "посчитай", "сравни", "план", "идеи", "брейншторм",
    "скилл", "музык", "песн", "стим", "телеграм", "почт", "отправь", "пришли",
    "удали", "измени", "сайт", "таблиц", "документ", "pdf", "excel",
    "word", "zip", "архив", "http", "d:\\", "c:\\", "работай",
    "продолжай", "анекдот", "шутк", "прикол", "смешн", "юмор",
    # состояние устройства и системы: «что на экране?», «сколько места на
    # диске?» — это задачи с инструментами, а не светская реплика. Иначе
    # лёгкая модель без инструментов отвечает «нет доступа к устройству».
    "экран", "монитор", "компьютер", "устройств", "ноутбук", "систем",
    "диск", "памят", "процесс", "батаре", "заряд", "камер", "микрофон",
    "динамик", "звук", "сеть", "интернет", "wi-fi", "wifi", "bluetooth",
    "принтер", "окно", "браузер", "приложени", "программ", "установлен",
    "характеристик", "видеокарт", "процессор", "оперативн", "ssd", "hdd",
    "рабочий стол", "рабочем столе", "корзин", "очист", "перезагруз",
    "заверши работу", "пауза", "перемотай", "трек", "плейлист",
    # частые действия с устройством и файлами: даже если предмет не назван,
    # это команда, а не светская беседа
    "помоги", "настрой", "подключи", "переключ", "нажми", "кликни", "щёлкни",
    "щелкни", "скопируй", "вставь", "вырежь", "сохрани", "запомн", "запиши",
    "посмотри", "отмени", "убери", "перезапусти", "сверни", "разверни",
    "передвинь", "перетащи", "напечатай", "введи", "выбери", "переимен",
    "стоп", "тише", "продолж", "играет", "яркост", "загрузк", "наушник",
    "колонк", "клавиатур", "мышь", "уведомлен", "буфер", "консол", "терминал",
    "командная строка", "реестр", "драйвер", "антивирус", "обои", "смени",
    # репродукция речи и правки: «скажи вслух», «прочитай», «доработай» —
    # это задачи, а не светская реплика
    "произнес", "скаж", "озвуч", "прочит", "повтор", "зачит", "продикт",
    "спой", "вслух", "доработ", "поправ", "обнов", "рефактор", "перепиш",
    # en
    "remind", "timer", "alarm", "weather", "forecast", "currency",
    "exchange rate", "what time", "clock", "date", "day of the week",
    "weekday", "open", "close", "launch", "start", "run", "turn on",
    "turn off", "switch on", "switch off", "volume", "mute", "find",
    "search", "look up", "show", "display", "check", "verify", "install",
    "download", "upload", "create", "build", "generate", "draw", "paint",
    "design", "presentation", "slides", "video", "image", "picture",
    "photo", "file", "folder", "directory", "screenshot", "screen",
    "code", "script", "skill", "error", "bug", "fix", "repair", "debug", "write",
    "tell me", "explain", "process", "improve", "redo", "rewrite",
    "refactor", "translate", "calculate", "compute", "count", "compare",
    "plan", "idea", "brainstorm", "music", "song", "playlist", "spotify",
    "steam", "telegram", "email", "mail", "send", "share", "delete",
    "remove", "change", "modify", "edit", "update", "website", "site",
    "table", "spreadsheet", "document", "pdf", "excel", "word", "zip",
    "archive", "http", "d:\\", "c:\\", "work on", "continue", "joke",
    "funny", "humor",
    "say", "speak", "aloud", "read aloud", "read out", "repeat",
    "dictate", "sing", "polish", "tweak",
    # состояние устройства и системы (см. русский блок выше)
    "monitor", "computer", "laptop", "device", "system", "disk", "storage",
    "memory", "ram", "cpu", "gpu", "battery", "charge", "camera",
    "microphone", "speaker", "sound", "network", "internet", "wifi", "wi-fi",
    "bluetooth", "printer", "window", "browser", "application", "program",
    "installed", "hardware", "desktop", "recycle bin", "reboot", "restart",
    "pause", "rewind", "track", "playlist",
    "help me", "set up", "connect", "switch", "click", "press", "copy",
    "paste", "cut", "save", "remember", "record", "look at", "cancel",
    "remove", "minimize", "maximize", "stop", "quieter", "louder", "continue",
    "playing", "scroll", "type", "enter", "select", "rename", "move", "drag",
    "play", "next", "previous", "brightness", "clipboard", "keyboard",
    "mouse", "headphones", "downloads", "trash", "task manager",
    "control panel", "settings", "wallpaper", "theme", "notification",
    "driver", "antivirus", "registry", "terminal", "console",
    "command prompt", "powershell",
)

#: предел длины простой реплики
SIMPLE_MAX_CHARS = 160

#: маркеры отказа, после которых повторный вызов бессмыслен
#: (политика или пользователь решили — не настаиваем; ru + en)
DENIAL_MARKERS: tuple[str, ...] = (
    "запрещён политикой",
    "отклонил",
    "denied by policy",
    "policy prohibits",
    "rejected by the user",
    "user declined",
)

#: откладывание работы или просьба разрешения: быстрый путь лёгкой модели
#: идёт без инструментов, поэтому такой «ответ» нельзя отдавать как финал —
#: запрос обязан уйти в основной цикл (ru + en, подстрока в нижнем регистре)
DEFERRAL_MARKERS: tuple[str, ...] = (
    # ru
    "делаем", "давай", "хочешь", "запустить", "запускать", "запущу",
    "скрипт", "код на", "библиотек", "понадобится", "подтверд",
    "разрешени", "сделать это", "начать",
    # en
    "do we", "should i", "shall i", "shall we", "want me", "let me",
    "run the", "script", "library", "confirm", "permission",
)

#: выдуманный отказ доступа к устройству. Это не результат инструмента и не
#: финальный ответ: у агента есть Python, консоль и управление компьютером.
#: Отказ к конкретному файлу/папке сюда не относится — это реальная ошибка.
_ACCESS_DENIAL_RE = re.compile(
    r"(?:"
    r"\b(?:нет|не\s+имею|не\s+имеет|без)\s+(?:физического\s+)?доступа\s+к\s+"
    r"(?:(?:тво|ваш|мо|этому|данному)\w*\s+)?"
    r"(?:устройств\w*|компьютер\w*|ноутбук\w*|экран\w*|монитор\w*|камер\w*|"
    r"микрофон\w*|динамик\w*|систем(?:а|ы|е|у|ой)\b|терминал\w*|диск\w*|"
    r"рабоч\w+\s+стол\w*|окн\w*|браузер\w*)"
    r"|"
    r"\b(?:не\s+могу|не\s+умею|невозможно)\s+"
    r"(?:получить\s+доступ|взаимодействовать|управлять|открывать|запускать|"
    r"просматривать|читать|изменять|нажимать|кликать)\b"
    r"[^.!?]{0,120}\b(?:устройств\w*|компьютер\w*|экран\w*|ноутбук\w*|"
    r"систем(?:а|ы|е|у|ой)\b|камер\w*|микрофон\w*|программ\w*|приложен\w*)\b"
    r"|"
    r"\b(?:i\s+(?:do\s+not|don't)\s+have\s+(?:the\s+ability\s+to\s+)?access\s+to|"
    r"i\s+(?:can't|cannot|am\s+unable\s+to)\s+access|"
    r"i\s+(?:can't|cannot)\s+interact\s+with)\s+"
    r"(?:your|the)\s+"
    r"(?:device|computer|laptop|screen|system|camera|microphone|files?|"
    r"apps?|applications?)"
    r"|"
    r"\bas\s+an?\s+(?:ai|language\s+model)\b"
    r")",
    re.IGNORECASE,
)


def looks_like_access_denial(text: str) -> bool:
    """Похоже ли сообщение на выдуманный отказ доступа к устройству.

    Модель иногда отвечает «у меня нет доступа к твоему компьютеру» вместо
    вызова инструментов. Такой текст нельзя отдавать пользователю как финал,
    иначе задача тихо умирает. Настоящие ошибки конкретных инструментов
    (например, «нет доступа к файлу отчёта») под этот шаблон не попадают.
    """
    low = " ".join((text or "").split()).lower()
    if not low or len(low) > 2000:
        return False
    return bool(_ACCESS_DENIAL_RE.search(low))


def reply_key(text: str) -> str:
    """Ключ для коротких реплик: без пунктуации и лишних пробелов."""
    key = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", key).strip()


def quick_reply(text: str) -> str | None:
    """Мгновенный ответ на прямое обращение, приветствие или спасибо."""
    return QUICK_REPLIES.get(reply_key(text))


def is_simple_request(text: str) -> bool:
    """Короткая светская реплика, которой не нужны инструменты и скиллы."""
    raw = (text or "").strip()
    if not raw or len(raw) > SIMPLE_MAX_CHARS or "\n" in raw:
        return False
    if raw.startswith("/"):
        return False
    low = raw.lower()
    return not any(marker in low for marker in SIMPLE_EXCLUDE)


#: как пользователи спрашивают о списке установленных скиллов (ru + en)
_SKILLS_QUESTION_MARKERS: tuple[str, ...] = (
    # ru
    "какие у тебя", "какие есть скилл", "какие скилл", "сколько скилл",
    "список скилл", "покажи скилл", "назови скилл", "перечисли скилл",
    "есть ли скилл", "какие скиллы", "какие скилы", "скиллы у тебя",
    # en
    "what skills", "which skills", "list skills", "show skills",
    "your skills", "skills do you have", "how many skills",
)


def looks_like_skills_question(text: str) -> bool:
    """Спрашивают ли, какие скиллы доступны.

    Такой вопрос нельзя отдавать быстрому пути: там нет списка скиллов,
    поэтому лёгкая модель отвечает общими словами и может оборваться по
    лимиту токенов. В Telegram на него сразу приходит список, в остальных
    интерфейсах — полный агентский промпт со списком.
    """
    low = " ".join((text or "").strip().lower().split())
    if not low or len(low) > 400:
        return False
    if low.strip(" ?!.") in {"скиллы", "скилы", "skills"}:
        return True
    return any(marker in low for marker in _SKILLS_QUESTION_MARKERS)


def looks_like_deferral(text: str) -> bool:
    """Похоже ли сообщение на откладывание работы или просьбу разрешения.

    Быстрый путь лёгкой модели выполняется без инструментов: если реплика
    всё же вышла «запущу скрипт, делаем?» или «сначала проверю доступ к
    библиотеке», её нельзя отдавать пользователю как финал — задача
    тихо умрёт без единого вызова инструмента.
    """
    low = (text or "").strip().lower()
    if not low or len(low) > 400:
        return False
    return any(marker in low for marker in DEFERRAL_MARKERS)


__all__ = [
    "REPLY_AUTO",
    "REPLY_CHOICES",
    "LANGUAGE_NAMES",
    "QUICK_REPLIES",
    "SIMPLE_EXCLUDE",
    "SIMPLE_MAX_CHARS",
    "DENIAL_MARKERS",
    "DEFERRAL_MARKERS",
    "looks_like_access_denial",
    "normalize",
    "detect_language",
    "language_name",
    "speech_language_code",
    "reply_language_directive",
    "anti_reasoning_directive",
    "hallucination_guard_directive",
    "reply_key",
    "quick_reply",
    "is_simple_request",
    "looks_like_skills_question",
    "looks_like_deferral",
]
