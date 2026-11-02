"""Диагностика Spotify (только чтение): токен, устройства, плейлисты, поиск.

Запуск:  venv\\Scripts\\python.exe scripts\\spotify_probe.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from nemus.config import load_config
    from nemus.core.tools.spotify import SCOPE

    cfg = load_config()
    s = cfg.spotify
    print(f"enabled={s.enabled} client_id={'задан' if s.client_id else 'ПУСТО'} "
          f"market={s.market!r} cache={s.cache_path}")
    print(f"token file exists: {Path(s.cache_path).exists()}")

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError as exc:
        print(f"spotipy не установлен: {exc}")
        return 1

    auth = SpotifyOAuth(
        client_id=s.client_id,
        client_secret=s.client_secret,
        redirect_uri=s.redirect_uri,
        scope=SCOPE,
        cache_path=s.cache_path,
        open_browser=False,
    )
    info = auth.get_cached_token()
    if not info:
        print("НЕТ ТОКЕНА (get_cached_token -> None)")
        return 1
    print(f"token scope: {info.get('scope', '?')}")
    print(f"required scope: {SCOPE}")
    print(f"token expired: {auth.is_token_expired(info)}")

    sp = spotipy.Spotify(auth_manager=auth, requests_timeout=10)

    print("\n--- устройства ---")
    try:
        for d in (sp.devices() or {}).get("devices", []):
            print(f"  {d.get('name')!r} active={d.get('is_active')} id={d.get('id')}")
    except Exception as exc:
        print(f"  ОШИБКА: {exc}")

    print("\n--- мои плейлисты (первые 20) ---")
    try:
        items = (sp.current_user_playlists(limit=20) or {}).get("items", [])
        if not items:
            print("  (пусто)")
        for p in items:
            print(f"  {p.get('name')!r} tracks={p.get('tracks', {}).get('total')}")
    except Exception as exc:
        print(f"  ОШИБКА: {exc}")

    print("\n--- поиск track: 'Алёна Швец' (market=RU) ---")
    try:
        res = sp.search(q="Алёна Швец", type="track", limit=5, market="RU")
        items = (res.get("tracks") or {}).get("items") or []
        if not items:
            print("  (ничего)")
        for t in items:
            artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
            print(f"  {t.get('name')!r} — {artists}")
    except Exception as exc:
        print(f"  ОШИБКА: {exc}")

    print("\n--- поиск artist: 'Алёна Швец' ---")
    try:
        res = sp.search(q="Алёна Швец", type="artist", limit=5)
        items = (res.get("artists") or {}).get("items") or []
        if not items:
            print("  (ничего)")
        for a in items:
            print(f"  {a.get('name')!r} id={a.get('id')}")
    except Exception as exc:
        print(f"  ОШИБКА: {exc}")

    print("\n--- что сейчас играет ---")
    try:
        pb = sp.current_playback()
        if pb and pb.get("item"):
            print(f"  playing={pb.get('is_playing')} «{pb['item'].get('name')}»")
        else:
            print("  (ничего не играет)")
    except Exception as exc:
        print(f"  ОШИБКА: {exc}")

    print("\nPROBE DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
