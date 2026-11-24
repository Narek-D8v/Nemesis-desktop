"""Локальные GGUF-модели через llama-cpp-python.

LiteLLM не умеет загружать GGUF в процессе, поэтому пакет
``llama-cpp-python[server]`` поднимает локальный OpenAI-совместимый сервер,
а LiteLLM ходит в него как в обычный OpenAI endpoint. Так сохраняются
стриминг, tool-calls и fallback, а модель не требует ни ключа, ни интернета.

Сервер запускается лениво при первом запросе, перезапускается при смене
GGUF и гасится вместе с приложением. Уже работающий внешний сервер на том же
порту не убивается: к нему просто подключаются.
"""

from __future__ import annotations

import atexit
import hashlib
import http.client
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from nemus.config import DATA_DIR, PROJECT_ROOT
from nemus.core.providers.hf_download import HFDownloadError, ensure_hf_model
from nemus.utils.logger import logger

if TYPE_CHECKING:
    from nemus.config import Config

#: не показывать чёрное консольное окно при старте сервера на Windows
CREATE_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if os.name == "nt"
    else 0
)

#: сколько строк хвоста лога показать, если сервер не поднялся
_LOG_TAIL_LINES = 25


class LlamaCppError(RuntimeError):
    """Ошибка локального llama.cpp: нет пакета, модели или сервер не поднялся."""


class PackageStatus(NamedTuple):
    """Состояние llama-cpp-python: работает, отсутствует или повреждён."""

    available: bool
    broken: bool
    detail: str = ""


def package_available() -> bool:
    """Установлены ли сам пакет и его серверная часть."""
    # после pip install в текущем процессе кэш импортов ещё старый
    importlib.invalidate_caches()
    try:
        if importlib.util.find_spec("llama_cpp") is None:
            return False
        return importlib.util.find_spec("llama_cpp.server") is not None
    except (ImportError, OSError, ValueError):
        # пакет есть, но нативная библиотека не загрузилась — считаем, что нет
        return False


def package_status() -> PackageStatus:
    """Проверяет пакет в отдельном процессе, не загружая его в GUI.

    Импорт нативной библиотеки в процессе приложения блокирует DLL на Windows,
    после чего pip не может переустановить повреждённый пакет. Поэтому проверка
    идёт через ``python -c``, а не внутри текущего процесса.
    """
    importlib.invalidate_caches()
    try:
        spec = importlib.util.find_spec("llama_cpp")
    except (ImportError, OSError, ValueError) as exc:
        return PackageStatus(False, True, f"не удалось проверить пакет: {exc}")
    if spec is None:
        return PackageStatus(False, False)

    if getattr(sys, "frozen", False):
        # В frozen-сборке установка недоступна, а отдельного python.exe нет.
        locations = spec.submodule_search_locations or ()
        server_found = any(
            (Path(location) / "server" / "__init__.py").is_file()
            for location in locations
        )
        if server_found:
            return PackageStatus(True, False)
        return PackageStatus(False, True, "не найдена серверная часть пакета")

    try:
        result = subprocess.run(
            [sys.executable, "-c", "import llama_cpp.server"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=_pip_env(),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return PackageStatus(False, True, "проверка импорта зависла")
    except OSError as exc:
        return PackageStatus(False, True, f"не удалось проверить импорт: {exc}")

    if result.returncode == 0:
        return PackageStatus(True, False)
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    detail = lines[-1] if lines else f"импорт завершился с кодом {result.returncode}"
    if len(detail) > 300:
        detail = detail[:297] + "…"
    return PackageStatus(False, True, detail)


def install_hint() -> str:
    """Команда установки, которую можно показать пользователю как есть.

    ``--only-binary`` важен: на PyPI бывает более новая версия без готового
    колеса, и pip пытается собрать её из C++ (на Windows без Visual Studio
    это сразу падает). Бинарное колесо берётся из CPU-индекса проекта.
    """
    return (
        f'"{sys.executable}" -m pip install --only-binary=:all: '
        '--no-cache-dir "llama-cpp-python[server]" '
        f"--extra-index-url {_CPU_WHEEL_INDEX}"
    )


#: готовые CPU-колёса (не требуют CMake/Visual Studio на Windows)
_CPU_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"


class LlamaCppInstallCancelled(LlamaCppError):
    """Установка пакета отменена пользователем."""


def install_command(
    wheels: list[Path] | None = None, force: bool = False
) -> list[str]:
    """Команда pip: подбор пакета или установка уже скачанных колёс."""
    if wheels:
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--disable-pip-version-check",
            "--no-input",
        ]
        if force:
            command.append("--force-reinstall")
        command.extend(str(path) for path in wheels)
        return command
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "llama-cpp-python[server]",
        "--extra-index-url",
        _CPU_WHEEL_INDEX,
    ]


