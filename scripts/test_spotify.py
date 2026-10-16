"""Офлайн-тесты инструмента Spotify (без сети и без spotipy)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import SpotifyConfig  # noqa: E402
from nemus.core.tools.base import RiskLevel, ToolResult  # noqa: E402
from nemus.core.tools.spotify import SpotifyTool, spotify_tools  # noqa: E402


class FakeSpotify:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._devices = [
            {"id": "dev1", "name": "Мой компьютер", "is_active": True}
        ]

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

    def search(self, q="", type="track", limit=1, market="RU"):
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


def _tool() -> SpotifyTool:
    tool = SpotifyTool()
    tool.settings = SpotifyConfig(enabled=True, client_id="id", client_secret="sec")
    tool._client = FakeSpotify()
    return tool


def main() -> None:
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
    tool = _tool()
    fake = tool._client
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

        def search(self, q="", type="track", limit=1, market=None):
            self.markets.append(market)
            key = "tracks" if type == "track" else "playlists"
            if market == "RU":
                return {key: {"items": [None]}}
            items = [None]
            if self._valid:
                items.append({"name": "Норм", "uri": "spotify:playlist:ok", "artists": []})
            return {key: {"items": items}}

    t2 = SpotifyTool()
    t2.settings = SpotifyConfig(
        enabled=True, client_id="a", client_secret="b", market="RU"
    )
    fake2 = _Noney(valid_second=True)
    t2._client = fake2
    r = t2.execute(action="play_playlist", query="X")
    assert r.ok and "Норм" in r.content, r.content
    assert fake2.markets == ["RU", None], fake2.markets
    assert any(c[0] == "start_playback" for c in fake2.calls)

    fake3 = _Noney(valid_second=False)
    t3 = SpotifyTool()
    t3.settings = SpotifyConfig(
        enabled=True, client_id="a", client_secret="b", market="RU"
    )
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
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    _hit(8899, "/callback?code=abc&state=S").join(timeout=3)
    assert (
        _wait_for_callback(8899, "S", "http://127.0.0.1:8899/callback", 10, False)
        == "abc"
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

    import nemus.core.tools.spotify as spmod

    tmp = os.path.join(tempfile.gettempdir(), "nemus_test_spotify_token.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    orig_get = so.SpotifyOAuth.get_access_token
    orig_wait = spmod._wait_for_callback
    captured = {}
    try:
        def _fake_get(self, code=None, **kw):
            captured["code"] = code
            return "tok123"

        so.SpotifyOAuth.get_access_token = _fake_get
        spmod._wait_for_callback = lambda *a, **k: "thecode"
        st = SimpleNamespace(
            client_id="cid",
            client_secret="sec",
            redirect_uri="http://127.0.0.1:8890/callback",
            cache_path=tmp,
        )
        out = spmod.authorize(st)
        assert out == tmp, out
        assert captured.get("code") == "thecode", captured
    finally:
        so.SpotifyOAuth.get_access_token = orig_get
        spmod._wait_for_callback = orig_wait
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

    print("\nALL SPOTIFY TESTS PASSED")


if __name__ == "__main__":
    main()
