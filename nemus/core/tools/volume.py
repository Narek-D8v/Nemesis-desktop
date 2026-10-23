"""Управление системной громкостью (Windows Core Audio, Linux/macOS — CLI).

Не требует сторонних зависимостей: на Windows используется Core Audio API
через ``ctypes``, на Linux — ``amixer``/``pactl``, на macOS — ``osascript``.

Публичный API:

* :func:`available` — доступно ли управление громкостью на этой платформе;
* :func:`get_volume` — текущий уровень 0–100 (или ``None``);
* :func:`set_volume` — установить уровень 0–100;
* :func:`change_volume` — изменить уровень на дельту;
* :func:`set_mute` / :func:`is_muted` — управление беззвучным режимом.
"""

from __future__ import annotations

import ctypes
import platform
import shutil
import subprocess
from ctypes import POINTER, byref, c_float, c_uint, c_void_p
from typing import Any

_SYSTEM = platform.system()

#: таймаут вызовов CLI-утилит громкости (секунды)
CLI_TIMEOUT = 10


class VolumeError(RuntimeError):
    """Не удалось применить операцию с громкостью."""


# --------------------------------------------------------------------------
#  Windows: Core Audio (IAudioEndpointVolume) через ctypes
# --------------------------------------------------------------------------
if _SYSTEM == "Windows":

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        def __init__(self, guid: str) -> None:
            super().__init__()
            ctypes.oledll.ole32.CLSIDFromString(guid, byref(self))

    _HRESULT = ctypes.c_long
    _CFG = ctypes.WINFUNCTYPE

    class _IAudioEndpointVolumeVtbl(ctypes.Structure):
        pass

    class _IAudioEndpointVolume(ctypes.Structure):
        pass

    _IAudioEndpointVolumeVtbl._fields_ = [
        ("QueryInterface", _CFG(_HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))),
        ("AddRef", _CFG(ctypes.c_ulong, c_void_p)),
        ("Release", _CFG(ctypes.c_ulong, c_void_p)),
        ("RegisterControlChangeNotify", _CFG(_HRESULT, c_void_p, c_void_p)),
        ("UnregisterControlChangeNotify", _CFG(_HRESULT, c_void_p, c_void_p)),
        ("GetChannelCount", _CFG(_HRESULT, c_void_p, POINTER(c_uint))),
        ("SetMasterVolumeLevel", _CFG(_HRESULT, c_void_p, c_float, POINTER(_GUID))),
        ("SetMasterVolumeLevelScalar", _CFG(_HRESULT, c_void_p, c_float, POINTER(_GUID))),
        ("GetMasterVolumeLevel", _CFG(_HRESULT, c_void_p, POINTER(c_float))),
        ("GetMasterVolumeLevelScalar", _CFG(_HRESULT, c_void_p, POINTER(c_float))),
        ("SetChannelVolumeLevel", _CFG(_HRESULT, c_void_p, c_uint, c_float, POINTER(_GUID))),
        ("SetChannelVolumeLevelScalar", _CFG(_HRESULT, c_void_p, c_uint, c_float, POINTER(_GUID))),
        ("GetChannelVolumeLevel", _CFG(_HRESULT, c_void_p, c_uint, POINTER(c_float))),
        ("GetChannelVolumeLevelScalar", _CFG(_HRESULT, c_void_p, c_uint, POINTER(c_float))),
        ("SetMute", _CFG(_HRESULT, c_void_p, ctypes.c_int, POINTER(_GUID))),
        ("GetMute", _CFG(_HRESULT, c_void_p, POINTER(ctypes.c_int))),
        ("GetVolumeStepInfo", _CFG(_HRESULT, c_void_p, POINTER(c_uint), POINTER(c_uint))),
        ("VolumeStepUp", _CFG(_HRESULT, c_void_p, POINTER(_GUID))),
        ("VolumeStepDown", _CFG(_HRESULT, c_void_p, POINTER(_GUID))),
        ("QueryHardwareSupport", _CFG(_HRESULT, c_void_p, POINTER(c_uint))),
        ("GetVolumeRange", _CFG(_HRESULT, c_void_p, POINTER(c_float), POINTER(c_float), POINTER(c_float))),
    ]

    _IAudioEndpointVolume._fields_ = [
        ("lpVtbl", POINTER(_IAudioEndpointVolumeVtbl)),
    ]

    class _IMMDeviceVtbl(ctypes.Structure):
        pass

    class _IMMDevice(ctypes.Structure):
        pass

    _IMMDeviceVtbl._fields_ = [
        ("QueryInterface", _CFG(_HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))),
        ("AddRef", _CFG(ctypes.c_ulong, c_void_p)),
        ("Release", _CFG(ctypes.c_ulong, c_void_p)),
        ("Activate", _CFG(_HRESULT, c_void_p, POINTER(_GUID), c_uint, c_void_p, POINTER(POINTER(_IAudioEndpointVolume)))),
        ("OpenPropertyStore", _CFG(_HRESULT, c_void_p, c_uint, POINTER(c_void_p))),
        ("GetId", _CFG(_HRESULT, c_void_p, POINTER(ctypes.c_wchar_p))),
        ("GetState", _CFG(_HRESULT, c_void_p, POINTER(c_uint))),
    ]

    _IMMDevice._fields_ = [("lpVtbl", POINTER(_IMMDeviceVtbl))]

    class _IMMDeviceEnumeratorVtbl(ctypes.Structure):
        pass

    class _IMMDeviceEnumerator(ctypes.Structure):
        pass

    _IMMDeviceEnumeratorVtbl._fields_ = [
        ("QueryInterface", _CFG(_HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))),
        ("AddRef", _CFG(ctypes.c_ulong, c_void_p)),
        ("Release", _CFG(ctypes.c_ulong, c_void_p)),
        ("EnumAudioEndpoints", _CFG(_HRESULT, c_void_p, c_uint, c_uint, POINTER(c_void_p))),
        ("GetDefaultAudioEndpoint", _CFG(_HRESULT, c_void_p, c_uint, c_uint, POINTER(POINTER(_IMMDevice)))),
        ("GetDevice", _CFG(_HRESULT, c_void_p, ctypes.c_wchar_p, POINTER(c_void_p))),
        ("RegisterEndpointNotificationCallback", _CFG(_HRESULT, c_void_p, c_void_p)),
        ("UnregisterEndpointNotificationCallback", _CFG(_HRESULT, c_void_p, c_void_p)),
    ]

    _IMMDeviceEnumerator._fields_ = [
        ("lpVtbl", POINTER(_IMMDeviceEnumeratorVtbl)),
    ]

    _CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    _IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    _IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
    _CLSCTX_ALL = 0x17
    _ERENDER = 0
    _EMULTIMEDIA = 1

    def _co_initialize() -> None:
        try:
            ctypes.oledll.ole32.CoInitializeEx(None, 0x2)  # APARTMENTTHREADED
        except OSError:
            pass

    def _endpoint_volume() -> ctypes.Structure:
        _co_initialize()
        enumerator = POINTER(_IMMDeviceEnumerator)()
        hr = ctypes.oledll.ole32.CoCreateInstance(
            byref(_GUID(_CLSID_MMDeviceEnumerator)),
            None,
            _CLSCTX_ALL,
            byref(_GUID(_IID_IMMDeviceEnumerator)),
            byref(enumerator),
        )
        if hr != 0 or not enumerator:
            raise VolumeError(f"CoCreateInstance MMDeviceEnumerator: 0x{hr & 0xFFFFFFFF:08X}")
        device = POINTER(_IMMDevice)()
        hr = enumerator.contents.lpVtbl.contents.GetDefaultAudioEndpoint(
            enumerator, _ERENDER, _EMULTIMEDIA, byref(device)
        )
        if hr != 0 or not device:
            raise VolumeError(f"GetDefaultAudioEndpoint: 0x{hr & 0xFFFFFFFF:08X}")
        volume = POINTER(_IAudioEndpointVolume)()
        hr = device.contents.lpVtbl.contents.Activate(
            device, byref(_GUID(_IID_IAudioEndpointVolume)), _CLSCTX_ALL, None, byref(volume)
        )
        if hr != 0 or not volume:
            raise VolumeError(f"Activate IAudioEndpointVolume: 0x{hr & 0xFFFFFFFF:08X}")
        return volume

    def _win_get() -> int:
        vol = _endpoint_volume()
        value = c_float()
        vol.contents.lpVtbl.contents.GetMasterVolumeLevelScalar(vol, byref(value))
        return int(round(value.value * 100))

    def _win_set(level: int) -> None:
        vol = _endpoint_volume()
        scalar = c_float(max(0.0, min(1.0, level / 100.0)))
        hr = vol.contents.lpVtbl.contents.SetMasterVolumeLevelScalar(vol, scalar, None)
        if hr != 0:
            raise VolumeError(f"SetMasterVolumeLevelScalar: 0x{hr & 0xFFFFFFFF:08X}")

    def _win_is_muted() -> bool:
        vol = _endpoint_volume()
        muted = ctypes.c_int()
        vol.contents.lpVtbl.contents.GetMute(vol, byref(muted))
        return bool(muted.value)

    def _win_set_mute(muted: bool) -> None:
        vol = _endpoint_volume()
        vol.contents.lpVtbl.contents.SetMute(vol, 1 if muted else 0, None)


