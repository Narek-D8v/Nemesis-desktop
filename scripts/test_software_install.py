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
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from nemus.config import Config  # noqa: E402
from nemus.core.download_events import (  # noqa: E402
    DownloadEvent,
    subscribe_download,
    unsubscribe_download,
)
from nemus.core.permissions import Decision, PermissionManager  # noqa: E402
from nemus.core.skills import load_skills, match_skills  # noqa: E402
from nemus.core.tools import downloads  # noqa: E402
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
        seen.append(f"{request.method} {request.url}")
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
    assert seen == [
        "HEAD https://vendor.example/files/setup.exe",
        "GET https://vendor.example/files/setup.exe",
    ], seen
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


def _range_handler(
    payload: bytes,
    ranges: list[str] | None = None,
    *,
    short: bool = False,
    etag: str = "v1",
) -> object:
    """HEAD + Range-сервер; short обрезает ответ, чтобы оставить часть для докачки."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={
                "Content-Length": str(len(payload)),
                "Accept-Ranges": "bytes",
                "ETag": etag,
            })
        rng = request.headers.get("Range")
        if rng:
            if ranges is not None:
                ranges.append(rng)
            start, end = (int(value) for value in rng.removeprefix("bytes=").split("-"))
            body = payload[start : min(end + 1, len(payload))]
            if short:
                body = body[:4]
            return httpx.Response(
                206,
                content=body,
                headers={"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"},
            )
        return httpx.Response(200, content=payload)

    return handler


def test_segmented_download_resumes_and_publishes(tmp_path: Path) -> None:
    """Короткий сегмент докачивается с offset, события несут общий task_id."""
    payload = b"0123456789"
    url = "https://example.test/setup.exe"
    root = _root(tmp_path)
    tool = downloads.DownloadFileTool()
    tool.root = root

    with patch(
        "nemus.core.tools.downloads._http_client",
        return_value=_client(_range_handler(payload, short=True)),
    ):
        first = tool.execute(url, "setup.exe")
    assert first.ok is False, first.content
    parts = list((root / ".parts").glob("*/part-*"))
    assert len(parts) == 1 and parts[0].stat().st_size == 4, parts

    ranges: list[str] = []
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    try:
        with patch(
            "nemus.core.tools.downloads._http_client",
            return_value=_client(_range_handler(payload, ranges)),
        ):
            result = tool.execute(url, "setup.exe")
    finally:
        unsubscribe_download(token)

    assert result.ok, result.content
    assert (root / "setup.exe").read_bytes() == payload
    assert (root / "setup.exe.sha256").read_text() == hashlib.sha256(payload).hexdigest()
    assert ranges == ["bytes=4-9"], ranges
    assert not (root / ".parts").exists()
    assert {event.task_id for event in seen} == {tool.last_task_id}
    assert seen[-1].phase == "done"
    assert seen[-1].done == seen[-1].total == len(payload)
    progresses = [event.done for event in seen if event.phase == "progress"]
    assert progresses == sorted(progresses), progresses
    print("ok  download_file: докачка сегмента и события прогресса")


def test_changed_etag_restarts_download(tmp_path: Path) -> None:
    """Если версия файла изменилась, старые части не подъезжают."""
    payload = b"0123456789"
    url = "https://example.test/setup.exe"
    root = _root(tmp_path)
    tool = downloads.DownloadFileTool()
    tool.root = root
    with patch(
        "nemus.core.tools.downloads._http_client",
        return_value=_client(_range_handler(payload, short=True)),
    ):
        assert tool.execute(url, "setup.exe").ok is False

    ranges: list[str] = []
    with patch(
        "nemus.core.tools.downloads._http_client",
        return_value=_client(_range_handler(payload, ranges, etag="v2")),
    ):
        result = tool.execute(url, "setup.exe")

    assert result.ok, result.content
    assert ranges == ["bytes=0-9"], ranges
    assert (root / "setup.exe").read_bytes() == payload
    print("ok  download_file: смена ETag сбрасывает части")


def test_invalid_meta_discards_stale_parts(tmp_path: Path) -> None:
    """Отсутствие/порча meta.json не должно подмешивать чужие части."""
    payload = b"0123456789"
    url = "https://example.test/setup.exe"
    cases = (("missing", None), ("corrupt", "{broken"), ("non-dict", "[]"))
    for label, meta in cases:
        root = _root(tmp_path / label)
        parts_dir = downloads._parts_dir(root, "setup.exe", url)
        parts_dir.mkdir(parents=True)
        (parts_dir / "part-00").write_bytes(b"XXXX")
        if meta is not None:
            (parts_dir / "meta.json").write_text(meta, encoding="utf-8")

        ranges: list[str] = []
        tool = downloads.DownloadFileTool()
        tool.root = root
        with patch(
            "nemus.core.tools.downloads._http_client",
            return_value=_client(_range_handler(payload, ranges)),
        ):
            result = tool.execute(url, "setup.exe")

        assert result.ok, (label, result.content)
        assert ranges == ["bytes=0-9"], (label, ranges)
        assert (root / "setup.exe").read_bytes() == payload, label
        assert (root / "setup.exe.sha256").read_text() == hashlib.sha256(
            payload
        ).hexdigest(), label
        assert not (root / ".parts").exists(), label
    print("ok  download_file: битая meta.json сбрасывает части")


def test_segments_are_parallel_and_merge_in_order(tmp_path: Path) -> None:
    """Файл больше 8 МБ делится на сегменты и собирается в исходном порядке."""
    size = 2 * downloads.SEGMENT_SIZE_MB * 1024 * 1024
    payload = bytes(range(256)) * (size // 256)
    ranges: list[str] = []
    root = _root(tmp_path)
    tool = downloads.DownloadFileTool()
    tool.root = root

    with patch(
        "nemus.core.tools.downloads._http_client",
        return_value=_client(_range_handler(payload, ranges)),
    ):
        result = tool.execute("https://example.test/big.bin", "big.bin")

    assert result.ok, result.content
    assert len(ranges) == 2, ranges
    assert ranges[0].startswith("bytes=0-"), ranges
    assert ranges[1].startswith("bytes=8388608-"), ranges
    assert (root / "big.bin").read_bytes() == payload
    assert (root / "big.bin.sha256").read_text() == hashlib.sha256(payload).hexdigest()
    assert not (root / ".parts").exists()
    print("ok  download_file: параллельные сегменты и порядок слияния")


def test_range_ignored_falls_back_to_single_stream(tmp_path: Path) -> None:
    """Без Accept-Ranges сервер не получает Range, файл качается одним потоком."""
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.headers.get("Range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(PAYLOAD))})
        # Даже если бы Range ушёл, сервер вернул бы полное тело с кодом 200.
        return httpx.Response(200, content=PAYLOAD)

    root = _root(tmp_path)
    tool = downloads.DownloadFileTool()
    tool.root = root
    with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
        result = tool.execute("https://example.test/setup.exe", "setup.exe")

    assert result.ok, result.content
    assert (root / "setup.exe").read_bytes() == PAYLOAD
    assert (root / "setup.exe.sha256").read_text() == hashlib.sha256(PAYLOAD).hexdigest()
    assert requests == [("HEAD", None), ("GET", None)], requests
    assert not (root / ".parts").exists()
    assert list(root.glob("*.part")) == []
    print("ok  download_file: откат на один поток без Range")


def test_200_instead_of_206_falls_back_and_cleans_parts(tmp_path: Path) -> None:
    """Сервер пообещал Range, но ответил 200: части удаляются, файл целый."""
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={
                "Content-Length": str(len(PAYLOAD)),
                "Accept-Ranges": "bytes",
                "ETag": "v1",
            })
        ranges.append(request.headers.get("Range"))
        return httpx.Response(200, content=PAYLOAD)

    root = _root(tmp_path)
    tool = downloads.DownloadFileTool()
    tool.root = root
    with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
        result = tool.execute("https://example.test/setup.exe", "setup.exe")

    assert result.ok, result.content
    assert ranges == ["bytes=0-20", None], ranges
    assert (root / "setup.exe").read_bytes() == PAYLOAD
    assert (root / "setup.exe.sha256").read_text() == hashlib.sha256(PAYLOAD).hexdigest()
    assert not (root / ".parts").exists()
    assert list(root.glob("*.part")) == []
    print("ok  download_file: 200 вместо 206 откатывает загрузку")


def test_cancel_keeps_parts(tmp_path: Path) -> None:
    """Отмена до старта не скачивает тело, но оставляет части для докачки."""
    controls = downloads.DownloadControls()
    task_id = "cancel-before-start"
    controls.cancel(task_id)
    gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={
                "Content-Length": "10",
                "Accept-Ranges": "bytes",
                "ETag": "v1",
            })
        gets.append(str(request.url))
        return httpx.Response(206, content=b"0123456789")

    root = _root(tmp_path)
    tool = downloads.DownloadFileTool()
    tool.root = root
    tool.controls = controls
    tool.task_id = task_id
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    try:
        with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
            result = tool.execute("https://example.test/setup.exe", "setup.exe")
    finally:
        unsubscribe_download(token)

    assert result.ok is False, result.content
    assert gets == [], gets
    part_files = list((root / ".parts").glob("*/part-*"))
    assert part_files, "части должны остаться для докачки"
    assert not (root / "setup.exe").exists()
    assert not (root / "setup.exe.sha256").exists()
    assert any(
        event.phase == "cancelled" and event.task_id == task_id for event in seen
    ), seen
    print("ok  download_file: отмена сохраняет части")


def test_oversize_cleans_up(tmp_path: Path) -> None:
    """Заголовок больше жёсткого лимита: ни файла, ни частей, ни манифеста."""
    huge = downloads.HARD_MAX_MB * 1024 * 1024 + 1
    url = "https://example.test/big.exe"
    root = _root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "big.exe.part").write_bytes(b"stale")
    stale_dir = downloads._parts_dir(root, "big.exe", url)
    stale_dir.mkdir(parents=True)
    (stale_dir / "part-00").write_bytes(b"stale")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={
                "Content-Length": str(huge),
                "Accept-Ranges": "bytes",
            })
        raise AssertionError("GET не должен выполняться при превышении лимита")

    tool = downloads.DownloadFileTool()
    tool.root = root
    with patch("nemus.core.tools.downloads._http_client", return_value=_client(handler)):
        result = tool.execute(url, "big.exe")

    assert result.ok is False, result.content
    assert not (root / "big.exe").exists()
    assert not (root / "big.exe.part").exists()
    assert not (root / "big.exe.sha256").exists()
    assert not (root / ".parts").exists()
    print("ok  download_file: превышение лимита убирает все артефакты")


def test_download_controls_register_cancel_forget() -> None:
    """Реестр отмены: register идемпотентен, forget даёт новый Event."""
    controls = downloads.DownloadControls()
    event = controls.register("task-1")
    assert controls.register("task-1") is event
    assert isinstance(event, threading.Event)
    assert not event.is_set()
    controls.cancel("task-1")
    assert event.is_set()
    controls.forget("task-1")
    fresh = controls.register("task-1")
    assert fresh is not event
    assert not fresh.is_set()
    print("ok  DownloadControls: register/cancel/forget")


def test_segment_plan_and_manifest_helpers(tmp_path: Path) -> None:
    """План сегментов ограничен восемью, хеш и манифест работают."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert downloads._plan_segments(0) == []
    assert downloads._plan_segments(10) == [(0, 9)]
    segment = downloads.SEGMENT_SIZE_MB * 1024 * 1024
    exact = downloads._plan_segments(8 * segment)
    assert len(exact) == downloads.MAX_SEGMENTS
    assert exact[0][0] == 0 and exact[-1][1] == 8 * segment - 1
    over = downloads._plan_segments(8 * segment + 1)
    assert len(over) == downloads.MAX_SEGMENTS
    assert over[-1][1] == 8 * segment
    assert all(end >= start for start, end in over)

    first = tmp_path / "part-00"
    second = tmp_path / "part-01"
    first.write_bytes(b"ab")
    second.write_bytes(b"cd")
    merged = tmp_path / "file.part"
    size, digest = downloads._merge_and_hash([first, second], merged)
    assert merged.read_bytes() == b"abcd"
    assert size == 4
    assert digest == hashlib.sha256(b"abcd").hexdigest()
    target = tmp_path / "file.exe"
    assert downloads._read_manifest(target) is None
    downloads._write_manifest(target, digest)
    assert target.with_name("file.exe.sha256").read_text() == digest
    assert downloads._read_manifest(target) == digest
    target.with_name("file.exe.sha256").write_text("not-a-digest")
    assert downloads._read_manifest(target) is None
    print("ok  helpers: план сегментов, слияние, манифест")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nemus_install_") as tmp:
        root = Path(tmp)
        test_skill_found_for_generic_install_request()
        test_install_command_requires_confirmation_in_full_auto()
        test_download_creates_folder_and_returns_hash(root / "a")
        test_download_rejects_plain_http(root / "b")
        test_download_enforces_size_limit(root / "c")
        test_download_sanitizes_filename(root / "d")
        test_segmented_download_resumes_and_publishes(root / "e")
        test_changed_etag_restarts_download(root / "e2")
        test_invalid_meta_discards_stale_parts(root / "e3")
        test_segments_are_parallel_and_merge_in_order(root / "f")
        test_range_ignored_falls_back_to_single_stream(root / "g")
        test_200_instead_of_206_falls_back_and_cleans_parts(root / "h")
        test_cancel_keeps_parts(root / "i")
        test_oversize_cleans_up(root / "j")
        test_download_controls_register_cancel_forget()
        test_segment_plan_and_manifest_helpers(root / "k")
    print("все тесты установки прошли")


if __name__ == "__main__":
    main()
