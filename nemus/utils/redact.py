"""Удаление секретов из текста (сообщений об ошибках, логов, GUI).

Ошибки сетевых библиотек (``websockets``, ``httpx``) иногда содержат URL и
заголовки целиком, включая ``?key=...`` или ``Bearer ...``. Такие строки
не должны попадать ни в лог, ни в интерфейс.
"""

from __future__ import annotations

import re

#: значения query-параметров с ключами/токенами
_QUERY_SECRET = re.compile(
    r"([?&](?:key|api_key|apikey|access_token|token|client_secret)=)([^&\s'\"<>]+)",
    re.IGNORECASE,
)
#: заголовки вида ``Authorization: Bearer <token>`` / ``x-goog-api-key: <key>``
_HEADER_SECRET = re.compile(
    r"((?:authorization|x-goog-api-key|api-key|x-api-key)\s*[:=]\s*)"
    r"(?:bearer\s+)?([^\s'\"<>]+)",
    re.IGNORECASE,
)
#: известные форматы ключей провайдеров
_PROVIDER_KEY = re.compile(
    r"(AIza[0-9A-Za-z_\-]{10,}|sk-[A-Za-z0-9_\-]{10,}|"
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{12,})"
)

_MASK = "***"


def redact_secrets(text: str) -> str:
    """Возвращает текст, в котором секреты заменены на ``***``."""
    if not text:
        return text
    text = _QUERY_SECRET.sub(r"\1" + _MASK, text)
    text = _HEADER_SECRET.sub(r"\1" + _MASK, text)
    text = _PROVIDER_KEY.sub(_MASK, text)
    return text


__all__ = ["redact_secrets"]
