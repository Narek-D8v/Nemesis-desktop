"""Тест автоскачивания GGUF с Hugging Face (без реальной сети).

Запуск:  venv\\Scripts\\python.exe scripts/test_hf_download.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import get_config  # noqa: E402
from nemus.core.providers.hf_catalog import (  # noqa: E402
    HFFile,
    choose_hf_file,
    group_hf_files,
    is_split_gguf,
    list_hf_gguf_files,
    parse_hf_files,
    parse_hf_search,
    search_hf_models,
)
from nemus.core.providers.hf_download import (  # noqa: E402
    HFDownloadError,
    download_hf_model,
    ensure_hf_model,
    format_bytes,
    hf_local_path,
    parse_hf_source,
    subscribe_model_download,
    unsubscribe_model_download,
)

MODEL = b"GGUF" + bytes(range(64)) * 2000


class _Handler(BaseHTTPRequestHandler):
    requests = 0
    ranges: list[str] = []

    def _json(self, data: object) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests += 1
        if self.path.startswith("/api/models/owner/model"):
            self._json(
                {
                    "id": "owner/model",
                    "siblings": [
                        {"rfilename": "model.gguf", "size": len(MODEL)},
                        {"rfilename": "mmproj-model.gguf", "size": 11},
                        {"rfilename": "notes.txt", "size": 3},
                    ],
                }
            )
            return
        if self.path.startswith("/api/models"):
            self._json(
                [
                    {
                        "id": "owner/model",
                        "tags": ["gguf"],
                        "downloads": 1234,
                        "likes": 7,
                    },
                    {"id": "owner/other", "tags": ["gguf"], "downloads": 5},
                ]
            )
            return
        if "missing" in self.path:
            self.send_response(404)
            self.end_headers()
            return
        range_header = self.headers.get("Range") or ""
        if range_header:
            type(self).ranges.append(range_header)
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            body = MODEL[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(MODEL) - 1}/{len(MODEL)}"
            )
        else:
            body = MODEL
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _cfg(server: ThreadingHTTPServer) -> object:
    cfg = get_config()
    cfg.llm.llama_cpp.models_dir = tempfile.mkdtemp(prefix="nemus_hf_")
    cfg.llm.llama_cpp.hf_endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    cfg.llm.llama_cpp.hf_repo = "owner/model"
    cfg.llm.llama_cpp.hf_file = "model.gguf"
    cfg.llm.llama_cpp.hf_revision = "main"
    cfg.llm.llama_cpp.auto_download = True
    cfg.llm.llama_cpp.model_path = ""
    return cfg


def test_parse_sources() -> None:
    src = parse_hf_source("owner/model", "sub/model.gguf")
    assert (src.repo_id, src.filename, src.revision) == (
        "owner/model",
        "sub/model.gguf",
        "main",
    ), src

    url = "https://huggingface.co/owner/model/resolve/v2/q4/model.gguf"
    src = parse_hf_source(url)
    assert (src.repo_id, src.revision, src.filename) == (
        "owner/model",
        "v2",
        "q4/model.gguf",
    ), src

    src = parse_hf_source(
        "owner/model", filename="https://huggingface.co/a/b/blob/main/x.gguf"
    )
    assert (src.repo_id, src.filename) == ("a/b", "x.gguf"), src

    for bad in ("../../evil.gguf", "/etc/passwd.gguf", "..\\evil.gguf"):
        src = parse_hf_source("owner/model", bad)
        assert ".." not in src.filename, src
        assert not src.filename.startswith("/"), src
    try:
        parse_hf_source("owner/model", "notes.txt")
    except HFDownloadError as exc:
        assert ".gguf" in str(exc), exc
    else:  # pragma: no cover
        raise AssertionError("не-GGUF файл должен отклоняться")
    print("ok  Hugging Face: repo id/URL/ревизия и безопасный путь")


def test_download_and_resume() -> None:
    server = _server()
    try:
        cfg = _cfg(server)
        progress: list[tuple[int, int]] = []
        target = download_hf_model(
            cfg, progress=lambda done, total: progress.append((done, total))
        )
        assert target == hf_local_path(cfg), (target, hf_local_path(cfg))
        assert target.is_file() and target.read_bytes() == MODEL, target
        assert progress and progress[-1][0] == len(MODEL), progress[-3:]
        assert target.parts[-2:] == ("model", "model.gguf"), target.parts

        first_requests = _Handler.requests
        again = download_hf_model(cfg)
        assert again == target
        assert _Handler.requests == first_requests, "готовый файл скачался повторно"

        # обрыв: .part уже содержит часть файла — сервер должен отдать Range
        target.unlink()
        part = target.with_name(target.name + ".part")
        part.write_bytes(MODEL[:1234])
        _Handler.ranges.clear()
        resumed = download_hf_model(cfg)
        assert resumed.read_bytes() == MODEL, resumed
        assert _Handler.ranges == ["bytes=1234-"], _Handler.ranges
    finally:
        server.shutdown()
        server.server_close()
    print("ok  Hugging Face: скачивание, прогресс, повтор и докачка через Range")


def test_catalog_and_events() -> None:
    server = _server()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        models = search_hf_models("model", endpoint=endpoint)
        assert [(m.repo_id, m.downloads) for m in models] == [
            ("owner/model", 1234),
            ("owner/other", 5),
        ], models
        files = list_hf_gguf_files("owner/model", endpoint=endpoint)
        assert [f.filename for f in files] == ["model.gguf"], files

        parsed = parse_hf_search(
            [
                {"id": "a/b", "tags": ["gguf"], "downloads": 2},
                {"id": "c/d", "tags": ["safetensors"]},
                {"id": "bad"},
            ]
        )
        assert [m.repo_id for m in parsed] == ["a/b"], parsed
        assert is_split_gguf("model-00001-of-00004.gguf")
        assert not is_split_gguf("model-q4_k_m.gguf")
        # кнопка «Скачать» берёт Q4_K_M, а не самый большой Q8_0
        variants = [
            HFFile("model-Q8_0.gguf", 900),
            HFFile("model-Q4_K_M.gguf", 500),
            HFFile("model-Q2_K.gguf", 300),
        ]
        chosen = choose_hf_file(variants)
        assert chosen is not None and chosen.filename == "model-Q4_K_M.gguf", chosen
        explicit = choose_hf_file(variants, "нужна q8 для качества")
        assert explicit is not None and explicit.filename == "model-Q8_0.gguf", explicit
        # шарды схлопываются, а число частей не зависит от последнего файла
        shards = [
            HFFile("a-00001-of-00002.gguf", 10),
            HFFile("a-00002-of-00002.gguf", 10),
            HFFile("b-00001-of-00004.gguf", 10),
            HFFile("b-00002-of-00004.gguf", 10),
            HFFile("b-00003-of-00004.gguf", 10),
            HFFile("b-00004-of-00004.gguf", 10),
        ]
        grouped = {item.filename: item.parts for item in group_hf_files(shards)}
        assert grouped == {
            "a-00001-of-00002.gguf": 2,
            "b-00001-of-00004.gguf": 4,
        }, grouped
        assert parse_hf_files(
            {"siblings": [{"rfilename": "x.gguf", "size": 9}]}
        )[0].filename == "x.gguf"

        events: list[str] = []
        listener = lambda event: events.append(event.phase)  # noqa: E731
        subscribe_model_download(listener)
        try:
            cfg = _cfg(server)
            download_hf_model(cfg, repo="owner/model", filename="model.gguf")
        finally:
            unsubscribe_model_download(listener)
        assert events[0] == "start" and "progress" in events, events
        assert events[-1] == "done", events
    finally:
        server.shutdown()
        server.server_close()
    print("ok  Hugging Face: поиск, файлы модели и события прогресса")


def test_errors_and_auto() -> None:
    server = _server()
    try:
        cfg = _cfg(server)
        server_missing = "http://127.0.0.1:%d" % server.server_address[1]
        cfg.llm.llama_cpp.hf_endpoint = server_missing
        cfg.llm.llama_cpp.hf_file = "missing.gguf"
        try:
            download_hf_model(cfg)
        except HFDownloadError as exc:
            assert "не найден" in str(exc), exc
        else:  # pragma: no cover
            raise AssertionError("404 должен давать понятную ошибку")

        cfg.llm.llama_cpp.hf_file = "model.gguf"
        cfg.llm.llama_cpp.auto_download = False
        assert ensure_hf_model(cfg, "llama_cpp/local") is None

        cfg.llm.llama_cpp.auto_download = True
        path = ensure_hf_model(cfg, "llama_cpp/local")
        assert path is not None and path.is_file(), path
        assert Path(cfg.llm.llama_cpp.model_path) == path

        # явный путь не подменяется HF-загрузкой
        assert ensure_hf_model(cfg, "llama_cpp/data/models/other.gguf") is None
    finally:
        server.shutdown()
        server.server_close()
    assert format_bytes(1024) == "1.0 КиБ", format_bytes(1024)
    print("ok  Hugging Face: 404, автоскачивание алиаса и явный путь")


def main() -> int:
    test_parse_sources()
    test_download_and_resume()
    test_catalog_and_events()
    test_errors_and_auto()
    print("\nHF DOWNLOAD TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
