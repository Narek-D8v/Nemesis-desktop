"""Инструмент выполнения команд оболочки.

Почему устроено именно так (инцидент 30.10.2026, команда
``cd D:\\Nemesis && npx hyperframes --version``):

``subprocess.run(..., capture_output=True, timeout=60)`` на Windows по
таймауту убивает только непосредственного потомка (``cmd.exe``), а затем
блокируется в ``communicate()`` **без таймаута** — внуки (``npx`` → ``node``)
унаследовали каналы вывода и держат их открытыми. Таймаут срабатывал, но
результат до модели не доходил: вызов висел вечно, история обрывалась на
вызове без ответа.

Отсюда правила:

* дедлайн контролируем сами и **всегда** возвращаемся к модели;
* по дедлайну процесс не убивается, а переходит в фоновую сессию — модель
  продолжает читать вывод и может ответить на запрос ввода;
* ``stdin`` по умолчанию закрыт (``DEVNULL``): интерактивный вопрос вроде
  «Ok to proceed? (y)» не может заблокировать чтение, а сам вопрос
  распознаётся и подсвечивается модели;
* дерево процессов убивается целиком (``taskkill /T /F``), чтобы не
  оставлять сирот, держащие каналы вывода;
* вывод собирается потоково и декодируется utf-8 → cp866 → cp1251.
"""

from __future__ import annotations

import atexit
import os
import platform
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import IO, Any

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.sandbox import PathGuard, PathNotAllowed

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 60
#: значения wait по умолчанию, если модель не передала свой
WAIT_BY_ACTION = {"start": 5, "read": 10}
MAX_SESSIONS = 6
#: сессия без активности дольше этого времени останавливается
SESSION_TTL = 30 * 60.0
BUFFER_LIMIT = 512_000
READ_CHUNK = 8192
#: ждём закрытия каналов вывода после выхода процесса
DRAIN_TIMEOUT = 1.0
#: ждём завершение процесса после kill — ограничено, чтобы не зависнуть снова
KILL_WAIT = 10.0

IS_WINDOWS = platform.system() == "Windows"
_ENCODINGS = ("utf-8", "cp866", "cp1251")

#: «Ok to proceed? (y/n)», «Continue? [Y/n]», «Press ENTER to continue»…
_PROMPT_RE = re.compile(
    r"(?i)(ok to proceed\?|\by/n\b|\[[ymn]/[yn]\]|\(y/n\)|\byes/no\b|"
    r"continue\?|are you sure|proceed\?|press enter|press any key|"
    r"продолжить\?|введите\s+да|да/нет)"
)
#: unix-утилиты, которых нет в cmd — признак команды в стиле POSIX
_POSIX_RE = re.compile(
    r"(?<![\w.-])(ls|cat|grep|egrep|tail|head|which|sed|awk|cut|sort|uniq|wc|"
    r"find|touch|mkdir|cp|mv|rm|ln|chmod|curl|wget|tar|ps|uname|date|diff|"
    r"export|source)(?![\w-])"
)
#: %LOCALAPPDATA% и подобное — синтаксис cmd, в bash он не работает
_CMD_ENV_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")

_SESSIONS: dict[str, _ShellSession] = {}
_SESSIONS_LOCK = threading.Lock()


