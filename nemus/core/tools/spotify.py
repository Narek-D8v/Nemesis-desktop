"""Инструмент управления Spotify через Web API (spotipy)."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from nemus.core.tools.base import RiskLevel, Tool, ToolResult

SCOPE = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing playlist-read-private user-read-private"
)

#: таймаут HTTP-запросов к Web API Spotify (секунды)
SPOTIFY_HTTP_TIMEOUT = 10

#: сколько треков из «дальше в очереди» возвращаем после включения нового трека
_QUEUE_RESTORE_LIMIT = 15
#: сколько раз ждём подтверждения, что воспроизведение реально началось
_PLAY_VERIFY_TRIES = 4
#: пауза между проверками старта воспроизведения (секунды)
_PLAY_VERIFY_DELAY = 0.25

#: служебные слова голосовых запросов («включи любую песню …») —
#: убираем их перед поиском, иначе Spotify ищет по мусорной фразе
_QUERY_FILLERS = (
    "включи",
    "включить",
    "включай",
    "поставь",
    "запусти",
    "давай",
    "пожалуйста",
    "мне",
    "нам",
    "любую",
    "любая",
    "любой",
    "любые",
    "любого",
    "какую-нибудь",
    "какую нибудь",
    "какой-нибудь",
    "что-нибудь",
    "что нибудь",
    "нибудь",
    "песню",
    "песня",
    "песни",
    "песен",
    "песенку",
    "песенки",
    "трек",
    "трека",
    "треки",
    "треков",
    "композицию",
    "композиция",
    "музыку",
    "музыка",
    "мелодию",
    "исполнителя",
    "исполнитель",
    "исполнительницы",
    "группы",
    "группа",
    "певца",
    "певицы",
    "spotify",
    "спотифай",
    "спотифая",
    "спотифае",
    "спотифайе",
    "из",
)


def _strip_fillers(query: str) -> str:
    """Убирает служебные слова из голосового запроса.

    «включи любую песню алены швец» -> «алены швец». Если после чистки ничего
    не осталось, возвращает исходный запрос.
    """
    q = (query or "").strip()
    if not q:
        return q
    cleaned = q
    for word in _QUERY_FILLERS:
        cleaned = re.sub(
            rf"(?<!\w){re.escape(word)}(?!\w)", " ", cleaned, flags=re.IGNORECASE
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned or q


def _norm_name(text: str) -> str:
    """Нормализация названия для сравнения: регистр, пунктуация, эмодзи."""
    low = (text or "").casefold()
    low = re.sub(r"[^\w\s]+", " ", low)
    return re.sub(r"\s+", " ", low).strip()


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Склонение существительного после числа: 1 трек, 2 трека, 5 треков."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def redirect_uri_problem(uri: str) -> str:
    """Возвращает описание проблемы с Redirect URI или пустую строку.

    Учитывает требования Spotify (2025+): HTTPS либо loopback ``http://127.0.0.1``
    / ``http://[::1]``, «localhost» запрещён, адрес должен совпадать посимвольно.
    """
    uri = (uri or "").strip()
    if not uri:
        return "адрес пустой."
    if "localhost" in uri.lower():
        return (
            "Spotify запрещает «localhost». Используйте 127.0.0.1, например "
            "http://127.0.0.1:8888/callback"
        )
    if uri.startswith("http://") and "127.0.0.1" not in uri and "[::1]" not in uri:
        return "Spotify принимает http:// только для 127.0.0.1 или [::1]."
    if uri.endswith("/"):
        return "уберите завершающий слеш (" + uri.rstrip("/") + ")."
    return ""


_CALLBACK_PAGE = (
    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
    "<title>Немуся</title>"
    "<body style='font-family:sans-serif;text-align:center;padding:3rem'>"
    "<h2>Готово!</h2><p>Вход в Spotify выполнен. "
    "Можно закрыть эту вкладку и вернуться в Немуся.</p></body></html>"
)
_ERROR_PAGE = (
    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
    "<title>Немуся</title>"
    "<body style='font-family:sans-serif;text-align:center;padding:3rem'>"
    "<h2>Ошибка входа</h2><p>Spotify вернул ошибку. "
    "Вернитесь в Немуся и попробуйте снова.</p></body></html>"
)


def _wait_for_callback(
    port: int, state: str, url: str, timeout: float, open_browser: bool
) -> str:
    """Ждёт redirect с кодом авторизации на локальном сервере.

    В отличие от spotipy, ограничивает ожидание таймаутом и корректно
    обрабатывает страницу ошибки Spotify (иначе ``handle_request`` зависает).
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict[str, list[str]] = {}

    class _Server(HTTPServer):
        allow_reuse_address = True

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            if "code" in params or "error" in params:
                result.update(params)
                body = (_ERROR_PAGE if "error" in params else _CALLBACK_PAGE).encode("utf-8")
                self.send_response(200)
            else:
                body = b"<h2>Wait for redirect...</h2>"
                self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # noqa: D102
            pass

    try:
        server = _Server(("127.0.0.1", port), _Handler)
    except OSError as exc:
        raise RuntimeError(
            f"Не удалось запустить локальный сервер на 127.0.0.1:{port} "
            f"(порт занят?). Закройте лишнее окно Немуся и повторите."
        ) from exc
    server.timeout = 0.5
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise RuntimeError(
            "Вход не завершён: истекло время ожидания. Откройте браузер, войдите и "
            "разрешите доступ, либо проверьте Redirect URI и нажмите «Войти» снова."
        )
    if "error" in result:
        raise RuntimeError("Spotify вернул ошибку: " + result["error"][0])
    if result.get("state", [""])[0] != state:
        raise RuntimeError("Проверка state не пройдена. Попробуйте войти ещё раз.")
    code = result.get("code", [""])[0]
    if not code:
        raise RuntimeError("Spotify не вернул код авторизации.")
    return code


