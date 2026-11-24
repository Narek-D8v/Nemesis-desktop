"""Тест локального провайдера llama.cpp: пути, команда сервера, каталог.

Запуск:  venv\\Scripts\\python.exe scripts\\test_llama_cpp.py

Сам сервер не запускается: тест проверяет подготовку конфига и понятные
ошибки, когда пакет/модель отсутствуют. Это позволяет гонять его в CI без
нативной сборки llama-cpp-python.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from nemus.config import get_config  # noqa: E402
from nemus.core.llm import ChatTarget, LLMClient  # noqa: E402
from nemus.core.providers import Message, provider_kwargs  # noqa: E402
from nemus.core.providers.llama_cpp import (  # noqa: E402
    LlamaCppError,
    _download_wheels,
    _report_command,
    _Wheel,
    _wheels_from_report,
    build_server_command,
    ensure_llama_cpp_server,
    install_command,
    install_hint,
    package_available,
    package_status,
    resolve_model_path,
)
from nemus.core.providers.models import (  # noqa: E402
    fetch_models,
    parse_llama_cpp,
    static_models,
)


def _cfg_with_model(path: Path, port: int = 9) -> object:
    cfg = get_config()
    cfg.llm.llama_cpp.model_path = str(path)
    cfg.llm.llama_cpp.models_dir = str(path.parent)
    cfg.llm.llama_cpp.host = "127.0.0.1"
    cfg.llm.llama_cpp.port = port
    cfg.llm.llama_cpp.model_alias = "nemus-local"
    cfg.llm.llama_cpp.auto_start = True
    cfg.llm.llama_cpp.start_timeout = 10.0
    cfg.llm.llama_cpp.n_ctx = 4096
    cfg.llm.llama_cpp.n_gpu_layers = -1
    cfg.llm.llama_cpp.n_threads = 4
    cfg.llm.llama_cpp.chat_format = "chatml"
    cfg.llm.providers.pop("llama_cpp", None)
    return cfg


def test_paths() -> None:
    model = Path(tempfile.mkdtemp(prefix="nemus_gguf_")) / "model.gguf"
    model.write_bytes(b"GGUF")
    cfg = _cfg_with_model(model)

    assert resolve_model_path(cfg, "llama_cpp/local") == model.resolve()
    assert resolve_model_path(cfg, "llama_cpp/nemus-local") == model.resolve()
    assert resolve_model_path(cfg, f"llama_cpp/{model}") == model.resolve()

    cfg.llm.llama_cpp.model_path = str(model.parent / "missing.gguf")
    try:
        resolve_model_path(cfg, "llama_cpp/local")
    except LlamaCppError as exc:
        assert "не найдена" in str(exc), exc
    else:  # pragma: no cover
        raise AssertionError("ожидалась ошибка о пропавшем GGUF")
    print("ok  llama.cpp: local/алиас/абсолютный путь и понятная ошибка файла")


def test_command_and_kwargs() -> None:
    model = Path(tempfile.mkdtemp(prefix="nemus_gguf_")) / "model.gguf"
    model.write_bytes(b"GGUF")
    cfg = _cfg_with_model(model, port=8099)
    cfg.llm.llama_cpp.extra_args = ["--flash_attn", "true"]

    cmd = build_server_command(cfg, model)
    assert "llama_cpp.server" in cmd, cmd
    assert "--model" in cmd and str(model) in cmd, cmd
    assert "--model_alias" in cmd and "nemus-local" in cmd, cmd
    assert "--n_ctx" in cmd and "4096" in cmd, cmd
    assert "--n_gpu_layers" in cmd and "-1" in cmd, cmd
    assert "--n_threads" in cmd and "4" in cmd, cmd
    assert "--chat_format" in cmd and "chatml" in cmd, cmd
    assert cmd[-2:] == ["--flash_attn", "true"], cmd

    sys.frozen = True  # type: ignore[attr-defined]
    try:
        frozen_cmd = build_server_command(cfg, model)
    finally:
        del sys.frozen  # type: ignore[attr-defined]
    assert frozen_cmd[:2] == [sys.executable, "--llama-server"], frozen_cmd

    kwargs = provider_kwargs("llama_cpp", cfg)
    assert kwargs["api_base"] == "http://127.0.0.1:8099/v1", kwargs
    assert kwargs["api_key"] == "local", kwargs

    client = LLMClient(cfg)
    built = client._build_kwargs(ChatTarget("llama_cpp", "llama_cpp/local"), None)
    assert built["model"] == "openai/nemus-local", built
    assert built["api_base"] == "http://127.0.0.1:8099/v1", built
    print("ok  llama.cpp: команда сервера, api_base и OpenAI-алиас для LiteLLM")


def test_catalog() -> None:
    root = Path(tempfile.mkdtemp(prefix="nemus_gguf_catalog_"))
    (root / "a.gguf").write_bytes(b"GGUF")
    (root / "nested").mkdir()
    (root / "nested" / "b.GGUF").write_bytes(b"GGUF")
    (root / "note.txt").write_text("не модель", encoding="utf-8")

    parsed = parse_llama_cpp(root)
    assert len(parsed) == 2, parsed
    assert all(free for _, free in parsed), parsed
    assert all(name.startswith("llama_cpp/") for name, _ in parsed), parsed
    assert static_models("llama_cpp") == [("llama_cpp/local", True)]

    cfg = _cfg_with_model(root / "a.gguf")
    live = fetch_models("llama_cpp", cfg)
    assert len(live) >= 2, live
    print("ok  llama.cpp: каталог .gguf (включая вложенные папки) и free-флаг")


def test_existing_server_is_reused() -> None:
    """Готовый внешний сервер с нужным алиасом не перезапускается."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"data": [{"id": "nemus-local"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        model = Path(tempfile.mkdtemp(prefix="nemus_gguf_")) / "model.gguf"
        model.write_bytes(b"GGUF")
        cfg = _cfg_with_model(model, port=server.server_address[1])
        assert (
            ensure_llama_cpp_server(cfg, "llama_cpp/local")
            == f"http://127.0.0.1:{server.server_address[1]}/v1"
        )
    finally:
        server.shutdown()
        server.server_close()
    print("ok  llama.cpp: готовый сервер с нужным алиасом переиспользуется")


