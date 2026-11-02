"""Клиент LLM: единая точка доступа к провайдерам через LiteLLM.

Поддерживает OpenAI, Anthropic, Google Gemini, OpenRouter и Ollama.
Реализует автоматический fallback между моделями и стриминг.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Literal

from nemus.config import Config
from nemus.core.providers import (
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    provider_kwargs,
)
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets

StreamKind = Literal["content", "thought", "final"]

#: попыток на одном провайдере, прежде чем уйти в фолбэк. Секундный обрыв
#: связи — не повод менять модель: смена провайдера означает повторную
#: отправку всей истории новому адресату (это и лишние потраченные лимиты,
#: и лишнее ожидание), а 503 от Gemini приходит пачками.
MAX_ATTEMPTS = 11
#: базовая пауза между попытками, сек (растёт с номером попытки)
RETRY_BACKOFF = 1.0
#: во сколько раз растёт пауза с каждой следующей попыткой
BACKOFF_GROWTH = 1.6
#: потолок паузы, сек — ждать одного провайдера дольше бессмысленно
MAX_BACKOFF = 5.0
#: суммарный бюджет времени на один провайдер, сек (от старта провайдера
#: до ухода в фолбэк). Быстрые ретраи (обрывы связи, 503) укладываются
#: в лестницу пауз ~35 с с огромным запасом; потолок нужен, чтобы зависший
#: на таймауте провайдер не держал диалог 11 × 120 с. Заниженный бюджет
#: (180 с) съедался одной медленной попыткой — и 503 уходил в фолбэк
#: без единого повтора, хотя повтор был бы успешен.
TARGET_BUDGET = 600.0

#: причина ухода в фолбэк:
#: ``limit`` — лимиты исчерпаны (повторять на этом же провайдере бесполезно),
#: ``fatal`` — ошибка, которую повтор не лечит (ключ, модель, контекст),
#: ``transient`` — обрыв связи/таймаут/5xx — здесь повторы и помогают
ErrorKind = Literal["limit", "fatal", "transient"]

#: признаки исчерпанных лимитов в тексте ошибки (после lower())
_LIMIT_MARKERS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "too many requests",
    "quota",
    "resource exhausted",
    "usage limit",
)
#: 429 — слишком много запросов (число ищем как отдельное слово,
#: иначе «4030 токенов» совпало бы с кодом 403)
_LIMIT_CODE = re.compile(r"\b429\b")
#: признаки неповторяемых ошибок: ключ, прав, модель, окно контекста
_FATAL_MARKERS = (
    "unauthorized",
    "invalid api key",
    "incorrect api key",
    # провайдер без ключа: 11 повторов не добавят кредов, только засыпят лог
    "missing credentials",
    "api_key client option must be set",
    "no api key",
    "authentication",
    "permission denied",
    "forbidden",
    "model not found",
    "context window",
    "context length",
    "prompt is too long",
)
#: 401/403 — неавторизован/доступ запрещён
_FATAL_CODE = re.compile(r"\b(?:401|403)\b")
#: классы исключений litellm, которых повтор не поможет
_FATAL_CLASSES = (
    "authentication",
    "permissiondenied",
    "notfound",
    "badrequest",
    "unprocessable",
    "invalidrequest",
    "contextwindow",
    "prompttoo",
    "contentpolicy",
)

#: сообщения litellm, которые дублируются в консоль на КАЖДЫЙ запрос
_LITELLM_NOISE_MARKERS = (
    # litellm логирует её каждый раз при temperature для Gemini 3+
    "DeprecationWarning: `temperature`, `top_p`, and `top_k`",
)


class _LiteLLMNoiseFilter(logging.Filter):
    """Гасит известный мусор litellm и только его (остальные записи идут)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        return not any(marker in message for marker in _LITELLM_NOISE_MARKERS)


_litellm_ready = False


#: параметры сэмплиинга, которые Gemini 3+ просит убрать из запроса
SAMPLING_KEYS = ("temperature", "top_p", "top_k")


