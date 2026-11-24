"""Самостоятельность Немуси: наблюдение за активностью за компьютером.

Модуль намеренно не делает скриншотов, не читает буфер обмена и не
перехватывает клавиатуру. Он знает только две вещи: сколько человек не
трогал мышь/клавиатуру и какое приложение сейчас на переднем плане. Этого
достаточно, чтобы предложить перерыв или заметить, что человек вернулся.

На Windows используется ``GetLastInputInfo`` и ``GetForegroundWindow``;
на других ОС наблюдатель просто молчит, не мешая остальному автопилоту.
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nemus.config import AutonomyConfig, Config
from nemus.core.providers import Message
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets


@dataclass(frozen=True)
class Activity:
    """Снимок активности пользователя."""

    available: bool
    idle_seconds: float = 0.0
    app: str = ""
    title: str = ""


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


class WindowsActivity:
    """Локальный снимок активности без скриншотов и перехвата ввода."""

    def __init__(self) -> None:
        self._enabled = sys.platform.startswith("win32")
        self._user32: Any = None
        if self._enabled:
            try:
                self._user32 = ctypes.windll.user32
            except Exception:  # noqa: BLE001
                self._enabled = False

    def sample(self, *, read_title: bool = False) -> Activity:
        if not self._enabled or self._user32 is None:
            return Activity(available=False)
        try:
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not self._user32.GetLastInputInfo(ctypes.byref(info)):
                return Activity(available=False)
            # GetLastInputInfo хранит 32-битный tick count. Сравниваем с
            # GetTickCount (а не с 64-битным), иначе после 49 дней аптайма
            # разница становится огромной и человек навсегда «уходит».
            tick = int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF
            idle_ms = (tick - int(info.dwTime)) & 0xFFFFFFFF
            idle_seconds = idle_ms / 1000.0

            hwnd = self._user32.GetForegroundWindow()
            title = ""
            app = ""
            if hwnd:
                if read_title:
                    length = int(self._user32.GetWindowTextLengthW(hwnd))
                    if length > 0:
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        self._user32.GetWindowTextW(hwnd, buffer, length + 1)
                        title = (buffer.value or "").strip()
                pid = ctypes.c_ulong(0)
                self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    try:
                        import psutil

                        app = psutil.Process(int(pid.value)).name()
                    except Exception:  # noqa: BLE001
                        app = ""
            return Activity(
                available=True,
                idle_seconds=idle_seconds,
                app=_clean_app(app),
                title=_clean_title(title),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Автономность: не удалось прочитать активность", exc_info=True)
            return Activity(available=False)


def _clean_app(value: str) -> str:
    name = (value or "").strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name[:80]


def _clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())[:160]


@dataclass(frozen=True)
class AutonomySignal:
    """Повод для самостоятельной реплики."""

    kind: str
    context: dict[str, Any]
    fingerprint: str
    created_at: float

    @property
    def rule_id(self) -> str:
        return f"autonomy:{self.kind}"


class AutonomyWatcher:
    """Считает непрерывную сессию, дневное время и смену приложений."""

    def __init__(
        self,
        cfg: AutonomyConfig,
        *,
        store: Any = None,
        sampler: Any = None,
        clock: Any = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.sampler = sampler or WindowsActivity()
        self.clock = clock or time.time
        self._last_sample = 0.0
        self._last_save = 0.0
        self._session_start = 0.0
        self._idle_since = 0.0
        self._daily_day = ""
        self._daily_seconds = 0.0
        self._app = ""
        self._app_since = 0.0
        self._last_comment = 0.0
        self._load()

    # --------------------------------------------------------------- storage
    def _meta(self, key: str, default: str = "") -> str:
        if self.store is None or not hasattr(self.store, "get_meta"):
            return default
        try:
            return str(self.store.get_meta(f"autonomy:{key}", default))
        except Exception:  # noqa: BLE001
            return default

    def _set_meta(self, key: str, value: str) -> None:
        if self.store is None or not hasattr(self.store, "set_meta"):
            return
        try:
            self.store.set_meta(f"autonomy:{key}", value)
        except Exception:  # noqa: BLE001
            logger.debug("Автономность: не удалось сохранить состояние", exc_info=True)

    @staticmethod
    def _number(raw: str) -> float:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _load(self) -> None:
        self._session_start = self._number(self._meta("session_start"))
        self._idle_since = self._number(self._meta("idle_since"))
        self._daily_day = self._meta("daily_day")
        self._daily_seconds = self._number(self._meta("daily_seconds"))
        self._app = self._meta("app")
        self._app_since = self._number(self._meta("app_since"))
        self._last_comment = self._number(self._meta("last_comment"))
        self._last_sample = self._number(self._meta("last_sample"))

    def _save(self, now: float, *, force: bool = False) -> None:
        if not force and now - self._last_save < 60:
            return
        self._last_save = now
        self._set_meta("session_start", str(self._session_start))
        self._set_meta("idle_since", str(self._idle_since))
        self._set_meta("daily_day", self._daily_day)
        self._set_meta("daily_seconds", f"{self._daily_seconds:.1f}")
        self._set_meta("app", self._app)
        self._set_meta("app_since", str(self._app_since))
        self._set_meta("last_comment", str(self._last_comment))
        self._set_meta("last_sample", str(now))

    # ----------------------------------------------------------------- check
    def check(self) -> AutonomySignal | None:
        if not self.cfg.enabled:
            return None
        now = self.clock()
        activity = self.sampler.sample(read_title=self.cfg.read_window_titles)
        if not activity.available:
            return None

        day = datetime.fromtimestamp(now).date().isoformat()
        if day != self._daily_day:
            self._daily_day = day
            self._daily_seconds = 0.0

        delta = 0.0
        if self._last_sample > 0:
            delta = min(max(0.0, now - self._last_sample), 120.0)
        self._last_sample = now

        idle_limit = max(1, int(self.cfg.idle_minutes)) * 60
        if activity.idle_seconds >= idle_limit:
            if self._session_start > 0:
                self._session_start = 0.0
                self._idle_since = now
            self._save(now, force=True)
            return None

        returning = False
        idle_minutes = 0
        if self._session_start <= 0:
            self._session_start = now
            if self._idle_since > 0:
                idle_minutes = max(0, int((now - self._idle_since) / 60))
                if (
                    self.cfg.welcome_back
                    and now - self._idle_since
                    >= max(1, self.cfg.welcome_after_minutes) * 60
                ):
                    returning = True
            self._idle_since = 0.0

        # активное время считаем только между двумя активными снимками
        if delta:
            self._daily_seconds += delta

        app = activity.app or "компьютер"
        if app != self._app:
            self._app = app
            self._app_since = now
        if self._app_since <= 0:
            self._app_since = now

        cooldown = max(1, int(self.cfg.cooldown_minutes)) * 60
        ready = now - self._last_comment >= cooldown
        session_minutes = max(0.0, (now - self._session_start) / 60.0)
        app_minutes = max(0.0, (now - self._app_since) / 60.0)

        signal: AutonomySignal | None = None
        if returning and ready:
            signal = self._signal(
                "welcome",
                now,
                {
                    "kind": "welcome",
                    "idle_minutes": idle_minutes,
                    "app": app,
                    "title": activity.title,
                    "session_minutes": 0,
                    "daily_minutes": int(self._daily_seconds / 60),
                },
            )
        elif (
            self.cfg.session_minutes > 0
            and session_minutes >= max(1, int(self.cfg.session_minutes))
            and ready
        ):
            signal = self._signal(
                "break",
                now,
                {
                    "kind": "break",
                    "session_minutes": int(session_minutes),
                    "app": app,
                    "title": activity.title,
                    "daily_minutes": int(self._daily_seconds / 60),
                },
            )
        elif (
            self.cfg.daily_limit_minutes > 0
            and self._daily_seconds >= max(1, int(self.cfg.daily_limit_minutes)) * 60
            and ready
        ):
            signal = self._signal(
                "daily",
                now,
                {
                    "kind": "daily",
                    "daily_minutes": int(self._daily_seconds / 60),
                    "session_minutes": int(session_minutes),
                    "app": app,
                    "title": activity.title,
                },
            )
        elif (
            self.cfg.same_app_minutes > 0
            and app_minutes >= max(1, int(self.cfg.same_app_minutes))
            and session_minutes >= 10
            and ready
        ):
            signal = self._signal(
                "peek",
                now,
                {
                    "kind": "peek",
                    "app": app,
                    "app_minutes": int(app_minutes),
                    "session_minutes": int(session_minutes),
                    "daily_minutes": int(self._daily_seconds / 60),
                    "title": activity.title,
                },
            )

        self._save(now, force=signal is not None)
        return signal

    def mark_emitted(self, now: float | None = None) -> None:
        """Фиксирует, что реплика действительно ушла (или поставлена в очередь)."""
        self._last_comment = self.clock() if now is None else now
        self._save(self._last_comment, force=True)

    @staticmethod
    def _signal(kind: str, now: float, context: dict[str, Any]) -> AutonomySignal:
        import hashlib

        raw = f"{kind}|{int(now // 60)}|{context.get('app', '')}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return AutonomySignal(
            kind=kind,
            context=context,
            fingerprint=fingerprint,
            created_at=now,
        )


def _activity_context(context: dict[str, Any]) -> str:
    kind = str(context.get("kind") or "")
    app = str(context.get("app") or "компьютер")
    title = str(context.get("title") or "").strip()
    session = int(context.get("session_minutes") or 0)
    daily = int(context.get("daily_minutes") or 0)
    app_minutes = int(context.get("app_minutes") or 0)
    idle = int(context.get("idle_minutes") or 0)
    lines: list[str] = []
    if kind == "break":
        lines.append(f"Он непрерывно за компьютером уже {session} мин.")
    elif kind == "daily":
        lines.append(f"Сегодня он за экраном уже {daily} мин.")
    elif kind == "peek":
        lines.append(f"Он уже {app_minutes} мин в приложении «{app}».")
    elif kind == "welcome":
        lines.append(f"Он вернулся после перерыва примерно {idle} мин.")
    if kind != "welcome":
        lines.append(f"Активное приложение: «{app}».")
        if title:
            lines.append(f"Заголовок окна: «{title}».")
        if session and kind != "break":
            lines.append(f"Текущая сессия: {session} мин.")
        if daily and kind != "daily":
            lines.append(f"Всего за экраном сегодня: {daily} мин.")
    return "\n".join(lines)


def compose_autonomy_message(
    config: Config,
    llm: Any,
    context: dict[str, Any],
) -> str:
    """Генерирует живую реплику по поводу активности пользователя."""
    if llm is None:
        return ""
    try:
        from nemus.core.emotions import EmotionStore, prompt_block

        state = EmotionStore(config.character.state_file).snapshot()
        character = prompt_block(config, state)
    except Exception:  # noqa: BLE001
        state = None
        character = ""
    name = getattr(config.assistant, "name", "") or "Немуся"
    personality = str(getattr(config.assistant, "personality", "") or "").strip()
    if len(personality) > 500:
        personality = personality[:500] + "…"
    last = ""
    if state is not None and state.last_proactive_text:
        last = (
            "\nТвоя прошлая инициатива (не повторяй её дословно): "
            f"«{state.last_proactive_text[:300]}»"
        )
    kind = str(context.get("kind") or "")
    goal = {
        "break": "Мягко предложи встать, размяться или сделать паузу.",
        "daily": "Заметь, что сегодня уже много экранного времени, и предложи остановиться.",
        "peek": "Живо прокомментируй, чем он занят, или мягко подколи.",
        "welcome": "Встреть его после перерыва, без допроса и без упрёка.",
    }.get(kind, "Скажи одну живую реплику по ситуации.")
    system = (
        f"Ты {name}. {personality}\n"
        f"{character}\n"
        "Ты живёшь на его ноутбуке и сейчас сама заметила, что происходит. "
        f"{goal}\n"
        "Ответь ОДНОЙ короткой репликой от себя, 1–2 предложения, по-русски, "
        "в своём характере. Не отчитывайся как система, не говори «автопилот», "
        "не перечисляй наблюдения и не выдумывай того, чего нет в контексте. "
        "Не начинай каждый раз с «Ты уже» и не спрашивай шаблонно «как дела». "
        "Если заголовок окна выглядит личным, не цитируй его дословно. "
        "Не предлагай список дел и не обещай действий, которых не было."
        f"{last}"
    )
    user = (
        "КОНТЕКСТ:\n"
        f"{_activity_context(context)}\n\n"
        "Напиши только саму реплику, без кавычек и пояснений."
    )
    try:
        response = llm.complete(
            [
                Message(role="system", content=system),
                Message(role="user", content=user),
            ],
            model=getattr(config.llm, "fast_model", "") or None,
            attempts=2,
            budget=25.0,
            fallback=True,
        )
        text = redact_secrets(str(response.content or "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Автономность: модель не ответила: {exc}")
        return ""
    text = text.strip().strip('"«»').strip()
    if len(text) > 700:
        text = text[:700].rstrip() + "…"
    if state is not None and state.last_proactive_text:
        if text == state.last_proactive_text.strip():
            logger.info("Автономность: модель повторила прошлую реплику — пропускаю")
            return ""
    return text


__all__ = [
    "Activity",
    "AutonomySignal",
    "AutonomyWatcher",
    "WindowsActivity",
    "compose_autonomy_message",
]
