"""Тесты установки приложений: поиск скилла, подтверждение и скачивание.

Сеть не нужна: HTTP-клиент заменяется MockTransport.
Запуск:  venv\\Scripts\\python.exe scripts\\test_software_install.py
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from nemus.config import Config  # noqa: E402
from nemus.core.permissions import Decision, PermissionManager  # noqa: E402
from nemus.core.skills import load_skills, match_skills  # noqa: E402
from nemus.core.tools.base import RiskLevel, Tool, ToolResult  # noqa: E402
from nemus.core.tools.downloads import DownloadFileTool  # noqa: E402

PAYLOAD = b"installer-bytes-12345"


class _Shell(Tool):
    """Заглушка опасного инструмента с именем run_shell."""

    name = "run_shell"
    description = "тестовая оболочка"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.DANGEROUS

    def execute(self, **_: object) -> ToolResult:
        return ToolResult.success("ok")


class _Download(Tool):
    """Заглушка скачивания установщика."""

    name = "download_file"
    description = "тестовое скачивание"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.MEDIUM

    def execute(self, **_: object) -> ToolResult:
        return ToolResult.success("ok")


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _root(tmp: Path) -> Path:
    return tmp / "data" / "downloads"


def test_skill_found_for_generic_install_request() -> None:
    """Установка по имени должна подгружать скилл без '/' и без слова «приложение»."""
    skills = load_skills()
    for request in (
        "установи Telegram",
        "поставь 7zip на ноутбук",
        "скачай и установи Firefox",
        "install app vim",
    ):
        found = {skill.name for skill in match_skills(request, skills)}
        assert "software-install" in found, (request, found)
    print("ok  software-install: общие просьбы установить")


def test_install_command_requires_confirmation_in_full_auto() -> None:
    """Даже в full-auto установка не должна выполняться без подтверждения."""
    config = Config()
    config.tools.permission_mode = "full-auto"
    manager = PermissionManager(config)
    shell = _Shell()

    dangerous = "winget install --id Telegram.TelegramDesktop --exact"
    assert manager.check(shell, {"command": dangerous}) == Decision.ASK
    assert manager.check(shell, {"command": "winget upgrade --id 7zip.7zip"}) == Decision.ASK
    assert manager.check(shell, {"command": "msiexec.exe /i setup.msi /qb"}) == Decision.ASK
    assert manager.check(shell, {"command": "pip install requests"}) == Decision.ASK
    # скилл вызывает winget полным путём и запускает скачанный установщик
    assert manager.check(
        shell,
        {"command": '"%LOCALAPPDATA%\\Microsoft\\WindowsApps\\winget.exe" install --id X.Y'},
    ) == Decision.ASK
    assert manager.check(
        shell,
        {"command": 'Start-Process -FilePath "D:\\downloads\\setup.exe" -Wait'},
    ) == Decision.ASK

    # безопасная разведка не должна дёргать подтверждение
    assert manager.check(shell, {"command": "winget search Telegram"}) == Decision.ALLOW
    assert manager.check(shell, {"command": "winget list --id Telegram.TelegramDesktop"}) == Decision.ALLOW
    assert manager.check(shell, {"command": "winget --version"}) == Decision.ALLOW

    # скачивание установщика тоже не должно идти тихо
    assert manager.check(_Download(), {"url": "https://vendor.example/setup.exe"}) == Decision.ASK
    print("ok  PermissionManager: установки требуют подтверждения")


def test_download_creates_folder_and_returns_hash(tmp_path: Path) -> None:
    """Скачивание само создаёт data/downloads и отдаёт путь, размер и SHA-256."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD)

    tool = DownloadFileTool()
    tool.root = _root(tmp_path)
    with patch(
        "nemus.core.tools.downloads._http_client", return_value=_client(handler)
    ):
        result = tool.execute(
            url="https://vendor.example/files/setup.exe", filename="setup.exe"
        )

    assert result.ok, result.content
    assert seen == ["https://vendor.example/files/setup.exe"], seen
    path_match = re.search(r"Путь:\s*(.+)", result.content)
    assert path_match is not None, result.content
    path = Path(path_match.group(1).strip())
    assert path.parent == _root(tmp_path).resolve(), path
    assert path.read_bytes() == PAYLOAD
    expected_hash = hashlib.sha256(PAYLOAD).hexdigest()
    assert expected_hash in result.content, result.content
    print("ok  download_file: папка, файл, размер, SHA-256")


def test_download_rejects_plain_http(tmp_path: Path) -> None:
    """Нешифрованный URL не должен скачиваться вообще."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=PAYLOAD)

    tool = DownloadFileTool()
    tool.root = _root(tmp_path)
    with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
        result = tool.execute(url="http://vendor.example/files/setup.exe")

    assert result.ok is False, result.content
    assert called is False
    print("ok  download_file: http отклонён без запроса")


def test_download_enforces_size_limit(tmp_path: Path) -> None:
    """Превышение лимита прерывает загрузку и не оставляет файла."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "2000000"},
            content=PAYLOAD,
        )

    tool = DownloadFileTool()
    tool.root = _root(tmp_path)
    with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
        result = tool.execute(
            url="https://vendor.example/setup.exe", filename="big.exe", max_mb=1
        )

    assert result.ok is False, result.content
    assert list(_root(tmp_path).glob("*")) == [], "частичный файл не должен остаться"
    print("ok  download_file: лимит размера и уборка частичного файла")


def test_download_sanitizes_filename(tmp_path: Path) -> None:
    """Имя файла не может выйти за пределы папки загрузок."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PAYLOAD)

    tool = DownloadFileTool()
    tool.root = _root(tmp_path)
    with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
        result = tool.execute(
            url="https://vendor.example/setup.exe", filename="../../evil.exe"
        )

    assert result.ok, result.content
    path_match = re.search(r"Путь:\s*(.+)", result.content)
    assert path_match is not None, result.content
    path = Path(path_match.group(1).strip())
    assert path.parent == _root(tmp_path).resolve(), path
    assert path.name == "evil.exe", path
    print("ok  download_file: имя не выходит из data/downloads")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nemus_install_") as tmp:
        root = Path(tmp)
        test_skill_found_for_generic_install_request()
        test_install_command_requires_confirmation_in_full_auto()
        test_download_creates_folder_and_returns_hash(root / "a")
        test_download_rejects_plain_http(root / "b")
        test_download_enforces_size_limit(root / "c")
        test_download_sanitizes_filename(root / "d")
    print("все тесты установки прошли")


if __name__ == "__main__":
    main()