def _import_litellm():
    """Импортирует litellm и один раз убирает его отладочный шум из вывода.

    Без этого на каждый запрос в консоль сыпалось:
    - ``LiteLLM:WARNING: ... DeprecationWarning: temperature ...`` (логгер
      ``LiteLLM`` со своим хендлером);
    - пустая строка + «Give Feedback / Get Help» + «LiteLLM.Info: ...» —
      печатается в stdout на каждую ошибку, пока не поднят ``suppress_debug_info``.
    """
    global _litellm_ready
    import litellm

    if not _litellm_ready:
        _litellm_ready = True
        litellm.suppress_debug_info = True
        logging.getLogger("LiteLLM").addFilter(_LiteLLMNoiseFilter())
    return litellm


def _is_gemini_3_plus(model: str) -> bool:
    """Модель семейства Gemini 3 и новее (там сэмплинг deprecated).

    Считаем так же, как litellm (``VertexGeminiConfig._is_gemini_3_or_newer``),
    чтобы не расходиться с его предупреждениями; если приватный API litellm
    переименуют — откатываемся на локальный разбор имени модели.
    """
    if not model:
        return False
    # сначала убеждаемся, что это вообще Gemini: litellm считает по части
    # имени, не проверяя провайдера, и для "gpt-4o-mini" вернул бы True
    name = model.split("/")[-1].lower()
    if not name.startswith("gemini"):
        return False
    try:
        from litellm.llms.vertex_ai.gemini import (
            vertex_and_google_ai_studio_gemini as _vertex_gemini,
        )

        return bool(_vertex_gemini.VertexGeminiConfig._is_gemini_3_or_newer(model))
    except Exception:  # noqa: BLE001
        if not name.startswith("gemini-"):
            return False
        major = name[len("gemini-") :].split(".", 1)[0]
        return major.isdigit() and int(major) >= 3


def _strip_sampling(model: str, kwargs: dict[str, Any]) -> None:
    """Убирает сэмплинг у Gemini 3+ — условно, старые модели не трогает.

    GPT-4o, Claude 3.5 и Gemini 1.5/2.0 получают ``temperature`` как раньше.
    Моделям Gemini 3+ litellm эти ключи всё равно применяет, но на каждый
    запрос в консоль сыпался DeprecationWarning, а temperature < 1.0 там
    снижает качество рассуждений (рекомендация litellm: сэмплинг переносить
    в system-инструкции) — поэтому до отправки ключи вырезаются.
    """
    if not _is_gemini_3_plus(model):
        return
    removed = [key for key in SAMPLING_KEYS if key in kwargs]
    for key in removed:
        kwargs.pop(key, None)
    if removed:
        logger.debug(
            f"{model}: параметры сэмплиинга отброшены ({', '.join(removed)})"
        )


def error_kind(exc: Exception) -> ErrorKind:
    """К какому типу относится ошибка провайдера.

    Класс исключения проверяется первым (litellm кидает ``RateLimitError``,
    ``AuthenticationError`` и т.п.), текст — запасной вариант для ошибок,
    пришедших обычным ``RuntimeError``/httpx.
    """
    name = type(exc).__name__.lower()
    if "ratelimit" in name:
        return "limit"
    if name.startswith(_FATAL_CLASSES):
        return "fatal"
    text = redact_secrets(str(exc)).lower()
    if _LIMIT_CODE.search(text) or any(m in text for m in _LIMIT_MARKERS):
        return "limit"
    if _FATAL_CODE.search(text) or any(m in text for m in _FATAL_MARKERS):
        return "fatal"
    return "transient"


def retry_delay(attempt: int) -> float:
    """Пауза после неудачной попытки ``attempt`` (сек)."""
    return min(RETRY_BACKOFF * (BACKOFF_GROWTH ** (attempt - 1)), MAX_BACKOFF)


def should_retry(kind: ErrorKind, attempt: int) -> bool:
    """Повторять ли запрос на том же провайдере после ошибки ``kind``.

    Лимиты и неповторяемые ошибки уходят в фолбэк сразу; всё остальное
    повторяем до ``MAX_ATTEMPTS`` раз включительно.
    """
    if kind in ("limit", "fatal"):
        return False
    return attempt < MAX_ATTEMPTS


