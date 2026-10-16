"""Оффлайн-тесты инструментов клавиатуры/медиа (pyautogui мокается)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.core.tools.all import all_builtin_tools  # noqa: E402
from nemus.core.tools.base import RiskLevel  # noqa: E402
from nemus.core.tools.keyboard import (  # noqa: E402
    MEDIA_ACTIONS,
    MediaControlTool,
    PressKeyTool,
    keyboard_tools,
)


class FakePyAutoGUI:
    KEYBOARD_KEYS = [
        "space", "enter", "f11", "ctrl", "c", "s", "playpause", "nexttrack",
        "prevtrack", "stop", "volumeup", "volumedown", "volumemute", "pause",
    ]

    def __init__(self) -> None:
        self.presses: list[tuple[str, int]] = []
        self.hotkeys: list[tuple[str, ...]] = []
        self.typed: list[str] = []

    def press(self, key, presses=1):  # noqa: ANN001
        self.presses.append((key, presses))

    def hotkey(self, *keys):  # noqa: ANN002
        self.hotkeys.append(tuple(keys))

    def typewrite(self, text, interval=0.0):  # noqa: ANN001
        self.typed.append(text)


def _install_fake() -> FakePyAutoGUI:
    fake = FakePyAutoGUI()
    sys.modules["pyautogui"] = fake  # type: ignore[assignment]
    return fake


def main() -> int:
    fake = _install_fake()

    # 1. медиа-действия -> корректные клавиши
    media = MediaControlTool()
    assert media.name == "media_control" and media.risk is RiskLevel.MEDIUM
    r = media.execute(action="play_pause")
    assert r.ok and fake.presses[-1] == ("playpause", 1), r.content
    media.execute(action="next")
    media.execute(action="previous")
    media.execute(action="volume_up")
    media.execute(action="volume_down")
    media.execute(action="mute")
    media.execute(action="stop")
    assert [p[0] for p in fake.presses] == [
        "playpause", "nexttrack", "prevtrack", "volumeup", "volumedown",
        "volumemute", "stop",
    ], fake.presses
    assert set(MEDIA_ACTIONS)  # непустой словарь
    print("OK media: play_pause/next/prev/vol/mute")

    # 2. times
    fake.presses.clear()
    media.execute(action="play_pause", times=3)
    assert fake.presses[-1] == ("playpause", 3), fake.presses
    media.execute(action="play_pause", times=0)  # 0 -> 1
    assert fake.presses[-1] == ("playpause", 1)
    print("OK media: times")

    # 3. неизвестное действие
    r = media.execute(action="dance")
    assert not r.ok and "Неизвестное" in r.content
    r = media.execute(action="")
    assert not r.ok
    print("OK media: unknown/empty")

    # 4. press_key: одна клавиша
    pk = PressKeyTool()
    fake.presses.clear()
    r = pk.execute(key="space")
    assert r.ok and fake.presses[-1] == ("space", 1), r.content
    r = pk.execute(key="space", times=2)
    assert fake.presses[-1] == ("space", 2)
    print("OK press_key: space")

    # 5. press_key: комбинация и текст
    fake.hotkeys.clear()
    fake.typed.clear()
    r = pk.execute(keys=["Ctrl", "S"])
    assert r.ok and fake.hotkeys[-1] == ("ctrl", "s"), fake.hotkeys
    r = pk.execute(text="привет")
    assert r.ok and fake.typed[-1] == "привет", fake.typed
    print("OK press_key: hotkey/text")

    # 6. валидация
    assert not pk.execute(key="несуществующая").ok
    assert not pk.execute(keys=["ctrl", "zzz"]).ok
    assert not pk.execute().ok
    print("OK press_key: validation")

    # 7. регистрация в общем наборе
    names = {t.name for t in all_builtin_tools()}
    assert {"media_control", "press_key"} <= names, names
    assert len(keyboard_tools()) == 2
    print("OK registered: media_control, press_key")

    print("\nALL KEYBOARD TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
