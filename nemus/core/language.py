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


def detect_language(text: str) -> str:
    """Грубо определяет язык текста: ``ru``/``en`` или ``""`` (неизвестно)."""
    if not text:
        return ""
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if cyrillic == 0 and latin == 0:
        return ""
    return "ru" if cyrillic >= latin else "en"


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


__all__ = [
    "REPLY_AUTO",
    "REPLY_CHOICES",
    "LANGUAGE_NAMES",
    "normalize",
    "detect_language",
    "language_name",
    "speech_language_code",
    "reply_language_directive",
    "anti_reasoning_directive",
]
