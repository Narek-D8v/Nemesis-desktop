"""Менеджер режима реального времени: выбирает и запускает сессию провайдера."""

from __future__ import annotations

import os
from typing import Any, Callable

from nemus.config import Config
from nemus.core.language import speech_language_code
from nemus.core.permissions import PermissionManager
from nemus.core.providers.registry import PROVIDER_SPECS
from nemus.core.tools.base import Tool
from nemus.core.tools.registry import ToolRegistry
from nemus.utils.logger import logger
from nemus.voice.realtime.base import BaseRealtimeSession, RealtimeCallbacks


class RealtimeManager:
    """Создаёт realtime-сессию по конфигу и управляет её жизненным циклом."""

    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        permissions: PermissionManager,
        callbacks: RealtimeCallbacks,
        confirm: Callable[[Tool, dict[str, Any]], bool] | None = None,
        system_prompt: str = "",
    ) -> None:
        self.config = config
        self.registry = registry
        self.permissions = permissions
        self.cb = callbacks
        self.confirm = confirm
        self.system_prompt = system_prompt
        self.session: BaseRealtimeSession | None = None

    @property
    def provider(self) -> str:
        return self.config.voice.realtime.provider

    @property
    def active(self) -> bool:
        return self.session is not None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        rc = self.config.voice.realtime
        if not rc.enabled:
            self.cb_error("Режим реального времени отключён в настройках.")
            return False

        api_key = self._api_key(rc.provider)
        if not api_key:
            self.cb_error(
                f"Не задан API-ключ для «{rc.provider}». Укажите его в настройках."
            )
            return False

        common = {
            "callbacks": self.cb,
            "registry": self.registry,
            "permissions": self.permissions,
            "confirm": self.confirm,
            "system_prompt": self.system_prompt,
            "barge_in": self.config.voice.barge_in,
            "noise_reducer": self._noise_reducer(),
            "reply_language": getattr(self.config.assistant, "reply_language", "auto"),
        }

        try:
            if rc.provider == "gemini":
                from nemus.voice.realtime.gemini_live import GeminiLiveSession

                self.session = GeminiLiveSession(
                    api_key=api_key,
                    model=rc.gemini_model,
                    audio=rc.audio,
                    voice=rc.gemini_voice,
                    language=self._language_code(),
                    **common,
                )
            else:
                from nemus.voice.realtime.openai_realtime import OpenAIRealtimeSession

                self.session = OpenAIRealtimeSession(
                    api_key=api_key,
                    model=rc.openai_model,
                    voice=rc.openai_voice,
                    audio=rc.audio,
                    temperature=self.config.llm.temperature,
                    **common,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось создать realtime-сессию")
            self.cb_error(str(exc))
            self.session = None
            return False

        logger.info(f"Запуск realtime-сессии: {rc.provider} ({rc.audio=})")
        return self.session.start()

    def stop(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None

    def send_text(self, text: str) -> None:
        if self.session is not None:
            self.session.send_text(text)

    # --------------------------------------------------------------- helpers
    def _language_code(self) -> str:
        """Язык распознавания/озвучки (не язык ответов — тот выбирается отдельно)."""
        return speech_language_code(self.config.assistant.language)

    def _noise_reducer(self):
        from nemus.voice.noise import create_noise_reducer

        return create_noise_reducer(self.config.voice.noise, sample_rate=16000, block_ms=60)

    def _api_key(self, provider: str) -> str:
        pc = self.config.llm.providers.get(provider)
        if pc and pc.api_key:
            return pc.api_key
        spec = PROVIDER_SPECS.get(provider)
        if spec and spec.env_key:
            return os.environ.get(spec.env_key, "")
        return ""

    def cb_error(self, message: str) -> None:
        if self.cb.on_error:
            self.cb.on_error(message)
