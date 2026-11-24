"""Фоновые команды с журналом, остановкой и уведомлением о завершении."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nemus.utils.logger import logger

#: сколько строк журнала храним на задачу
MAX_LINES = 500
#: сколько фоновых процессов разрешаем одновременно
MAX_RUNNING = 5
#: флаг Windows — не показывать консольное окно
CREATE_NO_WINDOW = 0x08000000
#: сколько ждём завершения после terminate, сек
STOP_TIMEOUT = 5.0


@dataclass(frozen=True)
class ProcessSnapshot:
    """Снимок состояния фоновой задачи."""

    job_id: int
    command: str
    chat_id: int
    started_at: float
    ended_at: float | None
    exit_code: int | None
    running: bool


class ProcessJob:
    """Один запущенный процесс и его журнал."""

    def __init__(
        self,
        job_id: int,
        command: str,
        chat_id: int,
        cwd: Path,
        on_done: Callable[["ProcessJob"], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.command = command
        self.chat_id = chat_id
        self.cwd = cwd
        self.on_done = on_done
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self._process: subprocess.Popen[str] | None = None
        self._lines: deque[str] = deque(maxlen=MAX_LINES)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("задача уже запущена")
        creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
        self._thread = threading.Thread(
            target=self._pump,
            name=f"job-{self.job_id}",
            daemon=True,
        )
        self._thread.start()

    def _append(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        with self._lock:
            self._lines.append(text)

    def _pump(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                self._append(line)
            process.wait()
        except Exception as exc:  # noqa: BLE001
            self._append(f"[ошибка чтения вывода: {exc}]")
        finally:
            self.ended_at = time.time()
            self.exit_code = process.poll()
            if self.exit_code is None:
                try:
                    self.exit_code = process.wait(timeout=1)
                except Exception:  # noqa: BLE001
                    self.exit_code = -1
            with self._lock:
                if self.exit_code == 0:
                    self._lines.append("✅ команда завершилась успешно")
                else:
                    self._lines.append(
                        f"❌ команда завершилась с кодом {self.exit_code}"
                    )
            callback = self.on_done
            if callback is not None:
                try:
                    callback(self)
                except Exception:  # noqa: BLE001
                    logger.exception("Фоновые команды: ошибка уведомления")

    def tail(self, lines: int = 40) -> str:
        with self._lock:
            items = list(self._lines)[-max(1, min(lines, MAX_LINES)) :]
        return "\n".join(items) if items else "(вывод пока пуст)"

    def stop(self) -> bool:
        """Завершает процесс и его дочерние процессы; True — если был жив."""
        process = self._process
        if process is None or process.poll() is not None:
            return False
        try:
            import psutil

            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except Exception:  # noqa: BLE001
                    pass
            parent.terminate()
            _, alive = psutil.wait_procs([parent, *children], timeout=STOP_TIMEOUT)
            for item in alive:
                try:
                    item.kill()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            try:
                process.terminate()
                process.wait(timeout=STOP_TIMEOUT)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass
        return True

    def snapshot(self) -> ProcessSnapshot:
        return ProcessSnapshot(
            job_id=self.job_id,
            command=self.command,
            chat_id=self.chat_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            exit_code=self.exit_code,
            running=self.running,
        )


class ProcessJobs:
    """Коллекция фоновых задач одного бота."""

    def __init__(
        self,
        workspace: str | Path = ".",
        on_done: Callable[[ProcessJob], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.on_done = on_done
        self._jobs: dict[int, ProcessJob] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    def start(self, command: str, chat_id: int) -> ProcessJob:
        command = command.strip()
        if not command:
            raise ValueError("пустая команда")
        with self._lock:
            running = sum(1 for job in self._jobs.values() if job.running)
            if running >= MAX_RUNNING:
                raise RuntimeError(
                    f"уже выполняется {running} команд (лимит {MAX_RUNNING}); "
                    "останови одну через /kill"
                )
            job_id = self._next_id
            self._next_id += 1
            self._jobs[job_id] = ProcessJob(
                job_id=job_id,
                command=command,
                chat_id=chat_id,
                cwd=self.workspace,
                on_done=self.on_done,
            )
            job = self._jobs[job_id]
        try:
            job.start()
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        return job

    def get(self, job_id: int) -> ProcessJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[ProcessJob]:
        with self._lock:
            return sorted(
                self._jobs.values(), key=lambda job: job.job_id, reverse=True
            )[:limit]

    def running(self) -> list[ProcessJob]:
        with self._lock:
            return [job for job in self._jobs.values() if job.running]

    def stop(self, job_id: int) -> bool:
        job = self.get(job_id)
        return job.stop() if job is not None else False

    def stop_all(self) -> None:
        for job in self.running():
            try:
                job.stop()
            except Exception:  # noqa: BLE001
                pass
