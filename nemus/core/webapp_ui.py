"""Telegram Mini App: отдаёт редактируемую HTML-страницу.

Дизайн и JavaScript лежат в ``nemus/core/webapp/index.html``. Файл читается
при каждом запросе, поэтому правки видны после перезагрузки страницы без
перезапуска бота. Пользовательских данных в нём нет: настройки приходят из
``/api/settings`` после проверки подписи Telegram.
"""

from __future__ import annotations

from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().with_name("webapp")
INDEX_PATH = WEBAPP_DIR / "index.html"


def index_html() -> str:
    """Текущее содержимое страницы Mini App."""
    return INDEX_PATH.read_text(encoding="utf-8")