def authorize(settings: Any, *, open_browser: bool = True, timeout: float = 120.0) -> str:
    """Выполняет разовый OAuth-вход в Spotify и сохраняет токен.

    Возвращает путь к файлу токена. При ошибке бросает ``RuntimeError`` с
    понятным текстом. Ожидание ограничено ``timeout`` секундами.
    """
    if settings is None or not getattr(settings, "client_id", "") or not getattr(
        settings, "client_secret", ""
    ):
        raise RuntimeError(
            "Укажите Client ID и Client Secret (Настройки → Spotify). Получить: "
            "https://developer.spotify.com/dashboard"
        )
    problem = redirect_uri_problem(getattr(settings, "redirect_uri", ""))
    if problem:
        raise RuntimeError(
            "Redirect URI: "
            + problem
            + " Проверьте, что в приложении на developer.spotify.com добавлен ровно "
            "ваш адрес (Add → Save), с указанием 127.0.0.1 без порта или с тем же портом."
        )
    try:
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("Не установлен пакет spotipy. Выполните: pip install spotipy") from exc

    parsed = urlparse(settings.redirect_uri)
    host = parsed.hostname or ""
    port = parsed.port
    if parsed.scheme != "http" or host not in ("127.0.0.1", "localhost") or not port:
        raise RuntimeError(
            "Для входа через браузер нужен Redirect URI вида "
            "http://127.0.0.1:8888/callback (127.0.0.1 и порт обязательны)."
        )

    Path(settings.cache_path).parent.mkdir(parents=True, exist_ok=True)
    oauth = SpotifyOAuth(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri,
        scope=SCOPE,
        cache_path=settings.cache_path,
        open_browser=False,
    )
    state = secrets.token_urlsafe(16)
    url = oauth.get_authorize_url(state=state)
    try:
        print("Ссылка для входа в Spotify:\n" + url, flush=True)
    except Exception:  # noqa: BLE001
        pass
    code = _wait_for_callback(port, state, url, timeout, open_browser)
    try:
        token = oauth.get_access_token(code=code, as_dict=False, check_cache=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_friendly_auth_error(exc, settings)) from exc
    if not token:
        raise RuntimeError("Не удалось получить токен Spotify.")
    return settings.cache_path


def _friendly_auth_error(exc: Exception, settings: Any) -> str:
    text = str(exc)
    low = text.lower()
    if "invalid_client" in low or "invalid client" in low:
        return "Неверный Client ID или Client Secret."
    if "invalid redirect" in low or "redirect_uri" in low or "insecure" in low:
        return (
            "Spotify отклонил Redirect URI. В приложении на "
            "https://developer.spotify.com/dashboard добавьте в Redirect URIs "
            f"ровно: {settings.redirect_uri}"
        )
    return f"Не удалось авторизоваться в Spotify: {text}"


