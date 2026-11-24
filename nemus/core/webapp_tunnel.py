"""Автоматический HTTPS-туннель для Telegram Mini App.

Cloudflare Quick Tunnel не требует аккаунта и токена: бот сам запускает
``cloudflared tunnel --url http://127.0.0.1:<port>``, читает публичный
``https://....trycloudflare.com`` из вывода и отдаёт его в ``/panel``.
Если ``cloudflared`` не установлен, бинарник скачивается в ``data/bin``.
"""

from __future__ import annotations

import atexit
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from nemus.config import DATA_DIR
from nemus.utils.logger import logger

#: быстрый туннель печатает ссылку в stderr в рамке из символов
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
#: ngrok печатает публичный URL в JSON-логе или в таблице
NGROK_URL_RE = re.compile(
    r"https://[a-z0-9-]+\.ngrok(?:-free)?\.(?:dev|app)", re.IGNORECASE
)


def find_tunnel_url(text: str) -> str:
    """Возвращает первый публичный URL из строки cloudflared."""
    match = TUNNEL_URL_RE.search(text or "")
    return match.group(0) if match else ""


def find_ngrok_url(text: str) -> str:
    """Возвращает первый постоянный dev-URL из вывода ngrok."""
    match = NGROK_URL_RE.search(text or "")
    return match.group(0) if match else ""


def _asset_name() -> str:
    """Имя бинарника cloudflared для текущей платформы."""
    machine = platform.machine().lower()
    arm = "arm64" in machine or "aarch64" in machine
    if os.name == "nt":
        return "cloudflared-windows-arm64.exe" if arm else "cloudflared-windows-amd64.exe"
    if sys.platform.startswith("linux"):
        return "cloudflared-linux-arm64" if arm else "cloudflared-linux-amd64"
    if sys.platform == "darwin":
        return "cloudflared-darwin-arm64.tgz" if arm else "cloudflared-darwin-amd64.tgz"
    return ""


