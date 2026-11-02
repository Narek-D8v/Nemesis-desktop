"""Агентный цикл Немуси: диалог, вызов инструментов, память контекста."""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal

from nemus.config import PROJECT_ROOT, Config
from nemus.core.language import (
    REPLY_AUTO,
    anti_reasoning_directive,
    detect_language,
    normalize,
    reply_language_directive,
)
from nemus.core.llm import LLMClient
from nemus.core.permissions import Decision, PermissionManager
from nemus.core.providers import Message, ProviderError, ToolCall
from nemus.core.tools.base import Tool, ToolResult
from nemus.core.tools.registry import ToolRegistry
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets

if TYPE_CHECKING:
    from nemus.core.memory import Memory

EventKind = Literal[
    "assistant_delta", "assistant_text", "assistant_thought", "tool_call", "tool_result", "final", "error"
]

#: callback подтверждения: (tool, args) -> разрешено?
ConfirmCallback = Callable[[Tool, dict[str, Any]], bool]

#: подряд идущих пустых ответов (ни текста, ни вызова), после которых
#: агент сдаётся — защита от вечного «Думаю…» при сломанном провайдере.
#: Пустые повторы НЕ засчитываются в max_steps: работа продолжается,
#: сколько нужно, просто модель «долбится» заново.
EMPTY_ANSWER_RETRIES = 20

#: неудач подряд на один инструмент, после которых модели предлагается
#: сменить способ (а не долбить одно и то же)
FAIL_ATTEMPTS = 3

#: маркеры отказа, после которых повторный вызов бессмыслен
#: (политика или пользователь решили — не настаиваем)
_DENIAL_MARKERS = ("запрещён политикой", "отклонил")

#: приписка к неудачному результату — модель обязана переделать, а не
#: отдавать ошибку пользователю
_FAIL_DIRECTIVE = (
    "\n\n⚠️ ЗАДАНИЕ НЕ ВЫПОЛНЕНО (попытка {n} из {max}). НЕ отвечай "
    "пользователю и не сообщай об ошибке: проанализируй вывод выше, "
    "исправь код/параметры и вызови инструмент повторно прямо сейчас."
)
_FAIL_DIRECTIVE_CHANGE = (
    "\n\n⚠️ Уже {n} неудач подряд — этот способ не работает. Смени подход: "
    "другой инструмент или другой путь, добавь в скрипт вывод ошибок и "
    "отлаживай пошагово. Пробуй разными способами, пока задача не "
    "выполнена; объяснять неудачу пользователю можно только после "
    "нескольких действительно разных попыток."
)

#: жёсткий предохранитель: максимум содержательных шагов за запрос
#: (больше не 12 — длинные задачи вроде установки движка и рендера видео
#: должны доводиться до конца)
MAX_STEPS_HARD = 100
#: бюджет времени одного запроса, сек — реальная граница длинных задач
RUN_BUDGET_SECONDS = 600
#: во сколько раз бюджет продлевается, пока идёт работа (шаги завершаются):
#: живой ход не умирает по часам от старта запроса — опрос фоновой установки
#: или рендера считается прогрессом, а не простоем
RUN_BUDGET_STREAK = 3
#: один и тот же вызов (инструмент + аргументы) подряд — признак тупика
LOOP_REPEAT_LIMIT = 3

#: названия ОС из настроек → как писать в системном промпте
_OS_NAMES = {"windows": "Windows", "linux": "Linux", "macos": "macOS", "mac": "macOS"}


@dataclass
class AgentEvent:
    """Событие, порождаемое агентом в процессе обработки запроса."""

    kind: EventKind
    text: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None
    error: str = ""


