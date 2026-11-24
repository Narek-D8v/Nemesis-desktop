# Установка и скачивание: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разрешить установку по явной просьбе с обязательной локальной проверкой, добавить FDM-подобный загрузчик, браузерный и торрент-fallback, общий прогресс в Telegram/GUI и устранить залипший статус и долгое думание.

**Architecture:** Одно типизированное событие `DownloadEvent` serves as the single progress channel. `download_file` becomes a segmented resumable downloader; browser and torrent tools publish the same events. Telegram/GUI consume events by `task_id`. Installation is gated by `verify_installer`/`install_verified`. Status cleanup is moved into `finally`-блоки.

**Tech Stack:** Python, `httpx`, `ThreadPoolExecutor`, Playwright (опционально), `libtorrent` (опционально), `python-telegram-bot` inline callbacks, PySide6 `QProgressBar`, `pytest`-style scripts in `scripts/`.

**Spec:** `docs/superpowers/specs/2026-11-24-install-downloads-design.md`

## Global Constraints

- Только HTTPS, имя файла очищается через `_safe_name`, папка `data/downloads`.
- `DEFAULT_MAX_MB = 500`, `HARD_MAX_MB = 2048`, `TIMEOUT = 120.0`.
- `SEGMENT_SIZE_MB = 8`, `MAX_SEGMENTS = 8`, полоса прогресса на 20 символов.
- Антивирус — только локальный Windows Defender; облачные сервисы не используются.
- Жёсткие блокировщики: Defender чистый и SHA-256 совпавший. Подпись `Valid` жёсткая для обычных файлов; при `explicit_piracy_request=True` невалидная подпись = предупреждение + отдельное подтверждение.
- `confirm_installs: true` остаётся; `install_verified` требует отдельного подтверждения.
- Не использовать установленный FDM; `libtorrent` — опциональный lazy-import, не обязательная зависимость.
- Не запускать реальные установки и реальные торренты в тестах.
- Не трогать чужие незакоммиченные файлы; пуш только по отдельной просьбе.
- Все новые тесты — в `scripts/`, как в текущем репозитории.

## Review Focus

1. Кракнутый `.exe` по явной просьбе: невалидная подпись не должна жёстко блокировать, но должна показывать предупреждение и требовать подтверждения — тест в Task 3.
2. Сервер игнорирует `Range` или отвечает `416`: корректный откат на один поток без повреждения файла — тест в Task 2.
3. Параллельные загрузки модели и файла: события не должны перетирать друг друга — тест `task_id` в Task 1 и UI-тест в Task 7.
4. Stop во время сегментированной загрузки: отмена останавливает потоки и оставляет части для докачки — тест в Task 2.
5. Ошибка финальной отправки в Telegram: статус «Думаю…» обязан быть снят — red-тест в Task 8.

---

### Task 1: Единое событие прогресса

**Files:**
- Create: `nemus/core/download_events.py`
- Modify: `nemus/core/providers/hf_download.py:115-123`
- Test: `scripts/test_download_events.py`

**Interfaces:**
- Consumes: `ModelDownloadEvent` из `hf_download.py`.
- Produces:
  - `DownloadEvent(kind: str, phase: str, name: str, done: int = 0, total: int = 0, path: str = "", message: str = "", task_id: str = "")` с `.percent: int | None`.
  - `subscribe_download(listener) -> Callable`, `unsubscribe_download(listener) -> None`, `publish_download(event) -> None`.
  - `new_download_id() -> str`.
  - `render_progress_bar(percent: int | None, width: int = 20) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_download_event_bus_and_bar():
    from nemus.core.download_events import (
        DownloadEvent, publish_download, subscribe_download,
        unsubscribe_download, render_progress_bar,
    )
    event = DownloadEvent(kind="file", phase="progress", name="setup.exe",
                          done=84, total=125, task_id="t1")
    assert event.percent == 67
    assert render_progress_bar(67) == "████████░░░░ 67%"
    seen = []
    token = subscribe_download(seen.append)
    try:
        publish_download(event)
    finally:
        unsubscribe_download(token)
    assert seen == [event]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_download_events.py`  