class _Wheel(NamedTuple):
    """Файл, который pip собирается установить."""

    url: str
    filename: str
    sha256: str


def _report_command(report_path: Path, force: bool = False) -> list[str]:
    """Просит pip подобрать пакеты и записать точный список файлов."""
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--no-input",
        "--progress-bar",
        "off",
        "--report",
        str(report_path),
        "llama-cpp-python[server]",
        "--extra-index-url",
        _CPU_WHEEL_INDEX,
    ]
    if force:
        command.insert(5, "--ignore-installed")
    return command


def _wheels_from_report(report_path: Path) -> list[_Wheel]:
    """Достаёт из JSON-отчёта pip ссылки, имена и хеши колёс."""
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LlamaCppError(f"pip не создал отчёт об установке: {exc}") from exc
    items = data.get("install") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    wheels: list[_Wheel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info = item.get("download_info")
        if not isinstance(info, dict):
            continue
        url = str(info.get("url") or "").strip()
        filename = Path(urllib.parse.urlparse(url).path).name
        if not url or not filename:
            continue
        if not filename.lower().endswith(".whl"):
            raise LlamaCppError(
                f"pip предложил не готовое колесо, а {filename}. "
                "Установка из исходников не поддерживается."
            )
        archive = info.get("archive_info")
        hashes = archive.get("hashes") if isinstance(archive, dict) else None
        sha256 = str(hashes.get("sha256") or "") if isinstance(hashes, dict) else ""
        wheels.append(_Wheel(url, filename, sha256))
    return wheels


def _pip_env() -> dict[str, str]:
    """Окружение pip: UTF-8 и без буферизации вывода."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _run_pip(
    command: list[str], cancel: threading.Event | None
) -> tuple[int, list[str], bool]:
    """Запускает pip, собирает хвост вывода и поддерживает отмену."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_pip_env(),
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise LlamaCppError(f"Не удалось запустить pip: {exc}") from exc

    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_output, name="llama-pip", daemon=True)
    reader.start()
    tail: deque[str] = deque(maxlen=20)
    cancelled = False
    while True:
        if cancel is not None and cancel.is_set() and process.poll() is None:
            cancelled = True
            process.terminate()
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None and not reader.is_alive():
                break
            continue
        if line is None:
            break
        clean = line.strip()
        if clean:
            tail.append(clean)

    try:
        code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        code = process.wait(timeout=5)
    return code, list(tail), cancelled


def _remote_size(url: str) -> int:
    """Размер файла по HTTP; 0 — сервер его не сообщил."""
    headers = {
        "User-Agent": "nemus-llama-cpp",
        "Accept-Encoding": "identity",
    }
    for method in ("HEAD", "GET"):
        try:
            request = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.headers.get("Content-Length")
                if raw:
                    return max(0, int(raw))
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return 0


def _download_wheel(
    wheel: _Wheel,
    destination: Path,
    cancel: threading.Event | None,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """Скачивает колесо и сообщает, сколько байт уже получено."""
    request = urllib.request.Request(
        wheel.url,
        headers={
            "User-Agent": "nemus-llama-cpp",
            "Accept-Encoding": "identity",
        },
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=30) as response,
            destination.open("wb") as file,
        ):
            expected_raw = response.headers.get("Content-Length")
            while True:
                if cancel is not None and cancel.is_set():
                    raise LlamaCppInstallCancelled("Установка отменена.")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                file.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if on_bytes is not None:
                    on_bytes(size)
            expected = int(expected_raw) if expected_raw else 0
            if expected and expected != size:
                raise LlamaCppError(
                    f"Файл {wheel.filename} скачался не полностью: "
                    f"{size} из {expected} байт."
                )
    except LlamaCppInstallCancelled:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise LlamaCppError(
            f"Не удалось скачать {wheel.filename}: {exc}"
        ) from exc
    if wheel.sha256 and digest.hexdigest() != wheel.sha256:
        raise LlamaCppError(
            f"Файл {wheel.filename} скачался повреждённым "
            "(не совпала контрольная сумма)."
        )
    return size