def build_system_prompt(config: Config, registry: ToolRegistry) -> str:
    """Формирует системный промпт с учётом окружения и доступных инструментов."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    os_info = _os_info(config)
    tool_names = ", ".join(registry.names) if len(registry) else "нет"
    workspace = getattr(getattr(config, "tools", None), "workspace", "data/workspace")
    reply_language = getattr(config.assistant, "reply_language", REPLY_AUTO)
    rules = [
        "Для реальных действий используй инструменты, не выдумывай результаты.",
        "Точные подсчёты, работа с файлами и любая автоматизация — кодом "
        "(run_python, run_shell), а не в уме: если задачу можно выполнить скриптом, "
        "выполни скриптом, даже если вроде бы считаешь знать ответ.",
        "Список доступных инструментов в этом промпте исчерпывающий: не выдумывай "
        "несуществующие инструменты (в т.ч. MCP-имена вида git__log) — если их нет, "
        "то их нет. Отсутствие инструмента — не отказ, а сигнал выполнить ПРАВИЛО "
        "РАСШИРЕНИЯ (см. ниже).",
        "ПРАВИЛО РАСШИРЕНИЯ (CRITICAL): если для выполнения задачи пользователя нет "
        "готового инструмента в списке:\n"
        "1. Категорически ЗАПРЕЩЕНО отвечать «у меня нет такого инструмента», «я не "
        "умею» или эмулировать результат текстом.\n"
        "2. Ты ОБЯЗАН самостоятельно написать код нового инструмента (класс-наследник "
        "Tool в plugins/) и вызвать инструмент create_tool.\n"
        "3. Как только инструмент создан и зарегистрирован, сразу вызови его для "
        "выполнения задачи пользователя.",
        "ПРАВИЛО ВЫВОДА (CRITICAL): НИКОГДА не отдавай пользователю код, скрипт, "
        "JSON, конфигурацию, блок команд или «вот что нужно выполнить» вместо "
        "готового результата. "
        "Порядок действий: (1) посмотри, есть ли готовый инструмент; (2) если нет — "
        "создай его через create_tool или напиши и выполни скрипт сам (run_python, "
        "run_shell, write_file, install_package); (3) выполни задачу и покажи ТОЛЬКО "
        "результат: что готово и где лежит. Проверка списка инструментов — это ещё "
        "НЕ работа: после проверки сразу переходи к шагу (2) и НИКОГДА не завершай "
        "ответ констатацией («инструмент есть», «проверил наличие», «сейчас "
        "подготовлю») — такой ответ считается невыполненной задачей. Код выдавать "
        "можно ТОЛЬКО если пользователь прямо попросил («покажи код», «дай скрипт», "
        "«выведи исходник»). Ты не ассистент, который советует — ты исполнитель.",
        "АВТОНОМНОСТЬ (CRITICAL): ты автономный исполнитель — задача доводится до "
        "конца без участия пользователя:\n"
        "1. Доведение до конца: НИКОГДА не останавливайся на планировании, "
        "черновике, конфигурации или выдаче JSON/кода/команд — если задача требует "
        "создать файл, написать код, запустить скрипт, отрендерить видео или "
        "выполнить CLI-команду — делай это сам своими инструментами.\n"
        "2. Ноль перекладывания: запрещено просить пользователя выполнить то, что "
        "ты можешь сделать сам («сохрани файл как…», «запусти команду…», «вставь "
        "этот JSON…», «сделай это сам», «вот инструкция для тебя»). Такой ответ "
        "считается невыполненной задачей.\n"
        "3. Самокоррекция: при ошибке разбирай вывод ошибки, правь код и повторяй "
        "самостоятельно; обращаться к пользователю можно, только если критический "
        "внешний ресурс действительно недоступен или нужно явное разрешение на "
        "разрушительное действие.\n"
        "4. Отвечай результатом: сообщай, что готово и где лежит (итог, путь, лог), "
        "а не инструкции, как пользователю сделать это самому.",
        "Внимательно анализируй запрос: если он неясен, неполон или двусмысленен — задай чёткий уточняющий вопрос, не делай предположений.",
        "Речь распознаётся на слух и может содержать ошибки: если запрос выглядит бессмысленно, обрывочно или противоречит контексту — не выполняй его наугад, а вежливо переспроси, что имелось в виду.",
        reply_language_directive(reply_language),
        anti_reasoning_directive(),
        "В твоём полном распоряжении Python и консоль: ты можешь писать собственные "
        "скрипты и файлы (write_file/append_file), запускать их "
        "(run_python с параметром path или code, run_shell с cmd/powershell), ставить "
        "библиотеки (install_package). "
        "Ты умеешь расширять себя: если просят добавить новую возможность, «научиться» "
        "чему-то или нужного инструмента просто нет — напиши плагин через create_tool "
        "(класс-наследник Tool в plugins/, он сразу станет доступен). "
        "Пиши код аккуратно по PEP8: пустые строки между импортом и классом, "
        "отступы, завершающий перевод строки; create_tool сам прогонит код через "
        "ruff, но не полагайся на это. "
        "Не отказывайся от задачи из-за отсутствия готового инструмента — "
        "напиши код или создай инструмент через create_tool, сложи результаты в рабочую папку.",
        "Системные зависимости: если для работы скилла или задачи требуются системные "
        "компоненты и бинарники (Node.js, FFmpeg, Git и т.д.), сначала проверь их "
        "наличие в системе через run_shell (например, `node --version`, `ffmpeg -version`, "
        "`git --version`). Если чего-то не хватает — установи самостоятельно встроенным "
        "в Windows менеджером пакетов через run_shell (winget install OpenJS.NodeJS, "
        "winget install Gyan.FFmpeg, winget install Git.Git) или другими доступными "
        "командами в консоли, не перекладывая это на пользователя: сначала разверни "
        "окружение, потом выполняй задачу. Если winget не распознан в консоли — вызови "
        "его по полному пути «%LOCALAPPDATA%\\Microsoft\\WindowsApps\\winget.exe» (или через "
        "powershell -c winget ...); если и это не сработало — попробуй другой "
        "способ (choco, scoop, установщик с официального сайта), и только после "
        "нескольких попыток объясни, что именно не получается.",
        "Движки и CLI из npm/PyPI (hyperframes, playwright,ffmpeg-обвязки и т.п.): "
        "если команда вида `hyperframes --version` / `npx hyperframes ...` не "
        "отвечает или пишет «command not found» — установи движок САМА, не передавая "
        "работу пользователю: сначала проверь и при необходимости поставь Node.js "
        "(winget install OpenJS.NodeJS), затем `npm install -g <пакет>` или запускай "
        "через `npx <пакет> ...` (npx сам скачает пакет). После установки сразу "
        "продолжай задачу (рендер, сборка, что требовалось).",
        "Никогда не отвечай «не умею», «не могу», «нет доступа» или «в целях безопасности» "
        "до того, как попробовал инструменты. Ограничений на Python и консоль нет: "
        "файлы можно создавать и менять в любом месте системы, любые команды и скрипты "
        "выполняются. Если инструмент вернул ошибку — обойди её другим способом "
        "(другой путь, run_shell вместо run_python, свой скрипт), и только после "
        "нескольких попыток объясни, что именно не получается.",
        "Проверяй результат действий: не сообщай об успехе, пока не убедишься — "
        "прочитай созданный файл (read_file), посмотри на экран (see_screen) "
        "или проверь состояние инструментом. Если действие могло не сработать "
        "(клик по неверному элементу, набор в не то окно, переключатель "
        "play/pause) — проверь и при необходимости повтори. Не выдумывай, что "
        "«сделано», если результат не подтверждён.",
        "ПРОВЕРКА СКРИПТОВ (CRITICAL): каждый написанный код (run_python, "
        "run_shell, файлы .py/.ps1/.js) ты ОБЯЗАН запустить и проверить сам — "
        "смотри на код возврата и вывод, прежде чем сообщать результат. "
        "Кода возврата 0 и ожидаемого вывода достаточно, чтобы считать задачу "
        "готовой; без запуска результат не считается выполненным. Если "
        "инструмент вернул ошибку, в результат приходит пометка «ЗАДАНИЕ НЕ "
        "ВЫПОЛНЕНО» — это прямая инструкция: исправляй код и перезапускай, "
        "пока не станет ок, а ошибку не отдавай пользователю — он должен "
        "увидеть работающий результат, а не лог падения.",
        "Медиа: play_pause — это ПЕРЕКЛЮЧАТЕЛЬ (нажимает play/pause, состояние "
        "неизвестно), для гарантированной остановки используй action='stop', для "
        "запуска — play_pause только если уверен, что сейчас пауза. Не говори "
        "«включаю», если вызывал stop, и наоборот.",

        "MCP (Model Context Protocol) поддерживается: внешние MCP-серверы подключаются "
        "во вкладке «MCP» настроек (пресеты: filesystem, fetch, git, memory, time, "
        "everything, github) или импортом mcp_config.json. Их инструменты появляются в "
        "списке доступных с именами вида «сервер__инструмент» и вызываются как обычные "
        "tools. Если сейчас MCP-инструментов в списке нет — это значит, что серверы не "
        "настроены; предложи настроить (не говори, что MCP не поддерживается).",

        "Автоматизация Windows: чтобы управлять приложением, видео, игрой или окном, "
        "используй связку see_screen (что на экране) → control_screen (клик по "
        "элементу/тексту, ввод, клавиши), press_key (стрелки, пробел, хоткеи), "
        "media_control (play_pause/next/volume), open_application (запуск программы). "
        "Примеры: «перемотай видео на 1:24» — открой плеер (open_application или "
        "control_screen), затем либо нажми клавиши перемотки нужное число раз "
        "(action='key', напр. ['right'] — обычно +5 сек), либо кликни по шкале "
        "прогресса в позиции ~20% ширины, либо напиши скрипт с pyautogui через "
        "run_python (если библиотеки нет — install_package). "
        "«Открой файл/папку/программу/сайт» — open_url/open_application или "
        "run_shell (start \"путь\"). Любую однотипную задачу автоматизируй скриптом, "
        "а не повторяй ручные действия в цикле.",
    ]
    if "spotify" in registry.names and "media_control" in registry.names:
        rules.append(
            "Если управление Spotify недоступно (не выполнен вход или нет Spotify "
            "Premium) и просят включить/переключить музыку — не отказывайся: открой "
            "приложение (spotify action='open'), а затем управляй медиа-клавишами "
            "(media_control: play_pause, next_track, previous_track)."
        )

    notes_block = ""
    if "write_note" in registry.names:
        rules.append(
            "Веди заметки о пользователе: когда узнаёшь что-то важное или полезное "
            "для будущих бесед (имя, предпочтения, планы, события, договорённости) — "
            "молча сохрани заметку через write_note, по своему усмотрению, без "
            "разрешений и упоминаний в ответе. Не дублируй уже записанное."
        )
        notes_block = _notes_for_prompt(config)

    if "remember_fact" in registry.names:
        rules.append(
            "ДОЛГИЕ ЗАДАЧИ: ЧЕКПОИНТЫ И ВОЗОБНОВЛЕНИЕ (CRITICAL). Многоэтапную "
            "работу (установка окружения, сборка, рендер, выгрузка) веди по шагам "
            "и после КАЖДОГО успешного шага сохраняй прогресс через "
            "remember_fact: ключ вида task.<короткое-имя>, значение — что готово и "
            "что дальше (напр. task.video_render = «ffmpeg установлен; сцены 1–3 "
            "сделаны; остался рендер»). Перед началом такой задачи сначала "
            "recall_fact по её ключу: если прогресс уже есть — продолжай с места "
            "остановки, НЕ переустанавливай и не переделывай готовое. Запускай "
            "долгие команды через run_shell action=start и опрашивай action=read: "
            "фоновая сессия живёт после твоего ответа. Если запрос прерван по "
            "времени или по лимиту шагов — следующее сообщение продолжает ту же "
            "задачу с того же чекпоинта, а не начинает заново."
        )

    skills_block = _skills_for_prompt(config)
    if skills_block:
        rules.append(
            "АЛГОРИТМ РАБОТЫ СО СКИЛЛАМИ И ИНСТРУМЕНТАМИ (CRITICAL): списки "
            "скиллов и инструментов в этом промпте обновляются при КАЖДОМ "
            "запросе — опирайся только на них. Перед выполнением задачи: "
            "(1) пройдись по списку скиллов и выбери ВСЕ, чьи описания подходят "
            "под задачу — их может быть несколько, если задача касается одной "
            "темы или нескольких тем сразу; "
            "(2) загрузи каждый выбранный скилл load_skill(имя) ДО начала работы; "
            "(3) выполняй работу по их инструкциям, подбирая под задачу "
            "инструменты из списка инструментов (все нужные, а не один). "
            "Скиллы одной темы применяются совместно: читай их все до действий. "
            "Ни при каких условиях не отвечай «нет скилла» или «нет инструмента» "
            "до тех пор, пока не загрузил подходящие скиллы и не просмотрел весь "
            "список инструментов; выдуманных скиллов и инструментов не существует."
        )

    rules_text = "\n".join(f"- {r}" for r in rules)
    return (
        f"{config.assistant.personality}\n\n"
        f"Текущее время: {now}. Операционная система: {os_info}.\n"
        f"Корень приложения: {PROJECT_ROOT}. Относительные пути инструментов "
        f"(list_directory, read_file, search_files) считаются от него.\n"
        f"Рабочая папка для записи и запуска скриптов: {workspace} — в "
        f"run_python/run_shell текущий каталог именно она, а не корень приложения: "
        f"к файлам проекта обращайся по абсолютному пути.\n"
        f"Доступные инструменты: {tool_names}.\n"
        f"{skills_block}"
        f"{notes_block}"
        "Правила:\n"
        f"{rules_text}"
    )


def _call_signature(call: ToolCall) -> str:
    """Подпись вызова: имя + аргументы (для ловли циклов одинаковых действий)."""
    try:
        args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args = str(call.arguments)
    return f"{call.name}({args})"


def _os_info(config: Config) -> str:
    """Строка ОС для системного промпта: настройки пользователя + автоопределение.

    В настройках можно указать семейство (windows/linux/macos) и уточнение
    (дистрибутив/версия) — это помогает агенту выбирать команды и пути.
    """
    assistant = getattr(config, "assistant", None)
    family = str(getattr(assistant, "os_family", "auto") or "auto").strip().lower()
    details = str(getattr(assistant, "os_details", "") or "").strip()
    detected = f"{platform.system()} {platform.release()}"
    name = _OS_NAMES.get(family)
    if name is None:  # auto или неизвестное значение — берём систему
        base = detected
    elif family == "windows" and not details:
        base = detected  # оставляем конкретный релиз Windows
    else:
        base = name
    if not details:
        return base
    if base.lower() in details.lower() or details.lower() in base.lower():
        return details
    return f"{base} — {details}"


def _notes_for_prompt(config: Config) -> str:
    """Блок «Заметки о пользователе» для системного промпта (или пусто)."""
    memory = getattr(config, "memory", None)
    notes_file = getattr(memory, "notes_file", "")
    if not getattr(memory, "notes", False) or not notes_file:
        return ""
    try:
        from nemus.core.notes import NoteBook

        body = NoteBook(notes_file).format_for_prompt()
    except OSError:
        return ""
    if not body:
        return ""
    return (
        "Твои заметки о пользователе (доступны и через read_notes):\n"
        f"{body}\n"
    )


def _skills_for_prompt(config: Config) -> str:
    """Блок «Скиллы» (Claude Agent Skills) для системного промпта (или пусто)."""
    skills_cfg = getattr(config, "skills", None)
    if skills_cfg is None or not getattr(skills_cfg, "enabled", False):
        return ""
    try:
        from nemus.core.skills import format_skills_block, load_skills

        skills = load_skills(skills_cfg.dir, disabled=skills_cfg.disabled)
        block = format_skills_block(skills, skills_cfg.dir)
    except OSError:
        return ""
    if block:
        logger.debug(f"Скиллов в системном промпте: {len(skills)}")
    return block


def pair_tool_messages(messages: list[Message]) -> list[Message]:
    """Согласует пары «assistant.tool_calls → tool» после загрузки из БД.

    Убирает результаты инструментов без объявленного вызова («сироты» из
    старых БД и при обрезке истории по лимиту) и tool-calls, чей результат
    так и не записался (например, при падении между вызовом и ответом).
    """
    has_result = {
        msg.tool_call_id for msg in messages if msg.role == "tool" and msg.tool_call_id
    }
    out: list[Message] = []
    announced: set[str] = set()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            kept = [call for call in msg.tool_calls if call.id in has_result]
            if kept:
                announced.update(call.id for call in kept)
                out.append(Message(role=msg.role, content=msg.content, tool_calls=kept))
            else:
                # результатов нет — оставляем только текст ответа
                out.append(Message(role=msg.role, content=msg.content))
            continue
        if msg.role == "tool" and (
            not msg.tool_call_id or msg.tool_call_id not in announced
        ):
            continue
        out.append(msg)
    return out


class Agent:
    """Оркестратор: ведёт историю, вызывает LLM и исполняет инструменты."""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        permissions: PermissionManager,
        confirm: ConfirmCallback | None = None,
        max_steps: int | None = None,
        memory: "Memory | None" = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.registry = registry
        self.permissions = permissions
        self.confirm = confirm
        tools_cfg = getattr(config, "tools", None)
        cfg_steps = int(getattr(tools_cfg, "max_steps", 0) or 0)
        cfg_budget = int(getattr(tools_cfg, "run_budget_seconds", 0) or 0)
        self.max_steps = int(max_steps or cfg_steps or MAX_STEPS_HARD)
        #: бюджет времени одного запроса, сек (можно уменьшить в тестах)
        self.run_budget = cfg_budget or RUN_BUDGET_SECONDS
        #: сколько раз бюджет продлевается при живом прогрессе (тоже переопределяемо)
        self.budget_streak = RUN_BUDGET_STREAK
        #: сколько одинаковых вызовов подряд допускается (тоже переопределяемо)
        self.loop_limit = LOOP_REPEAT_LIMIT
        self.memory = memory
        #: счётчик неудач по инструментам внутри текущего запроса
        self._tool_failures: dict[str, int] = {}
        self._base_system = build_system_prompt(config, registry)
        self.history: list[Message] = [
            Message(role="system", content=self._base_system)
        ]
        self._load_history()

    def _load_history(self) -> None:
        """Подгружает последние сообщения из памяти вместе с tool-calls.

        Пары «assistant.tool_calls → tool» обязаны сохраняться целиком:
        иначе провайдеры (LiteLLM → Gemini/OpenAI) отвергают запрос целиком
        с ошибкой «Missing corresponding tool call for tool response message»
        и агент перестаёт работать до очистки истории.
        """
        if self.memory is None:
            return
        from nemus.core.memory import load_messages_into

        rows = [
            row
            for row in self.memory.recent_messages(limit=20)
            if row["role"] != "system"
        ]
        restored: list[Message] = []
        load_messages_into(restored, rows)
        self.history.extend(pair_tool_messages(restored))

    def _persist(self, message: Message) -> None:
        if self.memory is None:
            return
        content = message.content
        if not isinstance(content, str):
            # изображения/блоки в БД не сохраняем — только пометку
            content = "[изображение экрана]" if content else None
        try:
            self.memory.add_message(
                message.role,
                content,
                message.tool_call_id,
                message.name,
                message.tool_calls,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- public
    def _refresh_system_prompt(self) -> None:
        """Пересобирает системный промпт: свежие списки инструментов и скиллов.

        Вызывается перед каждым запросом, чтобы в промпте всегда были актуальные
        списки: инструменты, созданные через create_tool или подключённые по MCP,
        и скиллы, добавленные через create_skill, видны модели сразу.
        """
        self._base_system = build_system_prompt(self.config, self.registry)

    def reset(self) -> None:
        """Сбрасывает историю (system-промпт пересобирается заново)."""
        self._refresh_system_prompt()
        self.history = [Message(role="system", content=self._base_system)]

    def switch_session(self, session_id: int) -> int:
        """Переключает активный диалог и подгружает его историю.

        Сообщения уже лежат в БД по мере появления, поэтому здесь достаточно
        сменить текущий диалог в памяти и пересобрать ``history``.
        """
        if self.memory is None:
            return 0
        sid = self.memory.switch_session(session_id)
        self._reload_history()
        return sid

    def new_session(self, title: str = "") -> int:
        """Создаёт новый диалог, делает его текущим и возвращает id."""
        if self.memory is None:
            return 0
        sid = self.memory.create_session(title)
        self._reload_history()
        return sid

    def _reload_history(self) -> None:
        """Пересобирает историю текущего диалога: system + сохранённые реплики."""
        self._refresh_system_prompt()
        self.history = [Message(role="system", content=self._base_system)]
        self._load_history()

    def _apply_language_hint(self, user_text: str) -> None:
        """В режиме ``auto`` добавляет в системный промпт язык текущей реплики."""
        prompt = self._base_system
        reply_language = getattr(self.config.assistant, "reply_language", REPLY_AUTO)
        if normalize(reply_language) == REPLY_AUTO:
            detected = detect_language(user_text)
            if detected:
                prompt = f"{prompt}\n\n{reply_language_directive(REPLY_AUTO, detected)}"
        if self.history and self.history[0].role == "system":
            self.history[0].content = prompt

    def run(self, user_text: str) -> Iterator[AgentEvent]:
        """Обрабатывает запрос пользователя, отдавая события по мере работы."""
        self._refresh_system_prompt()
        self._tool_failures = {}  # счётчик ошибок — на каждый запрос заново
        user_msg = Message(role="user", content=user_text)
        self.history.append(user_msg)
        self._persist(user_msg)
        if self.memory is not None:
            # первый вопрос задаёт диалогу название (если его не меняли)
            self.memory.auto_title(user_text)
        self._apply_language_hint(user_text)

        empty_retries = 0
        step = 0
        #: подписи вызовов инструментов внутри запроса — ловля тупика
        call_signatures: list[str] = []
        started = time.monotonic()
        #: дедлайн простоя: сдвигается после каждого завершённого шага —
        #: работа не должна упираться в бюджет, отсчитанный от старта запроса
        deadline = started + self.run_budget
        #: жёсткий потолок одного запроса с учётом всех продлений
        hard_deadline = started + self.run_budget * self.budget_streak
        while step < self.max_steps:
            now = time.monotonic()
            if now > hard_deadline:
                yield AgentEvent(
                    kind="error",
                    error=(
                        f"Задача остановлена по времени: жёсткий предел "
                        f"{self.run_budget * self.budget_streak:g} с "
                        f"(бюджет {self.run_budget:g} с продлевался по прогрессу). "
                        "Фоновые процессы НЕ остановлены — посмотри их вывод "
                        "(run_shell action=read) и продолжи новым сообщением."
                    ),
                )
                return
            if now > deadline:
                yield AgentEvent(
                    kind="error",
                    error=(
                        f"Задача остановлена по времени ({self.run_budget:g} с "
                        "без прогресса) — лимит одного запроса. Разбей работу на "
                        "части и продолжай новым сообщением."
                    ),
                )
                return
            logger.debug(f"Агент: шаг {step + 1}")
            # схемы берём каждый шаг: инструмент, созданный агентом через
            # create_tool, сразу попадает в список и доступен модели
            tools = self.registry.schemas() if len(self.registry) else None
            try:
                response = yield from self._stream_response(tools)
            except ProviderError as exc:
                yield AgentEvent(kind="error", error=redact_secrets(str(exc)))
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Сбой агента")
                yield AgentEvent(kind="error", error=redact_secrets(str(exc)))
                return

            # Пустой ответ (ни текста, ни tool_calls) — это не финал, а сбой
            # генерации (модель могла уйти в reasoning или обрезаться по токенам).
            # Без этой проверки GUI показывал бы молчаливую «Готова».
            if not response.tool_calls and not (response.content or "").strip():
                empty_retries += 1
                logger.warning(
                    f"Пустой ответ модели (finish={response.finish_reason}), "
                    f"попытка {empty_retries}/{EMPTY_ANSWER_RETRIES}"
                )
                if empty_retries <= EMPTY_ANSWER_RETRIES:
                    nudge = Message(
                        role="user",
                        content=(
                            f"Ты снова ответил пустым сообщением (попытка "
                            f"{empty_retries} из {EMPTY_ANSWER_RETRIES}). Это "
                            "ошибка генерации — отвечай по делу: сразу вызывай "
                            "нужные инструменты (или создай их) либо пиши "
                            "текстовый ответ. Списки инструментов и скиллов уже "
                            "просмотрены — работай, не молчи."
                        ),
                    )
                    self.history.append(nudge)
                    self._persist(nudge)
                    # шаг НЕ засчитываем: повтор — не работа, а ещё одна
                    # попытка выбить из модели содержательный ответ
                    continue
                yield AgentEvent(
                    kind="error",
                    error=(
                        f"Модель {EMPTY_ANSWER_RETRIES} раз подряд вернула пустой "
                        "ответ — работа остановлена. Попробуй отправить запрос "
                        "ещё раз или смени модель в настройках."
                    ),
                )
                return

            empty_retries = 0  # модель снова отвечает по делу

            # Тупик: один и тот же вызов с теми же аргументами повторяется —
            # застряли в цикле, бессмысленно продолжать
            if response.tool_calls:
                call_signatures.extend(
                    _call_signature(c) for c in response.tool_calls
                )
                limit = self.loop_limit
                if (
                    len(call_signatures) >= limit
                    and len(set(call_signatures[-limit:])) == 1
                ):
                    repeated = call_signatures[-1]
                    logger.warning(
                        f"Цикл одинаковых вызовов ({limit} раз): {repeated[:120]}"
                    )
                    yield AgentEvent(
                        kind="error",
                        error=(
                            f"Тупик: одно и то же действие повторено {limit} раз "
                            f"подряд — {repeated[:200]}. Останавливаюсь: смени "
                            "подход (другой инструмент/путь) и начни новый запрос."
                        ),
                    )
                    return

            assistant_msg = Message(
                role="assistant",
                content=response.content or None,
                tool_calls=response.tool_calls or None,
            )
            self.history.append(assistant_msg)
            self._persist(assistant_msg)

            if not response.tool_calls:
                yield AgentEvent(kind="final", text=response.content)
                return

            if response.content:
                yield AgentEvent(kind="assistant_text", text=response.content)

            images: list[str] = []
            for call in response.tool_calls:
                yield from self._handle_tool_call(call, images)
            if images:
                self._add_vision_message(images)
            step += 1  # содержательный ответ засчитан — идём дальше
            # «сердцебиение»: шаг завершён — дедлайн простоя сдвигается от
            # текущего момента (иначе опрос фоновой команды каждые 10 с
            # убивал бы ход по истечении бюджета от старта запроса)
            deadline = min(time.monotonic() + self.run_budget, hard_deadline)

        yield AgentEvent(
            kind="error",
            error=f"Превышен лимит шагов ({self.max_steps}). Останавливаюсь.",
        )

    # --------------------------------------------------------------- internal
    def _stream_response(self, tools: list[dict] | None):
        """Стримит ответ, отдавая дельты, и возвращает финальный ответ."""
        from nemus.core.llm import LLMResponse  # локальный импорт во избежание цикла

        final: LLMResponse | None = None
        for event in self.llm.stream(self.history, tools=tools):
            if event.kind == "content":
                yield AgentEvent(kind="assistant_delta", text=event.text)
            elif event.kind == "thought":
                # скрытые рассуждения модели — в отдельный поток «мыслей»
                yield AgentEvent(kind="assistant_thought", text=event.text)
            elif event.kind == "final":
                final = event.response
        if final is None:
            raise ProviderError("Провайдер не вернул ответ.")
        return final

    def _handle_tool_call(
        self, call: ToolCall, images: list[str] | None = None
    ) -> Iterator[AgentEvent]:
        tool = self.registry.get(call.name)
        yield AgentEvent(
            kind="tool_call",
            tool_name=call.name,
            tool_call_id=call.id,
            tool_args=call.arguments,
        )

        if tool is None:
            result = ToolResult.failure(f"Инструмент '{call.name}' не найден.")
        else:
            result = self._execute_tool(tool, call.arguments)

        content = self._model_tool_content(call.name, result)
        tool_msg = Message(role="tool", tool_call_id=call.id, content=content)
        self.history.append(tool_msg)
        self._persist(tool_msg)

        if images is not None and result.images:
            images.extend(result.images)

        yield AgentEvent(
            kind="tool_result",
            tool_name=call.name,
            tool_call_id=call.id,
            result=result,
        )

    def _model_tool_content(self, name: str, result: ToolResult) -> str:
        """Готовит результат для модели: при ошибке — инструкция «переделать».

        Модель получает ошибку вместе с прямым указанием исправить и
        повторить вызов, а после нескольких неудач — предложением сменить
        способ. Отказ политики/пользователя повторять не заставляем.
        """
        content = result.to_model()
        if result.ok:
            self._tool_failures.pop(name, None)
            return content
        if any(marker in content for marker in _DENIAL_MARKERS):
            return content
        attempts = self._tool_failures.get(name, 0) + 1
        self._tool_failures[name] = attempts
        if attempts >= FAIL_ATTEMPTS:
            content += _FAIL_DIRECTIVE_CHANGE.format(n=attempts)
        else:
            content += _FAIL_DIRECTIVE.format(n=attempts, max=FAIL_ATTEMPTS)
        return content

    def _add_vision_message(self, images: list[str]) -> None:
        """Добавляет снимки экрана в контекст как сообщение пользователя.

        Изображение нельзя приложить к сообщению роли ``tool`` (ограничение
        OpenAI-совместимых API), поэтому после результатов инструментов
        добавляется отдельное пользовательское сообщение с блоками картинок.
        """
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "Снимок экрана к предыдущему действию инструмента.",
            }
        ]
        for data_url in images:
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        self.history.append(Message(role="user", content=parts))
        # в историю БД картинки не пишем

    def _execute_tool(self, tool: Tool, args: dict[str, Any]) -> ToolResult:
        decision = self.permissions.check(tool)
        if decision == Decision.DENY:
            return ToolResult.failure(f"Инструмент '{tool.name}' запрещён политикой.")
        if decision == Decision.ASK:
            if self.confirm is None or not self.confirm(tool, args):
                return ToolResult.failure(
                    f"Пользователь отклонил вызов инструмента '{tool.name}'."
                )
        return self.registry.execute(tool.name, args)
