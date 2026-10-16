"""Инструмент управления Spotify через Web API (spotipy)."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
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
        "'play_track' — найти и включить трек (нужен параметр query), "
        "'play_playlist' — включить плейлист (параметр query), "
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
                    "play_playlist, volume, devices, set_device."
                ),
            },
            "query": {
                "type": "string",
                "description": "Название трека или плейлиста (для play_track/play_playlist).",
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
        self._client = spotipy.Spotify(auth_manager=auth, requests_timeout=10)
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
        if action == "set_device":
            return self._set_device(sp, kwargs.get("device", ""))

        device_id = self._active_device_id(sp)
        if device_id is None:
            return ToolResult.failure(
                "Нет доступных устройств Spotify. Откройте приложение Spotify "
                "на компьютере или телефоне и попробуйте снова."
            )

        if action == "play":
            sp.start_playback(device_id=device_id)
            return ToolResult.success("Продолжаю воспроизведение.")
        if action == "pause":
            sp.pause_playback(device_id=device_id)
            return ToolResult.success("Пауза.")
        if action == "next":
            sp.next_track(device_id=device_id)
            return ToolResult.success("Следующий трек.")
        if action == "previous":
            sp.previous_track(device_id=device_id)
            return ToolResult.success("Предыдущий трек.")
        if action == "volume":
            percent = int(kwargs.get("percent", 50))
            percent = max(0, min(100, percent))
            sp.volume(percent, device_id=device_id)
            return ToolResult.success(f"Громкость {percent}%.")
        if action == "play_track":
            return self._play_track(sp, device_id, kwargs.get("query", ""))
        if action == "play_playlist":
            return self._play_playlist(sp, device_id, kwargs.get("query", ""))
        return ToolResult.failure(f"Неизвестное действие: {action}")

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

    def _play_track(self, sp: Any, device_id: str, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Не указано название трека (query).")
        track = self._search_first(sp, query, "track")
        if not track:
            return ToolResult.failure(f"Трек «{query}» не найден.")
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
        sp.start_playback(device_id=device_id, uris=[track["uri"]])
        return ToolResult.success(f"Включаю «{track.get('name')}» — {artists}.")

    def _play_playlist(self, sp: Any, device_id: str, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Не указано название плейлиста (query).")
        playlist = self._search_first(sp, query, "playlist")
        if not playlist:
            return ToolResult.failure(f"Плейлист «{query}» не найден.")
        sp.start_playback(device_id=device_id, context_uri=playlist["uri"])
        return ToolResult.success(f"Включаю плейлист «{playlist.get('name')}».")

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        name = type(exc).__name__
        text = str(exc)
        if "PREMIUM_REQUIRED" in text or "player command failed" in text.lower():
            return "Для управления воспроизведением нужен аккаунт Spotify Premium."
        if "NO_ACTIVE_DEVICE" in text:
            return "Нет активного устройства Spotify. Откройте Spotify и включите музыку."
        if "403" in text:
            return (
                "Spotify отклонил запрос (403). С 2026 года Web API требует Spotify "
                "Premium у владельца приложения; также проверьте, что ваш аккаунт "
                "добавлен в User Management приложения."
            )
        return f"Ошибка Spotify ({name}): {text}"


def spotify_tools() -> list[Tool]:
    return [SpotifyTool()]