def _download_cloudflared(destination: Path) -> str:
    """Скачивает официальный бинарник Cloudflare в ``data/bin``."""
    asset = _asset_name()
    if not asset:
        logger.warning("Туннель: для этой платформы нет готового cloudflared.")
        return ""
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    try:
        logger.info(f"Туннель: скачиваю cloudflared ({asset})…")
        request = urllib.request.Request(
            url, headers={"User-Agent": "Nemus/Telegram-WebApp"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            with temp.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        if asset.endswith(".tgz"):
            with tarfile.open(temp, "r:gz") as archive:
                member = next(
                    (
                        item
                        for item in archive.getmembers()
                        if item.name.endswith("cloudflared") and item.isfile()
                    ),
                    None,
                )
                if member is None:
                    raise RuntimeError("в архиве нет cloudflared")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError("не удалось распаковать cloudflared")
                with destination.open("wb") as handle:
                    shutil.copyfileobj(extracted, handle)
            temp.unlink(missing_ok=True)
        else:
            os.replace(temp, destination)
        if os.name != "nt":
            destination.chmod(0o755)
        logger.info(f"Туннель: cloudflared готов: {destination}")
        return str(destination)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Туннель: cloudflared не скачан: {exc}")
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return ""


def cleanup_stale_tunnels(local_url: str) -> None:
    """Убивает cloudflared от предыдущего процесса с тем же localhost-портом.

    ``schtasks /End``/жёсткое завершение Python не даёт дочернему процессу
    закрыться; без уборки после каждого рестарта остаётся лишний туннель.
    """
    target = local_url.rstrip("/")
    if not target:
        return
    try:
        if os.name == "nt":
            # WMIC удалён в новых Windows, поэтому основной путь — PowerShell.
            script = (
                "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" "
                "| ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
            )
            completed = subprocess.run(
                [
                    shutil.which("powershell") or "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in completed.stdout.splitlines():
                if target not in line:
                    continue
                parts = line.split("\t", 1)
                pid = parts[0].strip()
                if not pid.isdigit() or int(pid) == os.getpid():
                    continue
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                logger.info(f"Туннель: убран старый cloudflared (PID {pid}).")
        else:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in completed.stdout.splitlines():
                if target not in line or "cloudflared" not in line.lower():
                    continue
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                if pid == os.getpid():
                    continue
                try:
                    os.kill(pid, 15)
                    logger.info(f"Туннель: убран старый cloudflared (PID {pid}).")
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Туннель: уборка старых процессов: {exc}")


def ensure_cloudflared(binary: str = "cloudflared", *, download: bool = True) -> str:
    """Ищет cloudflared в PATH/проекте, при необходимости скачивает."""
    candidate = (binary or "cloudflared").strip()
    path = Path(candidate).expanduser()
    if path.exists() and path.is_file():
        return str(path)
    found = shutil.which(candidate)
    if found:
        return found
    if not download:
        return ""
    destination = DATA_DIR / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if destination.exists():
        return str(destination)
    return _download_cloudflared(destination)


def _ngrok_asset() -> str:
    """Имя zip-архива ngrok для текущей платформы."""
    machine = platform.machine().lower()
    arm = "arm64" in machine or "aarch64" in machine
    if os.name == "nt":
        return "ngrok-v3-stable-windows-arm64.zip" if arm else "ngrok-v3-stable-windows-amd64.zip"
    if sys.platform.startswith("linux"):
        return "ngrok-v3-stable-linux-arm64.zip" if arm else "ngrok-v3-stable-linux-amd64.zip"
    if sys.platform == "darwin":
        return "ngrok-v3-stable-darwin-arm64.zip" if arm else "ngrok-v3-stable-darwin-amd64.zip"
    return ""


def _download_ngrok(destination: Path) -> str:
    """Скачивает официальный ngrok agent в ``data/bin``."""
    asset = _ngrok_asset()
    if not asset:
        logger.warning("ngrok: для этой платформы нет готового архива.")
        return ""
    url = f"https://bin.equinox.io/c/bNyj1mQVY4c/{asset}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    archive_path = destination.with_name(destination.name + ".zip")
    try:
        logger.info(f"ngrok: скачиваю agent ({asset})…")
        request = urllib.request.Request(
            url, headers={"User-Agent": "Nemus/Telegram-WebApp"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            with archive_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        with zipfile.ZipFile(archive_path) as archive:
            member = next(
                (item for item in archive.namelist() if item.endswith("ngrok.exe" if os.name == "nt" else "ngrok")),
                None,
            )
            if member is None:
                raise RuntimeError("в архиве нет ngrok")
            with archive.open(member) as source, temp.open("wb") as target:
                shutil.copyfileobj(source, target)
        os.replace(temp, destination)
        if os.name != "nt":
            destination.chmod(0o755)
        logger.info(f"ngrok: agent готов: {destination}")
        return str(destination)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ngrok: agent не скачан: {exc}")
        for path in (temp, archive_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return ""


def ensure_ngrok(binary: str = "ngrok", *, download: bool = True) -> str:
    """Ищет ngrok в PATH/проекте, при необходимости скачивает."""
    candidate = (binary or "ngrok").strip()
    path = Path(candidate).expanduser()
    if path.exists() and path.is_file():
        return str(path)
    found = shutil.which(candidate)
    if found:
        return found
    destination = DATA_DIR / "bin" / ("ngrok.exe" if os.name == "nt" else "ngrok")
    if destination.exists():
        return str(destination)
    if not download:
        return ""
    return _download_ngrok(destination)


class CloudflaredTunnel:
    """Процесс Cloudflare Quick Tunnel с публичным URL и остановкой."""

    def __init__(
        self,
        local_url: str,
        *,
        binary: str = "cloudflared",
        download: bool = True,
        timeout: float = 45.0,
        health_timeout: float = 12.0,
    ) -> None:
        self.local_url = local_url.rstrip("/")
        self.binary = binary
        self.download = download
        self.timeout = timeout
        self.health_timeout = health_timeout
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._url = ""
        self._error = ""
        self._lock = threading.Lock()
        atexit.register(self.stop)

    @property
    def command(self) -> list[str]:
        return [
            self.binary,
            "tunnel",
            "--url",
            self.local_url,
            "--no-autoupdate",
        ]

    @property
    def public_url(self) -> str:
        return self._url if self._ready.is_set() else ""

    @property
    def starting(self) -> bool:
        if self._ready.is_set() or self._error:
            return False
        return bool(self._process is not None) or bool(
            self._reader is not None and self._reader.is_alive()
        )

    @property
    def failed(self) -> bool:
        return bool(self._error)

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> bool:
        """Запускает cloudflared; ожидание URL идёт в фоне."""
        if self._process is not None:
            return True
        if self._stop.is_set():
            return False
        cleanup_stale_tunnels(self.local_url)
        resolved = ensure_cloudflared(self.binary, download=self.download)
        if self._stop.is_set():
            return False
        if not resolved:
            with self._lock:
                self._error = "cloudflared не найден"
            logger.warning(
                "Туннель: cloudflared не найден. Установи cloudflared или задай "
                "telegram.web_app_url вручную."
            )
            return False
        self.binary = resolved
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = str(exc)
            logger.warning(f"Туннель: не удалось запустить cloudflared: {exc}")
            return False
        self._reader = threading.Thread(
            target=self._pump, name="cloudflared-tunnel", daemon=True
        )
        self._reader.start()
        logger.info(f"Туннель: запускаю cloudflared для {self.local_url}")
        return True

    def _pump(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if self._stop.is_set():
                    break
                logger.debug(f"cloudflared: {line.rstrip()}")
                url = find_tunnel_url(line)
                if url and not self._url:
                    self._url = url
                    logger.info(f"Туннель: публичный URL {url}")
                    if self._healthy(url):
                        self._ready.set()
                    else:
                        logger.warning(
                            "Туннель: URL получен, но /api/health пока не отвечает."
                        )
                        # Всё равно отдаём URL: DNS может догнать через пару секунд.
                        self._ready.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Туннель: чтение вывода: {exc}")
        finally:
            code = process.poll()
            if code is None and not self._stop.is_set():
                # stdout закрылся, но процесс ещё жив: не теряем рабочий URL.
                # Ждём реального завершения, чтобы бот мог перезапустить туннель.
                try:
                    code = process.wait(timeout=120)
                except Exception:  # noqa: BLE001
                    code = process.poll()
            if not self._stop.is_set() and code is not None:
                with self._lock:
                    self._error = f"cloudflared завершился (код {code})"
                logger.warning(f"Туннель: cloudflared завершился с кодом {code}")
                self._ready.clear()

    def check_health(self, timeout: float = 5.0) -> bool:
        """Один быстрый запрос к публичному URL (для сторожа туннеля)."""
        url = self.public_url
        if not url:
            return False
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=timeout) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _healthy(self, url: str) -> bool:
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
                    if response.status == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.7)
        return False

    def wait_ready(self, timeout: float | None = None) -> str:
        self._ready.wait(self.timeout if timeout is None else timeout)
        return self.public_url

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        self._process = None
        self._ready.clear()
        self._url = ""
        if process is None:
            return
        try:
            process.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info("Туннель: cloudflared остановлен.")


class NgrokTunnel:
    """Постоянный HTTPS-туннель ngrok через официальный agent.

    На free-плане ngrok выдаёт один стабильный dev-домен вида
    ``name.ngrok-free.dev``; он не меняется при перезапуске агента.
    """

    def __init__(
        self,
        local_url: str,
        *,
        expected_url: str = "",
        authtoken: str = "",
        binary: str = "ngrok",
        download: bool = True,
        timeout: float = 45.0,
        health_timeout: float = 12.0,
    ) -> None:
        self.local_url = local_url.rstrip("/")
        self.expected_url = expected_url.strip().rstrip("/")
        self.authtoken = authtoken.strip()
        self.binary = binary
        self.download = download
        self.timeout = timeout
        self.health_timeout = health_timeout
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._url = self.expected_url
        self._error = ""
        self._lock = threading.Lock()
        atexit.register(self.stop)

    @property
    def target(self) -> str:
        return self.local_url.split("://", 1)[-1].rstrip("/")

    @property
    def command(self) -> list[str]:
        command = [
            self.binary,
            "http",
            self.target,
            "--log=stdout",
            "--log-format=json",
        ]
        if self.expected_url:
            command.extend(["--url", self.expected_url])
        return command

    @property
    def public_url(self) -> str:
        return self._url if self._ready.is_set() else ""

    @property
    def starting(self) -> bool:
        if self._ready.is_set() or self._error:
            return False
        return bool(self._process is not None) or bool(
            self._reader is not None and self._reader.is_alive()
        )

    @property
    def failed(self) -> bool:
        return bool(self._error)

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> bool:
        if self._process is not None:
            return True
        if self._stop.is_set():
            return False
        if not self.authtoken:
            with self._lock:
                self._error = "ngrok authtoken не задан"
            logger.warning(
                "ngrok: не задан authtoken. Укажи telegram.web_app_ngrok_authtoken."
            )
            return False
        resolved = ensure_ngrok(self.binary, download=self.download)
        if self._stop.is_set():
            return False
        if not resolved:
            with self._lock:
                self._error = "ngrok не найден"
            return False
        self.binary = resolved
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["NGROK_AUTHTOKEN"] = self.authtoken
        try:
            self._process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = str(exc)
            logger.warning(f"ngrok: не удалось запустить: {exc}")
            return False
        if self.expected_url:
            self._ready.set()
        self._reader = threading.Thread(target=self._pump, name="ngrok-tunnel", daemon=True)
        self._reader.start()
        logger.info(f"ngrok: запускаю agent для {self.local_url}")
        return True

    def _pump(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if self._stop.is_set():
                    break
                logger.debug(f"ngrok: {line.rstrip()}")
                if self._url:
                    continue
                url = find_ngrok_url(line)
                if url:
                    self._url = url
                    logger.info(f"ngrok: публичный URL {url}")
                    self._ready.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"ngrok: чтение вывода: {exc}")
        finally:
            code = process.poll()
            if code is None and not self._stop.is_set():
                try:
                    code = process.wait(timeout=120)
                except Exception:  # noqa: BLE001
                    code = process.poll()
            if not self._stop.is_set() and code is not None:
                with self._lock:
                    self._error = f"ngrok завершился (код {code})"
                logger.warning(f"ngrok: agent завершился с кодом {code}")
                self._ready.clear()

    def check_health(self, timeout: float = 5.0) -> bool:
        """Один быстрый запрос к публичному URL (для сторожа туннеля)."""
        url = self.public_url
        if not url:
            return False
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=timeout) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001
            return False

    def wait_ready(self, timeout: float | None = None) -> str:
        self._ready.wait(self.timeout if timeout is None else timeout)
        return self.public_url

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        self._process = None
        self._ready.clear()
        self._url = self.expected_url
        if process is None:
            return
        try:
            process.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info("ngrok: agent остановлен.")
