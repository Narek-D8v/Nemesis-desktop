"""Самовосстановление Немуси: инциденты из лога, патч, тесты и откат.

Автоматический патч — опасная операция, поэтому по умолчанию менеджер только
предлагает diff. Применение происходит после явного подтверждения, сначала
создаются резервные копии, затем ``git apply`` и проверочная команда. Если
проверка упала, файлы автоматически возвращаются в исходное состояние.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from nemus.config import PROJECT_ROOT, Config
from nemus.core.providers import Message
from nemus.utils.logger import logger

#: как часто читаем лог, сек
SCAN_SECONDS = 8.0
#: сколько последних строк лога прикладываем к инциденту
CONTEXT_LINES = 120
#: максимальный размер одного файла в промпте
MAX_SOURCE_CHARS = 18_000
#: таймаут проверочной команды, сек
TEST_TIMEOUT = 240


@dataclass
class HealingIncident:
    id: str
    fingerprint: str
    created_at: float
    title: str
    error: str
    context: str
    diagnosis: str = ""
    diff: str = ""
    status: str = "new"
    test_output: str = ""
    apply_error: str = ""
    files: list[str] = field(default_factory=list)
    backup_dir: str = ""
    new_files: list[str] = field(default_factory=list)
    attempts: int = 0

    @property
    def time_text(self) -> str:
        return datetime.fromtimestamp(self.created_at).strftime("%d.%m %H:%M:%S")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "HealingIncident":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


def normalize_error(line: str) -> str:
    """Убирает из строки лога всё, что меняется от запуска к запуску."""
    text = line.strip()
    marker = text.find(" - ")
    if marker >= 0:
        text = text[marker + 3 :]
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text, flags=re.I)
    text = re.sub(r"\b\d{2,}\b", "<n>", text)
    text = re.sub(r"0x[0-9a-f]+", "<addr>", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return text[:500]


def error_fingerprint(line: str) -> str:
    return hashlib.sha256(normalize_error(line).encode("utf-8")).hexdigest()[:16]


class HealingManager:
    """Монитор лога и хранилище инцидентов."""

    def __init__(
        self,
        config: Config,
        llm: Any,
        *,
        on_incident: Callable[[HealingIncident], None] | None = None,
        root: str | Path = PROJECT_ROOT,
    ) -> None:
        self.config = config
        self.cfg = config.healing
        self.llm = llm
        self.on_incident = on_incident
        self.root = Path(root).resolve()
        self.state_dir = Path(self.cfg.state_dir)
        self.incidents_dir = self.state_dir / "incidents"
        self.backups_dir = self.state_dir / "backups"
        self.state_file = self.state_dir / "state.json"
        self.incidents_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="self-healing", daemon=True
        )
        self._thread.start()
        logger.info(
            f"Self-healing запущен: порог {self.cfg.error_threshold} ошибок "
            f"за {self.cfg.window_minutes} мин"
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        logger.info("Self-healing остановлен")

    def scan_now(self) -> int:
        """Один проход по логу; возвращает число новых инцидентов."""
        return self._scan()

    def status_text(self) -> str:
        incidents = self.list_incidents()
        by_status: dict[str, int] = {}
        for item in incidents:
            by_status[item.status] = by_status.get(item.status, 0) + 1
        lines = [
            "**Self-healing**",
            f"• режим: {'✅ включён' if self.enabled else '⏸ выключен'}",
            f"• порог: {self.cfg.error_threshold} ошибок / {self.cfg.window_minutes} мин",
            f"• автопатч: {'да' if self.cfg.auto_propose else 'нет (только диагноз)'}",
            f"• автоприменение: {'да' if self.cfg.auto_apply else 'нет'}",
            f"• инцидентов: {len(incidents)}",
        ]
        if by_status:
            lines.append("• " + ", ".join(f"{k}: {v}" for k, v in by_status.items()))
        recent = incidents[:3]
        if recent:
            lines += ["", "**Последние**"]
            for item in recent:
                lines.append(f"• `#{item.id}` {item.status} — {item.title}")
        return "\n".join(lines)

    def list_text(self, limit: int = 10) -> str:
        incidents = self.list_incidents()[:limit]
        if not incidents:
            return "Инцидентов нет. Self-healing следит за логом."
        lines = ["**Инциденты**"]
        for item in incidents:
            lines.append(
                f"• `#{item.id}` — {item.status}, {item.time_text}\n"
                f"  {item.title}"
            )
        lines += ["", "`/heal show ID`, `/heal apply ID`, `/heal reject ID`"]
        return "\n".join(lines)

    def incident_text(self, incident_id: str) -> str:
        item = self.get_incident(incident_id)
        if item is None:
            return "Инцидент не найден."
        parts = [
            f"**Инцидент #{item.id}**",
            f"• статус: {item.status}",
            f"• время: {item.time_text}",
            f"• ошибка: `{item.error[:400]}`",
        ]
        if item.diagnosis:
            parts += ["", "**Диагноз**", item.diagnosis[:3000]]
        if item.diff:
            parts += ["", "**Патч**", f"```diff\n{item.diff[:6000]}\n```"]
        if item.test_output:
            parts += ["", "**Проверка**", f"```\n{item.test_output[-3000:]}\n```"]
        if item.apply_error:
            parts += ["", f"⚠️ {item.apply_error}"]
        return "\n".join(parts)

    # ------------------------------------------------------------- monitoring
    def _loop(self) -> None:
        # начинаем с конца: старые ошибки не должны стать инцидентом сразу
        self._scan(initialize=True)
        while not self._stop.is_set():
            self._wake.wait(timeout=SCAN_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._scan()
            except Exception:  # noqa: BLE001
                logger.exception("Self-healing: сбой сканирования лога")

    def _scan(self, initialize: bool = False) -> int:
        log_path = Path(self.config.logging.file)
        if not log_path.is_absolute():
            log_path = self.root / log_path
        try:
            size = log_path.stat().st_size
        except OSError:
            return 0
        state = self._load_state()
        offset = int(state.get("offset", 0) or 0)
        if initialize or offset <= 0 or offset > size:
            state["offset"] = size
            self._save_state(state)
            return 0
        try:
            with log_path.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read(1_000_000)
        except OSError:
            return 0
        if not raw:
            return 0
        state["offset"] = offset + len(raw)
        self._save_state(state)
        text = raw.decode("utf-8", errors="replace")
        occurrences: dict[str, list[float]] = state.setdefault("occurrences", {})
        created = 0
        now = time.time()
        window = max(1, self.cfg.window_minutes) * 60
        for line in text.splitlines():
            if not re.search(r"\b(ERROR|CRITICAL)\b", line):
                continue
            if "nemus.core.self_healing" in line:
                continue
            fingerprint = error_fingerprint(line)
            stamps = [
                float(stamp)
                for stamp in occurrences.get(fingerprint, [])
                if now - float(stamp) <= window
            ]
            stamps.append(now)
            occurrences[fingerprint] = stamps
            if len(stamps) < max(1, self.cfg.error_threshold):
                continue
            occurrences[fingerprint] = []
            if self._recent_incident(fingerprint, self.cfg.cooldown_minutes * 60):
                continue
            incident = self._create_incident(line, fingerprint, log_path)
            if incident is not None:
                created += 1
        cutoff = now - window * 2
        state["occurrences"] = {
            key: [stamp for stamp in values if stamp >= cutoff]
            for key, values in occurrences.items()
            if any(stamp >= cutoff for stamp in values)
        }
        self._save_state(state)
        return created

    def _create_incident(
        self, line: str, fingerprint: str, log_path: Path
    ) -> HealingIncident | None:
        incident_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + fingerprint[:4]
        context = self._tail(log_path, CONTEXT_LINES)
        item = HealingIncident(
            id=incident_id,
            fingerprint=fingerprint,
            created_at=time.time(),
            title=normalize_error(line)[:120] or "Ошибка в логе",
            error=line.strip()[:1000],
            context=context[-12_000:],
        )
        self._save_incident(item)
        logger.warning(f"Self-healing: инцидент {item.id} — {item.title}")
        if self.on_incident is not None:
            try:
                self.on_incident(item)
            except Exception:  # noqa: BLE001
                logger.exception("Self-healing: обработчик инцидента")
        if self.cfg.auto_propose:
            threading.Thread(
                target=self._prepare_incident,
                args=(item.id,),
                name=f"heal-{item.id}",
                daemon=True,
            ).start()
        return item

    def _prepare_incident(self, incident_id: str) -> None:
        item = self.get_incident(incident_id)
        if item is None:
            return
        diagnosis, diff, error = self._propose_patch(item)
        item.diagnosis = diagnosis
        item.diff = diff
        item.apply_error = error
        item.status = "proposed" if diff else "diagnosed"
        item.attempts += 1
        self._save_incident(item)
        if self.on_incident is not None:
            try:
                self.on_incident(item)
            except Exception:  # noqa: BLE001
                logger.exception("Self-healing: обработчик предложения")
        if diff and self.cfg.auto_apply:
            self.apply(incident_id)

    def _propose_patch(self, item: HealingIncident) -> tuple[str, str, str]:
        files = self._traceback_files(item.error, item.context)
        sources: list[str] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = path.relative_to(self.root).as_posix()
            sources.append(f"### {relative}\n```\n{text[:MAX_SOURCE_CHARS]}\n```")
        prompt = (
            "Ты — инженер по надёжности. Ниже ошибка из лога и исходники. "
            "Предложи минимальный безопасный патч. Верни строго JSON без "
            "markdown-обёртки: {\"diagnosis\": \"...\", \"patch\": \"unified diff\"}. "
            "Патч только для файлов проекта; никаких секретов, удаления данных "
            "и команд. Если исправление неочевидно, верни пустой patch.\n\n"
            f"ОШИБКА:\n{item.error}\n\nКОНТЕКСТ:\n{item.context}\n\n"
            + "\n\n".join(sources)
        )
        try:
            response = self.llm.complete(
                [
                    Message(role="system", content="Отвечай только валидным JSON."),
                    Message(role="user", content=prompt),
                ],
                max_tokens=5000,
            )
            text = (
                getattr(response, "text", "")
                or getattr(response, "content", "")
                or ""
            ).strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return "Модель не вернула диагноз.", "", "ответ модели не JSON"
            data = json.loads(match.group(0))
            diagnosis = str(data.get("diagnosis") or "").strip()
            diff = str(data.get("patch") or "").strip()
            if not diff:
                return diagnosis or "Патч не предложен.", "", ""
            error = self.validate_patch(diff)
            return diagnosis or "Патч готов.", diff if not error else "", error
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Self-healing: не удалось предложить патч: {exc}")
            return "Не удалось сгенерировать патч.", "", str(exc)

    def _traceback_files(self, error: str, context: str) -> list[Path]:
        found: list[Path] = []
        for raw in re.findall(r'File "([^"]+)"', error + "\n" + context):
            path = Path(raw)
            if not path.is_absolute():
                path = self.root / path
            try:
                resolved = path.resolve()
                resolved.relative_to(self.root)
            except (OSError, ValueError):
                continue
            if resolved.suffix != ".py" or resolved in found:
                continue
            found.append(resolved)
            if len(found) >= 6:
                break
        return found

    def validate_patch(self, diff: str) -> str:
        """Проверяет diff до применения; возвращает текст ошибки или пусто."""
        lines = diff.splitlines()
        if len(lines) > max(40, self.cfg.max_diff_lines):
            return f"патч слишком большой: {len(lines)} строк"
        if "GIT binary patch" in diff or "Binary files" in diff:
            return "бинарные файлы не поддерживаются"
        paths: list[str] = []
        for line in lines:
            if line.startswith("+++ "):
                raw = line[4:].strip()
                if raw == "/dev/null":
                    continue
                raw = raw.removeprefix("b/").removeprefix("a/")
                if raw.startswith("/") or ".." in Path(raw).parts:
                    return f"недопустимый путь: {raw}"
                if raw.startswith(("data/", ".git/")) or Path(raw).name.startswith(".env"):
                    return f"запрещённый путь: {raw}"
                paths.append(raw)
        if not paths:
            return "в патче нет файлов"
        if any(raw.startswith("-- ") or raw.startswith("++ ") for raw in paths):
            return "повреждённый diff"
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=self.root,
            input=diff,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if check.returncode != 0:
            return (check.stderr or check.stdout or "git apply --check не прошёл")[-800:]
        return ""

    # --------------------------------------------------------------- actions
    def apply(self, incident_id: str) -> tuple[bool, str]:
        item = self.get_incident(incident_id)
        if item is None:
            return False, "Инцидент не найден."
        if not item.diff:
            return False, "У инцидента нет патча."
        if item.status == "applied":
            return False, "Патч уже применён."
        error = self.validate_patch(item.diff)
        if error:
            item.apply_error = error
            item.status = "failed"
            self._save_incident(item)
            return False, f"Патч не прошёл проверку: {error}"
        files = self._diff_files(item.diff)
        item.files = files
        backup_dir = self.backups_dir / item.id
        backup_dir.mkdir(parents=True, exist_ok=True)
        backups: list[tuple[Path, Path | None]] = []
        new_files: list[str] = []
        try:
            for relative in files:
                path = (self.root / relative).resolve()
                path.relative_to(self.root)
                if path.exists():
                    target = backup_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    backups.append((path, target))
                else:
                    new_files.append(relative)
            item.backup_dir = str(backup_dir)
            item.new_files = new_files
            applied = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=self.root,
                input=item.diff,
                text=True,
                capture_output=True,
                timeout=60,
            )
            if applied.returncode != 0:
                raise RuntimeError(applied.stderr or applied.stdout or "git apply")
        except Exception as exc:  # noqa: BLE001
            self._restore(backups, new_files)
            item.status = "failed"
            item.apply_error = str(exc)[-1000:]
            self._save_incident(item)
            return False, f"Не удалось применить патч: {exc}"

        test_command = self.cfg.test_command.replace(
            "{python}", f'"{sys.executable}"'
        )
        try:
            check = subprocess.run(
                test_command,
                cwd=self.root,
                shell=True,
                text=True,
                capture_output=True,
                timeout=TEST_TIMEOUT,
            )
            output = (check.stdout or "") + (check.stderr or "")
            item.test_output = output[-6000:]
            if check.returncode != 0:
                self._restore(backups, new_files)
                item.status = "failed"
                item.apply_error = f"проверка упала (код {check.returncode})"
                self._save_incident(item)
                return False, f"Проверка не прошла, изменения откатили.\n{output[-3000:]}"
        except Exception as exc:  # noqa: BLE001
            self._restore(backups, new_files)
            item.status = "failed"
            item.apply_error = f"проверка не выполнена: {exc}"
            self._save_incident(item)
            return False, f"Проверка не выполнена, изменения откатили: {exc}"

        item.status = "applied"
        item.apply_error = ""
        self._save_incident(item)
        return True, f"Патч применён и проверен: {test_command}"

    def rollback(self, incident_id: str) -> tuple[bool, str]:
        item = self.get_incident(incident_id)
        if item is None:
            return False, "Инцидент не найден."
        if item.status != "applied":
            return False, "Откатывать можно только применённый патч."
        applied = subprocess.run(
            ["git", "apply", "-R", "--whitespace=nowarn", "-"],
            cwd=self.root,
            input=item.diff,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if applied.returncode != 0:
            # патч мог быть частично изменён — возвращаем резервные копии
            backups: list[tuple[Path, Path | None]] = []
            for relative in item.files:
                path = self.root / relative
                backup = Path(item.backup_dir) / relative if item.backup_dir else None
                if backup is not None and backup.exists():
                    backups.append((path, backup))
            self._restore(backups, item.new_files)
        item.status = "rolled_back"
        self._save_incident(item)
        return True, "Патч откатил."

    def reject(self, incident_id: str) -> tuple[bool, str]:
        item = self.get_incident(incident_id)
        if item is None:
            return False, "Инцидент не найден."
        item.status = "rejected"
        self._save_incident(item)
        return True, "Инцидент отклонён."

    def _restore(
        self, backups: list[tuple[Path, Path | None]], new_files: list[str]
    ) -> None:
        for path, backup in backups:
            try:
                if backup is not None and backup.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, path)
            except OSError:
                logger.exception(f"Self-healing: не удалось восстановить {path}")
        for relative in new_files:
            try:
                (self.root / relative).unlink(missing_ok=True)
            except OSError:
                pass

    def _diff_files(self, diff: str) -> list[str]:
        files: list[str] = []
        for line in diff.splitlines():
            if line.startswith("+++ "):
                raw = line[4:].strip()
                if raw == "/dev/null":
                    continue
                raw = raw.removeprefix("b/").removeprefix("a/")
                if raw and raw not in files:
                    files.append(raw)
        return files

    # ---------------------------------------------------------------- storage
    def list_incidents(self) -> list[HealingIncident]:
        items: list[HealingIncident] = []
        for path in self.incidents_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items.append(HealingIncident.from_json(data))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def get_incident(self, incident_id: str) -> HealingIncident | None:
        path = self.incidents_dir / f"{incident_id}.json"
        try:
            return HealingIncident.from_json(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return None

    def _save_incident(self, item: HealingIncident) -> None:
        path = self.incidents_dir / f"{item.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(item.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_file)

    def _recent_incident(self, fingerprint: str, cooldown: float) -> bool:
        cutoff = time.time() - cooldown
        return any(
            item.fingerprint == fingerprint and item.created_at >= cutoff
            for item in self.list_incidents()
        )

    @staticmethod
    def _tail(path: Path, lines: int) -> str:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return "".join(handle.readlines()[-lines:])
        except OSError:
            return ""


__all__ = [
    "HealingIncident",
    "HealingManager",
    "error_fingerprint",
    "normalize_error",
]
