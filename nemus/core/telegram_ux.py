"""Тексты и inline-кнопки подсказок для Telegram-бота.

Модуль намеренно не знает про Telegram и агента: только чистые строки и
пары «подпись, callback-данные». Так проще тестировать формулировки и
переиспользовать их в разных обработчиках.
"""

from __future__ import annotations

import re

HINT_SHOWN_PREFIX = "telegram_hint_shown:"
HINT_MUTED_PREFIX = "telegram_hints_muted:"


def start_text(full_name: str, aliases: str, hints: bool) -> str:
    """Первый экран: что умеет бот и с чего начать."""
    lines = [
        f"**Привет! Я {full_name}** — можно просто {aliases}. 🚀",
        "",
        "> Живу на твоём компьютере: открою программы, найду файлы, "
        "сделаю видео или соберу отчёт — и пришлю результат прямо сюда.",
        "",
        "**Что можно присылать**",
        "• текст или голосовое с задачей;",
        "• файлы, фото и видео — разберусь по содержимому.",
    ]
    if hints:
        lines += [
            "",
            "**Примеры:**",
            "• «найди файл с отчётом за март и пришли»",
            "• «сделай из этого видео кружок»",
            "• «что на этом фото?»",
            "• «собери краткий отчёт по этому PDF»",
        ]
    lines += [
        "",
        "**Команды:** /new — новый диалог, /status — состояние, "
        "/id — мой ID, /help — помощь.",
    ]
    return "\n".join(lines)


def help_text(hints: bool) -> str:
    """Краткая справка; примеры и «фишка» только при включённых подсказках."""
    lines = [
        "**Что я умею**",
        "• открывать программы и искать файлы;",
        "• читать документы и собирать отчёты;",
        "• обрабатывать фото, видео и голосовые;",
        "• присылать результат прямо в этот чат.",
        "",
        "**Команды:** /new — новый диалог, /status — состояние, "
        "/id — мой ID, /help — помощь.",
    ]
    if hints:
        lines += [
            "",
            "**Примеры:**",
            "• «найди файл с отчётом за март и пришли»",
            "• «сделай из этого видео кружок»",
            "• «что на этом фото?»",
            "• «собери краткий отчёт по этому PDF»",
            "",
            "**Фишка:** пришли видео или файл без подписи — покажу кнопки, "
            "что с ним можно сделать.",
        ]
    return "\n".join(lines)


def examples_text() -> str:
    """Примеры задач для кнопки «Примеры задач»."""
    return (
        "**Примеры задач**\n\n"
        "• «найди файл с отчётом за март и пришли»\n"
        "• «сделай из этого видео кружок»\n"
        "• «что на этом фото?»\n"
        "• «собери краткий отчёт по этому PDF»\n"
        "• «распознай речь из голосового и пришли текстом»\n"
        "• «покажи, какие файлы я менял сегодня»\n\n"
        "Можно писать обычным текстом или присылать файлы — я разберусь."
    )


def main_menu_rows(hints: bool) -> list[list[tuple[str, str]]]:
    """Inline-кнопки под первым экраном и помощью."""
    if not hints:
        return []
    return [
        [
            ("📋 Примеры задач", "ux:examples"),
            ("🧰 Что я умею", "ux:help"),
        ],
        [
            ("🆕 Новый диалог", "ux:new"),
            ("ℹ️ Статус", "ux:status"),
        ],
    ]


def skills_list_text(
    entries: list[tuple[str, str, str]], mcp_servers: list[str]
) -> str:
    """Компактный список установленных скиллов и MCP-серверов.

    ``entries`` — ``(имя, плагин, kind)``; пустой плагин попадает в группу
    «Без плагина». Команды плагинов приходят уже с именем ``плагин:команда``.
    """
    groups: dict[str, list[str]] = {}
    for name, plugin, _kind in entries:
        group = (plugin or "").strip() or "Без плагина"
        groups.setdefault(group, []).append(name)

    lines = [f"**Установлено скиллов: {len(entries)}**", ""]
    if not entries:
        lines.append("Скиллы не установлены")
    else:
        ordered = sorted(
            (group for group in groups if group != "Без плагина"),
            key=str.lower,
        )
        if "Без плагина" in groups:
            ordered.append("Без плагина")
        for group in ordered:
            names = sorted(groups[group], key=str.lower)
            lines.append(f"**{group}**")
            lines.append(", ".join(f"/{name}" for name in names))
            lines.append("")
        lines.append("Подробнее: /skills <имя>")

    lines += [
        "",
        "**MCP-серверы**",
        ", ".join(mcp_servers) or "не подключены",
    ]
    return "\n".join(lines)


def skill_details_text(
    name: str, description: str, plugin: str, kind: str
) -> str:
    """Короткая справка по одному скиллу или команде плагина."""
    plugin_text = (plugin or "").strip() or "нет"
    description_text = (description or "").strip()[:300] or "Без описания."
    kind_text = "команда плагина" if kind == "command" else "скилл"
    return (
        f"**/{name}**\n\n"
        f"Плагин: {plugin_text}\n"
        f"Тип: {kind_text}\n\n"
        f"{description_text}\n\n"
        f"Как запустить: `/{name}`"
    )