def _decode(data: bytes) -> str:
    """Восстанавливает кодировку вывода: utf-8 (node/python), иначе OEM/ANSI."""
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _git_bash() -> str | None:
    """Путь к Git Bash. WSL (C:\\Windows\\System32\\bash.exe) не берём:
    та оболочка выполняет команды в другой файловой системе."""
    if not IS_WINDOWS:
        return None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(entry.strip('"'), "bash.exe")
        normalized = candidate.replace("/", "\\").lower()
        if "\\git" in normalized and os.path.isfile(candidate):
            return candidate
    for candidate in (
        os.path.expandvars(r"%ProgramFiles%\Git\bin\bash.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Git\bin\bash.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\Git\bin\bash.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Останавливает процесс **вместе с дочерними** и ждёт не дольше KILL_WAIT."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 — запасной путь: убиваем только корень
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=KILL_WAIT)
    except subprocess.TimeoutExpired:
        pass  # канадидат-сирота: дальше уже не ждём, иначе зависнет сам инструмент


class _ShellSession:
    """Процесс оболочки плюс накапливаемый вывод, доступный модели на лету."""

    def __init__(
        self,
        command: str,
        shell: str,
        cwd: str,
        proc: subprocess.Popen,
    ) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.command = command
        self.shell = shell
        self.cwd = cwd
        self.proc = proc
        self.started = time.monotonic()
        self.last_active = self.started
        self.cond = threading.Condition()
        self.buf = bytearray()
        #: позиция (в символах), до которой вывод уже отдан модели
        self.read_chars = 0
        self.exit_code: int | None = None
        self._readers_alive = 0
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------- потоки
    def start_readers(self, stdout: IO[bytes] | None, stderr: IO[bytes] | None) -> None:
        for stream in (stdout, stderr):
            if stream is None:
                continue
            self._readers_alive += 1
            thread = threading.Thread(
                target=self._pump, args=(stream,), daemon=True, name=f"shell-{self.id}"
            )
            thread.start()
            self._threads.append(thread)
        thread = threading.Thread(target=self._reap, daemon=True, name=f"shell-wait-{self.id}")
        thread.start()
        self._threads.append(thread)

    def _pump(self, stream: IO[bytes]) -> None:
        # read1 (а не read) — read() на BufferedReader ждёт ПОЛНЫЕ n байт
        # и не отдаёт данные, пока процесс не завершится; read1 возвращается
        # сразу после первого чтения из канала.
        reader = getattr(stream, "read1", stream.read)
        try:
            while True:
                chunk = reader(READ_CHUNK)
                if not chunk:
                    break
                with self.cond:
                    self.buf.extend(chunk)
                    self._trim_locked()
                    self.cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self.cond:
                self._readers_alive -= 1
                self.cond.notify_all()
            try:
                stream.close()
            except OSError:
                pass

    def _reap(self) -> None:
        try:
            code = self.proc.wait()
        except Exception:  # noqa: BLE001
            code = -1
        with self.cond:
            self.exit_code = code
            self.cond.notify_all()

    def _trim_locked(self) -> None:
        """Не даёт буферу расти бесконечно, сохраняя корректный индекс чтения."""
        if len(self.buf) <= BUFFER_LIMIT:
            return
        cut = len(self.buf) - BUFFER_LIMIT // 2
        dropped = _decode(bytes(self.buf[:cut]))
        del self.buf[:cut]
        self.read_chars = max(0, self.read_chars - len(dropped))

    # ------------------------------------------------------------- вывод
    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def readers_alive(self) -> bool:
        with self.cond:
            return self._readers_alive > 0

    def sync_exit(self) -> None:
        """Фиксирует код выхода сразу же, не дожидаясь потока-смотрителя."""
        code = self.proc.poll()
        if code is None:
            return
        with self.cond:
            self.exit_code = code
            self.cond.notify_all()

    def text(self) -> str:
        with self.cond:
            return _decode(bytes(self.buf))

    def take_new(self) -> str:
        with self.cond:
            return self._take_new_locked()

    def _take_new_locked(self) -> str:
        full = _decode(bytes(self.buf))
        new = full[self.read_chars :]
        self.read_chars = len(full)
        self.last_active = time.monotonic()
        return new

    def wait_new(self, timeout: float) -> str:
        """Ждёт новые данные (или завершение) и возвращает только их."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self.cond:
            snapshot = len(self.buf)
            while True:
                if (
                    len(self.buf) != snapshot
                    or self.exit_code is not None
                    or self._readers_alive == 0
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            new = self._take_new_locked()

        # Каналы закрыты — процесс на грани выхода. Даём ему долю секунды,
        # иначе модель прочитает устаревшее «выполняется» и решит, что
        # команда ещё работает.
        if self.exit_code is None and self._readers_alive == 0:
            grace = time.monotonic() + 0.5
            while time.monotonic() < grace:
                self.sync_exit()
                if self.exit_code is not None:
                    break
                time.sleep(0.03)
        return new

    def wait_drain(self, timeout: float = DRAIN_TIMEOUT) -> None:
        """Ждёт, пока потоки допишут вывод после завершения процесса."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self._readers_alive > 0 and time.monotonic() < deadline:
                self.cond.wait(deadline - time.monotonic())

    def send(self, text: str) -> bool:
        stdin = self.proc.stdin
        if stdin is None or self.exit_code is not None:
            return False
        payload = text if text.endswith("\n") else text + "\n"
        try:
            # stdin — байтовый поток; utf-8 согласуется с PYTHONIOENCODING у детей
            stdin.write(payload.encode("utf-8", errors="replace"))
            stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def kill(self) -> None:
        _kill_tree(self.proc)

    def state(self) -> str:
        self.sync_exit()
        if self.exit_code is None:
            return "выполняется"
        return f"завершён, код возврата: {self.exit_code}"

    def prompt_hint(self) -> str:
        """Подсказка, если процесс жив и в конце вывода ждут ввода."""
        self.sync_exit()
        if not self.running:
            return ""
        tail = self.text()[-400:]
        if not _PROMPT_RE.search(tail):
            return ""
        return (
            "⚠️ Процесс ожидает ввода. Ответь: "
            f'action=write, session={self.id}, input="y" (или нужным текстом).'
        )


def _register(session: _ShellSession) -> None:
    _reap_sessions()
    with _SESSIONS_LOCK:
        _SESSIONS[session.id] = session
        while len(_SESSIONS) > MAX_SESSIONS:
            oldest = min(_SESSIONS.values(), key=lambda s: s.last_active)
            if oldest.id == session.id:
                break
            del _SESSIONS[oldest.id]
            oldest.kill()


def _reap_sessions() -> None:
    with _SESSIONS_LOCK:
        stale = [
            s for s in _SESSIONS.values() if time.monotonic() - s.last_active > SESSION_TTL
        ]
        for session in stale:
            del _SESSIONS[session.id]
    for session in stale:
        session.kill()


def _kill_all_sessions() -> None:
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.kill()


atexit.register(_kill_all_sessions)


def _write_stdin(proc: subprocess.Popen, text: str, close: bool = False) -> bool:
    """Отправляет строку в stdin процесса (байтовый поток, кодировка utf-8)."""
    stdin = proc.stdin
    if stdin is None:
        return False
    payload = text if text.endswith("\n") else text + "\n"
    try:
        stdin.write(payload.encode("utf-8", errors="replace"))
        stdin.flush()
    except (OSError, ValueError):
        return False
    if close:
        try:
            stdin.close()
        except OSError:
            pass
    return True


def session_help(session: _ShellSession, tool: str = "run_shell") -> str:
    """Инструкция для модели: как продолжить работу с фоновой сессией."""
    return (
        f"Фоновая сессия (управляется инструментом {tool}):\n"
        f"  {tool}: action=read,  session={session.id}, wait=10 — новый вывод в "
        "реальном времени\n"
        f'  {tool}: action=write, session={session.id}, input="y" — ответить на '
        "запрос ввода\n"
        f"  {tool}: action=kill,  session={session.id} — остановить вместе с "
        "дочерними"
    )


def _pump_bytes(stream: IO[bytes] | None, buf: bytearray) -> None:
    """Собирает байты одного канала потоково (read1 — не ждёт полного буфера)."""
    if stream is None:
        return
    reader = getattr(stream, "read1", stream.read)
    try:
        while True:
            chunk = reader(READ_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


@dataclass
class CapturedRun:
    """Захваченный вывод: stdout и stderr отдельно, зависаний не бывает."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    #: дедлайн истёк — процесс остановлен вместе с дочерними
    timed_out: bool = False


def run_capture(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 60,
    creationflags: int = 0,
    env_extra: dict[str, str] | None = None,
) -> CapturedRun:
    """Запускает процесс и собирает stdout/stderr с жёстким дедлайном.

    Замена ``subprocess.run(..., capture_output=True, timeout=...)``: на Windows
    тот по таймауту убивал только прямого потомка, а затем блокировался в
    ``communicate()`` **без таймаута** — внуки держали каналы вывода, и вызов
    висел вечно, теряя весь накопленный вывод.

    Здесь: дочерние процессы читаются потоками, по дедлайну убивается всё
    дерево (``taskkill /T /F``), ожидание потоков ограничено — функция всегда
    возвращается, а всё напечатанное возвращается вместе с ней.
    """
    env = os.environ.copy()
    # дети-питон печатают utf-8, иначе русский вывод декодируется в мусор
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_extra:
        env.update(env_extra)
    kwargs: dict[str, Any] = {}
    if IS_WINDOWS and creationflags:
        kwargs["creationflags"] = creationflags
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **kwargs,
    )
    out = bytearray()
    err = bytearray()
    threads = [
        threading.Thread(target=_pump_bytes, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_pump_bytes, args=(proc.stderr, err), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)  # деревом: одиночный kill оставляет сирот с каналами
    # ожидание потоков ограничено — именно тут старый код зависал навсегда
    for thread in threads:
        thread.join(timeout=2.0)

    code = proc.poll()
    if code is None:
        code = -1
    return CapturedRun(code, bytes(out), bytes(err), timed_out)


@dataclass
class BoundedRun:
    """Результат запуска с дедлайном: вывод не теряется никогда."""

    returncode: int | None
    output: str
    #: дедлайн истёк — процесс продолжает работать в фоновой сессии
    timed_out: bool
    #: не None, если процесс ещё жив или каналы вывода открыты
    session: _ShellSession | None = None


def run_bounded(
    argv: list[str],
    cwd: str | None,
    timeout: float,
    *,
    label: str,
    shell: str = "python",
    stdin_text: str | None = None,
) -> BoundedRun:
    """Запускает процесс с жёстким дедлайном и потоковым чтением вывода.

    В отличие от ``subprocess.run(..., timeout=...)`` на Windows здесь нет
    блокирующего ``communicate()`` после kill: по дедлайну процесс не убивается,
    а переходит в фоновую сессию, а дерево потомков убивается только через
    явный ``kill``. Вывод возвращается целиком — и то, что напечатано до
    дедлайна, и то, что идёт после (через ``session``).
    """
    env = os.environ.copy()
    # дети-питон печатают utf-8, иначе русский вывод декодируется в мусор
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # stdin всегда PIPE: наследование консоли позволяло программе зависнуть
    # на чтении, а здесь модель может ответить через action=write
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if stdin_text:
        _write_stdin(proc, stdin_text, close=True)
    session = _ShellSession(label, shell, cwd or "", proc)
    session.start_readers(proc.stdout, proc.stderr)
    try:
        proc.wait(timeout=max(1.0, float(timeout)))
        session.sync_exit()
    except subprocess.TimeoutExpired:
        session.wait_drain()
        _register(session)
        return BoundedRun(None, session.text(), True, session)

    session.sync_exit()
    session.wait_drain()
    if session.readers_alive:
        # потомки ещё держат каналы вывода — оставляем сессию открытой
        _register(session)
        return BoundedRun(session.exit_code, session.text(), False, session)
    try:
        if session.proc.stdin:
            session.proc.stdin.close()
    except OSError:
        pass
    return BoundedRun(session.exit_code, session.text(), False, None)


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "Выполняет команду в системной оболочке и возвращает её вывод.\n"
        "Оболочка: на Windows по умолчанию cmd, но команда в стиле POSIX "
        "(ls, grep, tail, which, | , && с unix-утилитами) выполняется в Git Bash "
        "автоматически — в ответе видно, в какой оболочке шло выполнение; "
        "shell='cmd' | 'powershell' | 'bash' задаётся явно. Рабочий каталог по "
        "умолчанию — рабочая папка ассистента: к файлам проекта обращайся по "
        "абсолютному пути (в bash путь D:\\Nemesis пиши как /d/Nemesis).\n"
        "Действия (action):\n"
        "• 'run' (по умолчанию) — выполнить и вернуть вывод; держится дедлайн "
        "`timeout` секунд, дальше команда НЕ убивается, а переходит в фоновую "
        "сессию — id будет в ответе;\n"
        "• 'start' — запустить в фоне и сразу вернуть id сессии и первые строки "
        "вывода;\n"
        "• 'read' — прочитать новый вывод сессии; так ты видишь терминал в "
        "реальном времени и опрашиваешь его, пока идёт рендер/установка/сборка;\n"
        "• 'write' — отправить строку в stdin живой сессии (ответить на вопрос "
        "«(y/n)», ввести пароль, нажать Enter);\n"
        "• 'kill' — остановить сессию вместе со всеми дочерними процессами.\n"
        "Долгие команды (рендер видео, npm/npx, установки, сборки, ожидание "
        "сервера) запускай через action=start и опрашивай action=read, а не "
        "action=run с большим таймаутом.\n"
        "stdin по умолчанию закрыт, поэтому программа, требующая ввода, тебя не "
        "заблокирует: она либо завершится, либо её запрос ввода будет распознан "
        "и отмечен в выводе — тогда отвечай через action=write."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Команда (для action='run'/'start').",
            },
            "action": {
                "type": "string",
                "enum": ["run", "start", "read", "write", "kill"],
                "description": "Что сделать (по умолчанию 'run').",
            },
            "session": {
                "type": "string",
                "description": "id сессии — для action='read'/'write'/'kill'.",
            },
            "input": {
                "type": "string",
                "description": (
                    "Текст для stdin: ответ на запрос ввода (для 'write') либо "
                    "входные данные команды (для 'run')."
                ),
            },
            "wait": {
                "type": "integer",
                "description": (
                    "Сколько секунд ждать новых данных ('start' — первый вывод, "
                    "по умолчанию 5; 'read' — по умолчанию 10)."
                ),
            },
            "shell": {
                "type": "string",
                "enum": ["cmd", "powershell", "bash", "sh"],
                "description": "Какую оболочку использовать (иначе автовыбор).",
            },
            "cwd": {
                "type": "string",
                "description": "Рабочий каталог (необязательно).",
            },
            "timeout": {
                "type": "integer",
                "description": "Дедлайн 'run' в секундах (по умолчанию 60).",
            },
        },
        "required": [],
    }
    risk = RiskLevel.DANGEROUS

    #: подставляется фабрикой из config.tools.workspace
    guard: PathGuard | None = None

    # ------------------------------------------------------------- вход
    def execute(
        self,
        command: str = "",
        action: str = "run",
        session: str = "",
        input: str = "",  # noqa: A002 — имя параметра продиктовано схемой
        wait: int = 0,
        shell: str = "",
        cwd: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        **_: object,
    ) -> ToolResult:
        action = (action or "run").lower().strip()
        if action in {"read", "write", "kill"}:
            return self._session_action(action, session, input, wait)
        if action not in {"run", "start"}:
            return ToolResult.failure(
                f"Неизвестное действие '{action}'. Доступны: run, start, read, write, kill."
            )

        if not command.strip():
            return ToolResult.failure("Пустая команда.")

        workdir = self._workdir(cwd)
        if isinstance(workdir, ToolResult):
            return workdir

        shell_name = self._resolve_shell(command, shell)
        # run без input: stdin закрыт, иначе программа, читающая stdin,
        # заблокируется навсегда. start: stdin открыт — через него пойдёт write.
        want_stdin = action == "start" or bool(input)
        try:
            proc = self._spawn(command, shell_name, workdir, want_stdin=want_stdin)
        except FileNotFoundError:
            return ToolResult.failure(f"Оболочка недоступна: {shell_name}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка запуска: {exc}")

        session_obj = _ShellSession(command, shell_name, workdir, proc)
        session_obj.start_readers(proc.stdout, proc.stderr)

        if input and action == "start":
            session_obj.send(input)
        elif input:
            self._write_and_close(proc, input)

        if action == "start":
            return self._started(session_obj, wait or WAIT_BY_ACTION["start"])
        return self._run(session_obj, timeout)

    # ------------------------------------------------------------- run
    def _run(self, session: _ShellSession, timeout: int) -> ToolResult:
        timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), 3600))
        try:
            session.proc.wait(timeout=timeout)
            session.sync_exit()
        except subprocess.TimeoutExpired:
            # Не убиваем: длинная команда уходит в фон, вывод продолжает идти.
            session.wait_drain()
            _register(session)
            return self._promoted(session, timeout)

        session.sync_exit()
        session.wait_drain()
        if session.readers_alive:
            # Каналы ещё открыты — кто-то из потомков продолжает писать.
            _register(session)
            return self._promoted(session, timeout, exited=True)
        return ToolResult(
            ok=session.exit_code == 0,
            content=self._report(session, session.text()),
        )

    def _promoted(
        self, session: _ShellSession, timeout: int, exited: bool = False
    ) -> ToolResult:
        tail = session.text()[-MAX_OUTPUT:]
        if exited:
            status = (
                f"завершён (код возврата: {session.exit_code}), но дочерний процесс "
                "ещё держит канал вывода — сессия осталась открытой"
            )
        else:
            status = f"выполняется дольше {timeout} с — переведена в фоновую сессию"
        parts = [
            f"$ {session.command}",
            f"оболочка: {session.shell}",
            f"статус: {status} {session.id}",
            "Вывод, который уже есть:" if tail else "Вывода пока нет.",
        ]
        if tail:
            parts.append(tail)
        parts.append(self._session_help(session))
        hint = session.prompt_hint()
        if hint:
            parts.append(hint)
        return ToolResult.success("\n".join(parts))

    # ------------------------------------------------------------- start
    def _started(self, session: _ShellSession, wait: int) -> ToolResult:
        new_output = session.wait_new(max(0, min(int(wait), 60)))
        session.sync_exit()
        _register(session)
        if session.exit_code is not None and not session.readers_alive:
            return ToolResult(
                ok=session.exit_code == 0,
                content=self._report(session, session.text()),
            )
        parts = [
            f"$ {session.command}",
            f"оболочка: {session.shell}",
            f"сессия запущена: {session.id} ({session.state()})",
            "Первый вывод:" if new_output else "Вывода пока нет — команда молчит, опрашивай.",
        ]
        if new_output:
            parts.append(new_output[-MAX_OUTPUT:])
        parts.append(self._session_help(session))
        hint = session.prompt_hint()
        if hint:
            parts.append(hint)
        return ToolResult.success("\n".join(parts))

    # ------------------------------------------------------------- read/write/kill
    def _session_action(
        self, action: str, session_id: str, text: str, wait: int
    ) -> ToolResult:
        session_id = (session_id or "").strip()
        with _SESSIONS_LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            active = ", ".join(sorted(_SESSIONS)) or "нет"
            return ToolResult.failure(
                f"Сессия '{session_id}' не найдена. Активные сессии: {active}. "
                "Запусти команду заново через action='run' или action='start'."
            )

        if action == "kill":
            session.kill()
            tail = session.take_new()
            with _SESSIONS_LOCK:
                _SESSIONS.pop(session.id, None)
            parts = [f"Сессия {session.id} остановлена вместе с дочерними процессами."]
            if tail:
                parts.append(f"Последний вывод:\n{tail[-MAX_OUTPUT:]}")
            return ToolResult.success("\n".join(parts))

        if action == "write":
            if not text:
                return ToolResult.failure("Не передан input — что отправить в stdin.")
            if not session.send(text):
                session.kill()
                with _SESSIONS_LOCK:
                    _SESSIONS.pop(session.id, None)
                return ToolResult.failure(
                    f"Сессия {session.id} уже завершилась (код: {session.exit_code}) — "
                    "ввод не отправлен."
                )
            tail = session.wait_new(1.0)
            session.sync_exit()
            parts = [f"Ввод отправлен в сессию {session.id}: {text}"]
            if tail:
                parts.append(f"Ответ программы:\n{tail[-MAX_OUTPUT:]}")
            return ToolResult.success("\n".join(parts))

        # read
        timeout = WAIT_BY_ACTION["read"]
        if wait is not None and int(wait) > 0:
            timeout = min(int(wait), 120)
        new_output = session.wait_new(timeout)
        session.sync_exit()
        parts = [f"Сессия {session.id}: {session.state()}."]
        if new_output:
            parts.append(new_output[-MAX_OUTPUT:])
        else:
            parts.append("(нового вывода нет)")
        hint = session.prompt_hint()
        if hint:
            parts.append(hint)
        return ToolResult.success("\n".join(parts))

    # ------------------------------------------------------------- helpers
    def _workdir(self, cwd: str | None) -> str | ToolResult:
        if self.guard is None:
            self.guard = PathGuard()
        self.guard.ensure_dir()
        if not cwd:
            return str(self.guard.workspace)
        try:
            return str(self.guard.resolve_write(cwd, what="Рабочий каталог"))
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc))

    def _resolve_shell(self, command: str, requested: str) -> str:
        if requested:
            return requested.strip().lower()
        if not IS_WINDOWS:
            return "sh"
        if _CMD_ENV_RE.search(command):
            return "cmd"
        if _POSIX_RE.search(command) and _git_bash():
            return "bash"
        return "cmd"

    @staticmethod
    def _spawn(
        command: str, shell: str, cwd: str, want_stdin: bool
    ) -> subprocess.Popen:
        env = os.environ.copy()
        # Дети-питон печатают utf-8, иначе русский вывод декодируется в мусор
        env.setdefault("PYTHONIOENCODING", "utf-8")
        stdin = subprocess.PIPE if want_stdin else subprocess.DEVNULL
        kwargs: dict[str, Any] = dict(
            cwd=cwd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return subprocess.Popen(RunShellTool._build_argv(command, shell), **kwargs)

    @staticmethod
    def _build_argv(command: str, shell: str) -> str | list[str]:
        """Что передать в Popen.

        Для cmd — строка, а не список: список Python экранирует кавычки как
        ``\\"``, а cmd.exe не понимает такой экранировки и превращает
        ``python -c \"...\"`` в несуществующую команду. Строка после
        ``cmd /c`` передаётся как есть, внешние кавычки снимает сам cmd.
        """
        shell = (shell or "cmd").lower()
        if IS_WINDOWS:
            if shell in {"powershell", "pwsh"}:
                return [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ]
            if shell == "bash":
                return [_git_bash() or "bash", "-c", command]
            if shell == "sh":
                return ["sh", "-c", command]
            return f'cmd /c "{command}"'
        if shell == "cmd":
            return ["sh", "-c", command]
        return [shell, "-c", command]

    @staticmethod
    def _write_and_close(proc: subprocess.Popen, text: str) -> None:
        _write_stdin(proc, text, close=True)

    @staticmethod
    def _report(session: _ShellSession, body: str) -> str:
        parts = [
            f"$ {session.command}",
            f"оболочка: {session.shell}",
            f"код возврата: {session.exit_code}",
        ]
        if body:
            text = body[-MAX_OUTPUT:]
            parts.append(f"вывод:\n{text}")
        elif session.exit_code == 0:
            parts.append("(вывода нет)")
        return "\n".join(parts)

    @staticmethod
    def _session_help(session: _ShellSession) -> str:
        return session_help(session, tool="run_shell")


def shell_tools() -> list[Tool]:
    return [RunShellTool()]
