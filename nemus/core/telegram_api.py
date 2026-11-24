"""Тонкий адаптер новых методов Telegram Bot API 10.3/10.4.

Установленная python-telegram-bot 22.8 знает Bot API 10.0, поэтому методы
``sendMessageDraft``, ``sendRichMessage`` и другие вызываются
напрямую через ``Bot._post``. Модуль не знает про Немусю: он только собирает
payload по документированному контракту, запоминает заведомо неподдерживаемые
методы и не даёт токену попасть в логи.

Любая ошибка пробрасывается вызывающему: Telegram-бот сам решает, откатиться
ли на старое поведение. Единственное, что адаптер делает сам, — помечает
endpoint недоступным, если сервер явно сказал, что метода нет.
"""

from __future__ import annotations

import re
from typing import Any

from nemus.utils.logger import logger

#: маркеры «метода нет»: только они кэшируются до перезапуска
_UNSUPPORTED_MARKERS = (
    "method not found",
    "unknown method",
    "not supported",
    "unsupported",
)

#: «not found» про объект (чат, файл, сообщение для правки) — временная
#: ошибка, а не отсутствие метода. Telegram пишет "message to edit not found",
#: поэтому между объектом и not found допускаем действие.
_MISSING_OBJECT = re.compile(
    r"\b(?:chat|user|message|file|topic|query)\b[^.]{0,40}\bnot found",
    re.IGNORECASE,
)


class TelegramAdvancedAPI:
    """Raw-вызовы Bot API 10.3/10.4 поверх PTB ``Bot``."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._unavailable: set[str] = set()

    def available(self, endpoint: str) -> bool:
        """Не помечен ли метод как неподдерживаемый этим сервером."""
        return endpoint not in self._unavailable

    # ------------------------------------------------------------ служебное
    def _token(self) -> str:
        token = getattr(self.bot, "token", "") or ""
        return token if isinstance(token, str) else str(token)

    def _safe_error(self, exc: Exception) -> str:
        """Текст ошибки без токена бота."""
        text = str(exc)
        token = self._token()
        return text.replace(token, "***") if token else text

    @staticmethod
    def _is_unsupported(exc: Exception) -> bool:
        """Явно ли сервер сказал, что метода/объекта не существует."""
        text = str(exc).lower()
        if any(marker in text for marker in _UNSUPPORTED_MARKERS):
            return True
        if "not found" in text:
            return _MISSING_OBJECT.search(text) is None
        return False

    @staticmethod
    def _message_id(result: Any) -> int | None:
        if isinstance(result, dict):
            value = result.get("message_id")
            if isinstance(value, int):
                return value
        return None

    async def _call(self, endpoint: str, payload: dict[str, Any]) -> Any:
        """Вызывает endpoint, отбрасывая None и кэшируя «метод не найден»."""
        if not self.available(endpoint):
            return None
        data = {key: value for key, value in payload.items() if value is not None}
        try:
            return await self.bot._post(endpoint, data)
        except Exception as exc:  # noqa: BLE001 — решение принимает вызывающий
            if self._is_unsupported(exc):
                self._unavailable.add(endpoint)
                logger.info(
                    "Telegram: метод %s недоступен (%s) — работаю по старой схеме",
                    endpoint,
                    self._safe_error(exc),
                )
            else:
                logger.debug(
                    "Telegram: %s не выполнен: %s",
                    endpoint,
                    self._safe_error(exc),
                )
            raise

    # -------------------------------------------------------------- методы
    async def send_message_draft(
        self, *, chat_id: int, draft_id: int, text: str
    ) -> bool:
        """Нативный черновик ответа с кнопкой «Стоп» (только личка)."""
        result = await self._call(
            "sendMessageDraft",
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "text": text,
                "can_stop": True,
                "keep_on_stop": True,
            },
        )
        return result is not None and result is not False

    async def send_rich_message(
        self,
        *,
        chat_id: int,
        rich_message: dict[str, Any],
        reply_parameters: dict[str, Any] | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        """Rich-сообщение с заголовками, таблицами и rich-кнопками."""
        result = await self._call(
            "sendRichMessage",
            {
                "chat_id": chat_id,
                "rich_message": rich_message,
                "reply_parameters": reply_parameters,
                "reply_markup": reply_markup,
            },
        )
        return self._message_id(result)

    async def send_ephemeral(
        self,
        *,
        chat_id: int,
        callback_query_id: str,
        html: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        """Сообщение, которое в группе увидит только нажавший кнопку."""
        result = await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": html,
                "parse_mode": "HTML",
                "ephemeral_message_parameters": {
                    "callback_query_id": callback_query_id
                },
                "reply_markup": reply_markup,
            },
        )
        return self._message_id(result)


__all__ = ["TelegramAdvancedAPI"]