class SpotifyTool(Tool):
    """Управляет воспроизведением Spotify: play/pause, треки, плейлисты, громкость."""

    name = "spotify"
    description = (
        "Управляет музыкой в Spotify. Действия (action): "
        "'open' — открыть приложение Spotify (не требует входа), "
        "'play' — продолжить воспроизведение, 'pause' — пауза, "
        "'next' — следующий трек, 'previous' — предыдущий, "
        "'current' — что сейчас играет, "
        "'play_track' — найти и включить трек (параметр query: название трека "
        "и/или исполнитель), "
        "'play_artist' — включить песни исполнителя (параметр query: имя; "
        "используй для «включи песню/песни X», «что-нибудь из X», «любую песню X»), "
        "'play_playlist' — включить плейлист (параметр query: название; свои "
        "плейлисты ищутся первыми), "
        "'playlists' — показать плейлисты пользователя (вызывай, если просят "
        "«какие у меня плейлисты» или название плейлиста неизвестно), "
        "'queue_track' — добавить трек в очередь БЕЗ остановки текущего "
        "(параметр query; используй, если просят «добавь в очередь/дальше»), "
        "'volume' — установить громкость в процентах (параметр percent), "
        "'devices' — список устройств, "
        "'set_device' — выбрать устройство по имени (параметр device). "
        "ВАЖНО: действия кроме 'open' требуют настройки Spotify Web API (Client ID/"
        "Secret) и входа; если пользователь просто просит открыть Spotify или "
        "плеер, используй action='open'. Если управление не настроено, а просят "
        "включить музыку — открой приложение (open), а для нажатия play/pause/"
        "next используй инструмент media_control (медиа-клавиши, работают без API)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Действие: open, play, pause, next, previous, current, play_track, "
                    "play_artist, play_playlist, playlists, queue_track, volume, "
                    "devices, set_device."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Название трека, исполнитель или плейлист "
                    "(для play_track/play_artist/play_playlist/queue_track)."
                ),
            },
            "percent": {
                "type": "integer",
                "description": "Громкость 0–100 (для volume).",
            },
            "device": {
                "type": "string",
                "description": "Имя устройства Spotify (для set_device).",
            },
        },
        "required": ["action"],
    }
    risk = RiskLevel.SAFE

    def __init__(self) -> None:
        self.settings: Any = None
        self._client: Any = None
        #: кэш типа аккаунта Spotify: Free не запускает одиночный трек по запросу
        self._free_account: bool | None = None

    # ------------------------------------------------------------------ client
    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        s = self.settings
        if s is None:
            raise RuntimeError("Spotify не сконфигурирован.")
        if not s.client_id or not s.client_secret:
            raise RuntimeError(
                "Spotify не настроен: укажите Client ID и Client Secret в Настройках "
                "(раздел Spotify). Получить: https://developer.spotify.com/dashboard"
            )
        try:
            import spotipy  # noqa: PLC0415
            from spotipy.oauth2 import SpotifyOAuth  # noqa: PLC0415
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "Не установлен пакет spotipy. Выполните: pip install spotipy"
            ) from exc

        Path(s.cache_path).parent.mkdir(parents=True, exist_ok=True)
        auth = SpotifyOAuth(
            client_id=s.client_id,
            client_secret=s.client_secret,
            redirect_uri=s.redirect_uri,
            scope=SCOPE,
            cache_path=s.cache_path,
            open_browser=True,
        )
        # Внутри ассистента нельзя ждать интерактивного входа (это блокирует GUI).
        # Требуем разовую авторизацию через Настройки или scripts/spotify_auth.py.
        if not auth.get_cached_token():
            exists = Path(s.cache_path).exists()
            raise RuntimeError(
                "Spotify ещё не авторизован (нет токена).\n"
                f"Файл токена: {s.cache_path} (существует: {'да' if exists else 'нет'}).\n"
                "Один раз войдите: Настройки → Spotify → «Войти в Spotify» и дождитесь "
                "надписи «Готово», либо в терминале:\n"
                "    venv\\Scripts\\python.exe scripts\\spotify_auth.py\n"
                "Убедитесь, что в браузере вы РАЗРЕШИЛИ доступ приложению и увидели "
                "страницу «Готово!». Для управления воспроизведением нужен Spotify Premium."
            )
        self._client = spotipy.Spotify(
            auth_manager=auth, requests_timeout=SPOTIFY_HTTP_TIMEOUT
        )
        return self._client

    def _active_device_id(self, sp: Any) -> str | None:
        devices = (sp.devices() or {}).get("devices", [])
        if not devices:
            return None
        preferred = (self.settings.default_device or "").strip().lower()
        if preferred:
            for d in devices:
                if preferred in (d.get("name", "").lower()):
                    return d["id"]
        for d in devices:
            if d.get("is_active"):
                return d["id"]
        return devices[0]["id"]

    # ----------------------------------------------------------------- execute
    def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        action = (action or "").strip().lower()
        if not action:
            return ToolResult.failure("Не указано действие (action).")
        # открыть приложение можно без авторизации и настройки API
        if action in ("open", "launch", "открой"):
            return self._open_app()

        s = self.settings
        if s is not None and not s.enabled:
            return ToolResult.failure("Spotify отключён в настройках.")

        try:
            sp = self._get_client()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(str(exc))

        try:
            return self._dispatch(sp, action, kwargs)
        except RuntimeError as exc:
            # наши сообщения уже дружелюбные (например, «Spotify не начал…»)
            return ToolResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(self._friendly_error(exc))

    @staticmethod
    def _open_app() -> ToolResult:
        try:
            if sys.platform.startswith("win"):
                os.startfile("spotify:")  # type: ignore[attr-defined]  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "spotify:"])
            else:
                subprocess.Popen(["xdg-open", "spotify:"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                f"Не удалось открыть Spotify: {exc}. Возможно, приложение не установлено."
            )
        return ToolResult.success("Открываю Spotify.")

    def _dispatch(self, sp: Any, action: str, kwargs: dict[str, Any]) -> ToolResult:
        if action == "current":
            return self._current(sp)
        if action == "devices":
            return self._devices(sp)
        if action == "playlists":
            return self._playlists(sp)
        if action == "set_device":
            return self._set_device(sp, kwargs.get("device", ""))

        device_id = self._active_device_id(sp)
        if device_id is None:
            return ToolResult.failure(
                "Нет доступных устройств Spotify. Откройте приложение Spotify "
                "на компьютере или телефоне и попробуйте снова."
            )

        if action == "play":
            return self._player_call(sp, device_id, "Продолжаю воспроизведение.", sp.start_playback)
        if action == "pause":
            return self._player_call(sp, device_id, "Пауза.", sp.pause_playback)
        if action == "next":
            return self._player_call(sp, device_id, "Следующий трек.", sp.next_track)
        if action == "previous":
            return self._player_call(sp, device_id, "Предыдущий трек.", sp.previous_track)
        if action == "volume":
            percent = int(kwargs.get("percent", 50))
            percent = max(0, min(100, percent))
            return self._player_call(sp, device_id, f"Громкость {percent}%.", sp.volume, percent)
        if action == "play_track":
            return self._play_track(sp, device_id, kwargs.get("query", ""))
        if action == "play_artist":
            return self._play_artist(sp, device_id, kwargs.get("query", ""))
        if action == "queue_track":
            return self._queue_track(sp, device_id, kwargs.get("query", ""))
        if action == "play_playlist":
            return self._play_playlist(sp, device_id, kwargs.get("query", ""))
        return ToolResult.failure(f"Неизвестное действие: {action}")

    # --------------------------------------------------------- устойчивый плеер
    @staticmethod
    def _wake_device(sp: Any, device_id: str) -> None:
        """Пробуждает устройство через transfer_playback (лучшее, что можем сделать)."""
        try:
            sp.transfer_playback(device_id, force_play=False)
        except Exception:  # noqa: BLE001
            pass

    def _player_call(
        self, sp: Any, device_id: str, ok_msg: str, fn: Any, *args: Any
    ) -> ToolResult:
        """Выполняет команду плеера; при NO_ACTIVE_DEVICE будит устройство и повторяет."""
        for attempt in (0, 1):
            if attempt:
                self._wake_device(sp, device_id)
                time.sleep(0.4)
            try:
                fn(*args, device_id=device_id)
                return ToolResult.success(ok_msg)
            except Exception as exc:  # noqa: BLE001
                if attempt == 0 and "NO_ACTIVE_DEVICE" in str(exc):
                    continue
                return ToolResult.failure(self._friendly_error(exc))
        return ToolResult.failure("Не удалось выполнить команду Spotify.")

    def _is_free_account(self, sp: Any) -> bool:
        """True для Free-аккаунта Spotify (результат кэшируется на сессию).

        Free не умеет «включить конкретный трек по запросу» — Spotify принимает
        команду, но воспроизведение не начинается. Запрос ``/me`` нужен один раз.
        """
        if self._free_account is not None:
            return self._free_account
        try:
            me = sp.me() or {}
        except Exception:  # noqa: BLE001
            return False  # не знаем — идём обычным путём
        product = str(me.get("product") or "").strip().lower()
        self._free_account = product == "free"
        return self._free_account

    def _wait_playing(self, sp: Any) -> bool:
        """Дожидается подтверждения, что воспроизведение реально идёт."""
        for _ in range(_PLAY_VERIFY_TRIES):
            try:
                pb = sp.current_playback()
            except Exception:  # noqa: BLE001
                pb = None
            if pb and pb.get("is_playing"):
                return True
            time.sleep(_PLAY_VERIFY_DELAY)
        return False

    def _start_playback(
        self, sp: Any, device_id: str, restore: list[str], **play_kwargs: Any
    ) -> None:
        """Стартует воспроизведение (первая попытка — как есть; если Spotify не
        начал играть — будим устройство через transfer_playback и повторяем).

        Восстановление очереди уходит в фоновый поток: раньше каждый трек
        добавлялся в очередь последовательными HTTP-запросами ДО ответа
        пользователю, из-за чего голосовой ответ задерживался на секунды.
        """
        for attempt in (0, 1):
            if attempt:
                self._wake_device(sp, device_id)
                time.sleep(0.4)
            try:
                sp.start_playback(device_id=device_id, **play_kwargs)
            except Exception as exc:
                if attempt == 0:
                    continue  # вторая попытка — после пробуждения устройства
                raise RuntimeError(self._friendly_error(exc)) from exc
            if self._wait_playing(sp):
                self._restore_queue_async(sp, restore)
                return
            # Spotify принял команду, но играть не начал: на Free-аккаунте так
            # ведёт себя запуск одиночного трека (context_uri работает). Повтор
            # той же команды не поможет — выходим, чтобы caller выбрал фолбэк.
            break
        raise RuntimeError(
            "Spotify не начал воспроизведение. Откройте приложение Spotify на "
            "нужном устройстве и попробуйте снова (также помогает: Настройки → "
            "Spotify → устройство по умолчанию)."
        )

    def _snapshot_queue(self, sp: Any) -> list[str]:
        """URI треков «дальше в очереди» (при сбое — пусто, игру не мешаем)."""
        try:
            data = sp.queue() or {}
        except Exception:  # noqa: BLE001
            return []
        items = data.get("queue") or []
        uris: list[str] = []
        for item in items:
            uri = item.get("uri") if isinstance(item, dict) else None
            if uri:
                uris.append(str(uri))
        return uris[:_QUEUE_RESTORE_LIMIT]

    def _restore_queue_async(self, sp: Any, uris: list[str]) -> None:
        """Возвращает очередь в фоне, чтобы не задерживать ответ пользователю."""
        if not uris:
            return
        threading.Thread(
            target=self._restore_queue,
            args=(sp, uris),
            daemon=True,
            name="spotify-queue",
        ).start()

    def _restore_queue(self, sp: Any, uris: list[str]) -> int:
        """Возвращает треки в очередь после включения нового (порядок сохраняется).

        ``add_to_queue`` добавляет только в конец, поэтому перебираем в исходном
        порядке: первый добавленный станет первым «дальше».
        """
        added = 0
        for uri in uris:
            try:
                sp.add_to_queue(uri)
            except Exception:  # noqa: BLE001
                break  # лимиты API или недоступный трек — не настаиваем
            added += 1
        return added

    # ------------------------------------------------------------------ helpers
    def _current(self, sp: Any) -> ToolResult:
        pb = sp.current_playback()
        if not pb or not pb.get("item"):
            return ToolResult.success("Сейчас ничего не воспроизводится.")
        item = pb["item"]
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
        state = "играет" if pb.get("is_playing") else "на паузе"
        return ToolResult.success(f"Сейчас {state}: «{item.get('name', '')}» — {artists}.")

    def _devices(self, sp: Any) -> ToolResult:
        devices = (sp.devices() or {}).get("devices", [])
        if not devices:
            return ToolResult.success("Устройства Spotify не найдены.")
        names = [
            f"{d.get('name', '?')} ({'активно' if d.get('is_active') else 'доступно'})"
            for d in devices
        ]
        return ToolResult.success("Устройства: " + "; ".join(names) + ".")

    def _set_device(self, sp: Any, name: str) -> ToolResult:
        name = (name or "").strip().lower()
        devices = (sp.devices() or {}).get("devices", [])
        for d in devices:
            if name and name in d.get("name", "").lower():
                sp.transfer_playback(d["id"], force_play=True)
                return ToolResult.success(f"Переключил на «{d.get('name')}».")
        return ToolResult.failure(f"Устройство «{name}» не найдено.")

    def _search_first(self, sp: Any, query: str, kind: str) -> dict[str, Any] | None:
        """Ищет первый доступный результат (пропускает None и без uri).

        Сначала применяет market из настроек, затем повторяет без market —
        Spotify нередко возвращает ``None`` для недоступных в регионе позиций.
        """
        market = (self.settings.market or "").strip() or None
        attempts = ([market, None] if market else [None])
        for m in attempts:
            try:
                res = sp.search(q=query, type=kind, limit=10, market=m)
            except Exception:  # noqa: BLE001
                continue
            items = ((res or {}).get(f"{kind}s") or {}).get("items") or []
            for it in items:
                if it and it.get("uri"):
                    return it
        return None

    @staticmethod
    def _queue_note(restore: list[str]) -> str:
        if not restore:
            return ""
        return (
            " Очередь сохранена: впереди ещё "
            + _plural(len(restore), "трек", "трека", "треков")
            + "."
        )

    def _play_track(self, sp: Any, device_id: str, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Не указано название трека (query).")
        track = self._search_first(sp, query, "track")
        if track is None:
            cleaned = _strip_fillers(query)
            if cleaned != query:
                track = self._search_first(sp, cleaned, "track")
        if track is None:
            # возможно, назвали исполнителя — включим его песни
            return self._play_artist(sp, device_id, query)
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
        # запоминаем «дальше в очереди»: start_playback затирает её, возвращаем
        restore = self._snapshot_queue(sp)
        album = (track.get("album") or {}).get("uri")
        if album and self._is_free_account(sp):
            # Free-аккаунт: одиночный трек по запросу Spotify молча не запускает,
            # поэтому сразу включаем альбом с нужной позиции (лишней секунды
            # ожидания неудачного запуска не тратим).
            self._start_playback(
                sp,
                device_id,
                restore,
                context_uri=album,
                offset={"uri": track["uri"]},
            )
        else:
            try:
                self._start_playback(sp, device_id, restore, uris=[track["uri"]])
            except RuntimeError:
                # Не знали тип аккаунта (или Premium с ограничением) — одиночный
                # трек не стартовал. Включаем альбом/исполнителя этого трека.
                based_on_album = bool(album)
                context = album
                if not context:
                    for artist in track.get("artists") or []:
                        context = artist.get("uri")
                        break
                if not context:
                    raise
                extra: dict[str, Any] = {}
                if based_on_album:
                    extra["offset"] = {"uri": track["uri"]}
                self._start_playback(
                    sp, device_id, restore, context_uri=context, **extra
                )
        message = f"Включаю «{track.get('name')}» — {artists}."
        message += self._queue_note(restore)
        return ToolResult.success(message)

    def _play_artist(self, sp: Any, device_id: str, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Не указан исполнитель (query).")
        artist = self._search_first(sp, query, "artist")
        if artist is None:
            cleaned = _strip_fillers(query)
            if cleaned != query:
                artist = self._search_first(sp, cleaned, "artist")
        if artist is None:
            return ToolResult.failure(f"Исполнитель «{query}» не найден.")
        restore = self._snapshot_queue(sp)
        self._start_playback(sp, device_id, restore, context_uri=artist["uri"])
        message = f"Включаю песни исполнителя «{artist.get('name')}»."
        message += self._queue_note(restore)
        return ToolResult.success(message)

    def _queue_track(self, sp: Any, device_id: str, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Не указано название трека (query).")
        track = self._search_first(sp, query, "track")
        if not track:
            return ToolResult.failure(f"Трек «{query}» не найден.")
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
        try:
            sp.add_to_queue(track["uri"], device_id=device_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(self._friendly_error(exc))
        return ToolResult.success(f"Добавил в очередь: «{track.get('name')}» — {artists}.")

    def _user_playlists(self, sp: Any) -> list[dict[str, Any]]:
        """Плейлисты пользователя (включая приватные); при сбое — пусто."""
        try:
            data = sp.current_user_playlists(limit=50) or {}
        except Exception:  # noqa: BLE001
            return []
        return [p for p in (data.get("items") or []) if p and p.get("name")]

    def _playlists(self, sp: Any) -> ToolResult:
        items = self._user_playlists(sp)
        if not items:
            return ToolResult.success("В вашей медиатеке Spotify плейлистов не найдено.")
        names = [str(p.get("name", "")).strip() for p in items]
        return ToolResult.success("Ваши плейлисты: " + "; ".join(names) + ".")

    def _find_user_playlist(self, sp: Any, query: str) -> dict[str, Any] | None:
        """Ищет свой плейлист по названию (точное, подстрока в обе стороны)."""
        q = _norm_name(query)
        if not q:
            return None
        partial = None
        for playlist in self._user_playlists(sp):
            name = _norm_name(str(playlist.get("name", "")))
            if not name:
                continue
            if name == q:
                return playlist
            if partial is None and (q in name or (len(name) >= 3 and name in q)):
                partial = playlist
        return partial

    def _play_playlist(self, sp: Any, device_id: str, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Не указано название плейлиста (query).")
        # свои плейлисты (в т.ч. приватные) каталожным поиском не находятся
        playlist = self._find_user_playlist(sp, query)
        if playlist is None:
            playlist = self._search_first(sp, query, "playlist")
        if playlist is None:
            cleaned = _strip_fillers(query)
            if cleaned != query:
                playlist = self._search_first(sp, cleaned, "playlist")
        if playlist is None:
            names = [str(p.get("name", "")) for p in self._user_playlists(sp)][:8]
            hint = (" Ваши плейлисты: " + "; ".join(names) + ".") if names else ""
            return ToolResult.failure(f"Плейлист «{query}» не найден.{hint}")
        restore = self._snapshot_queue(sp)
        self._start_playback(
            sp, device_id, restore, context_uri=playlist["uri"]
        )
        message = f"Включаю плейлист «{playlist.get('name')}»."
        message += self._queue_note(restore)
        return ToolResult.success(message)

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        name = type(exc).__name__
        text = str(exc)
        low = text.lower()
        if "restriction violated" in low:
            return (
                "Spotify запрещает это действие: на Free-аккаунте нельзя включать "
                "конкретный трек — только плейлист, альбом или исполнителя."
            )
        if "premium_required" in low or "premium required" in low:
            return "Для управления воспроизведением нужен аккаунт Spotify Premium."
        if "no_active_device" in low:
            return "Нет активного устройства Spotify. Откройте Spotify и включите музыку."
        if "player command failed" in low:
            return f"Spotify отклонил команду: {text}"
        if "403" in text:
            return (
                "Spotify отклонил запрос (403). С 2026 года Web API требует Spotify "
                "Premium у владельца приложения; также проверьте, что ваш аккаунт "
                "добавлен в User Management приложения."
            )
        if "429" in text:
            return "Слишком много запросов к Spotify — повторите через минуту."
        if "Restriction violated" in text:
            return "Spotify запрещает это действие для текущего трека (режим воспроизведения)."
        if "404" in text:
            return (
                "Команда отклонена (404): воспроизведение недоступно — проверьте, "
                "что приложение Spotify открыто на нужном устройстве."
            )
        return f"Ошибка Spotify ({name}): {text}"


def spotify_tools() -> list[Tool]:
    return [SpotifyTool()]
