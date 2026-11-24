"""Проверки Telegram Mini App: подпись initData, API и HTTP-сервер."""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import Config  # noqa: E402
from nemus.core import telegram_webapp  # noqa: E402
from nemus.core.providers import hf_download  # noqa: E402
from nemus.core.providers.gguf_split import (  # noqa: E402
    _parse_shard,
    merge_gguf_shards,
    merged_filename,
    shard_filenames,
    split_part,
)
from nemus.core.providers.hf_catalog import (  # noqa: E402
    HFFile,
    group_hf_files,
    parse_hf_search,
)
from nemus.core.telegram_webapp import WebAppServer, validate_init_data  # noqa: E402
from nemus.core.webapp_tunnel import (  # noqa: E402
    CloudflaredTunnel,
    NgrokTunnel,
    find_ngrok_url,
    find_tunnel_url,
)


def _init_data(token: str, user_id: int = 7) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Тест"}, ensure_ascii=False, separators=(",", ":")
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _request(
    url: str,
    *,
    init_data: str = "",
    method: str = "GET",
    payload: dict | None = None,
) -> tuple[int, dict | str]:
    headers = {"X-Telegram-Init-Data": init_data}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            if "application/json" in (response.headers.get("Content-Type") or ""):
                return response.status, json.loads(raw)
            return response.status, raw
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "code", 0)
        raw = getattr(exc, "read", lambda: b"")().decode("utf-8")
        try:
            return status, json.loads(raw)
        except Exception:  # noqa: BLE001
            return status, raw


def _write_split_gguf(
    path: Path, index: int, count: int, tensor: str, payload: bytes
) -> None:
    """Минимальный валидный GGUF-шард для тестов склейки."""

    def string(value: str) -> bytes:
        raw = value.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    def kv(key: str, value_type: int, raw: bytes) -> bytes:
        return string(key) + struct.pack("<I", value_type) + raw

    alignment = 32
    kvs = [
        kv("general.alignment", 4, struct.pack("<I", alignment)),
        kv("general.architecture", 8, string("llama")),
        kv("split.no", 4, struct.pack("<I", index - 1)),
        kv("split.count", 4, struct.pack("<I", count)),
        kv("split.tensors.count", 4, struct.pack("<I", count)),
    ]
    body = b"GGUF" + struct.pack("<IQQ", 3, 1, len(kvs))
    body += b"".join(kvs)
    body += string(tensor)
    body += struct.pack("<I", 1)
    body += struct.pack("<Q", len(payload))
    body += struct.pack("<I", 0)
    body += struct.pack("<Q", 0)
    body += b"\0" * ((alignment - len(body) % alignment) % alignment)
    body += payload
    path.write_bytes(body)


def test_signature() -> None:
    token = "123:secret"
    init = _init_data(token)
    assert validate_init_data(init, token, [7]) is not None
    assert validate_init_data(init, token, [8]) is None
    assert validate_init_data(init + "x", token, [7]) is None
    assert validate_init_data("", token, [7]) is None
    assert validate_init_data(init, "", [7]) is None
    print("ok  initData: подпись, срок и allowed_users проверяются")


def test_tunnel_helpers() -> None:
    assert (
        find_tunnel_url(
            "INF |  https://funny-words-1234.trycloudflare.com  |"
        )
        == "https://funny-words-1234.trycloudflare.com"
    )
    assert find_tunnel_url("no url here") == ""
    tunnel = CloudflaredTunnel(
        "http://127.0.0.1:8765", binary="definitely-no-cloudflared", download=False
    )
    assert tunnel.command[1:4] == ["tunnel", "--url", "http://127.0.0.1:8765"]
    assert tunnel.start() is False

    assert (
        find_ngrok_url('"url":"https://my-name.ngrok-free.dev"')
        == "https://my-name.ngrok-free.dev"
    )
    ngrok = NgrokTunnel(
        "http://127.0.0.1:8765",
        expected_url="https://my-name.ngrok-free.dev",
        authtoken="",
        binary="definitely-no-ngrok",
        download=False,
    )
    assert ngrok.command[1:3] == ["http", "127.0.0.1:8765"]
    assert ngrok.command[-2:] == ["--url", "https://my-name.ngrok-free.dev"]
    assert ngrok.start() is False  # без authtoken процесс не запускается
    print("ok  туннель: cloudflared и ngrok без запуска процесса")