# --------------------------------------------------------------------------
#  Linux / macOS: CLI-утилиты
# --------------------------------------------------------------------------
def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLI_TIMEOUT,
    )


def _linux_tool() -> tuple[str, str] | None:
    if shutil.which("pactl"):
        return "pactl", "@DEFAULT_SINK@"
    if shutil.which("amixer"):
        return "amixer", "Master"
    return None


# --------------------------------------------------------------------------
#  Публичный API
# --------------------------------------------------------------------------
def available() -> bool:
    """Доступно ли управление системной громкостью."""
    if _SYSTEM == "Windows":
        return True
    if _SYSTEM == "Darwin":
        return shutil.which("osascript") is not None
    return _linux_tool() is not None


def get_volume() -> int | None:
    """Текущий уровень громкости 0–100, либо ``None``, если определить нельзя."""
    try:
        if _SYSTEM == "Windows":
            return _win_get()
        if _SYSTEM == "Darwin":
            out = _run(
                ["osascript", "-e", "output volume of (get volume settings)"]
            )
            return int(out.stdout.strip()) if out.returncode == 0 else None
        tool = _linux_tool()
        if tool is None:
            return None
        name, target = tool
        if name == "pactl":
            out = _run(["pactl", "get-sink-volume", target])
            for token in out.stdout.split():
                if token.endswith("%"):
                    return int(token.rstrip("%"))
            return None
        out = _run(["amixer", "get", target])
        for line in out.stdout.splitlines():
            if "%" in line and "[" in line:
                return int(line.split("[", 1)[1].split("%", 1)[0])
        return None
    except Exception:  # noqa: BLE001
        return None


