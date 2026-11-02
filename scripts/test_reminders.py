"""Тест отложенных напоминаний: планировщик, инструмент remind, связка в фабрике.

Запуск:  venv\\Scripts\\python.exe scripts\\test_reminders.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import build_system_prompt  # noqa: E402
from nemus.core.factory import build_core  # noqa: E402
from nemus.core.reminder import ReminderScheduler  # noqa: E402
from nemus.core.tools.remind import RemindTool, parse_when  # noqa: E402


def test_parse_when() -> None:
    # время без даты — ближайшее наступление (через 5 минут или завтра)
    soon = parse_when(in_minutes=5)
    assert 4 * 60 < (soon - datetime.now()).total_seconds() < 6 * 60, soon

    hm = parse_when(at="23:30")
    assert hm.hour == 23 and hm.minute == 30, hm
    assert hm > datetime.now() - timedelta(minutes=1), hm  # не в прошлом

    iso = parse_when(at="2027-01-01 09:15")
    assert (iso.year, iso.hour, iso.minute) == (2027, 9, 15), iso

    for bad in ("", "вчера", "25:99"):
        try:
            parse_when(at=bad, in_minutes=None)
            raise AssertionError(f"ожидалась ошибка для {bad!r}")
        except ValueError:
            pass
    print("ok  разбор времени: in_minutes / 23:30 / ISO / ошибки")


def test_scheduler_fire_and_cancel() -> None:
    fired: list[str] = []
    scheduler = ReminderScheduler()
    scheduler.on_fire = fired.append

    # срабатывание
    reminder_id, at = scheduler.add(datetime.now() + timedelta(seconds=0.4), "выпить воды")
    assert len(scheduler.pending()) == 1
    time.sleep(1.2)
    assert fired == ["выпить воды"], fired
    assert scheduler.pending() == []
    print(f"ok  напоминание {reminder_id} сработало: {fired[0]}")

    # отмена
    scheduler.add(datetime.now() + timedelta(seconds=30), "не сработает")
    pending = scheduler.pending()
    assert len(pending) == 1
    assert scheduler.cancel(pending[0]["id"]) is True
    assert scheduler.cancel("нет-такого") is False
    assert scheduler.pending() == []
    print("ok  отмена по id и отмена несуществующего")

    # clear гасит всё
    scheduler.add(datetime.now() + timedelta(seconds=30), "первое")
    scheduler.add(datetime.now() + timedelta(seconds=31), "второе")
    assert len(scheduler.pending()) == 2
    scheduler.clear()
    assert scheduler.pending() == []
    print("ok  clear() гасит все напоминания")

    # границы
    try:
        scheduler.add(datetime.now() - timedelta(seconds=1), "в прошлом")
        raise AssertionError("напоминание в прошлом должно быть отклонено")
    except ValueError:
        pass
    try:
        scheduler.add(datetime.now() + timedelta(days=400), "слишком далеко")
        raise AssertionError("горизонт больше 30 дней должен быть отклонён")
    except ValueError:
        pass
    print("ok  границы: прошлое и горизонт 30 дней")


def test_tool_roundtrip() -> None:
    scheduler = ReminderScheduler()
    fired: list[str] = []
    scheduler.on_fire = fired.append

    tool = RemindTool()
    # планировщик не подключён -> понятная ошибка, а не падение
    assert not tool.execute(text="x").ok

    tool.scheduler = scheduler
    res = tool.execute(text="выпить воды", in_minutes=60)
    assert res.ok and "напоминание поставлено" in res.content.lower(), res.content
    reminder_id = res.content.split("id=")[1].strip()

    listing = tool.execute(action="list")
    assert listing.ok and "выпить воды" in listing.content, listing.content

    cancel = tool.execute(action="cancel", id=reminder_id)
    assert cancel.ok and "отменено" in cancel.content.lower(), cancel.content
    assert not tool.execute(action="cancel", id=reminder_id).ok

    # валидации
    assert not tool.execute(text="").ok  # пустой текст
    assert not tool.execute(text="x", at="штука").ok  # мусорное время
    assert not tool.execute(action="cancel").ok  # без id
    assert tool.execute(action="list").ok  # list без аргументов валиден
    print("ok  инструмент remind: set / list / cancel / валидация")


def test_wiring_in_factory() -> None:
    cfg = get_config()
    cfg.mcp.enabled = False  # без внешних серверов — тест быстрый и автономный
    core = build_core(cfg)

    assert "remind" in core.registry.names, core.registry.names
    assert core.reminder is not None
    tool = core.registry.get("remind")
    assert getattr(tool, "scheduler", None) is core.reminder

    prompt = build_system_prompt(cfg, core.registry)
    assert "remind" in prompt.split("Доступные инструменты:")[1].split("\n")[0]

    # срабатывание идёт через колбэк (в GUI — сигнал в главный поток Qt)
    seen: list[str] = []
    core.reminder.on_fire = seen.append
    core.registry.execute("remind", {"text": "проверка", "in_minutes": 0.01})
    time.sleep(1.0)
    assert seen == ["проверка"], seen

    core.reminder.clear()
    print("ok  фабрика: remind зарегистрирован, планировщик подключён и стреляет")


def main() -> int:
    test_parse_when()
    test_scheduler_fire_and_cancel()
    test_tool_roundtrip()
    test_wiring_in_factory()
    print("\nВСЕ ТЕСТЫ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
