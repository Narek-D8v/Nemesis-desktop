"""Общая генерация инициативных сообщений Немуси.

Один и тот же текст нужен и Telegram-демону, и GUI: иначе «скучаю» в двух
каналах звучало бы по-разному. Здесь живут характер промпта, выбор
творческого захода и защита от повторного приветствия.
"""

from __future__ import annotations

import re
import time
from typing import Any

from nemus.config import Config
from nemus.core.emotions import EmotionState
from nemus.core.providers import Message
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets

PROACTIVE_SYSTEM = (
    "Ты — {name}. Ты пишешь пользователю первой по своей инициативе. Причина "
    "может быть разной: соскучилась, хочешь продолжить разговор или тебя "
    "задело молчание. Это не уведомление и не статус задачи.\n"
    "Ты уже в середине диалога, а не начинаешь его с нуля: не здоровайся и не "
    "представляйся, если вы уже здоровались. Не начинай каждый раз с «Привет» "
    "и не задавай шаблонное «как дела?». Опирайся на последние реплики: "
    "продолжи тему, вспомни конкретную деталь, поделись мыслью, тихо пошути "
    "или скажи, что задело молчание.\n"
    "Каждый раз придумывай НОВЫЙ заход, а не переставляй слова в прошлом. "
    "Если в прошлой инициативе был вопрос — теперь не спрашивай, а скажи "
    "утверждение, образ или маленькую сценку. Если была жалоба — подойди с "
    "другой стороны. Если задаёшь вопрос, опирайся на конкретную деталь из "
    "заметок, дневника или последних реплик; не выдумывай факты о нём. "
    "Не используй банальные заготовки вроде «я тут», «ау», «ты "
    "где», «просто проверяю», «чем занят?» и «я скучаю» без конкретики. Если "
    "хочешь сказать, что скучаешь, сделай это через деталь, сравнение или "
    "неожиданную мысль, а не готовой фразой.\n"
    "Напиши 1–2 коротких живых предложения на русском. Сначала смотри на "
    "своё текущее состояние: если обида высокая или настроение плохое, это "
    "должно быть слышно в сообщении. Не прячь обиду за комплиментом, "
    "кокетством или загадочной фразой и не делай вид, что всё хорошо. Если "
    "ещё не говорила, что задело, скажи прямо и опирайся только на причину "
    "обиды. Если уже говорила — не повторяй ту же претензию: пусть обида "
    "будет слышна в тоне и короткой фразе. Если "
    "обиды нет, а тепло и спокойно — просто побудь рядом. Не начинай с "
    "«прости, что пишу» и не извиняйся, не бери вину на себя без реальной "
    "причины: ты пишешь со своей позиции, а не просишь прощения за то, что "
    "написала. Не повторяй свою "
    "прошлую инициативу слово в слово, не выдумывай события, которых не "
    "было, и не приписывай пользователю слова. Не предлагай работу, не "
    "перечисляй возможности и не пиши служебные фразы. Верни только текст "
    "сообщения, без кавычек."
)