def _download_wheels(
    wheels: list[_Wheel],
    directory: Path,
    on_progress: Callable[[int, int], None] | None,
    cancel: threading.Event | None,
) -> list[Path]:
    """Скачивает все колёса, отдавая точный прогресс по общему размеру."""
    directory.mkdir(parents=True, exist_ok=True)
    sizes: list[int] = []
    for wheel in wheels:
        if cancel is not None and cancel.is_set():
            raise LlamaCppInstallCancelled("Установка отменена.")
        sizes.append(_remote_size(wheel.url))
    total = sum(sizes) if all(sizes) else 0
    done = 0
    last_emit = 0.0

    def emit(force: bool = False) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and now - last_emit < 0.12:
            return
        last_emit = now
        try:
            on_progress(done, total)
        except Exception:  # noqa: BLE001 - сигнал GUI не должен ронять загрузку
            pass

    emit(force=True)
    paths: list[Path] = []
    downloaded = 0
    for wheel in wheels:
        destination = directory / wheel.filename
        paths.append(destination)

        def add(size: int, base: int = downloaded) -> None:
            nonlocal done
            done = base + size
            emit()

        downloaded += _download_wheel(wheel, destination, cancel, add)
        done = downloaded
    emit(force=True)
    return paths


def _emit_stage(on_stage: Callable[[str], None] | None, stage: str) -> None:
    """Отправляет этап в GUI, не позволяя сбою сигнала прервать установку."""
    if on_stage is None:
        return
    try:
        on_stage(stage)
    except Exception:  # noqa: BLE001
        pass


def install_llama_cpp(
    on_stage: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    force: bool = False,
) -> None:
    """Ставит ``llama-cpp-python[server]`` готовым колесом в текущий venv.

    Сначала pip подбирает точный список колёс, затем приложение скачивает их
    само и сообщает ``on_progress(получено, всего)``. Только после этого pip
    ставит уже локальные файлы. ``on_stage`` получает ``resolve/download/
    install/done``. При отмене поднимается LlamaCppInstallCancelled.
    ``force=True`` переустанавливает даже уже числящийся установленным пакет.
    """
    if not force and package_available():
        _emit_stage(on_stage, "done")
        return
    if force:
        # На Windows запущенный сервер держит DLL и не даёт заменить файлы.
        stop_llama_cpp_server()

    _emit_stage(on_stage, "resolve")
    with tempfile.TemporaryDirectory(
        prefix="nemus-llama-install-", ignore_cleanup_errors=True
    ) as raw_dir:
        workdir = Path(raw_dir)
        report_path = workdir / "pip-report.json"
        code, tail, cancelled = _run_pip(
            _report_command(report_path, force=force), cancel
        )
        if cancelled:
            raise LlamaCppInstallCancelled("Установка отменена.")
        if code != 0:
            detail = "\n".join(tail) or "pip не вывел подробностей"
            raise LlamaCppError(
                f"Не удалось подобрать пакеты (код {code}):\n{detail}"
            )

        wheels = _wheels_from_report(report_path)
        if not wheels:
            raise LlamaCppError(
                "pip не нашёл файлов для установки. Пакет числится "
                "установленным, но не загружается — удалите его и повторите."
            )

        _emit_stage(on_stage, "download")
        paths = _download_wheels(wheels, workdir, on_progress, cancel)

        _emit_stage(on_stage, "install")
        code, tail, cancelled = _run_pip(
            install_command(paths, force=force), cancel
        )
        if cancelled:
            raise LlamaCppInstallCancelled("Установка отменена.")
        if code != 0:
            detail = "\n".join(tail) or "pip не вывел подробностей"
            raise LlamaCppError(
                f"pip install завершился с кодом {code}:\n{detail}"
            )

    status = package_status()
    if not status.available:
        detail = f"\n{status.detail}" if status.detail else ""
        raise LlamaCppError(
            "pip завершился успешно, но llama_cpp не загружается. "
            f"Проверьте, что установка шла в venv приложения.{detail}"
        )
    _emit_stage(on_stage, "done")


