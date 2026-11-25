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
from nemus.core.agent import build_system_prompt  # noqa: E402
from nemus.core.download_events import (  # noqa: E402
    DownloadEvent,
    subscribe_download,
    unsubscribe_download,
)
from nemus.core.permissions import Decision, PermissionManager  # noqa: E402
from nemus.core.skills import load_skills, match_skills  # noqa: E402
from nemus.core.tools import ToolRegistry, downloads, system  # noqa: E402
from nemus.core.tools.base import RiskLevel, Tool, ToolResult  # noqa: E402
from nemus.core.tools.downloads import DownloadFileTool  # noqa: E402
from nemus.core.tools.shell import CapturedRun  # noqa: E402

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


def test_permission_matrix_for_install_and_torrent_tools() -> None:
    """Пользовательский гейт включён во всех режимах, пока confirm_installs=true."""
    from nemus.core.tools.all import all_builtin_tools  # noqa: PLC0415

    tools = {item.name: item for item in all_builtin_tools()}
    names = ("download_file", "verify_installer", "install_verified", "torrent_download")
    assert set(names) <= set(tools), set(tools)
    args = {"path": "setup.exe", "magnet": "magnet:?xt=urn:btih:test"}

    for mode in ("full-auto", "smart", "confirm-all"):
        for enabled in (True, False):
            config = Config()
            config.tools.permission_mode = mode
            config.tools.confirm_installs = enabled
            manager = PermissionManager(config)
            decisions = {name: manager.check(tools[name], args) for name in names}
            if enabled:
                assert set(decisions.values()) == {Decision.ASK}, (mode, decisions)
                # обычная установка через shell по-прежнему под гейтом
                assert manager.check(_Shell(), {"command": "winget install X.Y"}) == (
                    Decision.ASK
                )
                continue
            # осознанное отключение confirm_installs возвращает правила режима
            if mode == "full-auto":
                expected = {name: Decision.ALLOW for name in names}
            elif mode == "smart":
                expected = {
                    "download_file": Decision.ASK,
                    "verify_installer": Decision.ALLOW,
                    "install_verified": Decision.ASK,
                    "torrent_download": Decision.ASK,
                }
            else:
                expected = {name: Decision.ASK for name in names}  # auto_allow пуст
            assert decisions == expected, (mode, decisions, expected)
    print("ok  PermissionManager: матрица режимов и confirm_installs")


def test_agent_install_gate_uses_real_confirmation() -> None:
    """Модель не может запустить установку своим confirmed=True без кнопки."""
    from unittest.mock import patch  # noqa: PLC0415

    from nemus.core.agent import Agent  # noqa: PLC0415
    from nemus.core.tools.all import all_builtin_tools  # noqa: PLC0415

    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def execute(self, name: str, args: dict[str, object]) -> ToolResult:
            self.calls.append((name, dict(args)))
            return ToolResult.success("installer would have run")

    tool = next(item for item in all_builtin_tools() if item.name == "install_verified")
    args = {"path": "setup.exe", "confirmed": True, "explicit_piracy_request": True}
    payloads: list[dict[str, object]] = []
    registry = _Registry()

    def make_agent(enabled: bool, confirm: object) -> Agent:
        config = Config()
        config.tools.permission_mode = "full-auto"
        config.tools.confirm_installs = enabled
        agent = object.__new__(Agent)
        agent.permissions = PermissionManager(config)  # type: ignore[attr-defined]
        agent.confirm = confirm  # type: ignore[attr-defined]
        agent.registry = registry  # type: ignore[attr-defined]
        return agent

    verdict = {
        "ok": "true",
        "verdict": "warning",
        "warning": "подпись отсутствует; нужно отдельное подтверждение",
    }
    with patch(
        "nemus.core.tools.system.verification_confirmation", return_value=verdict
    ) as preview:

        def approve(_tool: object, confirm_args: dict[str, object]) -> bool:
            payloads.append(dict(confirm_args))
            return True

        agent = make_agent(True, approve)
        result = agent._execute_tool(tool, dict(args))
        assert result.ok, result.content
        assert preview.call_count == 1
        assert payloads[0]["verification"] == verdict, payloads
        assert "verification" not in registry.calls[0][1], registry.calls

        agent = make_agent(True, lambda _t, _a: False)
        denied = agent._execute_tool(tool, dict(args))
        assert not denied.ok, denied.content
        assert len(registry.calls) == 1, registry.calls

    # Отключённый гейт — осознанный выбор пользователя: кнопки не должно быть.
    agent = make_agent(False, lambda _t, _a: (_ for _ in ()).throw(
        AssertionError("confirm не должен вызываться при confirm_installs=false")
    ))
    allowed = agent._execute_tool(tool, dict(args))
    assert allowed.ok, allowed.content
    assert len(registry.calls) == 2, registry.calls
    print("ok  Agent: verified install требует реального подтверждения")


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


def _clean_scan(**_: object) -> ToolResult:
    return ToolResult.success("Windows Defender: угроз не найдено")


