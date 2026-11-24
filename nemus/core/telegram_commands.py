"""Каталог команд Telegram-бота: описания, синтаксис и примеры.

Модуль не зависит от Telegram: возвращает текст и пары «подпись, callback».
Так каталог можно тестировать и переиспользовать в `/help`, меню и подсказках.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandInfo:
    """Одна команда в человеческом виде."""

    name: str
    category: str
    title: str
    syntax: str
    description: str
    example: str
    #: пары «аргумент — что он значит»; пусто, если аргументы не нужны
    arguments: tuple[tuple[str, str], ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandCategory:
    id: str
    title: str
    icon: str
    description: str


CATEGORIES: tuple[CommandCategory, ...] = (
    CommandCategory(
        "main",
        "Основное",
        "🏠",
        "Диалог, справка, настройки и состояние.",
    ),
    CommandCategory(
        "pc",
        "Компьютер и сайты",
        "💻",
        "Ресурсы ПК, диагностика и чтение страниц.",
    ),
    CommandCategory(
        "jobs",
        "Фоновые задачи",
        "⚙️",
        "Долгие команды, их вывод и остановка.",
    ),
    CommandCategory(
        "auto",
        "Автопилот и ремонт",
        "🤖",
        "Проактивные события и инциденты.",
    ),
)

COMMANDS: tuple[CommandInfo, ...] = (
    CommandInfo(
        "start",
        "main",
        "Начать работу",
        "/start",
        "Приветствие, примеры и кнопки быстрого доступа.",
        "/start",
    ),
    CommandInfo(
        "help",
        "main",
        "Помощь без зубрёжки",
        "/help [команда]",
        "Каталог команд по категориям. С именем команды показывает синтаксис и пример.",
        "/help read",
        arguments=(
            ("[команда]", "имя команды без слеша; без аргумента откроется каталог"),
        ),
        aliases=("commands", "меню"),
    ),
    CommandInfo(
        "skills",
        "main",
        "Скиллы и плагины",
        "/skills [имя]",
        "Список установленных скиллов, MCP-серверов и что делает конкретный скилл.",
        "/skills",
        arguments=(
            ("[имя]", "имя скилла или плагина; без него — список всех"),
        ),
    ),
    CommandInfo(
        "new",
        "main",
        "Новый диалог",
        "/new",
        "Очищает контекст текущего чата. Текущую задачу сначала нужно дождаться.",
        "/new",
    ),
    CommandInfo(
        "status",
        "main",
        "Состояние",
        "/status",
        "Модель, провайдер, время работы, текущая задача и размер диалога.",
        "/status",
    ),
    CommandInfo(
        "emotions",
        "main",
        "Характер и чувства",
        "/emotions",
        "Показывает черты характера, текущее настроение, обиду, доверие и скуку.",
        "/emotions",
    ),
    CommandInfo(
        "diary",
        "main",
        "Дневник",
        "/diary",
        "Присылает её личный дневник файлом, если она достаточно доверяет.",
        "/diary",
    ),
    CommandInfo(
        "todo",
        "main",
        "План задачи",
        "/todo",
        "Показывает план текущей или последней задачи и прогресс по пунктам.",
        "/todo",
    ),
    CommandInfo(
        "btw",
        "main",
        "Вопрос субагенту",
        "/btw <вопрос>",
        "Отвечает на вопрос по контексту чата, пока основная задача ещё выполняется.",
        "/btw что уже готово и что осталось?",
        arguments=(
            ("<вопрос>", "что спросить, не дожидаясь окончания основной задачи"),
        ),
    ),
    CommandInfo(
        "panel",
        "main",
        "Настройки",
        "/panel",
        "Открывает Telegram Mini App с настройками Немуси.",
        "/panel",
    ),
    CommandInfo(
        "id",
        "main",
        "Мой ID",
        "/id",
        "Показывает Telegram ID — он нужен для списка разрешённых пользователей.",
        "/id",
    ),
    CommandInfo(
        "stop",
        "main",
        "Остановить бота",
        "/stop",
        "Выключает Telegram-бота. Не путать с остановкой текущей задачи.",
        "/stop",
    ),
    CommandInfo(
        "cancel",
        "main",
        "Отменить текущую задачу",
        "/cancel",
        "Прерывает выполняющуюся задачу, не выключая самого бота.",
        "/cancel",
    ),
    CommandInfo(
        "pc",
        "pc",
        "Ресурсы компьютера",
        "/pc",
        "CPU, RAM, диски, батарея, аптайм и топ процессов по памяти.",
        "/pc",
    ),
    CommandInfo(
        "doctor",
        "pc",
        "Самодиагностика",
        "/doctor",
        "Проверяет Telegram, Web App, туннель, LLM, MCP, диск и последние ошибки.",
        "/doctor",
    ),
    CommandInfo(
        "read",
        "pc",
        "Прочитать сайт",
        "/read <url> [вопрос]",
        "Достаёт заголовок, структуру, основной текст и ссылки. С вопросом передаёт страницу агенту.",
        "/read https://example.com кратко перескажи",
        arguments=(
            ("<url>", "полный адрес страницы, начиная с http:// или https://"),
            ("[вопрос]", "что сделать с текстом; необязательно — без него будет краткий обзор"),
        ),
    ),
    CommandInfo(
        "run",
        "jobs",
        "Запустить команду в фоне",
        "/run <команда>",
        "Запускает долгую команду в рабочей папке и присылает уведомление о завершении.",
        "/run python -m http.server 8000",
        arguments=(
            ("<команда>", "любая команда терминала; пиши её целиком после /run"),
        ),
    ),
    CommandInfo(
        "jobs",
        "jobs",
        "Список фоновых задач",
        "/jobs",
        "Показывает запущенные и недавние команды, их статус и код возврата.",
        "/jobs",
    ),
    CommandInfo(
        "log",
        "jobs",
        "Вывод фоновой задачи",
        "/log <id> [строк]",
        "Показывает хвост вывода команды. По умолчанию 60 строк, максимум 200.",
        "/log 1 80",
        arguments=(
            ("<id>", "номер фоновой задачи из списка /jobs"),
            ("[строк]", "сколько последних строк показать; по умолчанию 60, максимум 200"),
        ),
    ),
    CommandInfo(
        "kill",
        "jobs",
        "Остановить фоновую задачу",
        "/kill <id>",
        "Завершает команду и её дочерние процессы.",
        "/kill 1",
        arguments=(
            ("<id>", "номер фоновой задачи из списка /jobs"),
        ),
    ),
    CommandInfo(
        "auto",
        "auto",
        "Проактивный автопилот",
        "/auto [on|off|rules|events|check]",
        "Без аргументов — статус. События: файлы, лог, расписание, диск, батарея; самостоятельность — по активности за ПК.",
        "/auto check",
        arguments=(
            ("[on|off]", "включить или выключить автопилот"),
            ("[rules]", "показать правила: файлы, лог, расписание, диск, батарея"),
            ("[events]", "последние сработавшие события"),
            ("[check]", "проверить условия прямо сейчас, не дожидаясь таймера"),
        ),
    ),
    CommandInfo(
        "heal",
        "auto",
        "Инциденты и self-healing",
        "/heal [show|apply|reject|rollback|scan] <id>",
        "Показывает инциденты, диагноз и diff; применяет или откатывает патч после проверки.",
        "/heal show 20261113-193552-1718",
        arguments=(
            ("[show]", "показать инцидент, диагноз и предложенный diff"),
            ("[apply]", "применить исправление после проверки и теста"),
            ("[reject]", "отклонить предложенное исправление"),
            ("[rollback]", "откатить уже применённое исправление"),
            ("[scan]", "проверить логи на новые ошибки"),
            ("<id>", "номер инцидента; нужен для show/apply/reject/rollback"),
        ),
    ),
)

_BY_NAME = {command.name: command for command in COMMANDS}
_CATEGORY_BY_ID = {category.id: category for category in CATEGORIES}


def find_command(name: str) -> CommandInfo | None:
    """Ищет команду по имени, псевдониму или с ведущим слешем."""
    token = (name or "").strip().lstrip("/").split(maxsplit=1)[0].lower()
    if not token:
        return None
    if token in _BY_NAME:
        return _BY_NAME[token]
    for command in COMMANDS:
        if token in {alias.lower() for alias in command.aliases}:
            return command
    return None


def category(category_id: str) -> CommandCategory | None:
    return _CATEGORY_BY_ID.get((category_id or "").strip().lower())


def category_commands(category_id: str) -> list[CommandInfo]:
    return [item for item in COMMANDS if item.category == category_id]


def search_commands(query: str, limit: int = 5) -> list[CommandInfo]:
    """Ищет команды по имени и словам описания; умеет предлагать похожие."""
    text = (query or "").strip().lstrip("/").lower()
    if not text:
        return []
    words = [word for word in text.replace("ё", "е").split() if len(word) >= 2]
    scored: list[tuple[float, CommandInfo]] = []
    for item in COMMANDS:
        haystack = " ".join(
            [
                item.name,
                item.title,
                item.description,
                item.syntax,
                " ".join(item.aliases),
            ]
        ).replace("ё", "е").lower()
        score = 0.0
        if text == item.name or text in item.aliases:
            score = 10.0
        elif text in item.name:
            score = 6.0
        elif text in haystack:
            score = 4.0
        else:
            score = sum(1.5 for word in words if word in haystack)
            score = max(
                score,
                difflib.SequenceMatcher(None, text, item.name).ratio() * 5.0,
            )
        if score >= 1.5:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [item for _, item in scored[:limit]]


def command_home_text() -> str:
    """Главный экран справки: не заставляем помнить синтаксис."""
    return (
        "**Команды без зубрёжки**\n\n"
        "Выбери категорию — покажу команды, что они делают, синтаксис и пример.\n"
        "Можно также написать `/help read` или просто спросить меня обычными "
        "словами: «прочитай этот сайт», «что с компьютером?», «запусти это в фоне».\n\n"
        "В карточках: `<...>` — обязательно, `[...]` — можно не указывать.\n\n"
        "**Категории**\n"
        "• 🏠 Основное — диалог и настройки\n"
        "• 💻 Компьютер и сайты — ресурсы, диагностика, чтение страниц\n"
        "• ⚙️ Фоновые задачи — запуск, лог, остановка\n"
        "• 🤖 Автопилот и ремонт — события и инциденты\n\n"
        "Быстрый доступ: `/todo` — план задачи, `/btw` — вопрос субагенту "
        "во время работы, `/skills` — скиллы, `/panel` — настройки, "
        "`/status` — состояние, `/id` — твой Telegram ID."
    )


def command_home_rows() -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for item in CATEGORIES:
        pair.append((f"{item.icon} {item.title}", f"ux:hc:{item.id}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("📋 Примеры задач", "ux:examples")])
    rows.append([("🧹 Очистить старые файлы", "ux:cleanup")])
    return rows


def command_category_text(category_id: str) -> str:
    item = category(category_id)
    if item is None:
        return "Не знаю такой категории."
    commands = category_commands(item.id)
    lines = [
        f"**{item.icon} {item.title}**",
        item.description,
        "",
    ]
    for command in commands:
        lines.append(f"• `{command.syntax}` — {command.title}")
    lines += ["", "Нажми на команду, чтобы увидеть пример."]
    return "\n".join(lines)


def command_category_rows(category_id: str) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for item in category_commands(category_id):
        pair.append((f"/{item.name}", f"ux:hx:{item.name}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("⬅️ Категории", "ux:h")])
    return rows


def command_detail_text(name: str) -> str:
    item = find_command(name)
    if item is None:
        guesses = search_commands(name)
        if guesses:
            names = ", ".join(f"/{guess.name}" for guess in guesses)
            return f"Не нашла команду. Похожие: {names}. Открой /help."
        return "Не нашла такую команду. Открой /help — там все команды по категориям."
    category_item = category(item.category)
    if item.arguments:
        arguments = (
            "`<...>` — обязательно, `[...]` — необязательно.\n"
            + "\n".join(
                f"• `{placeholder}` — {meaning}"
                for placeholder, meaning in item.arguments
            )
        )
    else:
        arguments = "не нужны — просто отправь команду без хвоста."
    return (
        f"**/{item.name} — {item.title}**\n\n"
        f"{item.description}\n\n"
        f"**Синтаксис:** `{item.syntax}`\n"
        f"**Аргументы:**\n{arguments}\n\n"
        f"**Пример:** `{item.example}`\n\n"
        f"Категория: {category_item.icon if category_item else ''} "
        f"{category_item.title if category_item else item.category}"
    )


__all__ = [
    "CATEGORIES",
    "COMMANDS",
    "CommandCategory",
    "CommandInfo",
    "category",
    "category_commands",
    "command_category_rows",
    "command_category_text",
    "command_detail_text",
    "command_home_rows",
    "command_home_text",
    "find_command",
    "search_commands",
]