def test_http() -> None:
    cfg = Config()
    cfg.telegram.token = "123:secret"
    cfg.telegram.allowed_users = [7]
    saved: list[Config] = []
    server = WebAppServer(cfg, port=0, save_func=lambda copy: saved.append(copy))
    assert server.start(), "сервер должен подняться на свободном порту"
    base = f"http://127.0.0.1:{server.bound_port}"
    init = _init_data(cfg.telegram.token)
    try:
        status, html = _request(base + "/")
        assert status == 200 and "Настройки" in str(html)
        # Страница лежит отдельным редактируемым файлом и содержит свой JS.
        assert "id=\"content\"" in str(html)
        assert "/api/settings" in str(html)
        assert "/api/models" in str(html)
        assert "hf-panel" in str(html)

        status, _ = _request(base + "/api/settings")
        assert status == 401

        status, payload = _request(base + "/api/settings", init_data=init)
        assert status == 200 and payload["ok"] is True
        assert any(section["id"] == "llm" for section in payload["sections"])
        character = next(
            section for section in payload["sections"] if section["id"] == "character"
        )
        assert "настроение" in character["note"], character["note"]
        assert not any(field["id"] == "ch_warmth" for field in character["fields"])
        assert any(field["id"] == "ch_playfulness" for field in character["fields"])
        assert any(field["id"] == "ch_advanced" for field in character["fields"])
        assert any(field["id"] == "ch_prompt" for field in character["fields"])
        status, payload = _request(
            base + "/api/settings",
            init_data=init,
            method="POST",
            payload={
                "section": "character",
                "field": "ch_playfulness",
                "value": "80",
            },
        )
        assert status == 200 and payload["ok"] is True
        assert cfg.character.playfulness == 80
        for field_id, value in (
            ("ch_advanced", "true"),
            ("ch_prompt", "Говори мягко и с лёгкой иронией."),
        ):
            status, payload = _request(
                base + "/api/settings",
                init_data=init,
                method="POST",
                payload={"section": "character", "field": field_id, "value": value},
            )
            assert status == 200 and payload["ok"] is True, payload
        assert cfg.character.advanced is True
        assert "лёгкой иронией" in cfg.character.custom_prompt

        status, payload = _request(
            base + "/api/settings",
            init_data=init,
            method="POST",
            payload={"section": "llm", "field": "temp", "value": "0.5"},
        )
        assert status == 200 and payload["ok"] is True
        assert cfg.llm.temperature == 0.5
        assert saved and saved[-1].llm.temperature == 0.5

        status, payload = _request(
            base + "/api/settings",
            init_data=init,
            method="POST",
            payload={"section": "llm", "field": "temp", "value": "999"},
        )
        assert status == 400 and payload["ok"] is False
        assert cfg.llm.temperature == 0.5

        status, payload = _request(base + "/api/models?provider=openai", init_data=init)
        assert status == 200 and payload["ok"] is True
        assert payload["provider"] == "openai"
        assert payload["models"] and "id" in payload["models"][0]
        status, _ = _request(base + "/api/models?provider=nope", init_data=init)
        assert status == 400
        status, payload = _request(base + "/api/models/local", init_data=init)
        assert status == 200 and isinstance(payload["models"], list)
        status, payload = _request(
            base + "/api/models/delete",
            init_data=init,
            method="POST",
            payload={"model": "llama_cpp/../../outside.gguf"},
        )
        assert status == 400 and payload["ok"] is False

        parsed = parse_hf_search(
            [
                {
                    "id": "owner/whole-GGUF",
                    "tags": ["gguf"],
                    "siblings": [{"rfilename": "model-q4.gguf"}],
                },
                {
                    "id": "owner/split-only-GGUF",
                    "tags": ["gguf"],
                    "siblings": [
                        {"rfilename": "model-00001-of-00002.gguf"},
                        {"rfilename": "model-00002-of-00002.gguf"},
                    ],
                },
            ]
        )
        assert [m.repo_id for m in parsed] == [
            "owner/whole-GGUF",
            "owner/split-only-GGUF",
        ]
        grouped = group_hf_files(
            [
                HFFile("model-q4.gguf", 1024),
                HFFile("model-00002-of-00002.gguf", 512),
                HFFile("model-00001-of-00002.gguf", 512),
            ]
        )
        assert grouped[0].filename == "model-q4.gguf"
        assert grouped[1].filename == "model-00001-of-00002.gguf"
        assert grouped[1].parts == 2 and grouped[1].size == 1024

        old_search = telegram_webapp.search_hf_models
        old_files = telegram_webapp.list_hf_gguf_files
        telegram_webapp.search_hf_models = lambda query, endpoint=None: [
            SimpleNamespace(repo_id="owner/model-GGUF", downloads=12, likes=3, gated=False)
        ]
        telegram_webapp.list_hf_gguf_files = lambda repo, endpoint=None: [
            SimpleNamespace(filename="model-q4.gguf", size=1024),
            SimpleNamespace(filename="model-00001-of-00002.gguf", size=512),
            SimpleNamespace(filename="model-00002-of-00002.gguf", size=512),
        ]
        try:
            status, payload = _request(base + "/api/hf/search?q=qwen", init_data=init)
            assert status == 200 and payload["models"][0]["repo_id"] == "owner/model-GGUF"
            status, payload = _request(
                base + "/api/hf/files?repo=owner/model-GGUF", init_data=init
            )
            assert status == 200 and payload["files"][0]["filename"] == "model-q4.gguf"
            split = next(item for item in payload["files"] if item["split"])
            assert split["filename"] == "model-00001-of-00002.gguf"
            assert split["parts"] == 2 and split["size"] == 1024
        finally:
            telegram_webapp.search_hf_models = old_search
            telegram_webapp.list_hf_gguf_files = old_files

        status, payload = _request(
            base + "/api/hf/download",
            init_data=init,
            method="POST",
            payload={"repo": "broken", "file": "x.gguf"},
        )
        assert status == 400 and payload["ok"] is False

        with tempfile.TemporaryDirectory() as tmp:
            model_file = Path(tmp) / "tiny.gguf"
            model_file.write_bytes(b"GGUF")
            cfg.llm.llama_cpp.models_dir = tmp
            status, payload = _request(
                base + "/api/models/use",
                init_data=init,
                method="POST",
                payload={"model": f"llama_cpp/{model_file}"},
            )
            assert status == 200 and payload["ok"] is True
            assert cfg.llm.default_provider == "llama_cpp"
            assert cfg.llm.llama_cpp.model_path == str(model_file.resolve())
            assert cfg.llm.default_model.startswith("llama_cpp/")

        status, payload = _request(
            base + "/api/mcp",
            init_data=init,
            method="POST",
            payload={
                "action": "add",
                "command": "npx -y @modelcontextprotocol/server-filesystem",
                "fields": {"name": "files", "cwd": ".", "env": "ROOT=."},
            },
        )
        assert status == 200 and payload["ok"] is True
        assert cfg.mcp.servers and cfg.mcp.servers[-1].command == "npx"
        assert cfg.mcp.servers[-1].name == "files"
        assert cfg.mcp.servers[-1].env == {"ROOT": "."}
        status, payload = _request(
            base + "/api/mcp",
            init_data=init,
            method="POST",
            payload={
                "action": "update",
                "index": 0,
                "fields": {"name": "files-2", "args": "-y server"},
            },
        )
        assert status == 200 and payload["ok"] is True
        assert cfg.mcp.servers[0].name == "files-2"
        assert cfg.mcp.servers[0].args == ["-y", "server"]
        status, payload = _request(
            base + "/api/mcp",
            init_data=init,
            method="POST",
            payload={"action": "toggle", "index": 0},
        )
        assert status == 200 and cfg.mcp.servers[0].enabled is False
        status, payload = _request(
            base + "/api/mcp",
            init_data=init,
            method="POST",
            payload={"action": "delete", "index": 0},
        )
        assert status == 200 and not cfg.mcp.servers
    finally:
        server.stop()
    print("ok  Web App: статика, initData, чтение и запись настроек")