def _threat_scan(**_: object) -> ToolResult:
    return ToolResult.failure("Windows Defender нашёл угрозу")


def test_verify_installer_policy(tmp_path: Path) -> None:
    """Defender и хеш — жёсткие блоки;unsigned — только предупреждение по запросу."""
    folder = tmp_path / "policy"
    folder.mkdir(parents=True)
    installer = folder / "setup.exe"
    installer.write_bytes(PAYLOAD)
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    downloads._write_manifest(installer, digest)
    tool = system.VerifyInstallerTool()

    with (
        patch.object(system.ScanFileTool, "execute", side_effect=_clean_scan) as scan,
        patch.object(system, "_authenticode", return_value=(False, "Unsigned")),
    ):
        blocked = tool.execute(path=str(installer), expected_sha256=digest)
        assert blocked.ok is False, blocked.content
        for key in ("verdict:", "defender:", "signature:", "sha256:"):
            assert key in blocked.content, (key, blocked.content)
        assert scan.call_count == 1

        explicit = tool.execute(
            path=str(installer), expected_sha256=digest, explicit_piracy_request=True
        )
        assert explicit.ok, explicit.content
        assert "warning:" in explicit.content
        assert "verdict: warning" in explicit.content
        assert scan.call_count == 2

        bad_hash = tool.execute(
            path=str(installer), expected_sha256="0" * 64, explicit_piracy_request=True
        )
        assert bad_hash.ok is False, bad_hash.content
        assert scan.call_count == 3, "сканирование запускается даже при плохом хеше"

        malformed = tool.execute(
            path=str(installer), expected_sha256="not-a-hash", explicit_piracy_request=True
        )
        assert malformed.ok is False, malformed.content
        assert "warning:" in malformed.content, malformed.content

    with (
        patch.object(system.ScanFileTool, "execute", side_effect=_threat_scan),
        patch.object(system, "_authenticode", return_value=(True, "CN=Vendor")),
    ):
        threat = tool.execute(
            path=str(installer), expected_sha256=digest, explicit_piracy_request=True
        )
        assert threat.ok is False, threat.content
        assert "defender: threat" in threat.content, threat.content

    with (
        patch.object(system.ScanFileTool, "execute", side_effect=_clean_scan) as scan,
        patch.object(system, "_authenticode", return_value=(True, "CN=Vendor")) as signature,
    ):
        signed = tool.execute(path=str(installer))
        assert signed.ok, signed.content
        assert digest in signed.content, signed.content
        assert "warning:" not in signed.content, signed.content
        assert scan.call_count == 1 and signature.call_count == 1

    # Явно переданный хеш важнее манифеста, но расхождение — fail closed.
    installer.with_name("setup.exe.sha256").write_text("1" * 64, encoding="ascii")
    with (
        patch.object(system.ScanFileTool, "execute", side_effect=_clean_scan),
        patch.object(system, "_authenticode", return_value=(True, "CN=Vendor")),
    ):
        conflict = tool.execute(
            path=str(installer), expected_sha256=digest, explicit_piracy_request=True
        )
        assert conflict.ok is False, conflict.content
        assert "manifest" in conflict.content.casefold(), conflict.content

    installer.with_name("setup.exe.sha256").unlink()
    with (
        patch.object(system.ScanFileTool, "execute", side_effect=_clean_scan),
        patch.object(system, "_authenticode", return_value=(True, "CN=Vendor")),
    ):
        unknown = tool.execute(path=str(installer))
        assert unknown.ok is False, unknown.content
    print("ok  verify_installer: безопасная политика проверки установщика")


def test_authenticode_parses_powershell_status() -> None:
    """Подпись читается из Get-AuthenticodeSignature, а не из облачных сервисов."""
    target = Path("C:\\downloads\\setup.exe")
    with (
        patch.object(system.sys, "platform", "win32"),
        patch.object(
            system,
            "run_capture",
            return_value=CapturedRun(0, b"Status=Valid\r\nSigner=CN=Vendor", b"", False),
        ) as runner,
    ):
        valid, detail = system._authenticode(target)
        assert valid is True, detail
        assert "CN=Vendor" in detail, detail
        assert "Get-AuthenticodeSignature" in runner.call_args.args[0][-1]

    with (
        patch.object(system.sys, "platform", "win32"),
        patch.object(
            system,
            "run_capture",
            return_value=CapturedRun(0, b"Status=NotSigned", b"", False),
        ),
    ):
        valid, detail = system._authenticode(target)
        assert valid is False, detail
        assert "NotSigned" in detail, detail
    print("ok  _authenticode: статус подписи из локального PowerShell")