def next_delay(kind: ErrorKind, attempt: int, deadline: float, model: str) -> float | None:
    """Пауза перед следующей попыткой, либо ``None`` — пора в фолбэк.

    ``deadline`` — момент (``time.monotonic``), после которого провайдер
    считается исчерпанным по времени, независимо от оставшихся попыток.
    """
    if not should_retry(kind, attempt):
        return None
    delay = retry_delay(attempt)
    if time.monotonic() + delay > deadline:
        logger.warning(
            f"{model}: бюджет {TARGET_BUDGET:.0f} с на провайдер исчерпан — фолбэк"
        )
        return None
    return delay

#: имена тегов, внутри которых модель прячет рассуждения
_THINK_NAMES = "think|thinking|thought|thoughts|reasoning|reasoning_content|analysis"
_THINK_BLOCK = re.compile(
    rf"<(?P<tag>{_THINK_NAMES})\b[^>]*>.*?</(?P=tag)\s*>", re.DOTALL | re.IGNORECASE
)
_THINK_OPEN = re.compile(rf"<\s*(?:{_THINK_NAMES})\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(rf"<\s*/\s*(?:{_THINK_NAMES})\s*>", re.IGNORECASE)
_TAG_TAIL = re.compile(r"<[^>]*$")


def strip_reasoning(text: str) -> str:
    """Убирает из ответа блоки рассуждений вида ``<thinking>…</thinking>``."""
    if not text:
        return ""
    return _THINK_BLOCK.sub("", text).strip()


class _ReasoningFilter:
    """Потоковый фильтр: подавляет текст внутри ``<thinking>…</thinking>``.

    Работает на дельтах стрима, удерживая незавершённые теги между чанками.
    Подавленный текст не выбрасывается, а копится в ``take_hidden`` — GUI
    показывает его отдельно как «мысли» модели.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False
        self._hidden: list[str] = []

    def feed(self, text: str) -> str:
        data = self._pending + text
        self._pending = ""
        out: list[str] = []
        while data:
            if self._inside:
                match = _THINK_CLOSE.search(data)
                if match is None:
                    tail = _TAG_TAIL.search(data)
                    if tail:
                        self._hidden.append(data[: tail.start()])
                        self._pending = data[tail.start():]
                    else:
                        self._hidden.append(data)
                    break
                self._hidden.append(data[: match.start()])
                data = data[match.end():]
                self._inside = False
                continue
            match = _THINK_OPEN.search(data)
            if match is None:
                tail = _TAG_TAIL.search(data)
                if tail:
                    out.append(data[: tail.start()])
                    self._pending = data[tail.start():]
                else:
                    out.append(data)
                break
            out.append(data[: match.start()])
            data = data[match.end():]
            self._inside = True
        return "".join(out)

    def take_hidden(self) -> str:
        """Отдаёт накопленный скрытый текст рассуждений и очищает буфер."""
        text = "".join(self._hidden)
        self._hidden.clear()
        return text

    def flush(self) -> str:
        """Отдаёт оставшийся буфер (внутри блока рассуждений — в скрытые)."""
        text, self._pending = self._pending, ""
        if self._inside:
            if text:
                self._hidden.append(text)
            return ""
        return text


@dataclass
class StreamEvent:
    """Событие стрима: текстовый фрагмент или финальный ответ."""

    kind: StreamKind
    text: str = ""
    response: LLMResponse | None = None


class _ToolCallAccumulator:
    """Собирает фрагменты tool-calls из стрима LiteLLM."""

    def __init__(self) -> None:
        self._buf: dict[int, dict[str, str]] = {}

    def add(self, deltas: list[Any]) -> None:
        for tc in deltas:
            idx = getattr(tc, "index", 0) or 0
            slot = self._buf.setdefault(idx, {"id": "", "name": "", "args": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["args"] += fn.arguments

    def result(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for idx in sorted(self._buf):
            slot = self._buf[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {"_raw": slot["args"]}
            calls.append(
                ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=args)
            )
        return calls


@dataclass
class ChatTarget:
    """Конкретная модель у конкретного провайдера."""

    provider: str
    model: str


class LLMClient:
    """Обёртка над LiteLLM с fallback-цепочкой."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._target = ChatTarget(
            config.llm.default_provider, config.llm.default_model
        )
        self._fallbacks: list[ChatTarget] = [
            ChatTarget(fb.provider, fb.model) for fb in config.llm.fallback
        ]

    # ------------------------------------------------------------ properties
    @property
    def model(self) -> str:
        return self._target.model

    @property
    def provider(self) -> str:
        return self._target.provider

    def set_model(self, provider: str, model: str) -> None:
        self._target = ChatTarget(provider, model)

    # --------------------------------------------------------------- helpers
    def _targets(self) -> list[ChatTarget]:
        seen: list[ChatTarget] = []
        for t in [self._target, *self._fallbacks]:
            if t not in seen:
                seen.append(t)
        return seen

    def _build_kwargs(self, target: ChatTarget, tools: list[dict] | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": target.model,
            "temperature": self.config.llm.temperature,
            "max_tokens": self.config.llm.max_tokens,
            "timeout": self.config.llm.request_timeout,
        }
        kwargs.update(provider_kwargs(target.provider, self.config))
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    @staticmethod
    def _messages_payload(messages: list[Message]) -> list[dict[str, Any]]:
        return [m.to_dict() for m in messages]

    # -------------------------------------------------------------- complete
    def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        **overrides: Any,
    ) -> LLMResponse:
        """Синхронный запрос: повторы на провайдере, затем fallback."""
        litellm = _import_litellm()

        payload = self._messages_payload(messages)
        failures: dict[str, str] = {}

        for target in self._targets():
            kwargs = self._build_kwargs(target, tools)
            kwargs.update(overrides)
            _strip_sampling(target.model, kwargs)
            deadline = time.monotonic() + TARGET_BUDGET
            for attempt in range(1, MAX_ATTEMPTS + 1):
                logger.debug(f"LLM → {target.model} (попытка {attempt}/{MAX_ATTEMPTS})")
                try:
                    resp = litellm.completion(messages=payload, **kwargs)
                    return self._parse_response(resp)
                except Exception as exc:  # noqa: BLE001
                    message = redact_secrets(str(exc))
                    failures[target.model] = (
                        f"{target.model}: {message} (попытка {attempt}/{MAX_ATTEMPTS})"
                    )
                    kind = error_kind(exc)
                    logger.warning(
                        f"Провайдер {target.model}, попытка {attempt}/{MAX_ATTEMPTS} "
                        f"({kind}): {message}"
                    )
                    delay = next_delay(kind, attempt, deadline, target.model)
                    if delay is None:
                        break
                    time.sleep(delay)

        raise ProviderError(
            "Все провайдеры недоступны:\n" + "\n".join(failures.values())
        )

    # ---------------------------------------------------------------- stream
    def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        **overrides: Any,
    ) -> Iterator[StreamEvent]:
        """Стриминг ответа. Отдаёт текстовые дельты и финальный ответ.

        Каждый провайдер пробуется до ``MAX_ATTEMPTS`` раз с растущей паузой:
        секундный обрыв связи не должен ронивать диалог и переключать модель.
        Фолбэк сразу — только когда лимиты исчерпаны (429/квота) либо ошибка,
        которую повтор не лечит (ключ, модель, окно контекста). Всё вместе
        ограничено бюджетом ``TARGET_BUDGET`` на один провайдер — одной
        медленной попытке не следует съедать все повторы.
        """
        litellm = _import_litellm()

        payload = self._messages_payload(messages)
        failures: dict[str, str] = {}

        for target in self._targets():
            kwargs = self._build_kwargs(target, tools)
            kwargs.update(overrides)
            kwargs["stream"] = True
            _strip_sampling(target.model, kwargs)
            deadline = time.monotonic() + TARGET_BUDGET

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    logger.debug(
                        f"LLM (stream) → {target.model} "
                        f"(попытка {attempt}/{MAX_ATTEMPTS})"
                    )
                    stream = litellm.completion(messages=payload, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    message = redact_secrets(str(exc))
                    failures[target.model] = (
                        f"{target.model}: {message} (попытка {attempt}/{MAX_ATTEMPTS})"
                    )
                    kind = error_kind(exc)
                    logger.warning(
                        f"Стрим {target.model}, попытка {attempt}/{MAX_ATTEMPTS} "
                        f"({kind}): {message}"
                    )
                    delay = next_delay(kind, attempt, deadline, target.model)
                    if delay is None:
                        break
                    time.sleep(delay)
                    continue

                emitted = False
                try:
                    for event in self._iter_stream(stream, target.model):
                        emitted = True
                        yield event
                    return
                except Exception as exc:  # noqa: BLE001
                    # Ошибка уже открытого стрима. Пока не отдано ни одной дельты —
                    # безопасно повторить; если частичный ответ ушёл пользователю,
                    # повтор его продублирует, поэтому возвращаем понятную ошибку
                    # вместо молчаливого сбоя.
                    message = f"{target.model}: стрим прерван: {redact_secrets(str(exc))}"
                    logger.warning(message)
                    failures[target.model] = (
                        f"{message} (попытка {attempt}/{MAX_ATTEMPTS})"
                    )
                    if emitted:
                        raise ProviderError(message) from None
                    delay = next_delay(error_kind(exc), attempt, deadline, target.model)
                    if delay is None:
                        break
                    time.sleep(delay)

        raise ProviderError(
            "Все провайдеры недоступны:\n" + "\n".join(failures.values())
        )

    def _iter_stream(self, stream: Any, model: str) -> Iterator[StreamEvent]:
        """Проходит по чанкам стрима, собирая текст и tool-calls.

        Тексты рассуждений (``reasoning_content`` и теги ``<thinking>``) не
        попадают ни в дельты, ни в финальный ответ — они отдаются отдельным
        типом событий ``thought``, чтобы GUI показывал их иначе, чем ответ.
        """
        accumulator = _ToolCallAccumulator()
        content_parts: list[str] = []
        finish_reason = "stop"
        reasoning = _ReasoningFilter()

        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            content = getattr(delta, "content", None)
            if self._is_reasoning_delta(delta):
                # дельты рассуждений: текст лежит в reasoning_content/reasoning
                thought_text = content or getattr(delta, "reasoning_content", None)
                thought_text = thought_text or getattr(delta, "reasoning", None)
                if thought_text:
                    yield StreamEvent(kind="thought", text=thought_text)
                content = None
            if content:
                content_parts.append(content)
                visible = reasoning.feed(content)
                if visible:
                    yield StreamEvent(kind="content", text=visible)
                hidden = reasoning.take_hidden()
                if hidden:
                    yield StreamEvent(kind="thought", text=hidden)
            tool_deltas = getattr(delta, "tool_calls", None)
            if tool_deltas:
                accumulator.add(tool_deltas)

        tail = reasoning.flush()
        if tail:
            yield StreamEvent(kind="content", text=tail)
        hidden = reasoning.take_hidden()
        if hidden:
            yield StreamEvent(kind="thought", text=hidden)

        response = LLMResponse(
            content=strip_reasoning("".join(content_parts)),
            tool_calls=accumulator.result(),
            finish_reason=finish_reason,
            model=model,
        )
        yield StreamEvent(kind="final", response=response)

    @staticmethod
    def _is_reasoning_delta(delta: Any) -> bool:
        """Дельта целиком относится к рассуждениям модели?"""
        for attr in ("reasoning_content", "reasoning"):
            if getattr(delta, attr, None):
                return True
        return False

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _parse_response(resp: Any) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for raw in (getattr(msg, "tool_calls", None) or []):
            try:
                tool_calls.append(ToolCall.from_litellm(raw))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Не удалось разобрать tool_call: {exc}")

        usage: dict[str, Any] = {}
        if getattr(resp, "usage", None):
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }

        return LLMResponse(
            content=strip_reasoning(getattr(msg, "content", "") or ""),
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            model=getattr(resp, "model", ""),
            usage=usage,
            raw=resp,
        )