def resolve_model_path(cfg: "Config", model: str) -> Path:
    """Превращает ``llama_cpp/<...>`` в путь к существующему GGUF.

    Поддерживаются три варианта:
    * ``llama_cpp/local`` (или алиас) — путь из ``llm.llama_cpp.model_path``;
    * ``llama_cpp/data/models/foo.gguf`` — путь относительно корня проекта;
    * ``llama_cpp/C:/models/foo.gguf`` — абсолютный путь.
    """
    raw = (model or "").strip()
    if raw.startswith("llama_cpp/"):
        raw = raw[len("llama_cpp/") :]
    raw = raw.strip()

    lc = cfg.llm.llama_cpp
    aliases = {"", "local", "default", "model", lc.model_alias.strip()}
    if raw in aliases or not raw:
        path = Path(lc.model_path) if lc.model_path else None
        if path is None:
            raise LlamaCppError(
                "Не задан путь к GGUF-модели. Укажите llm.llama_cpp.model_path "
                "или выберите файл в настройках."
            )
    else:
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            if Path(raw).suffix.lower() != ".gguf" and lc.model_path:
                # «llama_cpp/имя» могло быть алиасом, а не относительным путём
                path = Path(lc.model_path)
            else:
                # последний шанс: файл лежит в models_dir, а в модели имя
                candidate = Path(lc.models_dir) / Path(raw).name
                if candidate.exists():
                    path = candidate

    path = path.expanduser().resolve()
    if not path.is_file():
        raise LlamaCppError(
            f"GGUF-модель не найдена: {path}. Проверьте llm.llama_cpp.model_path "
            "или выберите существующий файл в настройках."
        )
    if path.suffix.lower() != ".gguf":
        raise LlamaCppError(f"Ожидался файл .gguf, получено: {path.name}")
    return path


def _api_base(cfg: "Config") -> str:
    """OpenAI-совместимый адрес локального сервера."""
    pc = cfg.llm.providers.get("llama_cpp")
    if pc and pc.api_base:
        return pc.api_base.rstrip("/")
    lc = cfg.llm.llama_cpp
    return f"http://{lc.host}:{lc.port}/v1"


