"""Инструменты Telegram: отправка файлов и реакции на сообщения.

Доступны только когда бот запущен: инструменты находят активный экземпляр
:class:`nemus.core.telegram_bot.TelegramBot` и работают с чатом, из которого
пришла текущая задача. Видео-кружок готовится автоматически: ffmpeg обрезает
ролик до квадрата 512×512 и минуты.
"""

from __future__ import annotations

from typing import Any

from nemus.core.tools.base import RiskLevel, Tool, ToolResult

#: эмодзи, которые Telegram принимает как реакции (список пользователя)
REACTION_EMOJIS: tuple[str, ...] = (
    "🤣", "😁", "🤩", "😍", "🥰", "🙂", "😐", "🤔", "🥱", "😴",
    "😢", "😭", "😱", "🤬", "😡", "🤮", "🤯", "🤪", "🥴", "😇",
    "🤓", "🧐", "🤨", "🫡", "🤗", "😎", "😘", "😨", "❤️", "💔",
    "❤️‍🔥", "💘", "👍", "👎", "👌", "🤝", "👏", "🙏", "🖕", "✍",
    "💅", "🤷", "🔥", "🎉", "💯", "🏆", "🍾", "👀", "🤡", "💩",
    "🗿", "🕊", "💊", "🎃", "🎄", "🎅", "☃", "🍓", "🍌", "🌭",
    "🌚", "😈", "👻", "🦄", "🐳", "⚡", "🆒", "👨‍💻",
)


def normalize_reaction(emoji: str) -> str:
    """Убирает вариационный селектор: Telegram ждёт ❤, а не ❤️.

    Составные эмодзи с ZWJ (❤️‍🔥, 👨‍💻) не трогаем: у них селектор —
    часть последовательности, и Telegram принимает её целиком.
    """
    text = (emoji or "").strip()
    if "\u200d" in text:
        return text
    return text.replace("\ufe0f", "")


#: сравнение без вариационных селекторов
_ALLOWED_REACTIONS = {normalize_reaction(emoji) for emoji in REACTION_EMOJIS}


class SendTelegramTool(Tool):
    """Присылает файл в Telegram: документ, фото, видео или видео-кружок."""

    name = "send_telegram"
    description = (
        "Отправляет файл в Telegram-чат, из которого пришла задача. Используй, "
        "когда пользователь просит прислать результат, файл, картинку или "
        "видео, а также чтобы сделать из видео круг (as=\"video_note\"). "
        "Работает только во время задачи из Telegram."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Путь к файлу на диске (абсолютный или относительно "
                    "рабочей папки). Файл должен существовать."
                ),
            },
            "caption": {
                "type": "string",
                "description": "Подпись к файлу, до 1024 символов.",
            },
            "as": {
                "type": "string",
                "enum": [
                    "auto",
                    "document",
                    "photo",
                    "video",
                    "audio",
                    "video_note",
                ],
                "description": (
                    "Как отправить: auto — по расширению, video_note — "
                    "видео-кружок (сам обрежет до квадрата и минуты)."
                ),
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.SAFE

    def execute(self, **kwargs: Any) -> ToolResult:
        from nemus.core.telegram_bot import active_bot

        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult.failure("Не указан путь к файлу (path).")
        bot = active_bot()
        if bot is None or not bot.running:
            return ToolResult.failure(
                "Telegram-бот не запущен — отправить файл в чат нельзя."
            )
        kind = str(kwargs.get("as") or "auto").strip().lower()
        caption = str(kwargs.get("caption") or "").strip()
        ok, message = bot.send_file(path, kind=kind, caption=caption)
        return ToolResult.success(message) if ok else ToolResult.failure(message)


class ReactTelegramTool(Tool):
    """Ставит выбранную реакцию на сообщение пользователя в Telegram."""

    name = "react_telegram"
    description = (
        "Ставит реакцию на сообщение пользователя, из которого пришла текущая "
        "задача. Выбирай эмодзи по смыслу сообщения, а не как статус: анекдот "
        "или шутка — 🤣 или 😁, важное — 🔥, поддержка — ❤️, согласие — 👍. "
        "Не ставь 👀, ✅ или ❌ как «приняла/работаю/готово» — для статуса есть "
        "«печатает…» и прогресс. Не спрашивай разрешения и не ставь реакцию "
        "на каждое сообщение: только когда она уместна. "
        "Работает только во время задачи из Telegram. "
        f"Доступные эмодзи: {' '.join(REACTION_EMOJIS)}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "enum": list(REACTION_EMOJIS),
                "description": "Эмодзи реакции, выбранный по смыслу.",
            },
        },
        "required": ["emoji"],
    }
    risk = RiskLevel.SAFE

    def execute(self, **kwargs: Any) -> ToolResult:
        from nemus.core.telegram_bot import active_bot

        raw = str(kwargs.get("emoji") or "")
        emoji = normalize_reaction(raw)
        if not emoji:
            return ToolResult.failure("Не выбран эмодзи реакции (emoji).")
        if emoji not in _ALLOWED_REACTIONS:
            return ToolResult.failure(
                f"Эмодзи «{raw}» недоступен для реакций Telegram. "
                "Выбери из списка в описании инструмента."
            )
        bot = active_bot()
        if bot is None or not bot.running:
            return ToolResult.failure(
                "Telegram-бот не запущен — поставить реакцию нельзя."
            )
        ok, message = bot.react_current(emoji)
        return ToolResult.success(message) if ok else ToolResult.failure(message)


def telegram_tools() -> list[Tool]:
    """Инструменты Telegram для реестра (только при запущенном боте)."""
    return [SendTelegramTool(), ReactTelegramTool()]


__all__ = [
    "REACTION_EMOJIS",
    "ReactTelegramTool",
    "SendTelegramTool",
    "normalize_reaction",
    "telegram_tools",
]