def test_install_verified_requires_confirmation(tmp_path: Path) -> None:
    """Без подтверждения и при проваленной проверке установщик не запускается."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    installer = tmp_path / "setup.exe"
    installer.write_bytes(PAYLOAD)
    tool = system.InstallVerifiedTool()
    verified = ToolResult.success("verdict: pass\ndefender: clean")

    with (
        patch.object(system.VerifyInstallerTool, "execute", return_value=verified) as verify,
        patch.object(
            system,
            "run_capture",
            return_value=CapturedRun(0, b"installed", b"", False),
        ) as runner,
    ):
        unconfirmed = tool.execute(path=str(installer))
        assert unconfirmed.ok is False, unconfirmed.content
        assert "confirmed=True" in unconfirmed.content, unconfirmed.content
        assert verify.call_count == 1
        assert runner.call_count == 0

        done = tool.execute(path=str(installer), confirmed=True)
        assert done.ok, done.content
        assert "installed" in done.content, done.content
        assert runner.call_count == 1
        argv = runner.call_args.args[0]
        assert "powershell" in argv[0].casefold(), argv
        assert "Start-Process" in argv[-1] and "-Wait" in argv[-1], argv
        assert str(installer) in argv[-1], argv

    with (
        patch.object(
            system.VerifyInstallerTool,
            "execute",
            return_value=ToolResult.failure("verdict: blocked"),
        ),
        patch.object(system, "run_capture") as runner,
    ):
        blocked = tool.execute(path=str(installer), confirmed=True)
        assert blocked.ok is False, blocked.content
        assert runner.call_count == 0

    msi = tmp_path / "setup.msi"
    msi.write_bytes(PAYLOAD)
    with (
        patch.object(system.VerifyInstallerTool, "execute", return_value=verified),
        patch.object(
            system,
            "run_capture",
            return_value=CapturedRun(
                0, b"x" * (system.INSTALL_OUTPUT_LIMIT + 100), b"", False
            ),
        ) as runner,
    ):
        installed_msi = tool.execute(path=str(msi), confirmed=True)
        assert installed_msi.ok, installed_msi.content
        argv = runner.call_args.args[0]
        assert "msiexec" in argv[0].casefold(), argv
        assert "/i" in argv and str(msi) in argv, argv
        assert "x" * system.INSTALL_OUTPUT_LIMIT in installed_msi.content
        assert "x" * (system.INSTALL_OUTPUT_LIMIT + 1) not in installed_msi.content

    with (
        patch.object(system.VerifyInstallerTool, "execute", return_value=verified),
        patch.object(
            system,
            "run_capture",
            return_value=CapturedRun(-1, b"partial", b"", True),
        ),
    ):
        timed_out = tool.execute(path=str(installer), confirmed=True)
        assert timed_out.ok is False, timed_out.content
        assert "таймаут" in timed_out.content.casefold(), timed_out.content
    print("ok  install_verified: подтверждение, верификация и запуск процессов")


def test_install_verified_reports_installer_failure(tmp_path: Path) -> None:
    """Код ошибки установщика должен становиться failure, а не маскироваться нулём."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    installer = tmp_path / "setup.exe"
    installer.write_bytes(PAYLOAD)
    tool = system.InstallVerifiedTool()

    with (
        patch.object(
            system.VerifyInstallerTool,
            "execute",
            return_value=ToolResult.success("verdict: pass"),
        ),
        patch.object(
            system,
            "run_capture",
            return_value=CapturedRun(3, b"installer failed", b"", False),
        ) as runner,
    ):
        failed = tool.execute(path=str(installer), confirmed=True)
        assert failed.ok is False, failed.content
        assert "Код: 3" in failed.content, failed.content
        assert "ошибкой" in failed.content.casefold(), failed.content
        command = runner.call_args.args[0][-1]
        assert "-PassThru" in command and "exit $process.ExitCode" in command, command
    print("ok  install_verified: код ошибки установщика не маскируется")


def test_install_tools_are_registered() -> None:
    """Оба инструмента доступны агенту через общий реестр."""
    from nemus.core.tools.all import all_builtin_tools  # noqa: PLC0415

    names = {item.name for item in all_builtin_tools()}
    assert {"verify_installer", "install_verified"} <= names, names
    print("ok  all_builtin_tools: verify_installer и install_verified зарегистрированы")


def test_install_policy_text() -> None:
    """Скилл и промпт называют цепочку загрузок и политику пиратских файлов."""
    skill = next(s for s in load_skills() if s.name == "software-install")
    for phrase in ("browser_download_file", "torrent_download", "инлайн"):
        assert phrase in skill.body, (phrase, skill.body)

    prompt = build_system_prompt(Config(), ToolRegistry())
    for phrase in ("install_verified", "предупреждение", "Сам не ищи пиратские сборки"):
        assert phrase in prompt, (phrase, prompt)
    print("ok  policy: скилл и промпт описывают безопасную установку")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nemus_install_") as tmp:
        root = Path(tmp)
        test_skill_found_for_generic_install_request()
        test_install_command_requires_confirmation_in_full_auto()
        test_permission_matrix_for_install_and_torrent_tools()
        test_agent_install_gate_uses_real_confirmation()
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
        test_verify_installer_policy(root / "verify")
        test_authenticode_parses_powershell_status()
        test_install_verified_requires_confirmation(root / "install")
        test_install_verified_reports_installer_failure(root / "install-failure")
        test_install_tools_are_registered()
        test_install_policy_text()
    print("все тесты установки прошли")


if __name__ == "__main__":
    main()
