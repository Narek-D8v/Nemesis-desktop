"""Списки моделей по провайдерам с пометкой «бесплатно/платно».

Для GUI (смена провайдера в настройках): модель не нужно помнить наизусть —
комбobox показывает известные модели выбранного провайдера, бесплатные сверху.

Источники:
- OpenRouter — публичный API ``/api/v1/models`` (без ключа), цены из ``pricing``;
  бесплатными считаются только модели с нулевой ценой и входа, и выхода;
- Gemini — список моделей по API-ключу; бесплатный tier есть у Flash и Gemma,
  Pro-модели платные (поэтому не выдаём их за бесплатные);
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
        ("gemini/gemini-3.5-flash-lite", True),
        ("gemini/gemini-3.5-flash", True),
        ("gemini/gemini-3.1-flash-lite", True),
        ("gemini/gemini-3.1-pro-preview", False),
        ("gemini/gemini-flash-latest", True),
        ("gemini/gemini-pro-latest", False),
    ],
    "openrouter": [
        ("openrouter/google/gemma-4-31b-it:free", True),
        ("openrouter/google/gemma-4-26b-a4b-it:free", True),
        ("openrouter/nvidia/nemotron-3.5-super-vl:free", True),
        ("openrouter/cohere/north-mini-code:free", True),
        ("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", True),
        ("openrouter/anthropic/claude-sonnet-4.6", False),
        ("openrouter/openai/gpt-5-mini", False),
        ("openrouter/deepseek/deepseek-chat", False),
        ("openrouter/qwen/qwen3.7-plus", False),
        ("openrouter/meta-llama/llama-3.3-70b-instruct", False),
    ],
    "anthropic": [
        ("anthropic/claude-haiku-4-5", False),
        ("anthropic/claude-sonnet-4-5", False),
        ("anthropic/claude-opus-4-1", False),
    ],
    "openai": [
        ("gpt-5-mini", False),
        ("gpt-5.4-mini", False),
        ("gpt-5.4", False),
        ("gpt-5-pro", False),
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
#: модели Gemini, которые не годятся для чата/агента (картинки, звук, OCR)
_GEMINI_SKIP = (
    "image",
    "tts",
    "audio",
    "transcribe",
    "live",
    "robotics",
    "computer-use",
    "deep-research",
    "antigravity",
    "lyria",
    "nano-banana",
    "embedding",
)


def _price(pricing: dict[str, Any], key: str) -> float | None:
    """Цена за токен как число; None — провайдер не сказал."""
    raw = pricing.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_openrouter(data: Any) -> list[tuple[str, bool]]:
    """Модели OpenRouter: только текстовые, бесплатные — с нулевой ценой.

    Раньше бесплатными считались любые модели с ``prompt <= 0``, поэтому
    роутеры вида ``openrouter/auto`` с ценой ``-1`` (цена неизвестна) и
    музыкальные модели попадали в «бесплатные» и тратили деньги. Теперь
    нужна нулевая цена и входа, и выхода, плюс текстовый вывод.
    """
    out: list[tuple[str, bool]] = []
    for item in (data or {}).get("data", []) or []:
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        arch = item.get("architecture") or {}
        outputs = arch.get("output_modalities") or []
        if outputs and "text" not in outputs:
            continue  # картинки/музыка/аудио — не для чата
        if "audio" in outputs or any(
            marker in mid.lower() for marker in ("lyria", "music", "tts")
        ):
            continue  # музыкальные/озвучивающие модели не умеют быть агентом
        params = item.get("supported_parameters") or []
        if params and "tools" not in params:
            continue  # без tool-calling агент на такой модели не работает
        pricing = item.get("pricing") or {}
        prompt_price = _price(pricing, "prompt")
        completion_price = _price(pricing, "completion")
        free = (
            prompt_price == 0
            and completion_price == 0
            and not mid.startswith("openrouter/")
        )
        full = mid if mid.startswith("openrouter/") else f"openrouter/{mid}"
        out.append((full, free))
    return out


def parse_gemini(data: Any) -> list[tuple[str, bool]]:
    """Модели Gemini для чата; бесплатными считаем Flash и Gemma.

    Google даёт бесплатный tier не всем моделям: Pro-модели платные, а
    помечать их «бесплатно» — прямой путь к неожиданным лимитам и счетам.
    """
    out: list[tuple[str, bool]] = []
    for item in (data or {}).get("models", []) or []:
        methods = item.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        name = str(item.get("name") or "").removeprefix("models/").strip()
        if not name or name.startswith("models/"):
            continue
        lowered = name.lower()
        if any(marker in lowered for marker in _GEMINI_SKIP):
            continue
        free = "flash" in lowered or lowered.startswith(("gemma", "gemini-flash"))
        out.append((f"gemini/{name}", free))
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