def skill_not_found_text() -> str:
    """Ответ на `/skills <неизвестное имя>`."""
    return "Не нашёл такой скилл. Список: /skills"


def first_hint_text() -> str:
    """Короткая подсказка после первой успешной задачи."""
    return "💡 Я умею ещё работать с файлами и медиа. Показать примеры?"


def first_hint_rows() -> list[list[tuple[str, str]]]:
    """Кнопки под первой подсказкой."""
    return [
        [
            ("Показать примеры", "ux:examples"),
            ("Больше не показывать", "ux:mute"),
        ]
    ]


#: тип исключения → короткая причина без внутренностей Python/Telegram
_ERROR_REASONS = {
    "FileNotFoundError": "Файл не найден",
    "PermissionError": "Нет доступа к файлу",
    "IsADirectoryError": "Ожидался файл, а не папка",
    "TimeoutError": "Превышено время ожидания",
    "ConnectionError": "Нет соединения",
    "NetworkError": "Нет соединения с Telegram",
    "BadRequest": "Telegram не принял запрос",
    "OSError": "Ошибка доступа к файлу",
}

_ERROR_PREFIX = re.compile(
    r"^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*):\s*(.*)$"
)


def _redact_paths(text: str) -> str:
    """Убирает абсолютные пути из причины ошибки."""
    text = re.sub(r"[A-Za-z]:\\[^\s'\"]+", "…", text)
    text = re.sub(r"(?<![\w:])/(?:[^\s/'\"]+/)+[^\s/'\"]+", "…", text)
    return text


def error_text(error: str) -> str:
    """Короткая человеческая причина без стектрейса и путей."""
    lines = [
        line.strip()
        for line in (error or "").splitlines()
        if line.strip()
    ]
    lines = [
        line
        for line in lines
        if not line.startswith("Traceback")
        and not re.match(r'^File ".*", line \d+', line)
    ]
    reason = lines[-1] if lines else "Неизвестная ошибка"
    match = _ERROR_PREFIX.match(reason)
    if match:
        kind, detail = match.group(1), match.group(2).strip()
        reason = _ERROR_REASONS.get(kind, detail or "Ошибка выполнения")
    reason = _redact_paths(reason)
    reason = re.sub(r"\s+", " ", reason).strip()
    reason = reason[:300] or "Неизвестная ошибка"
    return (
        "Не получилось выполнить задачу.\n\n"
        f"{reason}\n\n"
        "Попробуй повторить или сформулируй проще."
    )


def error_rows(can_retry: bool) -> list[list[tuple[str, str]]]:
    """Кнопки восстановления после ошибки."""
    row: list[tuple[str, str]] = []
    if can_retry:
        row.append(("🔁 Повторить", "ux:retry"))
    row.extend(
        [
            ("🆕 Новый диалог", "ux:new"),
            ("❓ Помощь", "ux:help"),
        ]
    )
    return [row]


#: действие → (подпись кнопки, промпт для агента)
_ATTACHMENT_ACTIONS: dict[str, tuple[str, str]] = {
    "photo_describe": ("📝 Описать", "Опиши, что на этом изображении."),
    "photo_ocr": (
        "🔎 Распознать текст",
        "Распознай текст на изображении и пришли его текстом.",
    ),
    "photo_what": ("❓ Что на фото?", "Что на этом фото? Опиши главное."),
    "video_note": ("⭕ Сделать кружок", "Сделай из этого видео кружок."),
    "video_audio": (
        "🔊 Извлечь звук",
        "Извлеки звук из этого видео и пришли аудиофайлом.",
    ),
    "video_trim": ("✂️ Обрезать", "Обрежь это видео до 30 секунд."),
    "audio_transcribe": (
        "📝 Распознать речь",
        "Распознай речь в этом аудио и пришли текст.",
    ),
    "audio_what": (
        "❓ Что здесь?",
        "Кратко опиши, что это за аудио, и распознай речь, если можешь.",
    ),
    "document_read": ("📖 Прочитать", "Прочитай этот файл и кратко перескажи."),
    "document_report": ("📄 Сделать отчёт", "Сделай краткий отчёт по этому файлу."),
    "document_send": (
        "📤 Отправить обратно",
        "Обработай этот файл и отправь результат обратно.",
    ),
}

_ATTACHMENT_ROWS: dict[str, list[list[str]]] = {
    "photo": [["photo_describe", "photo_ocr"], ["photo_what"]],
    "video": [["video_note", "video_audio"], ["video_trim"]],
    "audio": [["audio_transcribe", "audio_what"]],
    "document": [["document_read", "document_report"], ["document_send"]],
}


def attachment_action_rows(kind: str) -> list[list[tuple[str, str]]]:
    """Кнопки быстрых действий для одного вложения."""
    rows: list[list[tuple[str, str]]] = []
    for row in _ATTACHMENT_ROWS.get((kind or "").lower(), []):
        rows.append(
            [
                (_ATTACHMENT_ACTIONS[key][0], f"ux:attach:{key}")
                for key in row
            ]
        )
    return rows


def attachment_prompt(action: str) -> str | None:
    """Промпт для действия или None, если действие неизвестно."""
    item = _ATTACHMENT_ACTIONS.get(action)
    return item[1] if item else None
