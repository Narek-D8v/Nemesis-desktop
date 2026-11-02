"""Офлайн-тесты SpotifyTool (без сети): очередь, устройство, поиск, OAuth.

Две части: новые проверки устойчивого плеера (очередь не пропадает, устройство
«пробуждается») и прежний набор (схема, ошибки, поиск с market, authorize,
регистрация в ядре). Запуск:  venv\\Scripts\\python.exe scripts\\test_spotify.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import SpotifyConfig  # noqa: E402
from nemus.core.tools import spotify as sp_mod  # noqa: E402
from nemus.core.tools.base import RiskLevel, ToolResult  # noqa: E402
from nemus.core.tools.spotify import SpotifyTool, _plural, spotify_tools  # noqa: E402

NO_DEVICE_EXC = RuntimeError(
    "http status: 404, code:-1, error: Player command failed: "
    "No active device found (NO_ACTIVE_DEVICE)"
)


class FakeSp:
    """Мини-подделка spotipy.Spotify для проверки логики плеера."""

    def __init__(
        self,
        devices: list[dict] | None = None,
        queue_uris: list[str] | None = None,
        plays: bool = True,
        fail_start_times: int = 0,
        plays_single_tracks: bool = True,
        playlists: list[dict] | None = None,
        product: str = "premium",
    ) -> None:
        self._devices = (
            devices
            if devices is not None
            else [{"id": "dev1", "name": "PC", "is_active": True}]
        )
        self._queue_uris = list(queue_uris or [])
        self._plays = plays
        self._fail_start_times = fail_start_times
        self._plays_single = plays_single_tracks
        self._playlists = list(playlists or [])
        self._product = product
        self.calls: list[tuple] = []
        self.started_kwargs: dict | None = None

    def me(self) -> dict:
        return {"product": self._product}

    # --- состояние
    def devices(self) -> dict:
        return {"devices": self._devices}

    def queue(self) -> dict:
        return {
            "currently_playing": {"uri": "spotify:track:CURRENT"},
            "queue": [{"uri": u} for u in self._queue_uris],
        }

    def current_playback(self) -> dict | None:
        if not self._plays or self.started_kwargs is None:
            return None
        return {"is_playing": True, "item": {"name": "Трек"}}

    def current_user_playlists(self, limit: int = 50) -> dict:  # noqa: ARG002
        return {"items": self._playlists}

    def search(self, q=None, type=None, limit=None, market=None):  # noqa: A002, ARG002
        if type == "track":
            return {
                "tracks": {
                    "items": [
                        {
                            "uri": "spotify:track:T1",
                            "name": "Первое свидание",
                            "artists": [{"name": "Алёна Швец", "uri": "spotify:artist:A1"}],
                            "album": {"uri": "spotify:album:AL1"},
                        }
                    ]
                }
            }
        if type == "artist":
            return {
                "artists": {
                    "items": [{"uri": "spotify:artist:A1", "name": "Алёна Швец"}]
                }
            }
        return {"playlists": {"items": [{"uri": "spotify:playlist:P1", "name": "Плей"}]}}

    # --- команды плеера
    def start_playback(
        self,
        device_id=None,
        uris=None,
        context_uri=None,
        offset=None,
    ) -> None:
        self.calls.append(("start", device_id, tuple(uris or ()), context_uri, offset))
        if self._fail_start_times > 0:
            self._fail_start_times -= 1
            raise NO_DEVICE_EXC
        if uris and not self._plays_single:
            # Free-аккаунт: одиночный трек молча не стартует
            self.started_kwargs = None
            return
        self.started_kwargs = {
            "device_id": device_id,
            "uris": uris,
            "context_uri": context_uri,
            "offset": offset,
        }
        # start_playback в API затирает очередь — это и проверяем
        self._queue_uris = []

    def add_to_queue(self, uri, device_id=None):
        self.calls.append(("add", uri, device_id))
        self._queue_uris.append(uri)

    def transfer_playback(self, device_id, force_play=False):
        self.calls.append(("transfer", device_id, force_play))

    def pause_playback(self, device_id=None):
        self.calls.append(("pause", device_id))


def _tool(fake: FakeSp | None = None) -> SpotifyTool:
    tool = SpotifyTool()
    tool.settings = SpotifyConfig(enabled=True, client_id="id", client_secret="sec")
    tool._client = fake if fake is not None else FakeSp()
    return tool


# ============================================================ устойчивый плеер
def _wait_until(pred, timeout: float = 3.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_play_track_keeps_queue() -> None:
    """Очередь «дальше» переживает start_playback(uris=…) и сохраняет порядок."""
    fake = FakeSp(queue_uris=["spotify:track:A", "spotify:track:B", "spotify:track:C"])
    result = _tool(fake).execute(action="play_track", query="Первое свидание")
    assert result.ok, result.content
    assert fake.started_kwargs["uris"] == ["spotify:track:T1"]
    # очередь восстанавливается в фоне — дожидаемся
    expected = ["spotify:track:A", "spotify:track:B", "spotify:track:C"]
    assert _wait_until(lambda: fake._queue_uris == expected), fake._queue_uris
    assert "Очередь сохранена" in result.content and "3 трека" in result.content
    print("  play_track сохраняет очередь:", result.content)


def test_play_track_wakes_device() -> None:
    """NO_ACTIVE_DEVICE → transfer_playback и повторная попытка."""
    fake = FakeSp(fail_start_times=1)
    result = _tool(fake).execute(action="play_track", query="Первое свидание")
    assert result.ok, result.content
    assert ("transfer", "dev1", False) in fake.calls
    starts = [c for c in fake.calls if c[0] == "start"]
    assert len(starts) == 2
    print("  устройство пробуждено и повтор:", result.content)


def test_play_track_honest_failure() -> None:
    """Если Spotify так и не начал играть — понятная ошибка, не «успех»."""
    fake = FakeSp(plays=False)
    result = _tool(fake).execute(action="play_track", query="Первое свидание")
    assert not result.ok
    assert "не начал воспроизведение" in result.content
    print("  честный отказ:", result.content)


def test_queue_track_no_interrupt() -> None:
    """queue_track добавляет в очередь и не трогает воспроизведение."""
    fake = FakeSp(queue_uris=["spotify:track:A"])
    result = _tool(fake).execute(action="queue_track", query="Первое свидание")
    assert result.ok, result.content
    assert not any(c[0] == "start" for c in fake.calls)
    assert fake._queue_uris == ["spotify:track:A", "spotify:track:T1"]
    print("  queue_track без остановки:", result.content)


def test_no_devices() -> None:
    """Нет устройств — внятная подсказка, а не traceback."""
    fake = FakeSp(devices=[])
    result = _tool(fake).execute(action="play_track", query="x")
    assert not result.ok
    assert "Нет доступных устройств" in result.content
    print("  нет устройств:", result.content)


def test_pause_wakes_device() -> None:
    """Обычные команды тоже переживают NO_ACTIVE_DEVICE."""
    fake = FakeSp()
    original = fake.pause_playback
    state = {"failed": False}

    def flaky_pause(device_id=None):
        if not state["failed"]:
            state["failed"] = True
            raise NO_DEVICE_EXC
        original(device_id=device_id)

    fake.pause_playback = flaky_pause
    result = _tool(fake).execute(action="pause")
    assert result.ok, result.content
    assert ("transfer", "dev1", False) in fake.calls
    print("  pause с пробуждением:", result.content)


def test_friendly_errors() -> None:
    err = SpotifyTool._friendly_error
    assert "повторите" in err(RuntimeError("http status: 429, error: Too Many"))
    assert "Premium" in err(RuntimeError("403 Forbidden PREMIUM_REQUIRED"))
    assert "запрещает" in err(RuntimeError("Restriction violated"))
    assert "404" in err(RuntimeError("http status: 404"))
    print("  дружелюбные ошибки: 429/403/restriction/404 — ок")


def test_plural() -> None:
    assert _plural(1, "трек", "трека", "треков") == "1 трек"
    assert _plural(2, "трек", "трека", "треков") == "2 трека"
    assert _plural(5, "трек", "трека", "треков") == "5 треков"
    assert _plural(11, "трек", "трека", "треков") == "11 треков"
    assert _plural(21, "трек", "трека", "треков") == "21 трек"
    print("  склонение: ок")


def test_playlists_lists_own() -> None:
    """«Какие у меня плейлисты» — показываем свои, включая приватные."""
    fake = FakeSp(
        playlists=[
            {"name": "Чисто для души", "uri": "spotify:playlist:1"},
            {"name": "🥶", "uri": "spotify:playlist:2"},
        ]
    )
    result = _tool(fake).execute(action="playlists")
    assert result.ok and "Чисто для души" in result.content, result.content
    assert not any(c[0] == "start" for c in fake.calls)
    print("  playlists:", result.content)


def test_play_playlist_prefers_own() -> None:
    """Свой (приватный) плейлист находится по названию без каталожного поиска."""
    own = {"name": "Чисто для души", "uri": "spotify:playlist:OWN"}
    fake = FakeSp(playlists=[own])
    result = _tool(fake).execute(action="play_playlist", query="чисто для души")
    assert result.ok, result.content
    assert fake.started_kwargs["context_uri"] == "spotify:playlist:OWN"
    print("  свой плейлист:", result.content)


def test_play_playlist_unknown_hints() -> None:
    fake = FakeSp(playlists=[{"name": "Мой плей", "uri": "spotify:playlist:1"}])

    class _NoResults(FakeSp):
        def search(self, q=None, type=None, limit=None, market=None):  # noqa: A002, ARG002
            return {("tracks" if type == "track" else f"{type}s"): {"items": []}}

    tool = _tool(_NoResults(playlists=fake._playlists))
    result = tool.execute(action="play_playlist", query="неизвестный")
    assert not result.ok and "не найден" in result.content, result.content
    assert "Мой плей" in result.content, result.content
    print("  неизвестный плейлист + подсказка:", result.content)


def test_play_artist_context() -> None:
    """«Любая песня исполнителя X» — включаем контекст исполнителя."""
    fake = FakeSp()
    result = _tool(fake).execute(action="play_artist", query="алёна швец")
    assert result.ok, result.content
    assert fake.started_kwargs["context_uri"] == "spotify:artist:A1"
    assert fake.started_kwargs["uris"] is None
    print("  play_artist:", result.content)


def test_play_track_free_fallback_album() -> None:
    """Free-аккаунт не запускает одиночный трек — фолбэк на альбом с offset."""
    fake = FakeSp(plays_single_tracks=False)
    result = _tool(fake).execute(action="play_track", query="первое свидание")
    assert result.ok, result.content
    starts = [c for c in fake.calls if c[0] == "start"]
    assert len(starts) == 2, starts
    assert starts[1][3] == "spotify:album:AL1", starts[1]
    assert starts[1][4] == {"uri": "spotify:track:T1"}, starts[1]
    print("  Free: трек -> альбом с offset:", result.content)


def test_play_track_free_fast_path() -> None:
    """Известен Free-аккаунт — сразу альбом, без неудачной попытки трека."""
    fake = FakeSp(product="free")
    result = _tool(fake).execute(action="play_track", query="первое свидание")
    assert result.ok, result.content
    starts = [c for c in fake.calls if c[0] == "start"]
    assert len(starts) == 1, starts
    assert starts[0][3] == "spotify:album:AL1", starts[0]
    assert starts[0][4] == {"uri": "spotify:track:T1"}, starts[0]
    print("  Free (известен): сразу альбом с offset:", result.content)


def test_play_track_falls_back_to_artist() -> None:
    """Трек не найден, но это имя исполнителя — включаем исполнителя."""

    class _ArtistOnly(FakeSp):
        def search(self, q=None, type=None, limit=None, market=None):  # noqa: A002, ARG002
            if type == "track":
                return {"tracks": {"items": []}}
            return super().search(q=q, type=type, limit=limit, market=market)

    fake = _ArtistOnly()
    result = _tool(fake).execute(action="play_track", query="алёна швец")
    assert result.ok, result.content
    assert fake.started_kwargs["context_uri"] == "spotify:artist:A1"
    print("  play_track -> исполнитель:", result.content)


def test_strip_fillers() -> None:
    assert sp_mod._strip_fillers("включи любую песню алены швец") == "алены швец"
    assert sp_mod._strip_fillers("поставь трек") == "поставь трек"
    print("  фильтры запроса: ок")


# ==================================================== прежний набор проверок
class FakeSpotify:
    """Старый фейк: команды записываются, воспроизведение всегда идёт."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._devices = [{"id": "dev1", "name": "Мой компьютер", "is_active": True}]

    def devices(self):
        return {"devices": self._devices}

    def start_playback(self, **kw):
        self.calls.append(("start_playback", kw))

    def pause_playback(self, **kw):
        self.calls.append(("pause_playback", kw))

    def next_track(self, **kw):
        self.calls.append(("next_track", kw))

    def previous_track(self, **kw):
        self.calls.append(("previous_track", kw))

    def volume(self, percent, **kw):
        self.calls.append(("volume", percent, kw))

    def transfer_playback(self, device_id, force_play=False):
        self.calls.append(("transfer_playback", device_id))

    def current_playback(self):
        return {
            "is_playing": True,
            "item": {
                "name": "Test Song",
                "artists": [{"name": "Test Artist"}],
                "uri": "spotify:track:x",
            },
        }

    def search(self, q="", type="track", limit=1, market="RU"):  # noqa: A002
        if type == "track":
            return {
                "tracks": {
                    "items": [
                        {
                            "name": "Найденный трек",
                            "artists": [{"name": "Исполнитель"}],
                            "uri": "spotify:track:y",
                        }
                    ]
                }
            }
        return {"playlists": {"items": [{"name": "Плейлист", "uri": "spotify:playlist:p"}]}}


