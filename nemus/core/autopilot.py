"""Проактивный автопилот Немуси.

Наблюдает за файлами, логом, расписанием, диском и батареей. Сработавшее
правило создаёт событие; одинаковые события дедуплицируются через SQLite,
поэтому GUI и Telegram-демон могут работать одновременно и не присылать
одно и то же дважды.
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from nemus.config import PROJECT_ROOT, AutopilotRule, Config
from nemus.core.autonomy import AutonomyWatcher
from nemus.utils.logger import logger

#: сколько событий храним в базе
MAX_EVENTS = 1000
#: сколько изменений файлов отдаём за один проход
MAX_FILE_EVENTS = 5
#: сколько отложенных событий максимум ждут своей очереди
MAX_PENDING = 100


@dataclass(frozen=True)
class AutopilotEvent:
    """Событие, которое показывается пользователю или уходит агенту."""

    id: int
    rule_id: str
    kind: str
    title: str
    message: str
    prompt: str
    auto_run: bool
    created_at: float
    data: dict[str, Any]
    fingerprint: str = ""
    pending: bool = False

    @property
    def time_text(self) -> str:
        return datetime.fromtimestamp(self.created_at).strftime("%d.%m %H:%M:%S")


class AutopilotStore:
    """SQLite для событий, cooldown'ов и позиций чтения."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=30.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    rule_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    auto_run INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 1,
                    deliver_after REAL NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON events(created_at);
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(events)")
            }
            if "deliver_after" not in columns:
                # миграция баз, созданных до появления очереди на утро
                self._conn.execute(
                    "ALTER TABLE events ADD COLUMN deliver_after REAL NOT NULL DEFAULT 0"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_pending "
                "ON events(delivered, deliver_after)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def recent_fingerprint(self, fingerprint: str, cooldown: float) -> bool:
        if cooldown <= 0:
            return False
        cutoff = time.time() - cooldown
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE fingerprint = ? AND created_at >= ?",
                (fingerprint, cutoff),
            ).fetchone()
        return row is not None

    def claim(
        self,
        event: AutopilotEvent,
        *,
        delivered: bool = True,
        deliver_after: float = 0.0,
    ) -> int | None:
        """Атомарно добавляет событие; None — его уже добавил другой процесс."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO events "
                "(fingerprint, rule_id, kind, title, message, prompt, auto_run, "
                "created_at, delivered, deliver_after, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.fingerprint,
                    event.rule_id,
                    event.kind,
                    event.title,
                    event.message,
                    event.prompt,
                    int(event.auto_run),
                    event.created_at,
                    int(delivered),
                    float(deliver_after),
                    json.dumps(event.data, ensure_ascii=False),
                ),
            )
            if cursor.rowcount <= 0:
                return None
            event_id = int(cursor.lastrowid or 0)
            self._conn.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (MAX_EVENTS,),
            )
            if not delivered:
                self._conn.execute(
                    "DELETE FROM events WHERE delivered = 0 AND id NOT IN "
                    "(SELECT id FROM events WHERE delivered = 0 "
                    "ORDER BY id DESC LIMIT ?)",
                    (MAX_PENDING,),
                )
        return event_id

    def pending_ready(self, now: float, limit: int = 20) -> list[AutopilotEvent]:
        """События, которые ждали тихих часов или свободного слота."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE delivered = 0 AND deliver_after <= ? "
                "ORDER BY id ASC LIMIT ?",
                (float(now), int(limit)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE delivered = 0"
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_delivered(self, event_id: int) -> bool:
        """Забирает отложенное событие ровно один раз (между процессами)."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE events SET delivered = 1, deliver_after = 0 "
                "WHERE id = ? AND delivered = 0",
                (int(event_id),),
            )
            return cursor.rowcount > 0

    def events(self, limit: int = 10) -> list[AutopilotEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._row(row) for row in rows]

    def count_since(self, since: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE created_at >= ? AND delivered = 1",
                (since,),
            ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row(row: sqlite3.Row) -> AutopilotEvent:
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        return AutopilotEvent(
            id=int(row["id"]),
            rule_id=str(row["rule_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            message=str(row["message"]),
            prompt=str(row["prompt"]),
            auto_run=bool(row["auto_run"]),
            created_at=float(row["created_at"]),
            data=data if isinstance(data, dict) else {},
            pending=not bool(row["delivered"]),
        )


def _parse_clock(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def in_quiet_hours(start: str, end: str, now: datetime | None = None) -> bool:
    """Тихие часы, включая переход через полночь."""
    begin = _parse_clock(start)
    finish = _parse_clock(end)
    if begin is None or finish is None:
        return False
    moment = now or datetime.now()
    minutes = moment.hour * 60 + moment.minute
    start_min = begin[0] * 60 + begin[1]
    end_min = finish[0] * 60 + finish[1]
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= minutes < end_min
    return minutes >= start_min or minutes < end_min


def quiet_hours_end_ts(
    start: str, end: str, now: datetime | None = None
) -> float:
    """Когда закончатся тихие часы; 0 — если сейчас не тихо."""
    moment = now or datetime.now()
    if not in_quiet_hours(start, end, moment):
        return 0.0
    parsed = _parse_clock(end)
    if parsed is None:
        return 0.0
    finish = moment.replace(
        hour=parsed[0], minute=parsed[1], second=0, microsecond=0
    )
    if finish <= moment:
        finish += timedelta(days=1)
    return finish.timestamp()


def _resolve_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _fingerprint(*parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _safe_format(template: str, values: dict[str, Any]) -> str:
    """Подставляет только известные плейсхолдеры, не падая на фигурных скобках."""
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result


class Autopilot:
    """Поток-наблюдатель и правила автопилота."""

    def __init__(
        self,
        config: Config,
        *,
        on_event: Callable[[AutopilotEvent], None] | None = None,
        store: AutopilotStore | None = None,
        autonomy_message: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.config = config
        self.cfg = config.autopilot
        self.workspace = Path(config.tools.workspace)
        self.on_event = on_event
        self.autonomy_message = autonomy_message
        self.store = store or AutopilotStore(self.cfg.state_db)
        self._autonomy = AutonomyWatcher(self.cfg.autonomy, store=self.store)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_check: dict[str, float] = {}
        self._initialized: set[str] = set()
        self._autonomy_retry_at = 0.0
        #: последнее значение config.autopilot.enabled, чтобы переключатель
        #: в Mini App и inline-настройках применялся к уже запущенному watcher'у
        self._configured_enabled = bool(self.cfg.enabled)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="autopilot", daemon=True
        )
        self._thread.start()
        logger.info(
            f"Автопилот запущен: {len(self.cfg.rules)} правил, режим "
            f"{'включён' if self.enabled else 'выключен'}, "
            f"самостоятельность {'включена' if self.cfg.autonomy.enabled else 'выключена'}"
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        logger.info("Автопилот остановлен")

    def close(self) -> None:
        self.stop()
        self.store.close()

    @property
    def enabled(self) -> bool:
        configured = bool(self.cfg.enabled)
        if configured != self._configured_enabled:
            # Настройки меняют живой Config, а /auto on|off — состояние в SQLite.
            # Синхронизируем их, иначе переключатель в панели ничего не делает.
            self._configured_enabled = configured
            self.store.set_meta("enabled", "1" if configured else "0")
            logger.info("Автопилот " + ("включён" if configured else "выключен"))
        meta = self.store.get_meta("enabled", "1" if configured else "0")
        return meta == "1"

    def set_enabled(self, value: bool) -> None:
        self.store.set_meta("enabled", "1" if value else "0")
        self._wake.set()
        logger.info("Автопилот " + ("включён" if value else "выключен"))

    def check_now(self) -> int:
        """Выполняет проверку немедленно; возвращает число отправленных событий."""
        self._wake.set()
        return self._check_all(force=True)

    def status_text(self) -> str:
        events = self.store.events(limit=5)
        lines = [
            "**Автопилот**",
            f"• режим: {'✅ включён' if self.enabled else '⏸ выключен'}",
            f"• правил: {len(self.cfg.rules)}",
            f"• самостоятельность: "
            f"{'✅ включена' if self.cfg.autonomy.enabled else '⏸ выключена'}",
            f"• тихие часы: {self.cfg.quiet_hours_start}–{self.cfg.quiet_hours_end}",
            f"• лимит уведомлений: {self.cfg.max_notifications_per_hour}/час",
        ]
        pending = self.store.pending_count()
        if pending:
            lines.append(f"• ждут доставки: {pending}")
        if self.cfg.autonomy.enabled:
            lines.append(
                "• за экраном: "
                f"перерыв после {self.cfg.autonomy.session_minutes} мин, "
                f"пауза {self.cfg.autonomy.cooldown_minutes} мин"
            )
        if not self.cfg.rules:
            lines.append("• правил нет — добавь их в `autopilot.rules`")
        else:
            lines.append("")
            for rule in self.cfg.rules:
                mark = "✅" if rule.enabled else "⬜"
                lines.append(f"{mark} `{rule.id}` — {rule.trigger}")
        if events:
            lines += ["", "**Последние события**"]
            for event in events:
                lines.append(f"• {event.time_text} — {event.title}")
        return "\n".join(lines)

    def rules_text(self) -> str:
        if not self.cfg.rules:
            autonomy = (
                "Самостоятельность включена: замечает долгую работу за экраном "
                "и засиживание в приложении.\n\n"
                if self.cfg.autonomy.enabled
                else ""
            )
            return (
                autonomy
                + "Правил-событий пока нет. Пример:\n\n"
                "```yaml\n"
                "autopilot:\n"
                "  enabled: true\n"
                "  rules:\n"
                "    - id: new-invoice\n"
                "      trigger: file_created\n"
                "      path: data/workspace/Downloads/*.pdf\n"
                "      message: \"Новый PDF: {path}\"\n"
                "      prompt: \"Разбери новый документ {path}\"\n"
                "      auto_run: false\n"
                "```"
            )
        lines = ["**Правила автопилота**"]
        for rule in self.cfg.rules:
            lines.append(
                f"• `{rule.id}` — {rule.trigger}, "
                f"{'вкл' if rule.enabled else 'выкл'}, "
                f"cooldown {rule.cooldown_minutes} мин"
            )
        return "\n".join(lines)

    def events_text(self, limit: int = 10) -> str:
        events = self.store.events(limit=limit)
        if not events:
            return "Событий автопилота пока нет."
        lines = ["**Последние события**"]
        for event in events:
            mark = "⏳ " if event.pending else ""
            lines.append(
                f"• `#{event.id}` {mark}{event.time_text} — {event.title}\n"
                f"  {event.message[:300]}"
            )
        return "\n".join(lines)

    # ----------------------------------------------------------------- loop
    def _loop(self) -> None:
        # первый проход только запоминает текущее состояние, чтобы не
        # присылать пачку старых файлов/строк лога после запуска
        try:
            self._check_all(force=True, initialize=True)
        except Exception:  # noqa: BLE001
            logger.exception("Автопилот: первичная инициализация")
        while not self._stop.is_set():
            self._wake.wait(timeout=max(2, int(self.cfg.poll_seconds)))
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self.enabled:
                continue
            try:
                self._check_all()
            except Exception:  # noqa: BLE001
                logger.exception("Автопилот: сбой проверки правил")

    def _check_all(self, force: bool = False, initialize: bool = False) -> int:
        if not force and not self.enabled:
            return 0
        sent = 0
        now = time.time()
        if not initialize:
            sent += self._flush_pending()
        sent += self._check_autonomy(initialize=initialize)
        for rule in self.cfg.rules:
            if not rule.enabled:
                continue
            last = self._last_check.get(rule.id, 0.0)
            cooldown = max(1, rule.cooldown_minutes * 60)
            if not force and now - last < min(cooldown, 60):
                continue
            self._last_check[rule.id] = now
            try:
                events = self._check_rule(rule, initialize=initialize)
                if initialize and (
                    rule.trigger in ("file_created", "file_modified", "log_error")
                    or (rule.trigger == "schedule" and rule.interval_minutes > 0)
                ):
                    continue
                for event in events:
                    if self._deliver(event):
                        sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Автопилот: правило {rule.id}: {exc}")
        return sent

    def _check_autonomy(self, *, initialize: bool = False) -> int:
        """Проверяет активность за компьютером и говорит первой."""
        if not self.cfg.autonomy.enabled or not self.enabled:
            return 0
        try:
            signal = self._autonomy.check()
        except Exception:  # noqa: BLE001
            logger.exception("Автономность: сбой наблюдения")
            return 0
        now = time.time()
        if in_quiet_hours(self.cfg.quiet_hours_start, self.cfg.quiet_hours_end):
            # Ночью не копим пачку утренних реплик: просто обновляем сессию.
            if signal is not None:
                self._autonomy.mark_emitted(now)
            return 0
        if signal is None:
            return 0
        if initialize:
            # не вываливаем накопленную усталость сразу после запуска
            self._autonomy.mark_emitted(now)
            return 0
        if now < self._autonomy_retry_at:
            return 0
        text = ""
        callback = self.autonomy_message
        if callback is not None:
            try:
                text = str(callback(signal.context) or "").strip()
            except Exception:  # noqa: BLE001
                logger.exception("Автономность: не удалось сгенерировать реплику")
        if not text:
            # модель недоступна: не долбим её каждые 10 секунд
            self._autonomy_retry_at = now + 600.0
            logger.info("Автономность: реплика не сгенерирована — повторю позже")
            return 0
        self._autonomy.mark_emitted(now)
        event = AutopilotEvent(
            id=0,
            rule_id=signal.rule_id,
            kind="autonomy",
            title=signal.rule_id,
            message=text,
            prompt="",
            auto_run=False,
            created_at=now,
            data={
                **signal.context,
                "autonomy_kind": signal.kind,
                "fingerprint": signal.fingerprint,
            },
        )
        return 1 if self._deliver(event) else 0

    def _check_rule(
        self, rule: AutopilotRule, *, initialize: bool = False
    ) -> list[AutopilotEvent]:
        if rule.trigger in ("file_created", "file_modified"):
            return self._check_files(rule, initialize=initialize)
        if rule.trigger == "log_error":
            return self._check_log(rule, initialize=initialize)
        if rule.trigger == "schedule":
            return self._check_schedule(rule, initialize=initialize)
        if rule.trigger == "disk_low":
            return self._check_disk(rule)
        if rule.trigger == "battery_low":
            return self._check_battery(rule)
        return []

    # --------------------------------------------------------------- watchers
    def _file_key(self, rule: AutopilotRule) -> str:
        return f"files:{rule.id}"

    def _snapshot_files(self, pattern: str) -> dict[str, list[float]]:
        snapshot: dict[str, list[float]] = {}
        for name in glob.glob(pattern, recursive=True):
            path = Path(name)
            try:
                if path.is_file():
                    stat = path.stat()
                    snapshot[str(path)] = [stat.st_mtime_ns, stat.st_size]
            except OSError:
                continue
        return snapshot

    def _check_files(
        self, rule: AutopilotRule, *, initialize: bool
    ) -> list[AutopilotEvent]:
        raw = rule.path.strip()
        if not raw:
            return []
        pattern = raw if Path(raw).is_absolute() else str(PROJECT_ROOT / raw)
        key = self._file_key(rule)
        current = self._snapshot_files(pattern)
        saved = self.store.get_meta(key, "")
        if not saved or initialize:
            self.store.set_meta(key, json.dumps(current, ensure_ascii=False))
            return []
        try:
            previous = json.loads(saved)
        except (TypeError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        events: list[AutopilotEvent] = []
        for path, state in current.items():
            old = previous.get(path)
            if rule.trigger == "file_created":
                changed = old is None
            else:
                changed = old is not None and old != state
            if not changed:
                continue
            stat = Path(path).stat()
            stamp = int(stat.st_mtime_ns)
            message = _safe_format(
                rule.message or "Файл изменился: {path}",
                {
                    "path": path,
                    "name": Path(path).name,
                    "time": datetime.now().strftime("%d.%m %H:%M"),
                },
            )
            prompt = _safe_format(rule.prompt, {"path": path, "name": Path(path).name})
            events.append(
                AutopilotEvent(
                    id=0,
                    rule_id=rule.id,
                    kind=rule.trigger,
                    title=f"{rule.id}: {Path(path).name}",
                    message=message,
                    prompt=prompt,
                    auto_run=rule.auto_run,
                    created_at=time.time(),
                    data={
                        "path": path,
                        "fingerprint": _fingerprint(rule.id, path, stamp),
                    },
                )
            )
            if len(events) >= MAX_FILE_EVENTS:
                break
        self.store.set_meta(key, json.dumps(current, ensure_ascii=False))
        return events

    def _log_key(self, rule: AutopilotRule) -> str:
        return f"log:{rule.id}"

    def _check_log(
        self, rule: AutopilotRule, *, initialize: bool
    ) -> list[AutopilotEvent]:
        path = _resolve_path(rule.path or self.config.logging.file)
        try:
            size = path.stat().st_size
        except OSError:
            return []
        key = self._log_key(rule)
        raw_offset = self.store.get_meta(key, "")
        offset = int(raw_offset) if raw_offset.isdigit() else 0
        if initialize or not raw_offset or offset > size:
            self.store.set_meta(key, str(size))
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read(512_000)
        except OSError:
            return []
        self.store.set_meta(key, str(offset + len(raw)))
        text = raw.decode("utf-8", errors="replace")
        try:
            regex = re.compile(rule.pattern or r"\b(ERROR|CRITICAL|Traceback)\b")
        except re.error:
            regex = re.compile(r"\b(ERROR|CRITICAL|Traceback)\b")
        events: list[AutopilotEvent] = []
        for line in text.splitlines():
            if not line.strip() or not regex.search(line):
                continue
            clean = re.sub(r"\s+", " ", line).strip()[:500]
            message = _safe_format(
                rule.message or "Ошибка в логе: {line}", {"line": clean, "path": str(path)}
            )
            events.append(
                AutopilotEvent(
                    id=0,
                    rule_id=rule.id,
                    kind="log_error",
                    title=f"{rule.id}: ошибка в логе",
                    message=message,
                    prompt=_safe_format(rule.prompt, {"line": clean, "path": str(path)}),
                    auto_run=rule.auto_run,
                    created_at=time.time(),
                    data={
                        "path": str(path),
                        "line": clean,
                        "fingerprint": _fingerprint(rule.id, clean),
                    },
                )
            )
            if len(events) >= 5:
                break
        return events

    def _schedule_key(self, rule: AutopilotRule) -> str:
        return f"schedule:{rule.id}"

    def _check_schedule(
        self, rule: AutopilotRule, *, initialize: bool
    ) -> list[AutopilotEvent]:
        now = datetime.now()
        key = self._schedule_key(rule)
        raw = self.store.get_meta(key, "")
        if rule.at:
            clock = _parse_clock(rule.at)
            if clock is None:
                return []
            stamp = now.strftime("%Y-%m-%d")
            target = now.replace(
                hour=clock[0], minute=clock[1], second=0, microsecond=0
            )
            if now < target:
                return []
            late_minutes = (now - target).total_seconds() / 60.0
            grace = max(0, int(rule.catchup_minutes))
            if late_minutes >= 1.0 and grace == 0:
                if raw != stamp:
                    self.store.set_meta(key, stamp)
                return []
            if late_minutes > grace and grace > 0:
                # слишком поздно: не присылаем утренний бриф вечером
                if raw != stamp:
                    self.store.set_meta(key, stamp)
                return []
            if raw == stamp:
                return []
            self.store.set_meta(key, stamp)
        elif rule.interval_minutes > 0:
            last = float(raw) if raw else 0.0
            if initialize or not last:
                self.store.set_meta(key, str(time.time()))
                return []
            if time.time() - last < rule.interval_minutes * 60:
                return []
            self.store.set_meta(key, str(time.time()))
        else:
            return []
        message = _safe_format(
            rule.message or "Плановое событие автопилота: {time}",
            {"time": now.strftime("%d.%m %H:%M")},
        )
        return [
            AutopilotEvent(
                id=0,
                rule_id=rule.id,
                kind="schedule",
                title=rule.id,
                message=message,
                prompt=rule.prompt,
                auto_run=rule.auto_run,
                created_at=time.time(),
                data={"fingerprint": _fingerprint(rule.id, now.strftime("%Y-%m-%d-%H-%M"))},
            )
        ]

    def _check_disk(self, rule: AutopilotRule) -> list[AutopilotEvent]:
        try:
            import shutil

            usage = shutil.disk_usage(PROJECT_ROOT)
        except OSError:
            return []
        # shutil.disk_usage returns total/used/free, not a percent attribute
        total = float(usage.total or 0)
        percent = (float(usage.used) / total * 100.0) if total else 0.0
        if percent < (rule.threshold or 90):
            return []
        message = _safe_format(
            rule.message or "Мало места на диске: свободно {free} ГБ",
            {"percent": f"{percent:.0f}", "free": f"{usage.free / 1024**3:.1f}"},
        )
        return [
            AutopilotEvent(
                id=0,
                rule_id=rule.id,
                kind="disk_low",
                title=f"{rule.id}: диск заполнен на {percent:.0f}%",
                message=message,
                prompt=rule.prompt,
                auto_run=rule.auto_run,
                created_at=time.time(),
                data={"fingerprint": _fingerprint(rule.id, int(time.time() // 3600))},
            )
        ]

    def _check_battery(self, rule: AutopilotRule) -> list[AutopilotEvent]:
        try:
            import psutil

            battery = psutil.sensors_battery()
        except Exception:  # noqa: BLE001
            return []
        if battery is None or battery.power_plugged:
            return []
        if battery.percent > (rule.threshold or 20):
            return []
        message = _safe_format(
            rule.message or "Батарея разряжена: {percent}%",
            {"percent": f"{battery.percent:.0f}"},
        )
        return [
            AutopilotEvent(
                id=0,
                rule_id=rule.id,
                kind="battery_low",
                title=f"{rule.id}: батарея {battery.percent:.0f}%",
                message=message,
                prompt=rule.prompt,
                auto_run=rule.auto_run,
                created_at=time.time(),
                data={"fingerprint": _fingerprint(rule.id, int(time.time() // 3600))},
            )
        ]

    # ---------------------------------------------------------------- deliver
    def _cooldown_for(self, rule_id: str) -> float:
        if rule_id.startswith("autonomy:"):
            return max(1, int(self.cfg.autonomy.cooldown_minutes)) * 60
        return next(
            (max(0, r.cooldown_minutes) * 60 for r in self.cfg.rules if r.id == rule_id),
            1800,
        )

    def _rule_ready(self, rule_id: str, now: float) -> bool:
        cooldown = self._cooldown_for(rule_id)
        if cooldown <= 0:
            return True
        raw = self.store.get_meta(f"delivered:{rule_id}", "")
        try:
            last = float(raw) if raw else 0.0
        except ValueError:
            last = 0.0
        return now - last >= cooldown

    def _mark_rule_delivered(self, rule_id: str, now: float) -> None:
        self.store.set_meta(f"delivered:{rule_id}", str(now))

    def _with_fingerprint(
        self, event: AutopilotEvent, fingerprint: str
    ) -> AutopilotEvent:
        return replace(
            event,
            data={**event.data, "fingerprint": fingerprint},
            fingerprint=fingerprint,
        )

    def _queue(
        self,
        event: AutopilotEvent,
        fingerprint: str,
        *,
        deliver_after: float,
    ) -> bool:
        queued = self._with_fingerprint(event, fingerprint)
        event_id = self.store.claim(
            queued, delivered=False, deliver_after=deliver_after
        )
        if event_id is None:
            return False
        self._mark_rule_delivered(event.rule_id, time.time())
        logger.info(
            f"Автопилот: {event.rule_id} отложен до "
            f"{datetime.fromtimestamp(deliver_after).strftime('%d.%m %H:%M')}"
        )
        return True

    def _deliver(self, event: AutopilotEvent) -> bool:
        fingerprint = str(
            event.data.get("fingerprint") or _fingerprint(event.rule_id, event.title)
        )
        cooldown = self._cooldown_for(event.rule_id)
        if self.store.recent_fingerprint(fingerprint, cooldown):
            return False
        now = time.time()
        if not self._rule_ready(event.rule_id, now):
            logger.debug(f"Автопилот: cooldown правила {event.rule_id}")
            return False
        if in_quiet_hours(self.cfg.quiet_hours_start, self.cfg.quiet_hours_end):
            # Не теряем событие: оно дождётся конца тихих часов и придёт утром.
            self._queue(
                event,
                fingerprint,
                deliver_after=quiet_hours_end_ts(
                    self.cfg.quiet_hours_start, self.cfg.quiet_hours_end
                ),
            )
            return False
        if self.store.count_since(now - 3600) >= max(
            1, self.cfg.max_notifications_per_hour
        ):
            # Слот освободится позже; ставим в очередь, а не выбрасываем.
            self._queue(event, fingerprint, deliver_after=now)
            return False
        prepared = self._with_fingerprint(event, fingerprint)
        event_id = self.store.claim(prepared)
        if event_id is None:
            return False
        self._mark_rule_delivered(event.rule_id, now)
        delivered = replace(
            prepared, id=event_id, pending=False
        )
        logger.info(f"Автопилот: {event.rule_id} — {event.title}")
        if self.on_event is not None:
            try:
                self.on_event(delivered)
            except Exception:  # noqa: BLE001
                logger.exception("Автопилот: обработчик события")
        return True

    def _flush_pending(self) -> int:
        """Доставляет то, что ждало тихих часов или лимита в час."""
        if in_quiet_hours(self.cfg.quiet_hours_start, self.cfg.quiet_hours_end):
            return 0
        now = time.time()
        sent = 0
        for event in self.store.pending_ready(now, limit=20):
            if self.store.count_since(now - 3600) >= max(
                1, self.cfg.max_notifications_per_hour
            ):
                break
            if not self.store.mark_delivered(event.id):
                continue
            event = replace(event, pending=False)
            logger.info(f"Автопилот: доставлено отложенное {event.rule_id}")
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001
                    logger.exception("Автопилот: обработчик отложенного события")
            sent += 1
        return sent


__all__ = [
    "Autopilot",
    "AutopilotEvent",
    "AutopilotStore",
    "in_quiet_hours",
    "quiet_hours_end_ts",
]
