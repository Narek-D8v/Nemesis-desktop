"""Хранилище секретов (API-ключей) в системном keyring.

Если keyring недоступен (например, headless-среда), используется
fallback-файл ``data/.secrets.json`` с ограниченными правами.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from nemus.config import DATA_DIR
from nemus.utils.logger import logger

SERVICE_NAME = "nemus"
_FALLBACK_FILE = DATA_DIR / ".secrets.json"

try:
    import keyring
    from keyring.errors import KeyringError

    _KEYRING_OK = True
except Exception:  # pragma: no cover
    keyring = None  # type: ignore[assignment]
    KeyringError = Exception  # type: ignore[assignment,misc]
    _KEYRING_OK = False


def _load_fallback() -> dict[str, str]:
    if _FALLBACK_FILE.exists():
        try:
            return json.loads(_FALLBACK_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_fallback(data: dict[str, str]) -> None:
    _FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FALLBACK_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _restrict_permissions(_FALLBACK_FILE)


def _restrict_permissions(path: Path) -> None:
    """Ограничивает доступ к файлу секретов (best effort)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - зависит от ФС
        pass


def set_secret(name: str, value: str) -> None:
    """Сохраняет секрет. Пустое значение удаляет запись."""
    if _KEYRING_OK:
        try:
            if value:
                keyring.set_password(SERVICE_NAME, name, value)
            else:
                try:
                    keyring.delete_password(SERVICE_NAME, name)
                except KeyringError:
                    pass
            return
        except Exception as exc:  # pragma: no cover
            logger.warning(f"keyring недоступен, использую fallback: {exc}")

    data = _load_fallback()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    _save_fallback(data)


def get_secret(name: str) -> str | None:
    """Возвращает секрет или ``None``."""
    if _KEYRING_OK:
        try:
            value = keyring.get_password(SERVICE_NAME, name)
            if value:
                return value
        except Exception as exc:  # pragma: no cover
            logger.warning(f"keyring недоступен, читаю fallback: {exc}")
    return _load_fallback().get(name)


def delete_secret(name: str) -> None:
    set_secret(name, "")