def set_volume(level: int) -> int:
    """Устанавливает уровень громкости ``level`` (0–100). Возвращает применённое."""
    level = max(0, min(100, int(round(level))))
    if _SYSTEM == "Windows":
        _win_set(level)
        return level
    if _SYSTEM == "Darwin":
        out = _run(["osascript", "-e", f"set volume output volume {level}"])
    else:
        tool = _linux_tool()
        if tool is None:
            raise VolumeError("Не найден pactl или amixer для управления громкостью.")
        name, target = tool
        if name == "pactl":
            out = _run(["pactl", "set-sink-volume", target, f"{level}%"])
        else:
            out = _run(["amixer", "-q", "set", target, f"{level}%"])
    if out.returncode != 0:
        raise VolumeError((out.stderr or "команда управления громкостью не удалась").strip())
    return level


def change_volume(delta: int) -> int:
    """Меняет громкость на ``delta`` (может быть отрицательной)."""
    current = get_volume()
    if current is None:
        current = 50
    return set_volume(current + int(delta))


def is_muted() -> bool | None:
    """Включён ли беззвучный режим (``None``, если неизвестно)."""
    try:
        if _SYSTEM == "Windows":
            return _win_is_muted()
        if _SYSTEM == "Darwin":
            out = _run(["osascript", "-e", "output muted of (get volume settings)"])
            return out.stdout.strip().lower() == "true"
        return None
    except Exception:  # noqa: BLE001
        return None


def set_mute(muted: bool) -> None:
    """Включает/выключает беззвучный режим."""
    if _SYSTEM == "Windows":
        _win_set_mute(muted)
        return
    if _SYSTEM == "Darwin":
        state = "true" if muted else "false"
        _run(["osascript", "-e", f"set volume output muted {state}"])
        return
    tool = _linux_tool()
    if tool is None:
        raise VolumeError("Не найден pactl или amixer для управления громкостью.")
    name, target = tool
    if name == "pactl":
        _run(["pactl", "set-sink-mute", target, "1" if muted else "0"])
    else:
        _run(["amixer", "-q", "set", target, "mute" if muted else "unmute"])


def describe() -> dict[str, Any]:
    """Снимок состояния громкости (для диагностики)."""
    return {
        "platform": _SYSTEM,
        "available": available(),
        "volume": get_volume(),
        "muted": is_muted(),
    }