def test_download_manager() -> None:
    cfg = Config()
    cfg.telegram.token = "123:secret"
    cfg.telegram.allowed_users = [7]
    saved: list[Config] = []
    with tempfile.TemporaryDirectory() as tmp:
        cfg.llm.llama_cpp.models_dir = tmp
        settings = telegram_webapp.WebAppSettings(
            cfg, save_func=lambda copy: saved.append(copy)
        )
        old_download = telegram_webapp.download_hf_model

        def fake_download(
            config: Config,
            *,
            repo: str,
            filename: str,
            revision: str,
            progress=None,
            status=None,
            cancel=None,
        ) -> Path:
            assert repo == "owner/model"
            assert filename == "tiny.gguf"
            if progress:
                progress(4, 8)
            path = Path(config.llm.llama_cpp.models_dir) / "tiny.gguf"
            path.write_bytes(b"GGUF")
            if progress:
                progress(8, 8)
            return path

        telegram_webapp.download_hf_model = fake_download
        try:
            ok, _, _ = settings.start_download("owner/model", "tiny.gguf")
            assert ok
            for _ in range(100):
                status = settings.download_status()
                if status["state"] in ("done", "error", "cancelled"):
                    break
                time.sleep(0.02)
            assert status["state"] == "done", status
            assert status["percent"] == 100
            assert status["model"].startswith("llama_cpp/")
            ok, _ = settings.use_local_model(status["model"])
            assert ok and cfg.llm.default_provider == "llama_cpp"
            assert cfg.llm.llama_cpp.model_path.endswith("tiny.gguf")

            # Пустой file — кнопка «Скачать» в строке поиска: бэкенд сам
            # выбирает Q4_K_M, а не самый большой файл репозитория.
            old_list = telegram_webapp.list_hf_gguf_files

            def fake_list(repo: str, *, endpoint: str = "") -> list[HFFile]:
                assert repo == "owner/model"
                return [
                    HFFile("model-Q8_0.gguf", 900),
                    HFFile("model-Q4_K_M.gguf", 500),
                ]

            def fake_auto(
                config: Config,
                *,
                repo: str,
                filename: str,
                revision: str,
                progress=None,
                status=None,
                cancel=None,
            ) -> Path:
                assert repo == "owner/model"
                assert filename == "model-Q4_K_M.gguf", filename
                path = Path(config.llm.llama_cpp.models_dir) / "model-Q4_K_M.gguf"
                path.write_bytes(b"GGUF-auto")
                if progress:
                    progress(9, 9)
                return path

            telegram_webapp.list_hf_gguf_files = fake_list
            telegram_webapp.download_hf_model = fake_auto
            try:
                cfg.llm.default_provider = "gemini"
                cfg.llm.llama_cpp.model_path = ""
                ok, message, _ = settings.start_download(
                    "owner/model", "", "main", "qwen gguf"
                )
                assert ok and "Q4_K_M" in message, message
                for _ in range(100):
                    auto = settings.download_status()
                    if auto["state"] in ("done", "error", "cancelled"):
                        break
                    time.sleep(0.02)
                assert auto["state"] == "done", auto
                assert (Path(tmp) / "model-Q4_K_M.gguf").is_file()
            finally:
                telegram_webapp.list_hf_gguf_files = old_list
                telegram_webapp.download_hf_model = old_download
        finally:
            telegram_webapp.download_hf_model = old_download
    print("ok  Web App: каталог HF, авто-выбор файла и загрузка локальной модели")


