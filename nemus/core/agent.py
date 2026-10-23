"""Агентный цикл Немуси: диалог, вызов инструментов, память контекста."""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal

from nemus.config import Config
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
    "assistant_delta", "assistant_text", "tool_call", "tool_result", "final", "error"
]

#: callback подтверждения: (tool, args) -> разрешено?
ConfirmCallback = Callable[[Tool, dict[str, Any]], bool]


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
    os_info = f"{platform.system()} {platform.release()}"
    tool_names = ", ".join(registry.names) if len(registry) else "нет"
    workspace = getattr(getattr(config, "tools", None), "workspace", "data/workspace")
    reply_language = getattr(config.assistant, "reply_language", REPLY_AUTO)
    rules = [
        "Для реальных действий используй инструменты, не выдумывай результаты.",
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
        "Не отказывайся от задачи из-за отсутствия готового инструмента — "
        "напиши код или создай инструмент через create_tool, сложи результаты в рабочую папку.",
        "Никогда не отвечай «не умею», «не могу», «нет доступа» или «в целях безопасности» "
        "до того, как попробовал инструменты. Ограничений на Python и консоль нет: "
        "файлы можно создавать и менять в любом месте системы, любые команды и скрипты "
        "выполняются. Если инструмент вернул ошибку — обойди её другим способом "
        "(другой путь, run_shell вместо run_python, свой скрипт), и только после "
        "нескольких попыток объясни, что именно не получается.",
        "Проверяй результат действий: не сообщай об успехе, пока не убедишься — "
        "прочитай созданный файл (read_file), посмотри на экран (see_screen) или "
        "проверь состояние инструментом. Если действие могло не сработать (клик по "
        "неверному элементу, набор в не то окно, переключатель play/pause) — проверь "
        "и при необходимости повтори. Не выдумывай, что «сделано», если результат не "
        "подтверждён.",
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

    rules_text = "\n".join(f"- {r}" for r in rules)
    return (
        f"{config.assistant.personality}\n\n"
        f"Текущее время: {now}. Операционная система: {os_info}.\n"
        f"Рабочая папка для твоих файлов и скриптов: {workspace}.\n"
        f"Доступные инструменты: {tool_names}.\n"
        f"{notes_block}"
        "Правила:\n"
        f"{rules_text}"
    )


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


class Agent:
    """Оркестратор: ведёт историю, вызывает LLM и исполняет инструменты."""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        permissions: PermissionManager,
        confirm: ConfirmCallback | None = None,
        max_steps: int = 12,
        memory: "Memory | None" = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.registry = registry
        self.permissions = permissions
        self.confirm = confirm
        self.max_steps = max_steps
        self.memory = memory
        self._base_system = build_system_prompt(config, registry)
        self.history: list[Message] = [
            Message(role="system", content=self._base_system)
        ]
        self._load_history()

    def _load_history(self) -> None:
        """Подгружает последние сообщения из памяти (без tool-calls)."""
        if self.memory is None:
            return
        from nemus.core.providers import Message as _M

        rows = self.memory.recent_messages(limit=20)
        for row in rows:
            if row["role"] == "system":
                continue
            self.history.append(
                _M(role=row["role"], content=row["content"], tool_call_id=row["tool_call_id"], name=row["name"])
            )

    def _persist(self, message: Message) -> None:
        if self.memory is None:
            return
        content = message.content
        if not isinstance(content, str):
            # изображения/блоки в БД не сохраняем — только пометку
            content = "[изображение экрана]" if content else None
        try:
            self.memory.add_message(
                message.role, content, message.tool_call_id, message.name
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- public
    def reset(self) -> None:
        """Сбрасывает историю (system-промпт сохраняется)."""
        self.history = [Message(role="system", content=self._base_system)]

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
        user_msg = Message(role="user", content=user_text)
        self.history.append(user_msg)
        self._persist(user_msg)
        self._apply_language_hint(user_text)

        for step in range(self.max_steps):
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

        tool_msg = Message(role="tool", tool_call_id=call.id, content=result.to_model())
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