Expected: FAIL with `ModuleNotFoundError: nemus.core.download_events`.

- [ ] **Step 3: Implement `nemus/core/download_events.py`**

Thread-safe list of listeners, `publish_download` isolates listener exceptions. `percent` is `None` when `total <= 0`. `render_progress_bar` fills `round(width * percent / 100)` blocks and pads with `░`, suffix `f" {percent}%"`; `None` → only `░ * width` without percent.

- [ ] **Step 4: Adapter from model downloads**

Modify `hf_download._publish()`: after the existing loop, call `publish_download(DownloadEvent(kind="model", phase=event.phase, name=event.name, done=event.done, total=event.total, path=event.path, message=event.message))`. Add an assertion in the same test that a published `ModelDownloadEvent` is observed with `kind == "model"`.

- [ ] **Step 5: Run test to verify it passes**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_download_events.py`  
Expected: PASS.

- [ ] **Step 6: Commit**

`git add nemus/core/download_events.py nemus/core/providers/hf_download.py scripts/test_download_events.py`

---

### Task 2: Сегментированная докачка в `download_file`

**Files:**
- Modify: `nemus/core/tools/downloads.py`
- Modify: `scripts/test_software_install.py`
- Test: `scripts/test_software_install.py`

**Interfaces:**
- Consumes: `publish_download`, `new_download_id` from Task 1.
- Produces:
  - `DownloadFileTool.execute(url, filename, max_mb)` — публичный контракт без изменений.
  - `DownloadControls` with `register(task_id) -> threading.Event`, `cancel(task_id)`, `forget(task_id)`.
  - Helper functions `_plan_segments(total: int) -> list[tuple[int, int]]`, `_merge_and_hash(parts: list[Path], target: Path) -> tuple[int, str]`, `_write_manifest(target: Path, sha256: str)`, `_read_manifest(target: Path) -> str | None`.
  - Manifest file `<target>.sha256` contains only the hex digest.

- [ ] **Step 1: Write failing tests with `httpx.MockTransport`**

```python
def test_segmented_download_resumes_and_publishes(monkeypatch, tmp_path):
    ranges = []
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={
                "Content-Length": "10", "Accept-Ranges": "bytes", "ETag": "v1"})
        rng = request.headers.get("Range")
        if rng:
            ranges.append(rng)
            start = int(rng.removeprefix("bytes=").split("-")[0])
            return httpx.Response(206, content=b"0123456789"[start:])
        return httpx.Response(200, content=b"0123456789")
    monkeypatch.setattr(downloads, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    tool = downloads.DownloadFileTool(); tool.root = tmp_path
    result = tool.execute("https://example.test/setup.exe", "setup.exe")
    assert result.ok
    assert (tmp_path / "setup.exe").read_bytes() == b"0123456789"
    assert (tmp_path / "setup.exe.sha256").read_text().strip() == hashlib.sha256(b"0123456789").hexdigest()
    assert ranges and all(r.startswith("bytes=") for r in ranges)

def test_range_ignored_falls_back_to_single_stream(monkeypatch, tmp_path):
    # HEAD without Accept-Ranges; GET with Range -> 200 full body.
    # Assert resulting file is exactly the body and no .parts directory remains.

def test_cancel_keeps_parts(monkeypatch, tmp_path):
    # controls.cancel(task_id) before execute; assert not result.ok and part files exist.

def test_oversize_cleans_up(monkeypatch, tmp_path):
    # Content-Length above HARD_MAX_MB; assert no target and no part files.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_software_install.py`  
Expected: FAIL on new tests.

- [ ] **Step 3: Implement the downloader**

Keep the existing HTTPS/name/limit checks. On `HEAD`, if `Accept-Ranges: bytes` and `Content-Length <= limit`, split into `_plan_segments(total)`; use `.parts/<name>-<urlhash>/part-NN`, skip already-downloaded prefix by `Range` from existing part size; run segments in `ThreadPoolExecutor(max_workers=min(MAX_SEGMENTS, len(segments)))`. Publish `progress` events with summed bytes. On `200` instead of `206` or `416`, discard parts and use the existing single-stream loop. On error/cancel keep parts and publish `cancelled`/`error`. On success call `_merge_and_hash`, verify total, `_write_manifest`, replace `.part` → target, delete `.parts`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_software_install.py`  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/core/tools/downloads.py scripts/test_software_install.py`

---

### Task 3: `verify_installer` и `install_verified`

**Files:**
- Modify: `nemus/core/tools/system.py`
- Modify: `nemus/core/tools/all.py`
- Test: `scripts/test_software_install.py`

**Interfaces:**
- Consumes: `ScanFileTool`, `run_capture` from `nemus/core/tools/system.py`; manifest from Task 2.
- Produces:
  - `VerifyInstallerTool.execute(path: str, expected_sha256: str = "", explicit_piracy_request: bool = False) -> ToolResult`
  - `InstallVerifiedTool.execute(path: str, expected_sha256: str = "", explicit_piracy_request: bool = False, confirmed: bool = False, timeout: int = 300) -> ToolResult`
  - Helpers `_authenticode(path: Path) -> tuple[bool, str]` and `_file_hash(path: Path) -> str`.
  - Result text contains `verdict: ...`, `defender: ...`, `signature: ...`, `sha256: ...`, and `warning: ...` when unsigned-but-explicit.

- [ ] **Step 1: Write failing tests**

```python
def test_verify_installer_policy(monkeypatch, tmp_path):
    # monkeypatch ScanFileTool.execute to success/failure
    # monkeypatch _authenticode to (False, "Unsigned") / (True, "CN=Vendor")
    # create file and manifest
    tool = system.VerifyInstallerTool()
    assert not tool.execute(path=str(p), expected_sha256=h).ok          # unsigned, ordinary
    ok = tool.execute(path=str(p), expected_sha256=h,
                      explicit_piracy_request=True)
    assert ok.ok and "warning" in ok.content
    assert not tool.execute(path=str(p), expected_sha256="0"*64,
                            explicit_piracy_request=True).ok            # hash hard blocker

def test_install_verified_requires_confirmation(monkeypatch, tmp_path):
    # monkeypatch VerifyInstallerTool.execute to success and subprocess run
    tool = system.InstallVerifiedTool()
    assert not tool.execute(path=str(p), confirmed=False).ok
    assert tool.execute(path=str(p), confirmed=True).ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_software_install.py`  
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the tools**

`VerifyInstallerTool`: always run `scan_file`; hash via `_file_hash` compared to `expected_sha256` or `_read_manifest`; signature via `_authenticode`. Ordinary path: unsigned ⇒ `ok=False`. Explicit path: unsigned ⇒ `ok=True` with `warning`. Defender threat/inconclusive or hash mismatch ⇒ `ok=False` even in explicit mode. Register both tools in `all.py`.

`InstallVerifiedTool`: call `VerifyInstallerTool`; if not ok or `confirmed=False`, return failure. Else run `Start-Process -Wait` for `.exe` or `msiexec` for `.msi` in a separate thread via `run_capture(..., timeout=timeout)`; truncate output and return it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_software_install.py`  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/core/tools/system.py nemus/core/tools/all.py scripts/test_software_install.py`

---

### Task 4: Политика скилла и системного промпта

**Files:**
- Modify: `nemus/skills/software-install/SKILL.md`
- Modify: `nemus/core/agent.py:606-632`
- Test: `scripts/test_software_install.py`

**Interfaces:**
- Consumes: tool names from Tasks 2–3.
- Produces: prompt/skill text only; no new Python API.

- [ ] **Step 1: Write failing test**

```python
def test_install_policy_text():
    from nemus.core.skills import load_skills
    from nemus.core.agent import build_system_prompt
    skill = next(s for s in load_skills() if s.name == "software-install")
    assert "browser_download_file" in skill.instructions
    assert "torrent_download" in skill.instructions
    assert "инлайн" in skill.instructions
    from nemus.config import Config
    from nemus.core.tools.base import ToolRegistry
    prompt = build_system_prompt(Config(), ToolRegistry())
    assert "install_verified" in prompt
    assert "предупреждение" in prompt.lower()
    assert "Сам не ищи пиратские сборки" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_software_install.py`  
Expected: FAIL on first assertion.

- [ ] **Step 3: Rewrite policy text**

In rule 6: explicit request allowed only via `install_verified`; proactive search forbidden; unsigned explicit file requires warning + confirmation. Add fallback section: `download_file` → `browser_download_file` → user-approved `torrent_download`. Report template: always show Defender verdict, signature, SHA-256 and warning. Mirror the same in `agent.py` prompt block.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_software_install.py`  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/skills/software-install/SKILL.md nemus/core/agent.py scripts/test_software_install.py`

---

### Task 5: Браузерный загрузчик

**Files:**
- Modify: `nemus/core/tools/browser.py`
- Modify: `scripts/test_browser.py`
- Test: `scripts/test_browser.py`

**Interfaces:**
- Consumes: `publish_download`, `new_download_id` from Task 1; `_safe_name` and limits from Task 2.
- Produces: `BrowserDownloadFileTool` with `name = "browser_download_file"`, `execute(url, filename="", max_mb=DEFAULT_MAX_MB, selector="") -> ToolResult`. Registered in `browser_tools()`.

- [ ] **Step 1: Write failing tests**

```python
def test_browser_download_saves_file_and_publishes(monkeypatch, tmp_path):
    # fake sync_playwright -> page.expect_download() context manager,
    # fake download.save_as writes b"browser-bytes"
    result = browser.BrowserDownloadFileTool().execute(
        "https://example.test/setup.exe", "setup.exe")
    assert result.ok and "setup.exe" in result.content
    # assert a DownloadEvent with kind == "browser" was published

def test_browser_download_without_playwright(monkeypatch):
    # force ImportError; assert not result.ok and "Playwright" in result.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_browser.py`  
Expected: FAIL with missing tool.

- [ ] **Step 3: Implement the tool**

Use `page.expect_download()` around `page.goto`/optional `page.click(selector)`, then `download.save_as(str(partial))`; reuse `_safe_name`, root, size cap, SHA-256 manifest from Task 2; publish `browser` events; on missing Playwright/engine return honest failure and call the existing `_open_in_system_browser()` only when explicitly requested by the agent, not silently.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_browser.py`  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/core/tools/browser.py scripts/test_browser.py`

---

### Task 6: Торрент отдельным инструментом с инлайн-согласием

**Files:**
- Create: `nemus/core/tools/torrents.py`
- Modify: `nemus/core/tools/all.py`
- Modify: `nemus/core/telegram_bot.py`
- Test: `scripts/test_telegram.py`

**Interfaces:**
- Consumes: `publish_download`, `new_download_id` from Task 1; existing `_keyboard`, `_pending`, `_on_callback` from `telegram_bot.py`.
- Produces:
  - `TorrentDownloadTool.execute(magnet: str = "", torrent_url: str = "", filename: str = "", max_mb: int = DEFAULT_MAX_MB) -> ToolResult`
  - `TelegramBot._ask_torrent(chat_id, user_id, text) -> asyncio.Future[bool]` using callback namespace `td:<chat>:<user>:<ns>:y|n`.

- [ ] **Step 1: Write failing tests**

```python
def test_torrent_tool_publishes_progress(monkeypatch, tmp_path):
    # fake _libtorrent() session; assert result.ok, file bytes, kind == "torrent"

def test_torrent_inline_button_gate():
    # using existing Telegram fakes, call _ask_torrent,
    # assert keyboard contains "Скачать через торрент" and future resolves only on ":y"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_telegram.py`  
Expected: FAIL with missing tool/method.

- [ ] **Step 3: Implement**

Lazy-import `libtorrent`; if unavailable return `ToolResult.failure` with installation hint. Accept only user-supplied magnet/HTTPS `.torrent`; never search. Reuse root, `_safe_name`, limit, SHA-256 manifest, `DownloadEvent(kind="torrent")`. In `TelegramBot`, `_ask_torrent` mirrors `_ask_confirm()` structure and `_on_callback` resolves `td:` callbacks; validate chat/user and timeout with `CONFIRM_TIMEOUT`. GUI uses the existing `ConfirmDialog` path — no new GUI code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_telegram.py`  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/core/tools/torrents.py nemus/core/tools/all.py nemus/core/telegram_bot.py scripts/test_telegram.py`

---

### Task 7: Прогресс в Telegram и GUI

**Files:**
- Modify: `nemus/core/telegram_bot.py`
- Modify: `nemus/gui/main_window.py`
- Modify: `nemus/gui/i18n.py`
- Test: `scripts/test_telegram.py`, `scripts/smoke_gui.py`

**Interfaces:**
- Consumes: `DownloadEvent`, `subscribe_download`, `unsubscribe_download`, `render_progress_bar`, `new_download_id` from Task 1.
- Produces: `TelegramBot._on_download_event(event)`; `MainWindow._on_download_event(event)`; i18n key `download.file_progress` = `"{name}: {percent}% ({done} / {total})"`.

- [ ] **Step 1: Write failing tests**

```python
def test_telegram_download_bar_uses_task_id():
    # publish two events with different task_id; assert two distinct UI rows,
    # row for t1 shows render_progress_bar(67) and name "setup.exe"

def test_gui_download_bar_shows_file_name():
    # emit DownloadEvent(kind="file", name="setup.exe", done=84, total=125)
    # assert window.download_label.text() contains "setup.exe" and "67%"
    # assert window.download_bar.value() == 670
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_telegram.py` and `$env:QT_QPA_PLATFORM='offscreen'; .\\venv\\Scripts\\python.exe scripts\\smoke_gui.py`  
Expected: FAIL on new assertions.

- [ ] **Step 3: Implement consumers**

Telegram: subscribe in constructor; `_on_download_event` bridges with `run_coroutine_threadsafe`; keep dict keyed by `(chat_id, task_id)`; render `render_progress_bar` via `_Progress.download()`/`_DraftProgress.download()`; throttle with `PROGRESS_INTERVAL`, force on `done/error`. GUI: subscribe in `MainWindow.__init__`, emit a Qt signal, keep dict keyed by `task_id`; reuse `self.download_bar`/`download_label`; replace model-specific i18n wording with `download.file_progress`. Unsubscribe on shutdown.

- [ ] **Step 4: Run tests to verify they pass**

Run: both commands from Step 2.  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/core/telegram_bot.py nemus/gui/main_window.py nemus/gui/i18n.py scripts/test_telegram.py scripts/smoke_gui.py`

---

### Task 8: Залипший статус «Думаю…»

**Files:**
- Modify: `nemus/core/telegram_bot.py:3819-3884`
- Modify: `nemus/gui/main_window.py:786-807`
- Modify: `nemus/app.py:433-455`
- Modify: `nemus/voice/manager.py:243-260`
- Test: `scripts/test_telegram.py`, `scripts/smoke_gui.py`

**Interfaces:**
- Consumes: existing `_Progress`/`_DraftProgress`, `AssistState`, fakes from `scripts/test_telegram.py`.
- Produces: no new public API; behavior contract: after `final`, `error`, `stopped` or any exception, `_active_progress is None`, no draft text contains «Думаю», GUI state is not `THINKING` unless work is still running.

- [ ] **Step 1: Write red tests**

```python
def test_telegram_status_cleared_on_final_error_and_send_exception():
    # fake agent yields final; assert _active_progress is None and no "Думаю"
    # fake _send_final raising once; assert progress closed and status cleared
    # fake agent yielding error; assert status cleared

def test_gui_realtime_tool_restores_state():
    window.show_realtime_tool("x", {}, "start")
    assert window.state_label.text() == i18n.tr("state.thinking")
    window.show_realtime_tool("x", {}, "ok")
    assert window.state_label.text() != i18n.tr("state.thinking")
```

- [ ] **Step 2: Run tests to verify they are red**

Run: `.\\venv\\Scripts\\python.exe scripts\\test_telegram.py` and `$env:QT_QPA_PLATFORM='offscreen'; .\\venv\\Scripts\\python.exe scripts\\smoke_gui.py`  
Expected: FAIL — current code has no outer `finally`, and realtime `ok` does not restore state.

- [ ] **Step 3: Fix cleanup**

Telegram: wrap lines 3838–3884 in `try/finally`; `finally` calls `await progress.close()` only if not already closed and resets `_active_progress`. If rich and HTML final sends fail, edit the status message into short plain text, then only log. Cancel the `_request_stop()` note task before closing. GUI: `show_realtime_tool(phase="start")` saves previous state, `ok/fail` restores it in `finally`. In `app.py` wake-skill and `voice/manager.py` STT, move `IDLE` reset into `finally`.

- [ ] **Step 4: Run tests to verify they pass**

Run: both commands from Step 2.  
Expected: PASS.

- [ ] **Step 5: Commit**

`git add nemus/core/telegram_bot.py nemus/gui/main_window.py nemus/app.py nemus/voice/manager.py scripts/test_telegram.py scripts/smoke_gui.py`

---

### Task 9: Долгое думание — измерить и вернуть быстрый путь

**Files:**
- Create: `scripts/bench_agent_latency.py`
- Modify: `nemus/core/agent.py:2063-2080`
- Test: `scripts/bench_agent_latency.py`

**Interfaces:**
- Consumes: `Agent.run`, `quick_reply`, `is_simple_request`, `_run_fast_reply`, `_character_prompt_block`.
- Produces: `bench_agent_latency.py` prints phase timings; no new Python API.

- [ ] **Step 1: Write failing test**

```python
def test_simple_request_uses_fast_path_with_character_enabled():
    cfg = Config(); cfg.character.enabled = True
    agent = build_agent(cfg)
    events = list(agent.run("Немуся?", stop=None))
    assert any(e.kind == "final" for e in events)
    assert not any(e.kind == "tool_call" for e in events)
```

- [ ] **Step 2: Run baseline and verify it is red**

Run: `.\\venv\\Scripts\\python.exe scripts\\bench_agent_latency.py`  
Expected: «Немуся?» goes through the full loop; timing output shows prompt refresh + LLM + retries dominate.

- [ ] **Step 3: Fix the fast path**

Keep the static `quick_reply` only when `character` is disabled. Remove the guard that skips `_run_fast_reply()` when `character.enabled` is true — `_run_fast_reply()` already builds its prompt with `_character_prompt_block(self.config)` at `agent.py:1459`. Keep `images` and `quality_task` exclusions.

- [ ] **Step 4: Run test and benchmark again**

Run: `.\\venv\\Scripts\\python.exe scripts\\bench_agent_latency.py`  
Expected: PASS; simple request produces `final` without `tool_call` and is measurably faster.

- [ ] **Step 5: Commit**

`git add scripts/bench_agent_latency.py nemus/core/agent.py`

---

## Self-review notes

- Spec coverage: every section has an owning task: event bus (1), FDM-like downloader (2), policy/verification (3–4), browser (5), torrent (6), UI progress (7), stuck status (8), slow thinking (9).
- Review Focus pinned: unsigned explicit file (Task 3), `Range` fallback and cancel/oversize (Task 2), parallel `task_id` (Tasks 1/7), final-send exception (Task 8).
- Type consistency: `DownloadEvent`, `publish_download`, `new_download_id`, `render_progress_bar`, `DownloadControls`, `VerifyInstallerTool`, `InstallVerifiedTool`, `BrowserDownloadFileTool`, `TorrentDownloadTool`, `_ask_torrent`, `_on_download_event` keep the same names across tasks.
