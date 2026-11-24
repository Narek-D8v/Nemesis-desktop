"""Тесты проактивного автопилота и self-healing без сети и Telegram.

Запуск:
    venv\\Scripts\\python.exe scripts/test_autopilot_healing.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime as RealDatetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import (  # noqa: E402
    AutonomyConfig,
    AutopilotRule,
    Config,
    HealingConfig,
)
from nemus.core import telegram_settings as settings_ui  # noqa: E402
from nemus.core.autonomy import (  # noqa: E402
    Activity,
    AutonomySignal,
    AutonomyWatcher,
    compose_autonomy_message,
)
from nemus.core.autopilot import Autopilot, in_quiet_hours  # noqa: E402
from nemus.core.self_healing import (  # noqa: E402
    HealingIncident,
    HealingManager,
    error_fingerprint,
    normalize_error,
)


def test_autopilot_files_and_log() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        cfg = Config()
        cfg.autopilot.enabled = True
        cfg.autopilot.state_db = str(folder / "autopilot.db")
        cfg.autopilot.quiet_hours_start = "00:00"
        cfg.autopilot.quiet_hours_end = "00:00"
        watched = folder / "report.txt"
        watched.write_text("one", encoding="utf-8")
        log = folder / "test.log"
        log.write_text("INFO start\n", encoding="utf-8")
        cfg.autopilot.rules = [
            AutopilotRule(
                id="report",
                trigger="file_modified",
                path=str(watched),
                message="Изменён {name}",
                cooldown_minutes=0,
            ),
            AutopilotRule(
                id="errors",
                trigger="log_error",
                path=str(log),
                message="Ошибка: {line}",
                cooldown_minutes=0,
            ),
        ]
        events = []
        service = Autopilot(cfg, on_event=events.append)
        service._check_all(force=True, initialize=True)
        watched.write_text("two", encoding="utf-8")
        time.sleep(0.01)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("2026-01-01 | ERROR | test:1 - сломалось\n")
        sent = service.check_now()
        assert sent == 2, sent
        assert any("report.txt" in event.message for event in events)
        assert any("сломалось" in event.message for event in events)
        assert len(service.store.events()) == 2
        service.close()
    print("ok  автопилот: файл и строка лога превращаются в события")


def test_autopilot_settings_toggle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.autopilot.enabled = True
        cfg.autopilot.state_db = str(Path(tmp) / "autopilot.db")
        cfg.autopilot.quiet_hours_start = "00:00"
        cfg.autopilot.quiet_hours_end = "00:00"
        service = Autopilot(cfg)
        assert service.enabled

        field = settings_ui.find_field("autopilot", "auto_enabled")
        assert field is not None
        ok, message = settings_ui.apply_text(cfg, field, "false")
        assert ok, message
        assert not cfg.autopilot.enabled
        assert not service.enabled, "выключение из настроек должно гасить watcher"

        ok, message = settings_ui.apply_text(cfg, field, "true")
        assert ok, message
        assert service.enabled, "включение из настроек должно поднимать watcher"

        service.set_enabled(False)
        assert not service.enabled, "/auto off должен работать независимо от конфига"
        service.close()
    print("ok  автопилот: переключатель настроек применяется к запущенному watcher'у")


def test_disk_rule_uses_real_percent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.autopilot.enabled = True
        cfg.autopilot.state_db = str(Path(tmp) / "autopilot.db")
        service = Autopilot(cfg)
        rule = AutopilotRule(
            id="disk",
            trigger="disk_low",
            threshold=80,
            message="Занято {percent}%, свободно {free} ГБ",
        )
        usage = SimpleNamespace(total=100, used=85, free=15)
        with patch.object(shutil, "disk_usage", return_value=usage):
            events = service._check_disk(rule)
        assert len(events) == 1, events
        assert "85%" in events[0].message, events[0].message
        assert "диск заполнен на 85%" in events[0].title
        service.close()
    print("ok  disk_low считает процент из total/used, без несуществующего .percent")


def test_quiet_hours_queue_and_flush() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.autopilot.enabled = True
        cfg.autopilot.state_db = str(Path(tmp) / "autopilot.db")
        cfg.autopilot.quiet_hours_start = "00:00"
        cfg.autopilot.quiet_hours_end = "00:00"
        events = []
        service = Autopilot(cfg, on_event=events.append)
        event = service._check_disk(
            AutopilotRule(id="disk", trigger="disk_low", threshold=1)
        )[0]
        with patch("nemus.core.autopilot.in_quiet_hours", return_value=True), patch(
            "nemus.core.autopilot.quiet_hours_end_ts", return_value=0.0
        ):
            assert service._deliver(event) is False
        assert service.store.pending_count() == 1
        assert service.store.events(1)[0].pending is True
        assert not events
        assert service._flush_pending() == 1
        assert service.store.pending_count() == 0
        assert len(events) == 1
        service.close()
    print("ok  событие в тихие часы ждёт в очереди и доставляется после них")


def test_schedule_catchup() -> None:
    class Frozen(RealDatetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 1, 1, 10, 30)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.autopilot.enabled = True
        cfg.autopilot.state_db = str(Path(tmp) / "autopilot.db")
        service = Autopilot(cfg)
        fresh = AutopilotRule(
            id="late", trigger="schedule", at="10:00", catchup_minutes=120
        )
        stale = AutopilotRule(
            id="stale", trigger="schedule", at="06:00", catchup_minutes=30
        )
        with patch("nemus.core.autopilot.datetime", Frozen):
            assert len(service._check_schedule(fresh, initialize=False)) == 1
            assert service._check_schedule(fresh, initialize=False) == []
            assert service._check_schedule(stale, initialize=False) == []
        service.close()
    print("ok  schedule догоняет пропущенное время в пределах catchup_minutes")


def test_autonomy_session_and_idle() -> None:
    now = [1_000_000.0]
    activity = [Activity(available=True, idle_seconds=0, app="Code")]

    class Sampler:
        def sample(self, *, read_title: bool = False) -> Activity:
            return activity[0]

    cfg = AutonomyConfig(
        enabled=True,
        session_minutes=90,
        idle_minutes=5,
        cooldown_minutes=45,
        same_app_minutes=45,
        welcome_back=True,
        welcome_after_minutes=15,
        daily_limit_minutes=0,
    )
    watcher = AutonomyWatcher(cfg, sampler=Sampler(), clock=lambda: now[0])
    assert watcher.check() is None
    now[0] += 91 * 60
    signal = watcher.check()
    assert signal is not None and signal.kind == "break", signal
    watcher.mark_emitted(now[0])
    now[0] += 60
    assert watcher.check() is None, "cooldown должен удерживать от повторной реплики"
    activity[0] = Activity(available=True, idle_seconds=10 * 60, app="Code")
    now[0] += 10 * 60
    assert watcher.check() is None
    activity[0] = Activity(available=True, idle_seconds=0, app="Code")
    now[0] += 20 * 60
    assert watcher.check() is None, "встреча после перерыва тоже уважает cooldown"
    print("ok  самостоятельность замечает долгую сессию, перерыв и cooldown")


def test_autonomy_watcher_reaches_autopilot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.autopilot.enabled = True
        cfg.autopilot.state_db = str(Path(tmp) / "autopilot.db")
        cfg.autopilot.autonomy.enabled = True
        cfg.autopilot.quiet_hours_start = "00:00"
        cfg.autopilot.quiet_hours_end = "00:00"
        events = []
        service = Autopilot(
            cfg,
            on_event=events.append,
            autonomy_message=lambda ctx: "Пора размяться.",
        )
        seen = {}

        class FakeWatcher:
            def check(self):
                return AutonomySignal(
                    kind="break",
                    context={"kind": "break", "session_minutes": 95, "app": "Code"},
                    fingerprint="abc123",
                    created_at=time.time(),
                )

            def mark_emitted(self, now=None):  # noqa: ANN001
                seen["marked"] = now

        service._autonomy = FakeWatcher()
        sent = service._check_autonomy()
        assert sent == 1
        assert len(events) == 1 and events[0].kind == "autonomy"
        assert events[0].message == "Пора размяться."
        assert seen.get("marked")
        service.close()
    print("ok  автономность превращается в реплику автопилота без user-промпта")


def test_autonomy_message_uses_character() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.character.state_file = str(Path(tmp) / "emotions.json")
        cfg.llm.fast_model = "test/fast"
        captured = {}

        class FakeLLM:
            def complete(self, messages, **kwargs):  # noqa: ANN001
                captured["messages"] = messages
                captured["kwargs"] = kwargs
                return SimpleNamespace(content="Ты уже долго в коде. Встань, пожалуйста.")

        text = compose_autonomy_message(
            cfg,
            FakeLLM(),
            {
                "kind": "break",
                "session_minutes": 95,
                "app": "Code",
                "title": "project.py",
                "daily_minutes": 200,
            },
        )
        assert text.startswith("Ты уже долго"), text
        assert captured["kwargs"]["model"] == "test/fast"
        system = captured["messages"][0].content
        assert "95 мин" not in system
        assert "не скриншоты" not in system
    print("ok  самостоятельная реплика генерируется в характере через быструю модель")


def test_autopilot_quiet_hours() -> None:
    from datetime import datetime

    assert in_quiet_hours("23:00", "08:00", datetime(2026, 1, 1, 23, 30))
    assert not in_quiet_hours("23:00", "08:00", datetime(2026, 1, 1, 12, 0))
    assert in_quiet_hours("13:00", "14:00", datetime(2026, 1, 1, 13, 30))
    assert not in_quiet_hours("00:00", "00:00", datetime(2026, 1, 1, 0, 0))
    print("ok  тихие часы корректно переходят через полночь")


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "init",
        ],
        cwd=root,
        check=True,
    )


def _incident(manager: HealingManager, diff: str) -> HealingIncident:
    item = HealingIncident(
        id="incident-1",
        fingerprint="abc",
        created_at=time.time(),
        title="Тестовая ошибка",
        error="ValueError: bad",
        context="",
        status="proposed",
        diff=diff,
    )
    manager._save_incident(item)
    return item


def test_self_healing_apply_and_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "check.py").write_text(
            "import value\nassert value.VALUE == 2\n", encoding="utf-8"
        )
        _git_repo(root)
        cfg = Config()
        cfg.healing = HealingConfig(
            enabled=True,
            state_dir=str(root / "state"),
            test_command="{python} check.py",
        )
        manager = HealingManager(cfg, llm=None, root=root)
        diff = (
            "--- a/value.py\n"
            "+++ b/value.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
        )
        _incident(manager, diff)
        assert manager.validate_patch(diff) == ""
        ok, message = manager.apply("incident-1")
        assert ok, message
        assert "VALUE = 2" in (root / "value.py").read_text(encoding="utf-8")
        ok, message = manager.rollback("incident-1")
        assert ok, message
        assert "VALUE = 1" in (root / "value.py").read_text(encoding="utf-8")
    print("ok  self-healing: патч применяется, проверяется и откатывается")


def test_self_healing_rolls_back_on_failed_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "check.py").write_text(
            "import value\nassert value.VALUE == 99\n", encoding="utf-8"
        )
        _git_repo(root)
        cfg = Config()
        cfg.healing = HealingConfig(
            enabled=True,
            state_dir=str(root / "state"),
            test_command="{python} check.py",
        )
        manager = HealingManager(cfg, llm=None, root=root)
        diff = (
            "--- a/value.py\n"
            "+++ b/value.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
        )
        _incident(manager, diff)
        ok, _ = manager.apply("incident-1")
        assert not ok, "упавшая проверка не должна оставлять патч"
        assert "VALUE = 1" in (root / "value.py").read_text(encoding="utf-8")
        assert manager.get_incident("incident-1").status == "failed"
    print("ok  self-healing: упавшая проверка автоматически откатывает файлы")


def test_healing_monitor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = root / "nemus.log"
        log.write_text("INFO start\n", encoding="utf-8")
        cfg = Config()
        cfg.logging.file = str(log)
        cfg.healing = HealingConfig(
            enabled=True,
            state_dir=str(root / "state"),
            error_threshold=2,
            auto_propose=False,
        )
        incidents = []
        manager = HealingManager(cfg, llm=None, root=root, on_incident=incidents.append)
        manager._scan(initialize=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("2026-01-01 | ERROR | mod:1 - ValueError: bad 123\n")
            handle.write("2026-01-01 | ERROR | mod:2 - ValueError: bad 456\n")
        assert manager._scan() == 1
        assert incidents and incidents[0].status == "new"
        assert manager.list_incidents()[0].title
    print("ok  self-healing: повторяющиеся ERROR создают один инцидент")


def test_error_helpers_and_settings() -> None:
    first = "2026-11-13 10:00:00 | ERROR | a:b:12 - ValueError: bad id=123"
    second = "2026-11-13 10:01:00 | ERROR | a:b:99 - ValueError: bad id=456"
    assert normalize_error(first) == normalize_error(second)
    assert error_fingerprint(first) == error_fingerprint(second)
    assert settings_ui.find_field("autopilot", "auto_enabled") is not None
    assert settings_ui.find_field("healing", "heal_enabled") is not None
    assert any(section.id == "autopilot" for section in settings_ui.SECTIONS)
    print("ok  нормализация ошибок и разделы Mini App на месте")


if __name__ == "__main__":
    test_autopilot_files_and_log()
    test_autopilot_settings_toggle()
    test_disk_rule_uses_real_percent()
    test_quiet_hours_queue_and_flush()
    test_schedule_catchup()
    test_autonomy_session_and_idle()
    test_autonomy_watcher_reaches_autopilot()
    test_autonomy_message_uses_character()
    test_autopilot_quiet_hours()
    test_self_healing_apply_and_rollback()
    test_self_healing_rolls_back_on_failed_test()
    test_healing_monitor()
    test_error_helpers_and_settings()
    print("\nAUTOPILOT & HEALING TESTS PASSED")