def _legacy_suite() -> None:
    # 1. схема / риск
    tools = spotify_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "spotify"
    assert tool.risk == RiskLevel.SAFE
    sch = tool.schema()
    assert sch["function"]["name"] == "spotify"
    assert "action" in sch["function"]["parameters"]["properties"]
    print("OK schema/risk")

    # 2. не настроен
    t = SpotifyTool()
    t.settings = SpotifyConfig(enabled=True, client_id="", client_secret="")
    r = t.execute(action="pause")
    assert not r.ok and "Настройк" in r.content, r.content
    print("OK not-configured:", r.content)

    # 3. выключен
    t = SpotifyTool()
    t.settings = SpotifyConfig(enabled=False, client_id="a", client_secret="b")
    r = t.execute(action="play")
    assert not r.ok and "отключён" in r.content, r.content
    print("OK disabled")

    # 4. действия
    tool = SpotifyTool()
    tool.settings = SpotifyConfig(enabled=True, client_id="id", client_secret="sec")
    fake = FakeSpotify()
    tool._client = fake
    assert tool.execute(action="pause").ok
    assert tool.execute(action="next").ok
    assert tool.execute(action="previous").ok
    assert tool.execute(action="volume", percent=70).ok
    assert ("volume", 70, {"device_id": "dev1"}) in fake.calls
    r = tool.execute(action="current")
    assert r.ok and "Test Song" in r.content, r.content
    r = tool.execute(action="devices")
    assert r.ok and "Мой компьютер" in r.content, r.content
    r = tool.execute(action="set_device", device="мой")
    assert r.ok, r.content
    print("OK play/pause/next/prev/volume/current/devices/set_device")

    # 5. поиск и включение
    r = tool.execute(action="play_track", query="что-то")
    assert r.ok and "Найденный трек" in r.content, r.content
    assert any(c[0] == "start_playback" for c in fake.calls)
    r = tool.execute(action="play_playlist", query="ноктюрн")
    assert r.ok and "Плейлист" in r.content, r.content
    r = tool.execute(action="play_track", query="")
    assert not r.ok, "должен быть отказ без query"
    print("OK search play track/playlist + empty query")

    # 5b. поиск пропускает None (недоступные в регионе) и повторяет без market
    class _Noney:
        def __init__(self, valid_second: bool) -> None:
            self.calls = []
            self.markets = []
            self._valid = valid_second
            self._devices = [{"id": "dev1", "name": "PC", "is_active": True}]

        def devices(self):
            return {"devices": self._devices}

        def start_playback(self, **kw):
            self.calls.append(("start_playback", kw))

        def current_playback(self):
            return {"is_playing": True, "item": {"name": "Норм"}}

        def search(self, q="", type="track", limit=1, market=None):  # noqa: A002
            self.markets.append(market)
            key = "tracks" if type == "track" else "playlists"
            if market == "RU":
                return {key: {"items": [None]}}
            items = [None]
            if self._valid:
                items.append({"name": "Норм", "uri": "spotify:playlist:ok", "artists": []})
            return {key: {"items": items}}

    t2 = SpotifyTool()
    t2.settings = SpotifyConfig(enabled=True, client_id="a", client_secret="b", market="RU")
    fake2 = _Noney(valid_second=True)
    t2._client = fake2
    r = t2.execute(action="play_playlist", query="X")
    assert r.ok and "Норм" in r.content, r.content
    assert fake2.markets == ["RU", None], fake2.markets
    assert any(c[0] == "start_playback" for c in fake2.calls)

    fake3 = _Noney(valid_second=False)
    t3 = SpotifyTool()
    t3.settings = SpotifyConfig(enabled=True, client_id="a", client_secret="b", market="RU")
    t3._client = fake3
    r = t3.execute(action="play_playlist", query="X")
    assert not r.ok and "не найден" in r.content, r.content
    print("OK поиск: пропуск None + retry без market + отказ при пустом")

    # 6. неизвестное действие
    r = tool.execute(action="dance")
    assert not r.ok and "Неизвестное" in r.content, r.content
    r = tool.execute(action="")
    assert not r.ok
    print("OK unknown/empty action")

    # 6b. action='open' работает даже без настройки API
    opened = {"n": 0}

    def fake_open():
        opened["n"] += 1
        return ToolResult.success("Открываю Spotify.")

    orig_open = SpotifyTool._open_app
    SpotifyTool._open_app = staticmethod(fake_open)
    try:
        t = SpotifyTool()
        t.settings = SpotifyConfig(enabled=True, client_id="", client_secret="")
        r = t.execute(action="open")
        assert r.ok and opened["n"] == 1, r.content
    finally:
        SpotifyTool._open_app = orig_open
    print("OK action=open без авторизации")

    # 6c. open_application понимает имена ('spotify' -> 'spotify:')
    from nemus.core.tools import system as _sysmod

    captured = {}

    def fake_startfile(target):
        captured["target"] = target

    orig_sf = getattr(_sysmod.os, "startfile", None)
    _sysmod.os.startfile = fake_startfile  # type: ignore[attr-defined]
    try:
        from nemus.core.tools.system import OpenApplicationTool

        res = OpenApplicationTool().execute(path="Spotify")
        assert res.ok and captured.get("target") == "spotify:", res.content
        captured.clear()
        res = OpenApplicationTool().execute(path="Блокнот")
        target = str(captured.get("target", "")).lower()
        assert res.ok and (target.endswith("notepad.exe") or target == "notepad"), res.content
    finally:
        if orig_sf is not None:
            _sysmod.os.startfile = orig_sf  # type: ignore[attr-defined]
    print("OK open_application: алиасы Spotify/Блокнот")

    # 6d. проверка Redirect URI (требования Spotify 2025+)
    from nemus.core.tools.spotify import redirect_uri_problem

    assert redirect_uri_problem("http://127.0.0.1:8888/callback") == ""
    assert redirect_uri_problem("http://[::1]:8888/callback") == ""
    assert redirect_uri_problem("https://example.com/callback") == ""
    assert redirect_uri_problem("http://localhost:8888/callback") != ""
    assert redirect_uri_problem("http://127.0.0.1:8888/callback/") != ""
    assert redirect_uri_problem("http://192.168.1.5:8888/callback") != ""
    assert redirect_uri_problem("") != ""
    print("OK redirect_uri: проверка требований Spotify")

    # 6e. локальный сервер OAuth: успех, ошибка, state, таймаут
    import threading
    import time
    import urllib.error
    import urllib.request

    from nemus.core.tools.spotify import _wait_for_callback

    def _hit(port, path):
        def run():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=2
                    ).read()
                    return
                except urllib.error.HTTPError:
                    return
                except OSError:
                    time.sleep(0.05)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    _hit(8899, "/callback?code=abc&state=S").join(timeout=3)
    assert (
        _wait_for_callback(8899, "S", "http://127.0.0.1:8899/callback", 10, False) == "abc"
    )
    _hit(8900, "/callback?code=abc&state=WRONG")
    try:
        _wait_for_callback(8900, "S", "u", 10, False)
        raise AssertionError("ожидалась ошибка state")
    except RuntimeError as exc:
        assert "state" in str(exc)
    _hit(8901, "/callback?error=access_denied")
    try:
        _wait_for_callback(8901, "S", "u", 10, False)
        raise AssertionError("ожидалась ошибка access_denied")
    except RuntimeError as exc:
        assert "access_denied" in str(exc)
    start = time.monotonic()
    try:
        _wait_for_callback(8902, "S", "u", 1.0, False)
        raise AssertionError("ожидался таймаут")
    except RuntimeError as exc:
        assert "не завершён" in str(exc)
    assert time.monotonic() - start < 6
    print("OK _wait_for_callback: успех/ошибка/state/таймаут")

    # 6f. authorize сохраняет токен (мок обмена кода на токен)
    import tempfile
    from types import SimpleNamespace

    import spotipy.oauth2 as so

    tmp = os.path.join(tempfile.gettempdir(), "nemus_test_spotify_token.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    orig_get = so.SpotifyOAuth.get_access_token
    orig_wait = sp_mod._wait_for_callback
    captured_auth: dict = {}
    try:

        def _fake_get(self, code=None, **kw):
            captured_auth["code"] = code
            return "tok123"

        so.SpotifyOAuth.get_access_token = _fake_get
        sp_mod._wait_for_callback = lambda *a, **k: "thecode"
        st = SimpleNamespace(
            client_id="cid",
            client_secret="sec",
            redirect_uri="http://127.0.0.1:8890/callback",
            cache_path=tmp,
        )
        out = sp_mod.authorize(st)
        assert out == tmp, out
        assert captured_auth.get("code") == "thecode", captured_auth
    finally:
        so.SpotifyOAuth.get_access_token = orig_get
        sp_mod._wait_for_callback = orig_wait
    print("OK authorize: обмен кода на токен (mock)")

    # 7. регистрация в ядре
    from nemus.config import load_config  # noqa: E402
    from nemus.core.factory import build_core  # noqa: E402

    cfg = load_config()
    core = build_core(cfg)
    st = core.registry.get("spotify")
    if cfg.spotify.enabled:
        assert st is not None, "spotify не зарегистрирован при enabled=true"
        assert st.settings is cfg.spotify, "settings не инжектированы"
        print("OK registered + settings injected")
    else:
        print("SKIP spotify disabled in config")


def main() -> int:
    # ускоряем проверку старта (в коде — реальные паузы)
    sp_mod.time.sleep = lambda *_: None

    tests = [
        test_play_track_keeps_queue,
        test_play_track_wakes_device,
        test_play_track_honest_failure,
        test_queue_track_no_interrupt,
        test_no_devices,
        test_pause_wakes_device,
        test_friendly_errors,
        test_plural,
        test_playlists_lists_own,
        test_play_playlist_prefers_own,
        test_play_playlist_unknown_hints,
        test_play_artist_context,
        test_play_track_free_fallback_album,
        test_play_track_free_fast_path,
        test_play_track_falls_back_to_artist,
        test_strip_fillers,
    ]
    for test in tests:
        test()
    _legacy_suite()
    print("\nALL SPOTIFY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
