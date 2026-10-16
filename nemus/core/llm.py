"""Клиент LLM: единая точка доступа к провайдерам через LiteLLM.

Поддерживает OpenAI, Anthropic, Google Gemini, OpenRouter и Ollama.
Реализует автоматический fallback между моделями и стриминг.
"""

from __future__ import annotations

import json
import re
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

StreamKind = Literal["content", "final"]

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
    """

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False

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
                        self._pending = data[tail.start():]
                    break
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

    def flush(self) -> str:
        """Отдаёт оставшийся буфер (внутри блока рассуждений — ничего)."""
        text, self._pending = self._pending, ""
        return "" if self._inside else text


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
        """Синхронный запрос с автоматическим fallback."""
        import litellm

        payload = self._messages_payload(messages)
        errors: list[str] = []

        for target in self._targets():
            kwargs = self._build_kwargs(target, tools)
            kwargs.update(overrides)
            try:
                logger.debug(f"LLM → {target.model}")
                resp = litellm.completion(messages=payload, **kwargs)
                return self._parse_response(resp)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{target.model}: {redact_secrets(str(exc))}")
                logger.warning(
                    f"Провайдер {target.model} недоступен: {redact_secrets(str(exc))}"
                )

        raise ProviderError(
            "Все провайдеры недоступны:\n" + "\n".join(errors)
        )

    # ---------------------------------------------------------------- stream
    def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        **overrides: Any,
    ) -> Iterator[StreamEvent]:
        """Стриминг ответа. Отдаёт текстовые дельты и финальный ответ.

        Пробует провайдеров по цепочке fallback; при ошибке создания потока
        переходит к следующему.
        """
        import litellm

        payload = self._messages_payload(messages)
        errors: list[str] = []

        for target in self._targets():
            kwargs = self._build_kwargs(target, tools)
            kwargs.update(overrides)
            kwargs["stream"] = True
            try:
                logger.debug(f"LLM (stream) → {target.model}")
                stream = litellm.completion(messages=payload, **kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{target.model}: {redact_secrets(str(exc))}")
                logger.warning(
                    f"Стрим {target.model} недоступен: {redact_secrets(str(exc))}"
                )
                continue

            yield from self._iter_stream(stream, target.model)
            return

        raise ProviderError("Все провайдеры недоступны:\n" + "\n".join(errors))

    def _iter_stream(self, stream: Any, model: str) -> Iterator[StreamEvent]:
        """Проходит по чанкам стрима, собирая текст и tool-calls.

        Тексты рассуждений (``reasoning_content`` и теги ``<thinking>``) не
        попадают ни в дельты, ни в финальный ответ.
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
            if content and not self._is_reasoning_delta(delta):
                content_parts.append(content)
                visible = reasoning.feed(content)
                if visible:
                    yield StreamEvent(kind="content", text=visible)
            tool_deltas = getattr(delta, "tool_calls", None)
            if tool_deltas:
                accumulator.add(tool_deltas)

        tail = reasoning.flush()
        if tail:
            yield StreamEvent(kind="content", text=tail)

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
