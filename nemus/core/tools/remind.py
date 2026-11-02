"""Инструмент отложенных напоминаний (напомни в 23:30, таймеры)."""

from __future__ import annotations

from datetime import datetime, timedelta

from nemus.core.reminder import ReminderScheduler
from nemus.core.tools.base import RiskLevel, Tool, ToolResult

#: форматы, в которых модель может прислать время
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M")


def parse_when(at: str = "", in_minutes: float | None = None) -> datetime:
    """Превращает «23:30» / «2026-10-29 09:00» / «через N минут» в дату.

    Время без даты трактуется как ближайшее наступление: если часы сегодня
    уже прошли — завтра.
    """
    if in_minutes is not None:
        minutes = float(in_minutes)
        if minutes <= 0:
            raise ValueError("Количество минут должно быть больше нуля.")
        return datetime.now() + timedelta(minutes=minutes)

    raw = (at or "").strip()
    if not raw:
        raise ValueError("Не указано время: передай at ('23:30') или in_minutes.")

    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass

    now = datetime.now()
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.year == 1900 and fmt in ("%H:%M", "%H:%M:%S"):
            parsed = now.replace(
                hour=parsed.hour,
                minute=parsed.minute,
                second=parsed.second,
                microsecond=0,
            )
            if parsed <= now:
                parsed += timedelta(days=1)
        return parsed
    raise ValueError(f"Не удалось разобрать время «{raw}» — используй формат 23:30.")


class RemindTool(Tool):
    name = "remind"
    description = (
        "Ставит отложенное напоминание или таймер. Сработает в указанное время: "
        "текст появится в чате и будет произнесён голосом. Действия (action): "
        "'set' — поставить напоминание (нужны text и время), 'list' — показать "
        "активные, 'cancel' — снять по id. Время задавай через at ('23:30', "
        "'2026-10-29 09:00') либо in_minutes (через сколько минут). "
        "Используй, когда просят «напомни…», «поставь таймер», «в 23:30 скажи…»."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "set | list | cancel (по умолчанию set).",
            },
            "text": {
                "type": "string",
                "description": "Текст напоминания, например «выпить воды».",
            },
            "at": {
                "type": "string",
                "description": "Когда сработать: 23:30 или 2026-10-29 09:00.",
            },
            "in_minutes": {
                "type": "number",
                "description": "Через сколько минут сработать (вместо at).",
            },
            "id": {"type": "string", "description": "id напоминания для cancel."},
        },
    }
    risk = RiskLevel.SAFE

    #: подставляется фабрикой
    scheduler: ReminderScheduler | None = None

    def execute(
        self,
        action: str = "set",
        text: str = "",
        at: str = "",
        in_minutes: float | None = None,
        id: str = "",
        **_: object,
    ) -> ToolResult:
        scheduler = self.scheduler
        if scheduler is None:
            return ToolResult.failure("Планировщик напоминаний не подключён.")

        action = (action or "set").strip().lower()
        if action == "list":
            items = scheduler.pending()
            if not items:
                return ToolResult.success("Активных напоминаний нет.")
            lines = [f"{i['at']} — {i['text']} (id={i['id']})" for i in items]
            return ToolResult.success("\n".join(lines))

        if action == "cancel":
            if not id:
                return ToolResult.failure("Не передан id напоминания (см. action='list').")
            if scheduler.cancel(id):
                return ToolResult.success(f"Напоминание {id} отменено.")
            return ToolResult.failure(f"Напоминание {id} не найдено (оно уже сработало?).")

        if not text.strip():
            return ToolResult.failure("Пустой текст напоминания — что напомнить?")

        try:
            when = parse_when(at, in_minutes)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        try:
            reminder_id, fired_at = scheduler.add(when, text.strip())
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        delay = fired_at - datetime.now()
        minutes = max(1, round(delay.total_seconds() / 60))
        return ToolResult.success(
            f"Напоминание поставлено на {fired_at:%d.%m %H:%M} (через ~{minutes} мин): "
            f"{text.strip()}. id={reminder_id}"
        )


def remind_tools(scheduler: ReminderScheduler | None = None) -> list[Tool]:
    tool = RemindTool()
    tool.scheduler = scheduler
    return [tool]


__all__ = ["RemindTool", "remind_tools", "parse_when"]
