"""Системные инструменты: запуск приложений, URL, информация о системе."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.sandbox import PathGuard, PathNotAllowed


class GetSystemInfoTool(Tool):
    name = "get_system_info"
    description = "Возвращает краткую информацию о системе (ОС, архитектура, Python, время)."
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE

    def execute(self, **_: object) -> ToolResult:
        info = [
            f"ОС: {platform.system()} {platform.release()} ({platform.version()})",
            f"Архитектура: {platform.machine()}",
            f"CPU: {platform.processor()}",
            f"Python: {sys.version}",
            f"Время: {datetime.now().isoformat(timespec='seconds')}",
        ]
        return ToolResult.success("\n".join(info))


class OpenUrlTool(Tool):
    name = "open_url"
    description = "Открывает URL в браузере по умолчанию."
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }
    risk = RiskLevel.MEDIUM

    def execute(self, url: str = "", **_: object) -> ToolResult:
        if not url.strip():
            return ToolResult.failure("Пустой URL.")
        try:
            import webbrowser

            webbrowser.open_new_tab(url)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось открыть: {exc}")
        return ToolResult.success(f"Открыт URL: {url}")


class OpenApplicationTool(Tool):
    name = "open_application"
    description = (
        "Открывает приложение, файл или системную ссылку. Понимает понятные "
        "имена ('spotify', 'блокнот', 'notepad', 'калькулятор', 'проводник', "
        "'google chrome', 'visual studio code'), URI-протоколы (spotify:, mailto:) "
        "и пути к .exe/.lnk. Используй, когда просят открыть/запустить программу."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Имя приложения, протокол (spotify:) или путь к .exe/.lnk.",
            }
        },
        "required": ["path"],
    }
    risk = RiskLevel.MEDIUM

    _ALIASES = {
        "spotify": "spotify:",
        "спотифай": "spotify:",
        "спотик": "spotify:",
        "блокнот": "notepad",
        "калькулятор": "calc",
        "проводник": "explorer",
        "explorer": "explorer",
        "notepad": "notepad",
        "calc": "calc",
    }
    _COMMON_DIRS = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
        os.path.expandvars(r"%ProgramFiles%"),
        os.path.expandvars(r"%ProgramFiles(x86)%"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    _uwp_cache: list[tuple[str, str]] | None = None

    def execute(self, path: str = "", **_: object) -> ToolResult:
        target = (path or "").strip().strip('"').strip()
        if not target:
            return ToolResult.failure("Не указано приложение (path).")
        alias = self._ALIASES.get(target.lower())
        if alias:
            target = alias
        if sys.platform.startswith("win"):
            return self._open_windows(target)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                f"Не удалось запустить «{target}»: {exc}. Проверьте, что приложение установлено."
            )
        return ToolResult.success(f"Открываю: {target}")

    def _open_windows(self, target: str) -> ToolResult:
        tried: list[str] = []

        # 1) URI / протокол / файл-ассоциация (spotify:, mailto:, http://, .pdf …)
        if self._looks_like_uri(target):
            try:
                os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
                return ToolResult.success(f"Открываю: {target}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"uri({exc})")

        # 2) уже существующий путь
        expanded = os.path.expandvars(os.path.expanduser(target))
        if os.path.exists(expanded):
            try:
                os.startfile(expanded)  # type: ignore[attr-defined]  # noqa: S606
                return ToolResult.success(f"Открываю: {expanded}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"path({exc})")

        # 3) на PATH
        exe = self._which_any(target)
        if exe:
            try:
                os.startfile(exe)  # type: ignore[attr-defined]  # noqa: S606
                return ToolResult.success(f"Открываю: {exe}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"which({exc})")

        # 4) ярлык из меню «Пуск»
        lnk = self._find_start_menu(target)
        if lnk:
            try:
                os.startfile(lnk)  # type: ignore[attr-defined]  # noqa: S606
                return ToolResult.success(f"Открываю: {os.path.splitext(os.path.basename(lnk))[0]}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"lnk({exc})")

        # 5) UWP / приложение из Microsoft Store (по AUMID)
        aumid = self._find_uwp(target)
        if aumid:
            try:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{aumid}"])
                return ToolResult.success(f"Открываю: {target}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"uwp({exc})")

        # 6) поиск .exe в типовых папках установки
        exe = self._find_common_exe(target)
        if exe:
            try:
                os.startfile(exe)  # type: ignore[attr-defined]  # noqa: S606
                return ToolResult.success(f"Открываю: {exe}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"common({exc})")

        # 7) системная оболочка «start»
        try:
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
            return ToolResult.success(f"Открываю: {target}")
        except Exception as exc:  # noqa: BLE001
            tried.append(f"start({exc})")

        detail = "; ".join(tried) if tried else "стратегии не сработали"
        return ToolResult.failure(
            f"Не удалось открыть «{target}». Пробовал: {detail}. "
            f"Проверьте точное имя приложения или укажите полный путь к .exe/.lnk."
        )

    @staticmethod
    def _looks_like_uri(t: str) -> bool:
        if "://" in t:
            return True
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", t)
        # одиночная буква (диск C:) — не протокол
        return bool(m and len(m.group(1)) > 1)

    @staticmethod
    def _norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    @classmethod
    def _which_any(cls, name: str) -> str | None:
        for cand in (name, name + ".exe", name + ".bat", name + ".cmd", name + ".lnk"):
            found = shutil.which(cand)
            if found:
                return found
        return None

    @classmethod
    def _find_start_menu(cls, name: str) -> str | None:
        roots = [
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        ]
        key = cls._norm(name)
        partial: str | None = None
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    if not fname.lower().endswith((".lnk", ".appref-ms")):
                        continue
                    stem = os.path.splitext(fname)[0]
                    stem_norm = cls._norm(stem)
                    if stem_norm == key:
                        return os.path.join(dirpath, fname)
                    if key and key in stem_norm and partial is None:
                        partial = os.path.join(dirpath, fname)
        return partial

    @classmethod
    def _find_uwp(cls, name: str) -> str | None:
        if cls._uwp_cache is None:
            cls._uwp_cache = cls._load_uwp_apps()
        key = cls._norm(name)
        partial: str | None = None
        for app_name, appid in cls._uwp_cache:
            app_norm = cls._norm(app_name)
            if app_norm == key:
                return appid
            if key and (key in app_norm or app_norm in key) and partial is None:
                partial = appid
        return partial

    @staticmethod
    def _load_uwp_apps() -> list[tuple[str, str]]:
        try:
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                    "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            apps: list[tuple[str, str]] = []
            for line in (out.stdout or "").splitlines():
                if "|" in line:
                    app_name, appid = line.split("|", 1)
                    if app_name.strip() and appid.strip():
                        apps.append((app_name.strip(), appid.strip()))
            return apps
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def _find_common_exe(cls, name: str) -> str | None:
        key = cls._norm(name)
        if not key:
            return None
        for root in cls._COMMON_DIRS:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.listdir(root):
                    p = os.path.join(root, entry)
                    if os.path.isdir(p):
                        try:
                            for sub in os.listdir(p):
                                if sub.lower().endswith(".exe") and cls._norm(os.path.splitext(sub)[0]) == key:
                                    return os.path.join(p, sub)
                        except OSError:
                            continue
                    elif entry.lower().endswith(".exe") and cls._norm(os.path.splitext(entry)[0]) == key:
                        return p
            except OSError:
                continue
        return None


class ScreenshotTool(Tool):
    name = "screenshot"
    description = (
        "Сделать скриншот экрана и сохранить в файл. "
        "По умолчанию — в рабочую папку ассистента."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Путь сохранения (по умолчанию — в рабочей папке).",
            }
        },
    }
    risk = RiskLevel.MEDIUM

    #: подставляется фабрикой из config.tools.workspace
    guard: PathGuard | None = None

    def execute(self, path: str = "", **_: object) -> ToolResult:
        from datetime import datetime

        if self.guard is None:
            self.guard = PathGuard()
        self.guard.ensure_dir()
        if not path:
            path = str(
                self.guard.workspace / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
            )
        try:
            target = self.guard.resolve_write(path, what="Файл скриншота")
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            import pyautogui

            img = pyautogui.screenshot()
            img.save(target)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось сделать скриншот: {exc}")
        return ToolResult.success(f"Скриншот сохранён: {target}")


def system_tools() -> list[Tool]:
    return [
        GetSystemInfoTool(),
        OpenUrlTool(),
        OpenApplicationTool(),
        ScreenshotTool(),
    ]
