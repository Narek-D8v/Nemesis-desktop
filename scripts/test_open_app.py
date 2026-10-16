"""Проверка стратегий разрешения имён приложений в OpenApplicationTool."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.core.tools.system import OpenApplicationTool  # noqa: E402

T = OpenApplicationTool()

# --- _looks_like_uri ---
assert T._looks_like_uri("spotify:"), "spotify: должен быть URI"
assert T._looks_like_uri("mailto:a@b.c"), "mailto: должен быть URI"
assert T._looks_like_uri("http://x"), "http:// должен быть URI"
assert not T._looks_like_uri("C:\\Windows\\notepad.exe"), "путь к диску — не URI"
assert not T._looks_like_uri("notepad"), "простое имя — не URI"
print("ok  _looks_like_uri")

# --- _which_any ---
exe = T._which_any("python")
assert exe, "python должен найтись на PATH"
print(f"ok  _which_any(python) -> {exe}")

# --- Start Menu ---
lnk = T._find_start_menu("visual studio code") or T._find_start_menu("notepad") or T._find_start_menu("calc")
print(f"ok  _find_start_menu -> {lnk}")

# --- UWP (Microsoft Store) ---
uwp = T._find_uwp("калькулятор") or T._find_uwp("calc") or T._find_uwp("spotify") or T._find_uwp("photos")
print(f"ok  _find_uwp -> {uwp}")

# --- поиск .exe в типовых папках ---
common = T._find_common_exe("code") or T._find_common_exe("chrome") or T._find_common_exe("notepad")
print(f"ok  _find_common_exe -> {common}")

# --- реальный запуск безопасного встроенного приложения ---
res = T.execute("notepad")
assert res.ok, f"notepad не запустился: {res.content}"
print(f"ok  execute(notepad) -> {res.content}")

print("LAUNCH STRATEGY TEST PASSED")
