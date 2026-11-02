"""Разовая авторизация Spotify (OAuth). Запустите и войдите в браузере.

Использование:
    .\\venv\\Scripts\\python.exe scripts\\spotify_auth.py

Требуется указать client_id/client_secret в config.yaml или в Настройках GUI.
Токен сохраняется в data/spotify_token.json и используется инструментом spotify.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    sp = cfg.spotify

    from nemus.core.tools.spotify import authorize

    try:
        path = authorize(sp)
    except RuntimeError as exc:
        print(exc)
        if not sp.client_id or not sp.client_secret:
            print(
                "Получить Client ID/Secret: https://developer.spotify.com/dashboard\n"
                "Redirect URI должен быть: " + sp.redirect_uri
            )
        return 1

    print("Успешно! Токен сохранён:", path)
    print("Теперь можно просить: «включи музыку», «поставь трек ...» и т.д.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
