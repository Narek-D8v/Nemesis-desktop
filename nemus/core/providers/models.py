"""Списки моделей по провайдерам с пометкой «бесплатно/платно».

Для GUI (смена провайдера в настройках): модель не нужно помнить наизусть —
комбobox показывает известные модели выбранного провайдера, бесплатные сверху.

Источники:
- OpenRouter — публичный API ``/api/v1/models`` (без ключа), цены из ``pricing``;
- Gemini — список моделей по API-ключу, у всех есть бесплатный tier (с лимитами);
- Ollama — локальный ``localhost:11434/api/tags`` (модели уже скачаны, они бесплатны);
- Anthropic/OpenAI — публичных списков без ключа нет, всегда статический фолбэк.

При ошибке сети или пустом ответе возвращается ``STATIC_MODELS`` — приложение
не зависит от доступности внешних API.
"""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING, Any

from nemus.core.providers.registry import PROVIDER_SPECS
from nemus.utils.logger import logger

if TYPE_CHECKING:
    from nemus.config import Config

#: статический фолбэк: провайдер → [(полное имя модели для конфига, бесплатно?)]
#: имена пишутся так же, как в ``llm.default_model`` — уже с префиксом LiteLLM.
STATIC_MODELS: dict[str, list[tuple[str, bool]]] = {
    "gemini": [
        ("gemini/gemini-2.0-flash", True),
        ("gemini/gemini-2.5-flash", True),
        ("gemini/gemini-2.5-pro", True),
        ("gemini/gemini-3.8-flash", True),
    ],
    "openrouter": [
        ("openrouter/google/gemma-3-27b-it:free", True),
        ("openrouter/deepseek/deepseek-chat-v3-0324:free", True),
        ("openrouter/meta-llama/llama-3.3-70b-instruct:free", True),
        ("openrouter/qwen/qwen3-235b-a22b:free", True),
        ("openrouter/openai/gpt-4o-mini", False),
        ("openrouter/anthropic/claude-3.5-sonnet", False),
        ("openrouter/deepseek/deepseek-chat", False),
    ],
    "anthropic": [
        ("anthropic/claude-3-5-haiku-latest", False),
        ("anthropic/claude-sonnet-4-5", False),
        ("anthropic/claude-opus-4-1", False),
    ],
    "openai": [
        ("gpt-4o-mini", False),
        ("gpt-4o", False),
        ("gpt-4.1-mini", False),
        ("gpt-4.1", False),
        ("o4-mini", False),
    ],
    "ollama": [
        ("ollama/llama3.2", True),
        ("ollama/qwen3", True),
        ("ollama/gemma3", True),
    ],
}


def sort_models(models: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Бесплатные сверху, внутри — по алфавиту; без дублей."""
    seen: set[str] = set()
    unique = [m for m in models if m[0] and not (m[0] in seen or seen.add(m[0]))]
    return sorted(unique, key=lambda m: (not m[1], m[0].lower()))


def _http_json(url: str, timeout: float = 6.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "nemus"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# ------------------------------------------------------------------ парсеры
def parse_openrouter(data: Any) -> list[tuple[str, bool]]:
    """Модели OpenRouter: бесплатные — ``:free`` в имени или нулевая цена."""
    out: list[tuple[str, bool]] = []
    for item in (data or {}).get("data", []) or []:
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        pricing = item.get("pricing") or {}
        try:
            raw_price = pricing.get("prompt")
            prompt_price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            prompt_price = None
        # бесплатная ли: суффикс :free либо явно указанная нулевая цена
        free = mid.endswith(":free") or (prompt_price is not None and prompt_price <= 0)
        out.append((f"openrouter/{mid}", free))
    return out


def parse_gemini(data: Any) -> list[tuple[str, bool]]:
    """Модели Gemini: подходят для чата, все с бесплатным tier (с лимитами)."""
    out: list[tuple[str, bool]] = []
    for item in (data or {}).get("models", []) or []:
        methods = item.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        name = str(item.get("name") or "").removeprefix("models/").strip()
        if name:
            out.append((f"gemini/{name}", True))
    return out


def parse_ollama(data: Any) -> list[tuple[str, bool]]:
    """Локальные модели Ollama — работают офлайн, значит бесплатны."""
    return [
        (f"ollama/{str(item.get('name') or '').strip()}", True)
        for item in (data or {}).get("models", []) or []
        if str(item.get("name") or "").strip()
    ]


# ------------------------------------------------------------------- живой список
def fetch_models(provider: str, cfg: "Config | None" = None) -> list[tuple[str, bool]]:
    """Живой список моделей провайдера. Бросает исключение при недоступности."""
    if provider == "openrouter":
        return parse_openrouter(_http_json("https://openrouter.ai/api/v1/models"))
    if provider == "gemini":
        key = ""
        if cfg is not None:
            pc = cfg.llm.providers.get("gemini")
            key = (pc.api_key if pc else "").strip()
        if not key:
            raise RuntimeError("нет ключа Gemini")
        return parse_gemini(
            _http_json(
                "https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={key}&pageSize=200"
            )
        )
    if provider == "ollama":
        return parse_ollama(_http_json("http://localhost:11434/api/tags", timeout=2.0))
    raise RuntimeError(f"для провайдера {provider} нет публичного списка моделей")


def list_models(
    provider: str, cfg: "Config | None" = None
) -> tuple[list[tuple[str, bool]], bool]:
    """Список ``(имя, бесплатно)`` для провайдера и флаг «живой источник».

    Сначала пробуем API провайдера, при любой ошибке — статический фолбэк.
    """
    if provider not in PROVIDER_SPECS:
        return [], False
    try:
        live = fetch_models(provider, cfg)
        if live:
            return sort_models(live), True
        logger.debug(f"Провайдер {provider} вернул пустой список моделей — фолбэк")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Список моделей {provider} недоступен ({exc}) — статический")
    return sort_models(STATIC_MODELS.get(provider, [])), False


def static_models(provider: str) -> list[tuple[str, bool]]:
    """Статический фолбэк для провайдера (без сети)."""
    return sort_models(STATIC_MODELS.get(provider, []))


__all__ = [
    "STATIC_MODELS",
    "fetch_models",
    "list_models",
    "parse_gemini",
    "parse_openrouter",
    "parse_ollama",
    "sort_models",
    "static_models",
]