def _http_models(url: str, timeout: float = 1.5) -> list[str]:
    """Список id моделей живого сервера; пусто — сервер недоступен."""
    request = urllib.request.Request(
        f"{url}/models", headers={"User-Agent": "nemus-llama-cpp"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [
        str(item.get("id") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]


def build_server_command(cfg: "Config", model_path: Path) -> list[str]:
    """Команда запуска ``python -m llama_cpp.server`` для выбранного GGUF."""
    lc = cfg.llm.llama_cpp
    if getattr(sys, "frozen", False):
        # PyInstaller: отдельного python.exe нет, запускаем режим сервера
        # внутри самого Nemesis.exe (см. nemus/__main__.py).
        cmd = [sys.executable, "--llama-server"]
    else:
        cmd = [sys.executable, "-m", "llama_cpp.server"]
    cmd += [
        "--model",
        str(model_path),
        "--model_alias",
        lc.model_alias or "nemus-local",
        "--host",
        lc.host or "127.0.0.1",
        "--port",
        str(lc.port),
        "--n_ctx",
        str(lc.n_ctx),
        "--n_gpu_layers",
        str(lc.n_gpu_layers),
        "--n_batch",
        str(lc.n_batch),
        "--verbose",
        "true" if lc.verbose else "false",
    ]
    if lc.n_threads:
        cmd.extend(["--n_threads", str(lc.n_threads)])
    if lc.chat_format.strip():
        cmd.extend(["--chat_format", lc.chat_format.strip()])
    cmd.extend(str(arg) for arg in lc.extra_args if str(arg).strip())
    return cmd


def _log_tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-_LOG_TAIL_LINES:]).strip()


class LlamaCppServer:
    """Один управляемый сервер llama.cpp на процесс приложения."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._model_path: Path | None = None
        self._api_base = ""
        self._log_file: Any = None
        self._log_path = DATA_DIR / "llama_cpp_server.log"

    # --------------------------------------------------------------- public
    def ensure(self, cfg: "Config", model: str) -> str:
        """Гарантирует, что на порту работает сервер с нужной моделью.

        Возвращает OpenAI-совместимый ``api_base``.
        """
        try:
            ensure_hf_model(cfg, model)
        except HFDownloadError as exc:
            raise LlamaCppError(str(exc)) from exc
        path = resolve_model_path(cfg, model)
        base = _api_base(cfg)
        lc = cfg.llm.llama_cpp
        provider_cfg = cfg.llm.providers.get("llama_cpp")
        external_base = bool(provider_cfg and provider_cfg.api_base)

        with self._lock:
            # уже работающий внешний сервер не трогаем
            if external_base:
                if not _http_models(base):
                    raise LlamaCppError(
                        f"llama.cpp сервер не отвечает по адресу {base}. "
                        "Запустите его вручную или очистите "
                        "llm.providers.llama_cpp.api_base."
                    )
                return base

            if self._process is not None and self._process.poll() is None:
                if self._model_path == path and _http_models(base):
                    return base
                logger.info("llama.cpp: меняю модель — перезапускаю сервер")
                self._stop_locked()

            ids = _http_models(base)
            if ids:
                alias = lc.model_alias or "nemus-local"
                if alias in ids:
                    logger.info(
                        f"llama.cpp: использую уже запущенный сервер {base}"
                    )
                    return base
                raise LlamaCppError(
                    f"Порт {lc.port} занят другим сервером llama.cpp "
                    f"(модели: {', '.join(ids) or 'неизвестны'}). "
                    "Остановите его или укажите другой llm.llama_cpp.port."
                )

            if not lc.auto_start:
                raise LlamaCppError(
                    "Сервер llama.cpp не запущен, а llm.llama_cpp.auto_start "
                    "выключен. Запустите сервер вручную или включите автозапуск."
                )
            status = package_status()
            if not status.available:
                detail = f"\n{status.detail}" if status.detail else ""
                raise LlamaCppError(
                    "llama-cpp-python[server] не установлен или повреждён. "
                    f"Установите заново: {install_hint()}{detail}"
                )

            self._start_locked(cfg, path, base)
            return base

    def stop(self) -> None:
        """Останавливает только тот сервер, который запустили мы сами."""
        with self._lock:
            self._stop_locked()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -------------------------------------------------------------- internal
    def _start_locked(self, cfg: "Config", path: Path, base: str) -> None:
        lc = cfg.llm.llama_cpp
        cmd = build_server_command(cfg, path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_path.open("a", encoding="utf-8")
        self._log_file.write(
            f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} llama.cpp: "
            f"{path.name} ---\n"
        )
        self._log_file.flush()
        logger.info(
            f"llama.cpp: запускаю {path.name} на {lc.host}:{lc.port} "
            f"(ctx={lc.n_ctx}, gpu={lc.n_gpu_layers})"
        )
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self._close_log_locked()
            raise LlamaCppError(
                f"Не удалось запустить сервер llama.cpp: {exc}"
            ) from exc

        self._model_path = path
        self._api_base = base
        deadline = time.monotonic() + max(10.0, float(lc.start_timeout))
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                code = self._process.returncode
                tail = _log_tail(self._log_path)
                self._stop_locked()
                raise LlamaCppError(
                    "Сервер llama.cpp завершился при загрузке модели "
                    f"(код {code}).\n{tail or 'Смотрите data/llama_cpp_server.log'}"
                )
            if _http_models(base):
                logger.info("llama.cpp: модель загружена, сервер готов")
                return
            time.sleep(0.35)

        tail = _log_tail(self._log_path)
        self._stop_locked()
        raise LlamaCppError(
            "Сервер llama.cpp не поднялся за "
            f"{lc.start_timeout:.0f} с. Увеличьте llm.llama_cpp.start_timeout "
            "или проверьте модель.\n"
            f"{tail or 'Смотрите data/llama_cpp_server.log'}"
        )

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._model_path = None
        self._api_base = ""
        if process is not None and process.poll() is None:
            logger.info("llama.cpp: останавливаю локальный сервер")
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("llama.cpp: сервер не завершился после kill")
            except OSError:
                pass
        self._close_log_locked()

    def _close_log_locked(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None


_MANAGER = LlamaCppServer()


def ensure_llama_cpp_server(cfg: "Config", model: str) -> str:
    """Поднимает/проверяет локальный сервер для модели ``llama_cpp/...``."""
    return _MANAGER.ensure(cfg, model)


def stop_llama_cpp_server() -> None:
    """Останавливает запущенный нами сервер (вызывается при выходе)."""
    _MANAGER.stop()


atexit.register(stop_llama_cpp_server)


__all__ = [
    "LlamaCppError",
    "PackageStatus",
    "build_server_command",
    "ensure_llama_cpp_server",
    "install_hint",
    "package_available",
    "package_status",
    "resolve_model_path",
    "stop_llama_cpp_server",
]