def test_local_management() -> None:
    cfg = Config()
    cfg.telegram.token = "123:secret"
    cfg.telegram.allowed_users = [7]
    saved: list[Config] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg.llm.llama_cpp.models_dir = tmp
        done = root / "done.gguf"
        done.write_bytes(b"GGUF")
        partial = root / "part" / "part.gguf.part"
        partial.parent.mkdir()
        partial.write_bytes(b"GGUF-part")
        sidecar = partial.with_name(partial.name + ".json")
        sidecar.write_text(
            json.dumps(
                {
                    "repo": "owner/part",
                    "filename": "part.gguf",
                    "revision": "main",
                    "target": str(root / "part" / "part.gguf"),
                }
            ),
            encoding="utf-8",
        )
        settings = telegram_webapp.WebAppSettings(
            cfg, save_func=lambda copy: saved.append(copy)
        )
        items = settings.local_models()
        assert len(items) == 2, items
        by_name = {item["filename"]: item for item in items}
        assert by_name["done.gguf"]["partial"] is False
        assert by_name["part.gguf"]["partial"] is True
        assert by_name["part.gguf"]["can_resume"] is True
        assert "owner/part" in by_name["part.gguf"]["source"]

        cfg.llm.default_model = by_name["done.gguf"]["model"]
        cfg.llm.llama_cpp.model_path = str(done)
        ok, message = settings.delete_local_model(by_name["done.gguf"]["model"])
        assert not ok and "активна" in message.lower()

        cfg.llm.default_model = ""
        cfg.llm.llama_cpp.model_path = ""
        ok, _ = settings.delete_local_model(by_name["done.gguf"]["model"])
        assert ok and not done.exists()

        old_download = telegram_webapp.download_hf_model

        def fake_download(
            config: Config,
            *,
            repo: str,
            filename: str,
            revision: str,
            progress=None,
            status=None,
            cancel=None,
        ) -> Path:
            assert repo == "owner/part" and filename == "part.gguf"
            target = Path(config.llm.llama_cpp.models_dir) / "part" / "part.gguf"
            target.write_bytes(b"GGUF-complete")
            if progress:
                progress(10, 10)
            return target

        telegram_webapp.download_hf_model = fake_download
        try:
            ok, _, _ = settings.resume_local_model(by_name["part.gguf"]["model"])
            assert ok
            for _ in range(100):
                status = settings.download_status()
                if status["state"] in ("done", "error", "cancelled"):
                    break
                time.sleep(0.02)
            assert status["state"] == "done", status
            assert (root / "part" / "part.gguf").is_file()
            assert not sidecar.exists()
        finally:
            telegram_webapp.download_hf_model = old_download
    print("ok  Web App: список, удаление и докачка локальных GGUF")