def test_unavailable_local_falls_back() -> None:
    """Если локальный сервер не поднялся, облачный fallback всё равно ответит."""
    import litellm

    model = Path(tempfile.mkdtemp(prefix="nemus_gguf_")) / "model.gguf"
    model.write_bytes(b"GGUF")
    cfg = _cfg_with_model(model)
    cfg.llm.default_provider = "llama_cpp"
    cfg.llm.default_model = "llama_cpp/local"
    cfg.llm.fallback = [
        SimpleNamespace(provider="gemini", model="gemini/fallback")
    ]
    client = LLMClient(cfg)
    calls: list[str] = []
    original = litellm.completion

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ответ из облака", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            model=kwargs["model"],
        )

    litellm.completion = fake_completion
    try:
        with mock.patch(
            "nemus.core.llm.ensure_llama_cpp_server",
            side_effect=LlamaCppError("локальный сервер не поднялся"),
        ):
            response = client.complete([Message(role="user", content="привет")])
    finally:
        litellm.completion = original

    assert calls == ["gemini/fallback"], calls
    assert response.content == "ответ из облака", response
    print("ok  llama.cpp недоступен → задача уходит в облачный fallback")


def test_install_command() -> None:
    """Кнопка установки должна брать готовое колесо, а не собирать исходники."""
    cmd = install_command()
    assert cmd[:3] == [sys.executable, "-m", "pip"], cmd
    assert "install" in cmd, cmd
    assert "--only-binary=:all:" in cmd, cmd
    assert "--no-cache-dir" in cmd, cmd
    assert "llama-cpp-python[server]" in cmd, cmd
    assert "--extra-index-url" in cmd, cmd
    assert "whl/cpu" in install_hint(), install_hint()

    force_cmd = install_command([Path("demo.whl")], force=True)
    assert "--force-reinstall" in force_cmd, force_cmd
    force_report = _report_command(Path("report.json"), force=True)
    assert "--ignore-installed" in force_report, force_report
    print("ok  llama.cpp: установка и принудительная переустановка")


