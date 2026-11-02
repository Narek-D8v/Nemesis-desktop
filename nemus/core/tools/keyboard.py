"""Инструменты эмуляции клавиатуры: медиа-клавиши и нажатия (pyautogui)."""

from __future__ import annotations

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.clipboard import paste_text

#: человекочитаемое действие -> имя клавиши pyautogui
MEDIA_ACTIONS = {
    "play_pause": "playpause",
    "playpause": "playpause",
    "play": "playpause",
    "pause": "pause",
    "next": "nexttrack",
    "next_track": "nexttrack",
    "previous": "prevtrack",
    "prev": "prevtrack",
    "prev_track": "prevtrack",
    "stop": "stop",
    "volume_up": "volumeup",
    "volume_down": "volumedown",
    "mute": "volumemute",
}

#: действия, требующие точного управления громкостью (обрабатываются не клавишами)
_VOLUME_ACTIONS = {
    "set_volume",
    "volume_set",
    "volume",
    "set_volume_level",
    "get_volume",
    "mute_on",
    "mute_off",
    "unmute",
}


class MediaControlTool(Tool):
    name = "media_control"
    description = (
        "Управляет воспроизведением музыки/видео и громкостью. "
        "Работает глобально — даже если окно плеера не в фокусе. "
        "Действия (action): play_pause (играть/пауза), next, previous, stop, "
        "volume_up, volume_down, mute (переключить беззвучный режим), "
        "set_volume (установить точный уровень 0–100 — обязательно передай level), "
        "get_volume (узнать текущую громкость), "
        "mute_on / mute_off (точно выключить/включить звук). "
        "Используй set_volume с level, когда просят задать конкретную громкость, "
        "например «поставь громкость 15%» → action='set_volume', level=15."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "play_pause | next | previous | stop | volume_up | volume_down | "
                    "mute | mute_on | mute_off | set_volume | get_volume"
                ),
            },
            "level": {
                "type": "integer",
                "description": "Уровень громкости 0–100 для action='set_volume'.",
            },
            "times": {
                "type": "integer",
                "description": "Сколько раз нажать (по умолчанию 1).",
            },
        },
        "required": ["action"],
    }
    risk = RiskLevel.MEDIUM

    def execute(
        self, action: str = "", times: int = 1, level: int | None = None, **_: object
    ) -> ToolResult:
        name = (action or "").strip().lower()

        if name in _VOLUME_ACTIONS:
            return self._volume_action(name, level)

        key = MEDIA_ACTIONS.get(name)
        if not key:
            return ToolResult.failure(
                f"Неизвестное медиа-действие: «{action}». "
                f"Доступно: {', '.join(sorted(set(MEDIA_ACTIONS) | _VOLUME_ACTIONS))}."
            )
        try:
            n = max(1, int(times or 1))
        except (TypeError, ValueError):
            n = 1
        try:
            import pyautogui

            pyautogui.press(key, presses=n)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось нажать медиа-клавишу: {exc}")
        return ToolResult.success(f"Медиа-клавиша «{key}» нажата {n} раз." + (" (это переключатель play/pause)" if name == "play_pause" else ""))

    def _volume_action(self, name: str, level: int | None) -> ToolResult:
        from nemus.core.tools import volume

        if not volume.available():
            return ToolResult.failure(
                "Управление системной громкостью недоступно на этой платформе."
            )
        try:
            if name == "get_volume":
                current = volume.get_volume()
                if current is None:
                    return ToolResult.failure("Не удалось определить текущую громкость.")
                return ToolResult.success(f"Текущая громкость: {current}%.", data={"level": current})

            if name in ("mute_on", "mute_off", "unmute"):
                volume.set_mute(name == "mute_on")
                return ToolResult.success(
                    "Звук выключен." if name == "mute_on" else "Звук включён."
                )

            if level is None:
                return ToolResult.failure(
                    "Для установки громкости нужен параметр level (0–100)."
                )
            try:
                target = max(0, min(100, int(round(float(level)))))
            except (TypeError, ValueError):
                return ToolResult.failure(f"Некорректный level: {level!r}. Ожидается 0–100.")

            applied = volume.set_volume(target)
            muted = volume.is_muted()
            if muted:
                volume.set_mute(False)
            note = " (снял беззвучный режим)" if muted else ""
            return ToolResult.success(
                f"Громкость установлена на {applied}%{note}.",
                data={"level": applied},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось изменить громкость: {exc}")


class PressKeyTool(Tool):
    name = "press_key"
    description = (
        "Нажимает клавишу или комбинацию клавиш на клавиатуре (эмуляция ввода). "
        "Используй key для одной клавиши ('space', 'enter', 'f11'), keys для "
        "комбинации (например ['ctrl', 's']) или text чтобы набрать текст. "
        "ВАЖНО: обычные клавиши действуют только в активном окне — сначала "
        "сфокусируй/открой нужное окно. Для управления музыкой предпочитай "
        "инструмент media_control (работает без фокуса)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Имя одной клавиши pyautogui, например 'space'.",
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Комбинация клавиш, например ['ctrl', 'c'].",
            },
            "text": {"type": "string", "description": "Текст для набора."},
            "times": {
                "type": "integer",
                "description": "Повторов для одной клавиши (по умолчанию 1).",
            },
        },
    }
    risk = RiskLevel.MEDIUM

    def execute(
        self,
        key: str = "",
        keys: list[str] | None = None,
        text: str = "",
        times: int = 1,
        **_: object,
    ) -> ToolResult:
        if not key and not keys and not text:
            return ToolResult.failure("Укажите key, keys или text.")
        try:
            import pyautogui

            valid = set(pyautogui.KEYBOARD_KEYS)
            if text:
                value = str(text)
                # pyautogui.typewrite теряет кириллицу (нет кодов клавиш) —
                # нелатинский текст вставляем через буфер обмена.
                if value.isascii():
                    pyautogui.typewrite(value, interval=0.02)
                    return ToolResult.success(
                        f"Набран текст ({len(value)} символов)."
                    )
                if paste_text(pyautogui, value):
                    return ToolResult.success(
                        f"Вставлен текст ({len(value)} символов) из буфера обмена."
                    )
                return ToolResult.failure(
                    "Не удалось вставить нелатинский текст: буфер обмена недоступен. "
                    "Текст НЕ введён — не повторяй ввод, сообщи об ошибке."
                )
            if keys:
                combo = [str(k).lower() for k in keys]
                bad = [k for k in combo if k not in valid]
                if bad:
                    return ToolResult.failure(f"Неизвестные клавиши: {', '.join(bad)}.")
                pyautogui.hotkey(*combo)
                return ToolResult.success(f"Нажата комбинация: {'+'.join(combo)}.")
            name = str(key).lower()
            if name not in valid:
                return ToolResult.failure(f"Неизвестная клавиша: «{key}».")
            try:
                n = max(1, int(times or 1))
            except (TypeError, ValueError):
                n = 1
            pyautogui.press(name, presses=n)
            return ToolResult.success(f"Клавиша «{name}» нажата {n} раз.")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка эмуляции клавиатуры: {exc}")


def keyboard_tools() -> list[Tool]:
    return [MediaControlTool(), PressKeyTool()]