def test_gguf_split() -> None:
    """Шарды склеиваются в валидный GGUF без служебных split-ключей."""
    assert split_part("model-q4-00001-of-00002.gguf").count == 2
    assert merged_filename("model-q4-00001-of-00002.gguf") == "model-q4.gguf"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        names = shard_filenames("model-q4-00001-of-00002.gguf")
        shards = []
        for index, name in enumerate(names, start=1):
            path = root / name
            _write_split_gguf(
                path, index, len(names), f"tensor.{index}", bytes([index]) * 10
            )
            shards.append(path)
        output = root / "model-q4.gguf"
        merge_gguf_shards(shards, output)
        parsed = _parse_shard(output)
        assert len(parsed.tensors) == 2
        assert [tensor.offset for tensor in parsed.tensors] == [0, 32]
        assert all(not key.startswith("split.") for key, _, _ in parsed.kvs)
        data = output.read_bytes()[parsed.data_start :]
        assert data[:10] == b"\x01" * 10
        assert data[32:42] == b"\x02" * 10

        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            cfg = Config()
            cfg.llm.llama_cpp.models_dir = str(root2)
            cfg.llm.llama_cpp.hf_endpoint = "https://huggingface.co"
            old_list = hf_download.list_hf_gguf_files
            old_source = hf_download._download_hf_model_source
            messages: list[str] = []

            def fake_list(repo: str, *, endpoint: str = "") -> list[HFFile]:
                assert repo == "owner/model"
                return [HFFile(name, 256) for name in names]

            def fake_source(
                config: Config, source, *, progress=None, cancel=None, force=False
            ):
                part = split_part(source.filename)
                assert part is not None
                target = hf_download.hf_local_path(config, source)
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_split_gguf(
                    target,
                    part.index,
                    part.count,
                    f"tensor.{part.index}",
                    bytes([part.index]) * 10,
                )
                if progress:
                    progress(10, 10)
                return target

            hf_download.list_hf_gguf_files = fake_list
            hf_download._download_hf_model_source = fake_source
            try:
                merged = hf_download.download_hf_model(
                    cfg,
                    repo="owner/model",
                    filename="model-q4-00001-of-00002.gguf",
                    status=messages.append,
                )
                assert merged.name == "model-q4.gguf" and merged.is_file()
                downloaded_shards = [root2 / "model" / name for name in names]
                assert not any(part.is_file() for part in downloaded_shards)
                assert any("Склеиваю" in message for message in messages)
                assert len(_parse_shard(merged).tensors) == 2
            finally:
                hf_download.list_hf_gguf_files = old_list
                hf_download._download_hf_model_source = old_source
    print("ok  GGUF: разбитые шарды проверяются и склеиваются")


def main() -> None:
    test_signature()
    test_tunnel_helpers()
    test_http()
    test_download_manager()
    test_local_management()
    test_gguf_split()
    print("\nWEB APP TESTS PASSED")


if __name__ == "__main__":
    main()