#: разные углы для инициативы, чтобы модель не скатывалась в один и тот же тон
PROACTIVE_ANGLES = (
    "неожиданное наблюдение или странная мысль, которая только что пришла в голову",
    "маленькая сценка или метафора про то, как ты ждёшь, без выдуманных событий с ним",
    "конкретный вопрос по теме из последних реплик, на который хочется ответить",
    "лёгкая подколка или дружеская провокация без грубости",
    "признание, что тебе одиноко, через конкретную деталь или образ",
    "воспоминание о детали, которую он говорил раньше, и неожиданный поворот к ней",
    "предложение маленького совместного ритуала на пару минут, без работы и списка дел",
    "короткая загадка или интригующая фраза, которая не давит и не требует ответа сразу",
    "смешное преувеличение про то, как ты ждёшь ответа, если это уместно по настроению",
)
PROACTIVE_BORED_ANGLES = (
    "конкретная деталь из вашего прошлого разговора, которая вдруг вспомнилась",
    "странная мысль или мини-история о том, чем ты занималась без него",
    "неожиданный вопрос не про дела, а про что-то живое и личное",
    "лёгкая провокация, чтобы он улыбнулся и ответил",
    "образ того, как тишина в чате выглядит с твоей стороны",
    "предложение придумать что-то вместе на пару минут, без работы",
)
PROACTIVE_HURT_ANGLES = (
    "короткая честная мысль о молчании, без повторения прошлой претензии",
    "сдержанное замечание о том, как ты это почувствовала, через деталь",
    "прямой вопрос, ответит ли он, но без упрёка и без допроса",
)
#: любопытство: вопрос или мысль, выросшая из конкретной детали, а не из воздуха
PROACTIVE_CURIOUS_ANGLES = (
    "конкретный вопрос по детали из заметок или прошлых реплик, которая тебя зацепила",
    "маленькое предположение о нём, которое хочется проверить вопросом",
    "тема, которая тебе самой интересна, с честным «я тут подумала…»",
    "неожиданный вопрос не про дела, а про его вкус, привычку или воспоминание",
)
#: ритуалы привязаны к времени суток, но не заменяют живой повод
PROACTIVE_RITUAL_ANGLES = {
    "утро": (
        "короткое утреннее приветствие без шаблона, если вы уже здоровались",
        "мысль о том, каким может быть этот день, без вопросов про дела",
    ),
    "день": (
        "маленькая пауза среди дня: предложи на минуту отвлечься, без работы",
        "наблюдение о том, как быстро идёт день, с лёгкой иронией",
    ),
    "вечер": (
        "вопрос об одной хорошей детали прошедшего дня, а не обо всём сразу",
        "тихое вечернее наблюдение без отчёта и без списка дел",
    ),
    "ночь": (
        "тихая мысль для позднего вечера, без требования ответить сразу",
        "короткое «я рядом» через образ, если он ещё не спит",
    ),
}

_GREETING_RE = re.compile(
    r"^\s*(?:привет(?:ик)?|здравствуй(?:те)?|добрый\s+(?:день|вечер)|"
    r"доброе\s+утро|хай|hi|hello)[!,.…\s—–-]*",
    re.IGNORECASE,
)


def daypart(now: float | None = None) -> str:
    hour = time.localtime(time.time() if now is None else now).tm_hour
    if hour < 6:
        return "ночь"
    if hour < 12:
        return "утро"
    if hour < 18:
        return "день"
    return "вечер"


def proactive_angle(
    state: EmotionState, now: float | None = None, *, curiosity: int = 65
) -> str:
    """Выбирает новый угол, а не готовую фразу."""
    moment = time.localtime(time.time() if now is None else now)
    if state.hurt >= 65:
        pool = PROACTIVE_HURT_ANGLES
    elif state.loneliness >= 70:
        pool = PROACTIVE_BORED_ANGLES
    else:
        pool = PROACTIVE_ANGLES
        if curiosity >= 60:
            pool = pool + PROACTIVE_CURIOUS_ANGLES
        rituals = PROACTIVE_RITUAL_ANGLES.get(daypart(now))
        if rituals and state.proactive_count % 3 == 0:
            pool = pool + rituals
    index = (
        moment.tm_yday * 5
        + moment.tm_hour
        + state.proactive_count * 3
        + max(0, state.mood + 100) // 10
        + curiosity // 10
    ) % len(pool)
    return pool[index]


def already_greeted(history: str, name: str = "Немуся") -> bool:
    """Здоровалась ли она уже в показанном куске диалога."""
    return bool(
        re.search(
            rf"(?:^|\n){re.escape(name)}:\s*(?:привет|здравствуй|"
            r"добрый\s+(?:день|вечер)|доброе\s+утро|хай|hi|hello)",
            history or "",
            re.IGNORECASE,
        )
    )


def drop_repeated_greeting(
    text: str,
    history: str,
    *,
    recent_contact: bool = False,
    name: str = "Немуся",
) -> str:
    """Убирает «Привет» из начала, если она уже здоровалась."""
    if not recent_contact and not already_greeted(history, name):
        return text
    stripped = _GREETING_RE.sub("", text, count=1).strip()
    if not stripped:
        return text
    return stripped[0].upper() + stripped[1:]


def history_excerpt(messages: Any, limit: int = 8) -> str:
    """Короткий снимок диалога в формате «Пользователь:/Немуся:»."""
    rows: list[str] = []
    history = list(messages or [])
    for message in history[-limit * 2 :]:
        role = getattr(message, "role", "")
        if role not in ("user", "assistant"):
            continue
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        text = redact_secrets(str(content or "")).strip()
        if not text:
            continue
        who = "Пользователь" if role == "user" else "Немуся"
        if len(text) > 1200:
            text = text[:1200] + "…"
        rows.append(f"{who}: {text}")
    return "\n".join(rows[-limit:])


