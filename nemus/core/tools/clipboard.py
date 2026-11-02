"""Буфер обмена для ввода текста: Unicode из любого потока.

Инструменты выполняются в рабочих потоках (агент, realtime), где Qt-клипборд
падает с ``OleSetClipboard ... CoInitialize`` — COM в этих потоках не
инициализирован. Поэтому на Windows текст кладётся в буфер напрямую через
WinAPI (``CF_UNICODETEXT``), без COM и Qt. Qt/tkinter остаются запасными
вариантами для других платформ и для главного потока GUI.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _win32_set_clipboard(text: str) -> bool:
    """Кладёт Unicode-текст в буфер обмена через WinAPI (любой поток)."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not handle:
        logger.warning("Буфер обмена: GlobalAlloc не выделил память")
        return False
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        logger.warning("Буфер обмена: GlobalLock не удался")
        return False
    try:
        ctypes.memmove(locked, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)

    # другой процесс может держать буфер — пробуем несколько раз
    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)
    if not opened:
        kernel32.GlobalFree(handle)
        logger.warning("Буфер обмена занят другим приложением")
        return False

    try:
        if not user32.EmptyClipboard():
            return False
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            return False
        # владение памятью перешло системе — сами не освобождаем
        handle = None
        return True
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def _win32_get_clipboard() -> str | None:
    """Читает текст из буфера обмена (нужно для проверки и тестов)."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return None
        try:
            return ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _qt_set_clipboard(text: str) -> bool:
    """Qt-клипборд — только из главного потока (COM инициализирован Qt)."""
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        return False
    app = QApplication.instance()
    if app is None:
        return False
    try:
        if QThread.currentThread() != app.thread():
            return False
        clip = app.clipboard()
        clip.setText(text)
        # Qt не бросает исключение, если OLE не смог — проверяем результат
        return clip.text() == text
    except Exception:  # noqa: BLE001
        return False


def _tk_set_clipboard(text: str) -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
        finally:
            root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def set_clipboard(text: str) -> bool:
    """Кладёт текст в буфер обмена. Возвращает ``True`` только при успехе."""
    if not text:
        return False
    if sys.platform == "win32":
        try:
            if _win32_set_clipboard(text):
                return True
        except Exception:  # noqa: BLE001
            logger.debug("WinAPI-буфер обмена недоступен", exc_info=True)
    if _qt_set_clipboard(text):
        return True
    return _tk_set_clipboard(text)


def get_clipboard() -> str | None:
    """Текущий текст буфера обмена (``None``, если недоступен)."""
    if sys.platform == "win32":
        try:
            return _win32_get_clipboard()
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось прочитать буфер обмена", exc_info=True)
    return None


def paste_text(pyautogui_module: Any, text: str) -> bool:
    """Печатает текст через буфер обмена и Ctrl+V (Unicode, любая раскладка)."""
    if not set_clipboard(text):
        return False
    # даём системе завершить владение буфером перед вставкой
    time.sleep(0.03)
    modifier = "command" if sys.platform == "darwin" else "ctrl"
    pyautogui_module.hotkey(modifier, "v")
    return True


__all__ = ["get_clipboard", "paste_text", "set_clipboard"]
