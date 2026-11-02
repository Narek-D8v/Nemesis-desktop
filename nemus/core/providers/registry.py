"""Реестр провайдеров: сопоставление провайдер→ CLI-имена LiteLLM и ключи."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nemus.config import Config


@dataclass(frozen=True)
class ProviderSpec:
    """Описание провайдера для LiteLLM."""

    name: str
    #: префикс модели LiteLLM (например, ``openrouter/`` или ``gemini/``)
    model_prefix: str
    #: имя переменной окружения для ключа (используется как fallback)
    env_key: str
    #: нужен ли api_base
    needs_base: bool = False
    #: человекочитаемое имя
    label: str = ""


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        "openrouter", "openrouter/", "OPENROUTER_API_KEY", True, "OpenRouter"
    ),
    "gemini": ProviderSpec(
        "gemini", "gemini/", "GEMINI_API_KEY", False, "Google Gemini"
    ),
    "anthropic": ProviderSpec(
        "anthropic", "anthropic/", "ANTHROPIC_API_KEY", False, "Anthropic Claude"
    ),
    "openai": ProviderSpec(
        "openai", "openai/", "OPENAI_API_KEY", True, "OpenAI"
    ),
    "ollama": ProviderSpec(
        "ollama", "ollama/", "", True, "Ollama (локально)"
    ),
}


def provider_kwargs(provider: str, cfg: "Config") -> dict[str, Any]:
    """Возвращает kwargs для LiteLLM (api_key, api_base) по имени провайдера."""
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        return {}

    pc = cfg.llm.providers.get(provider)
    kwargs: dict[str, Any] = {}

    if pc and pc.api_key:
        kwargs["api_key"] = pc.api_key
    if spec.needs_base and pc and pc.api_base:
        kwargs["api_base"] = pc.api_base

    return kwargs