def test_package_status() -> None:
    """Повреждённый пакет отличается от отсутствующего."""
    import nemus.core.providers.llama_cpp as lc

    with mock.patch.object(lc.importlib.util, "find_spec", return_value=None):
        missing = package_status()
    assert not missing.available and not missing.broken, missing

    spec = SimpleNamespace(submodule_search_locations=[])
    broken_run = SimpleNamespace(
        returncode=1, stdout="Traceback...\nImportError: DLL load failed"
    )
    with (
        mock.patch.object(lc.importlib.util, "find_spec", return_value=spec),
        mock.patch.object(lc.subprocess, "run", return_value=broken_run),
    ):
        broken = package_status()
    assert broken.broken and not broken.available, broken
    assert "DLL load failed" in broken.detail, broken

    ok_run = SimpleNamespace(returncode=0, stdout="")
    with (
        mock.patch.object(lc.importlib.util, "find_spec", return_value=spec),
        mock.patch.object(lc.subprocess, "run", return_value=ok_run),
    ):
        ok = package_status()
    assert ok.available and not ok.broken, ok
    print("ok  llama.cpp: отсутствующий, повреждённый и рабочий пакет")


def test_install_report_and_progress() -> None:
    """Отчёт pip даёт точный список колёс, а загрузка — точный прогресс."""
    import http.server
    import threading
    from functools import partial

    root = Path(tempfile.mkdtemp(prefix="nemus_wheels_"))
    out = root / "out"
    out.mkdir()
    payload = b"x" * (300 * 1024 + 17)
    (root / "demo-1.0-py3-none-any.whl").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # noqa: D102
            return

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/demo-1.0-py3-none-any.whl"
    try:
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "install": [
                        {
                            "download_info": {
                                "url": url,
                                "archive_info": {"hashes": {"sha256": digest}},
                            }
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        wheels = _wheels_from_report(report)
        assert wheels == [_Wheel(url, "demo-1.0-py3-none-any.whl", digest)], wheels

        seen: list[tuple[int, int]] = []
        paths = _download_wheels(
            wheels,
            out,
            lambda done, total: seen.append((done, total)),
            None,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert paths == [out / "demo-1.0-py3-none-any.whl"], paths
    assert seen[0] == (0, len(payload)), seen[0]
    assert seen[-1] == (len(payload), len(payload)), seen[-1]
    assert all(a <= b for (a, _), (b, _) in zip(seen, seen[1:], strict=False)), seen
    print("ok  llama.cpp: отчёт pip и точный прогресс")


def test_missing_package_is_clear() -> None:
    model = Path(tempfile.mkdtemp(prefix="nemus_gguf_")) / "model.gguf"
    model.write_bytes(b"GGUF")
    cfg = _cfg_with_model(model)

    import nemus.core.providers.llama_cpp as lc

    original = lc.package_status
    lc.package_status = lambda: lc.PackageStatus(False, False, "")
    try:
        ensure_llama_cpp_server(cfg, "llama_cpp/local")
    except LlamaCppError as exc:
        text = str(exc)
        assert "llama-cpp-python" in text, text
        assert "pip install" in text, text
    else:  # pragma: no cover
        raise AssertionError("без пакета сервер не должен запускаться")
    finally:
        lc.package_status = original
    print("ok  llama.cpp: без пакета — подсказка с командой установки")


def main() -> int:
    print("llama-cpp-python установлен:", package_available())
    test_paths()
    test_command_and_kwargs()
    test_catalog()
    test_existing_server_is_reused()
    test_unavailable_local_falls_back()
    test_install_command()
    test_package_status()
    test_install_report_and_progress()
    test_missing_package_is_clear()
    print("\nLLAMA.CPP TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