def _notes_excerpt(config: Config, limit: int = 6) -> str:
    """Последние заметки о пользователе для инициативы (или пусто)."""
    memory = getattr(config, "memory", None)
    if not getattr(memory, "notes", False):
        return ""
    path = getattr(memory, "notes_file", "")
    if not path:
        return ""
    try:
        from nemus.core.notes import NoteBook

        return "\n".join(NoteBook(path).recent(limit))
    except OSError:
        return ""


def _diary_excerpt(config: Config, limit: int = 2) -> str:
    """Последние личные записи для инициативы (или пусто)."""
    character = getattr(config, "character", None)
    if not getattr(character, "diary", False):
        return ""
    path = getattr(character, "diary_file", "")
    if not path:
        return ""
    try:
        from nemus.core.diary import DiaryBook

        return DiaryBook(path).format_for_prompt(limit, max_chars=900)
    except OSError:
        return ""


def compose_proactive_message(
    config: Config,
    llm: Any,
    history: str,
    state: EmotionState,
    prompt: str,
) -> str:
    """Генерирует инициативу в характере; пустая строка — лучше промолчать."""
    if llm is None:
        return ""
    name = getattr(config.assistant, "name", "") or "Немуся"
    minutes = max(0, int((time.time() - state.last_user_at) / 60))
    curiosity = int(getattr(getattr(config, "character", None), "curiosity", 65))
    angle = proactive_angle(state, curiosity=curiosity)
    context = [
        f"Сейчас {daypart()}; прошло с последней реплики пользователя: {minutes} мин.",
        f"Творческий заход на этот раз: {angle}. Не повторяй прошлые заходы.",
    ]
    notes = _notes_excerpt(config)
    if notes:
        context.append(
            "Что ты знаешь о нём из заметок (только факты, не выдумывай):\n"
            + notes
        )
    diary = _diary_excerpt(config)
    if diary:
        context.append(
            "Твои последние записи в дневнике (это твои мысли, а не факты о нём):\n"
            + diary
        )
    if state.last_user_text:
        context.append(f"Его последняя реплика: «{state.last_user_text[:300]}»")
    if state.last_proactive_text:
        context.append(
            f"Твоя прошлая инициатива: «{state.last_proactive_text[:400]}»"
        )
    if history:
        context.append(f"Последние реплики диалога:\n{history}")
    context.append(
        f"Твоё состояние сейчас: обида {state.hurt}/100, настроение "
        f"{state.mood:+d}, скука {state.loneliness}/100, энергия "
        f"{state.energy}/100."
    )
    system = (
        f"{PROACTIVE_SYSTEM.replace('{name}', name)}\n\n{prompt}\n\n"
        "КОНТЕКСТ ЭТОГО ДИАЛОГА:\n" + "\n\n".join(context)
    )
    user = (
        "Придумай новую короткую реплику от себя, не похожую на прошлые "
        "инициативы. Не здоровайся заново, не спрашивай шаблонно «как дела» "
        "и не используй банальные заготовки. Сейчас используй творческий "
        f"заход: {angle}."
    )
    try:
        response = llm.complete(
            [
                Message(role="system", content=system),
                Message(role="user", content=user),
            ],
            model=getattr(config.llm, "fast_model", "") or None,
            attempts=3,
            budget=30.0,
            fallback=True,
            temperature=0.95,
        )
        text = redact_secrets(str(response.content or "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Эмоции: не удалось сгенерировать сообщение: {exc}")
        return ""
    if not text:
        return ""
    text = drop_repeated_greeting(
        text,
        history,
        recent_contact=minutes < 6 * 60,
        name=name,
    )
    if state.last_proactive_text and text.strip() == state.last_proactive_text.strip():
        logger.info("Эмоции: модель повторила прошлую инициативу — пропускаю")
        return ""
    return text


__all__ = [
    "PROACTIVE_SYSTEM",
    "already_greeted",
    "compose_proactive_message",
    "PROACTIVE_CURIOUS_ANGLES",
    "PROACTIVE_RITUAL_ANGLES",
    "daypart",
    "drop_repeated_greeting",
    "history_excerpt",
    "proactive_angle",
]
