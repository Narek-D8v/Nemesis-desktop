"""Тест Telegram-бота: конфиг, доступ, файлы, видео-кружки, инструмент.

Без сети и без реального токена — проверяется логика, которую легко
сломать: фильтр пользователей, разбивка сообщений, безопасные имена файлов,
отправка файла без запущенного бота, регистрация инструмента и подготовка
видео-кружка через ffmpeg.

Запуск:  venv\\Scripts\\python.exe scripts\\test_telegram.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import Config  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.telegram_bot import (  # noqa: E402
    MAX_DOWNLOAD_MB,
    SEND_LIMIT_MB,
    TelegramBot,
    _attachment_question,
    _reaction_from_reply,
    convert_to_video_note,
    guess_kind,
    safe_filename,
    split_message,
    telegram_status,
)
from nemus.core.telegram_ux import (  # noqa: E402
    attachment_action_rows,
    attachment_prompt,
    examples_text,
    help_text,
    main_menu_rows,
    start_text,
)
from nemus.core.tools.registry import ToolRegistry  # noqa: E402
from nemus.core.tools.telegram import (  # noqa: E402
    _ALLOWED_REACTIONS,
    REACTION_EMOJIS,
    ReactTelegramTool,
    normalize_reaction,
    telegram_tools,
)


def _update(uid: int, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, username="owner"),
        effective_chat=SimpleNamespace(id=uid, type=chat_type),
    )


def test_config_defaults() -> None:
    cfg = Config()
    assert cfg.telegram.enabled is False, "бот не должен включаться сам"
    assert cfg.telegram.allowed_users == [], cfg.telegram.allowed_users
    assert cfg.telegram.allow_groups is False
    assert cfg.telegram.transcribe_voice is True
    assert cfg.telegram.hints_enabled is True, "подсказки должны быть включены по умолчанию"
    assert cfg.telegram.max_download_mb > 0
    print("ok  конфиг: бот выключен, список ID пуст, группы запрещены")


def test_message_split() -> None:
    assert split_message("") == []
    assert split_message("привет") == ["привет"]
    long = "\n\n".join(f"Абзац {i} " + "слово " * 40 for i in range(20))
    chunks = split_message(long, limit=1000)
    assert len(chunks) > 1, len(chunks)
    assert all(len(chunk) <= 1000 for chunk in chunks), [len(c) for c in chunks]
    assert "\n".join(chunks).replace("\n", " ") != ""
    assert sum(chunk.count("Абзац") for chunk in chunks) == 20
    print(f"ok  длинный ответ режется на {len(chunks)} сообщения по 1000 символов")


def test_safe_names() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    name = safe_filename("круг: видео?.mp4")
    assert name.endswith(".mp4") and not set(name) & set(':/\\?*"<>|'), name
    assert safe_filename("") == "file"
    assert safe_filename("...") == "file"
    print("ok  имена файлов очищаются от путей и недопустимых символов")


def test_kind_detection() -> None:
    assert guess_kind(Path("a.jpg")) == "photo"
    assert guess_kind(Path("a.MP4")) == "video"
    assert guess_kind(Path("a.ogg")) == "audio"
    assert guess_kind(Path("a.zip")) == "document"
    print("ok  тип отправки определяется по расширению")


def test_ffmpeg_video_note() -> None:
    ffmpeg = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, check=False
    )
    if ffmpeg.returncode != 0:
        print("skip видео-кружок: ffmpeg не установлен")
        return
    with tempfile.TemporaryDirectory(prefix="nemus_tg_") as tmp:
        source = Path(tmp) / "vertical.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=720x1280:rate=25:duration=2",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=True,
        )
        target = Path(tmp) / "note.mp4"
        convert_to_video_note(source, target)
        assert target.exists() and target.stat().st_size > 0
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == "512x512", probe.stdout
    print("ok  видео-кружок: вертикальный ролик обрезан до 512×512")


def test_authorization() -> None:
    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    assert bot.notify_chat_id == 42, "чат для напоминаний берётся из allowed_users"
    assert bot._authorized(_update(42)), "владелец должен проходить"
    assert not bot._authorized(_update(7)), "чужой пользователь не должен проходить"
    assert not bot._authorized(_update(42, chat_type="group")), (
        "группы по умолчанию запрещены"
    )
    cfg.telegram.allow_groups = True
    bot.cfg.allow_groups = True
    assert bot._authorized(_update(42, chat_type="group")), (
        "с разрешёнными группами владелец проходит"
    )
    bot._chat_id = None
    assert bot._chat_target() == 42, (
        "после ответа напоминание всё ещё должно знать, куда идти"
    )
    suffix = bot._system_suffix()
    assert "send_telegram" in suffix, suffix
    assert "react_telegram" in suffix, suffix
    assert "Немесис" in suffix and "Немо" in suffix and "Markdown" in suffix, suffix
    # уточнения в Telegram — брейншторм с вариантами, а не открытый вопрос
    assert "брейншторм" in suffix.lower(), suffix
    assert "1A, 2B" in suffix, suffix
    # и честность про озвучку: голосовых ответов в TG нет
    assert "Голосовых ответов" in suffix, suffix
    print("ok  доступ: только ID владельца; группы — по настройке")


def test_reminder_delivery() -> None:
    """Напоминание уходит в Telegram, даже когда активной задачи уже нет."""
    import asyncio
    import threading
    import time
    from datetime import datetime, timedelta

    from nemus.core.reminder import ReminderScheduler

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []
            self.reactions: list[tuple[int, int, str]] = []

        async def send_message(self, chat_id: int, text: str, **_: object) -> None:
            self.sent.append((chat_id, text))

        async def set_message_reaction(self, **kwargs: object) -> None:
            reaction = kwargs["reaction"][0]  # type: ignore[index]
            self.reactions.append(
                (kwargs["chat_id"], kwargs["message_id"], reaction.emoji)  # type: ignore[index]
            )

    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    reminder = ReminderScheduler()
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
        reminder=reminder,
    )
    bot = TelegramBot(cfg, core)
    fake = _FakeBot()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        bot._loop = loop
        bot._app = SimpleNamespace(bot=fake)
        bot._thread = threading.current_thread()
        bot._chat_id = None  # задача уже закончилась
        reminder.on_fire = lambda text: bot.send_text(f"⏰ {text}")
        reminder.add(datetime.now() + timedelta(seconds=0.1), "выпить воды")
        deadline = time.time() + 3
        while not fake.sent and time.time() < deadline:
            time.sleep(0.05)
        assert fake.sent, "напоминание не отправилось в Telegram"
        chat_id, text = fake.sent[0]
        assert chat_id == 42 and "выпить воды" in text, fake.sent

        # реакция ставится на сообщение текущей задачи, а не на чат вообще
        bot._chat_id, bot._message_id = 42, 11
        ok, message = bot.react_current("🔥")
        assert ok, message
        assert fake.reactions == [(42, 11, "🔥")], fake.reactions
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
    print("ok  напоминание доставляется в Telegram-чат после завершения задачи")


def test_send_text_chunks() -> None:
    """Длинный текст напоминаний режется, а не теряется на лимите Telegram."""
    import asyncio
    import threading

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str, str | None]] = []

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> None:
            mode = kwargs.get("parse_mode")
            self.sent.append((chat_id, text, mode if isinstance(mode, str) else None))

    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    fake = _FakeBot()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        bot._loop = loop
        bot._app = SimpleNamespace(bot=fake)
        bot._thread = threading.current_thread()
        bot._chat_id = None
        long_text = "**Отчёт.** " + "Строка результата. " * 900
        assert bot.send_text(long_text), "длинный текст должен уйти частями"
        assert len(fake.sent) >= 2, fake.sent
        assert all(len(text) <= 4096 for _, text, _ in fake.sent), [
            len(text) for _, text, _ in fake.sent
        ]
        assert all(mode == "HTML" for _, _, mode in fake.sent), fake.sent
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
    print("ok  длинный send_text режется на части Telegram")


def test_stt_import_without_numpy() -> None:
    """Импорт STT не тянет numpy: контейнер Telegram его не ставит."""
    code = (
        "import sys; sys.modules['numpy'] = None; "
        "import nemus.voice.stt; print('STT OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "STT OK" in result.stdout, result.stdout
    print("ok  STT импортируется без numpy (облачная расшифровка в контейнере)")


def test_cloud_stt_without_numpy() -> None:
    """Облачное распознавание не падает из-за отсутствия numpy в контейнере."""
    import threading
    from unittest.mock import patch

    import nemus.voice.stt as stt_module

    seen: list[dict[str, object]] = []

    class _FakeGemini:
        def __init__(self, **kwargs: object) -> None:
            seen.append(kwargs)

        def transcribe(self, path: Path) -> str:
            return "привет из облака"

    cfg = Config()
    cfg.voice.stt.engine = "gemini"
    cfg.llm.providers["gemini"] = SimpleNamespace(api_key="test", api_base="")
    bot = object.__new__(TelegramBot)
    bot.config = cfg
    bot._stt = None
    bot._stt_lock = threading.Lock()
    with (
        patch.object(stt_module, "GeminiTranscriber", _FakeGemini),
        patch.dict(sys.modules, {"numpy": None}),
    ):
        assert bot._transcribe(Path("voice.ogg")) == "привет из облака"
    assert seen, "GeminiTranscriber должен быть вызван"
    print("ok  облачное STT работает без numpy")


def test_start_timeout_stops_hung_thread() -> None:
    """Таймаут старта не оставляет зомби-поток, который поллит без замка."""
    import time

    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    # изолируем замок от рабочего data/telegram.lock
    tmp = tempfile.mkdtemp(prefix="nemus-tg-")
    cfg.memory.database = str(Path(tmp) / "nemus.db")
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)

    def _hang() -> None:
        # «зависший» старт: готовность не наступает, ждём стоп-сигнал
        while not bot._stopping.is_set():
            time.sleep(0.02)

    bot._thread_main = _hang  # type: ignore[method-assign]
    bot._ready = SimpleNamespace(  # type: ignore[assignment]
        clear=lambda: None, wait=lambda timeout=None: False
    )
    try:
        assert bot.start() is False, "старт должен честно вернуть False"
        assert bot._stopping.is_set(), (
            "таймаут обязан подать стоп-сигнал зависшему потоку"
        )
        assert not bot.running, "зомби-поток не должен считаться работающим"
    finally:
        bot._stopping.set()
        if bot._thread is not None:
            bot._thread.join(timeout=3)
        bot._release_lock()
    print("ok  таймаут старта гасит поток и снимает замок без зомби")


def test_stop_watchdog_kills_zombie_loop() -> None:
    """Сторож гасит цикл бота, если стоп пришёл уже после проверки в старте."""
    import asyncio
    import threading
    import time

    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    stopped = threading.Event()

    async def fake_shutdown() -> None:
        stopped.set()

    bot._shutdown = fake_shutdown  # type: ignore[method-assign]
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bot._loop = loop
        loop.create_task(bot._stop_watchdog())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        time.sleep(0.2)  # сторож крутится, стоп-сигнала нет
        assert thread.is_alive(), "цикл должен работать до стоп-сигнала"
        assert not stopped.is_set(), "без stop() гасить нечего"
        bot._stopping.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "сторож обязан остановить цикл"
        assert stopped.is_set(), "перед остановкой цикл должен погасить бота"
    finally:
        bot._stopping.set()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
    print("ok  сторож гасит зомби-цикл бота после таймаута старта")


def test_formatting() -> None:
    """Markdown → HTML Telegram: жирный, курсив, код, цитаты, ссылки, таблицы."""
    from nemus.core.telegram_format import html_chunks, strip_html, to_html

    text = "**жирный** и *курсив* и `x < y` и ~~старое~~ и ||тайна||"
    html_text = to_html(text)
    assert "<b>жирный</b>" in html_text, html_text
    assert "<i>курсив</i>" in html_text, html_text
    assert "<code>x &lt; y</code>" in html_text, html_text
    assert "<s>старое</s>" in html_text, html_text
    assert "<tg-spoiler>тайна</tg-spoiler>" in html_text, html_text

    quote = to_html("до\n> главный вывод\n> вторая строка\nпосле")
    assert "<blockquote>главный вывод\nвторая строка</blockquote>" in quote, quote

    link = to_html("[ссылка](https://example.com/?a=1&b=2)")
    assert (
        '<a href="https://example.com/?a=1&amp;b=2">ссылка</a>' in link
    ), link

    code = to_html("```python\nprint(1 < 2)\n```")
    assert code == (
        '<pre><code class="language-python">print(1 &lt; 2)</code></pre>'
    ), code

    inline = to_html("`**не жирный**`")
    assert "<b>" not in inline and "**" in inline, inline

    table = to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert table.startswith("<pre>|") and table.endswith("</pre>"), table

    dangerous = to_html("<script>alert(1)</script> & всё")
    assert "<script>" not in dangerous and "&lt;script&gt;" in dangerous, dangerous

    parts = html_chunks("Абзац. \n" * 300, limit=400)
    assert len(parts) > 1 and all(len(part) <= 4096 for part in parts), [
        len(part) for part in parts
    ]
    assert strip_html("<b>жирный</b>") == "жирный"
    print("ok  форматирование: Markdown → HTML, экранирование и разбивка")


def test_typing_and_reactions() -> None:
    """«Печатает…» держится, пока задача выполняется, и ставится реакция."""
    import asyncio

    from nemus.core.telegram_bot import TelegramBot, start_message

    class _FakeBot:
        def __init__(self) -> None:
            self.actions: list[tuple[int, str]] = []
            self.reactions: list[tuple[int, int, str]] = []
            self.sent: list[tuple[str, dict]] = []
            self.reject_html = False

        async def send_chat_action(self, chat_id: int, action: str) -> None:
            self.actions.append((chat_id, action))

        async def set_message_reaction(self, **kwargs) -> None:
            reaction = kwargs["reaction"][0]
            self.reactions.append(
                (kwargs["chat_id"], kwargs["message_id"], reaction.emoji)
            )

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            if self.reject_html and kwargs.get("parse_mode") == "HTML":
                raise RuntimeError("can't parse entities")
            self.sent.append((text, kwargs))

    cfg = Config()
    cfg.telegram.token = "123:test"
    fake = _FakeBot()
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    bot._app = SimpleNamespace(bot=fake)

    async def scenario() -> None:
        task = asyncio.create_task(bot._keep_typing(7))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot._react(7, 11, "👀")
        await start_message(fake, 7, "**готово**", reply_to=11)
        fake.reject_html = True
        await start_message(fake, 7, "**ещё раз**")

    asyncio.run(scenario())
    assert fake.actions and fake.actions[0] == (7, "typing"), fake.actions
    assert fake.reactions == [(7, 11, "👀")], fake.reactions
    html_text, kwargs = fake.sent[0]
    assert "<b>готово</b>" in html_text, html_text
    assert kwargs["parse_mode"] == "HTML", kwargs
    assert kwargs["reply_to_message_id"] == 11, kwargs
    # если Telegram не принял разметку — тот же текст ушёл обычным
    assert fake.sent[1][0] == "ещё раз", fake.sent[1][0]
    print("ok  «печатает…», реакции и HTML-ответ с запасным текстом")


def test_ux_texts() -> None:
    """Тексты первого экрана и примеров зависят от переключателя."""
    with_hints = start_text("Немесис", "Немо", True)
    without_hints = start_text("Немесис", "Немо", False)
    assert "Примеры" in with_hints and "сделай из этого видео кружок" in with_hints
    assert "Примеры" not in without_hints
    assert "Фишка" in help_text(True)
    assert "Фишка" not in help_text(False)
    assert "PDF" in examples_text()
    assert main_menu_rows(True)
    assert main_menu_rows(False) == []
    print("ok  тексты первого экрана и примеров зависят от подсказок")


def test_start_help_respect_hints() -> None:
    """При выключенных подсказках нет примеров и кнопок-подсказок."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append((text, kwargs))

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(message_id=11),
    )

    asyncio.run(bot._cmd_start(update, None))
    text, kwargs = fake.sent[-1]
    assert "Примеры" in text
    assert kwargs.get("reply_markup") is not None

    fake.sent.clear()
    cfg.telegram.hints_enabled = False
    asyncio.run(bot._cmd_start(update, None))
    text, kwargs = fake.sent[-1]
    assert "Примеры" not in text
    assert kwargs.get("reply_markup") is None

    fake.sent.clear()
    asyncio.run(bot._cmd_help(update, None))
    assert "Фишка" not in fake.sent[-1][0]
    print("ok  /start и /help уважают общий выключатель подсказок")


def test_ux_examples_callback() -> None:
    """Кнопка «Примеры задач» отправляет примеры, а не молчит."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append(text)

    class _Query:
        data = "ux:examples"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(chat_id=42)

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    query = _Query()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(update, None))
    assert fake.sent and "Примеры задач" in fake.sent[-1], fake.sent
    print("ok  кнопка «Примеры задач» присылает примеры")


def test_attachment_actions() -> None:
    """Для каждого типа вложения есть понятные кнопки и промпт."""
    photo = attachment_action_rows("photo")
    assert any("Описать" in label for row in photo for label, _ in row)
    assert any(data == "ux:attach:photo_describe" for row in photo for _, data in row)
    assert "кружок" in attachment_prompt("video_note").lower()  # type: ignore[union-attr]
    assert "отчёт" in attachment_prompt("document_report").lower()  # type: ignore[union-attr]
    assert attachment_prompt("unknown") is None
    print("ok  для вложений есть кнопки действий и промпты")


def test_pending_attachments_are_per_chat() -> None:
    """Вложение одного чата не забирает другой чат."""
    bot = object.__new__(TelegramBot)
    bot._pending_attachments = {}
    bot._remember_pending_attachment(1, "чат 1", ["img1"])
    bot._remember_pending_attachment(2, "чат 2", ["img2"])

    assert bot._peek_pending_attachment(1)[0] == "чат 1"
    assert bot._peek_pending_attachment(2)[0] == "чат 2"
    assert bot._take_pending_attachment(1)[0] == "чат 1"
    assert bot._peek_pending_attachment(1) == ("", [])
    assert bot._peek_pending_attachment(2)[0] == "чат 2"
    bot._clear_pending_attachments(2)
    assert bot._peek_pending_attachment(2) == ("", [])
    print("ok  вложения без подписи хранятся по чатам")


def test_attachment_callback_runs_task() -> None:
    """Кнопка под фото сразу запускает задачу с этим фото."""
    import asyncio

    class _Query:
        data = "ux:attach:photo_describe"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(chat_id=42, message_id=5)

        def __init__(self) -> None:
            self.answers: list[str] = []
            self.edits: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.edits.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot._pending_attachments = {}
    bot._remember_pending_attachment(
        42, "Пользователь прислал файл(ы):\n- photo.jpg", []
    )
    query = _Query()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    seen: list[dict] = []

    async def _run_task_for(**kwargs: object) -> None:
        seen.append(kwargs)

    bot._run_task_for = _run_task_for  # type: ignore[method-assign]
    asyncio.run(bot._on_callback(update, None))
    assert seen and "photo.jpg" in seen[0]["text"]
    assert "Опиши" in seen[0]["text"]
    assert bot._peek_pending_attachment(42) == ("", [])

    stale = _Query()
    asyncio.run(
        bot._on_callback(
            SimpleNamespace(
                callback_query=stale,
                effective_user=stale.from_user,
                effective_chat=SimpleNamespace(id=42, type="private"),
            ),
            None,
        )
    )
    assert len(seen) == 1, "старая кнопка не должна запускать вторую задачу"
    assert "уже не актуально" in stale.answers[-1]
    print("ok  кнопка под вложением сразу запускает задачу")


def test_attachment_without_caption() -> None:
    """Фото без подписи — вопрос, а не выдуманная задача."""
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        from PIL import Image

        image = Path(tmp) / "photo.jpg"
        Image.new("RGB", (16, 16), "red").save(image, "JPEG")
        assert "изображение" in _attachment_question([image])
        assert "чиби" not in _attachment_question([image]).lower()
        doc = Path(tmp) / "report.pdf"
        doc.write_bytes(b"pdf")
        assert "report.pdf" in _attachment_question([doc])
        assert "файлы" in _attachment_question([image, doc]).lower()

        cfg = Config()
        cfg.telegram.enabled = True
        cfg.telegram.token = "123:test"
        cfg.telegram.allowed_users = [42]
        core = SimpleNamespace(
            llm=None,
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            config=cfg,
        )
        bot = TelegramBot(cfg, core)

        class _FakeBot:
            def __init__(self) -> None:
                self.reactions: list[tuple[int, int, str]] = []

            async def send_chat_action(self, chat_id: int, action: str) -> None:
                pass

            async def set_message_reaction(self, **kwargs: object) -> None:
                reaction = kwargs["reaction"][0]  # type: ignore[index]
                self.reactions.append(
                    (kwargs["chat_id"], kwargs["message_id"], reaction.emoji)  # type: ignore[index]
                )

        class _Message:
            message_id = 1
            voice = None
            caption = ""
            document = None
            video = None
            video_note = None
            animation = None
            audio = None
            photo = [SimpleNamespace(file_size=10)]

            def __init__(self) -> None:
                self.replies: list[tuple[str, dict]] = []

            async def reply_text(self, text: str, **kwargs: object) -> None:
                self.replies.append((text, kwargs))

        message = _Message()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42, username="owner"),
            effective_chat=SimpleNamespace(id=42, type="private"),
            message=message,
        )

        async def _download(chat_id: int, attachment: object, default: str) -> Path:
            return image

        called: list[str] = []
        seen_images: list[list[str]] = []

        async def _run_task_for(**kwargs: object) -> None:
            called.append(str(kwargs["text"]))
            seen_images.append(kwargs.get("images") or [])  # type: ignore[arg-type]

        fake_bot = _FakeBot()
        bot._app = SimpleNamespace(bot=fake_bot)
        bot._download = _download  # type: ignore[method-assign]
        bot._run_task_for = _run_task_for  # type: ignore[method-assign]
        asyncio.run(bot._on_media(update, None))
        assert not called, "вложение без подписи не должно уходить агенту"
        assert message.replies
        question, reply_kwargs = message.replies[-1]
        assert question.startswith("Я получила изображение. Что с ним сделать?")
        assert reply_kwargs.get("reply_markup") is not None, reply_kwargs
        assert bot._peek_pending_attachment(42) != ("", [])
        assert not fake_bot.reactions, "👀 как статус больше не ставится"

        # следующая текстовая задача должна знать путь к фото и видеть его
        message.text = "опиши"  # type: ignore[attr-defined]
        asyncio.run(bot._on_text(update, None))
        assert called and image.name in called[-1], called
        assert "опиши" in called[-1], called[-1]
        assert seen_images and seen_images[-1], "фото должно уйти модели как изображение"
        assert seen_images[-1][0].startswith("data:image/"), seen_images[-1][0][:40]
        assert bot._peek_pending_attachment(42) == ("", [])
    print("ok  фото без подписи: бот спрашивает задачу и передаёт изображение")


def test_stt_failure_does_not_ask_attachment() -> None:
    """Сбой распознавания — это ошибка, а не «вложение без подписи»."""
    import asyncio

    cfg = Config()
    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)

    class _FakeBot:
        async def send_chat_action(self, chat_id: int, action: str) -> None:
            pass

    class _Notice:
        def __init__(self) -> None:
            self.edits: list[str] = []

        async def edit_text(self, text: str) -> None:
            self.edits.append(text)

    class _Message:
        message_id = 1
        voice = SimpleNamespace(file_size=10)
        caption = ""
        document = None
        video = None
        video_note = None
        animation = None
        audio = None
        photo = None

        def __init__(self) -> None:
            self.replies: list[str] = []
            self.notice = _Notice()

        async def reply_text(self, text: str, **_: object) -> _Notice:
            self.replies.append(text)
            return self.notice

    message = _Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=message,
    )

    async def _download(
        chat_id: int, attachment: object, default: str
    ) -> Path:
        return Path("voice.ogg")

    def _transcribe(path: Path) -> str:
        raise RuntimeError("нет ключа")

    called: list[str] = []

    async def _run_task(
        update: object, text: str, images: list[str] | None = None
    ) -> None:
        called.append(text)

    bot._app = SimpleNamespace(bot=_FakeBot())
    bot._download = _download  # type: ignore[method-assign]
    bot._transcribe = _transcribe  # type: ignore[method-assign]
    bot._run_task = _run_task  # type: ignore[method-assign]
    asyncio.run(bot._on_media(update, None))
    assert not called, "после сбоя STT задача агенту не уходит"
    assert message.notice.edits, "должно быть сообщение об ошибке распознавания"
    assert "Не удалось распознать" in message.notice.edits[-1]
    assert not any("Я получила аудио" in text for text in message.replies)
    assert bot._peek_pending_attachment(42) == ("", []), (
        "голосовое не должно стать вложением"
    )
    print("ok  сбой STT не превращается в вопрос про вложение")


def test_download_limit_is_twenty_mb() -> None:
    """Bot API отдаёт файлы только до 20 МБ, даже если в конфиге больше."""
    import asyncio

    assert MAX_DOWNLOAD_MB == 20

    class _Attachment:
        file_name = "video.mp4"

        def __init__(self, size: int) -> None:
            self.file_size = size
            self.downloaded = False

        async def get_file(self) -> object:
            class _File:
                async def download_to_drive(self, custom_path: object) -> None:
                    Path(custom_path).write_bytes(b"x")  # type: ignore[arg-type]

            self.downloaded = True
            return _File()

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.tools.workspace = tmp
        cfg.telegram.max_download_mb = 100
        bot = object.__new__(TelegramBot)
        bot.config = cfg
        bot.cfg = cfg.telegram

        big = _Attachment(30 * 1024 * 1024)
        try:
            asyncio.run(bot._download(42, big, "video.mp4"))
        except RuntimeError as exc:
            assert "20 МБ" in str(exc), str(exc)
        else:
            raise AssertionError("30 МБ должны упираться в лимит Bot API")
        assert not big.downloaded, "файл больше лимита не скачивается"

        small = _Attachment(19 * 1024 * 1024)
        path = asyncio.run(bot._download(42, small, "video.mp4"))
        assert path.exists() and path.read_bytes() == b"x"
    print("ok  скачивание ограничено 20 МБ Bot API, 19 МБ проходит")


def test_send_file_retries_and_falls_back() -> None:
    """Таймаут отправки видео: один повтор, затем документ."""
    import asyncio

    from telegram.error import TimedOut

    calls: list[str] = []

    class _RetryBot:
        async def send_video(self, chat_id: int, **kwargs: object) -> None:
            calls.append("video")
            if calls.count("video") == 1:
                raise TimedOut("timed out")

    bot = object.__new__(TelegramBot)
    bot._app = SimpleNamespace(bot=_RetryBot())
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "video.mp4"
        video.write_bytes(b"x")
        ok, text = asyncio.run(bot._send_file(42, video, "video", "клип"))
    assert ok, text
    assert calls == ["video", "video"], calls
    assert "Отправлено" in text

    calls.clear()

    class _FallbackBot:
        async def send_video(self, chat_id: int, **kwargs: object) -> None:
            calls.append("video")
            raise TimedOut("timed out")

        async def send_document(self, chat_id: int, **kwargs: object) -> None:
            calls.append("document")

    bot._app = SimpleNamespace(bot=_FallbackBot())
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "video.mp4"
        video.write_bytes(b"x")
        ok, text = asyncio.run(bot._send_file(42, video, "video", "клип"))
    assert ok, text
    assert calls == ["video", "video", "document"], calls
    assert "документом" in text
    print("ok  таймаут отправки: повтор, затем запасной документ")


def test_application_concurrent_updates() -> None:
    """Подтверждения не должны ждать конца долгой задачи в очереди."""
    cfg = Config()
    cfg.telegram.token = "123:test"
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    app = bot._build_application()
    assert app.update_processor.max_concurrent_updates == 4
    print("ok  Application обрабатывает до 4 апдейтов параллельно")


def test_inbox_lock_serializes_text_and_media() -> None:
    """Текст и медиа не должны перемешиваться при параллельных апдейтах."""
    import asyncio

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(text="привет"),
    )
    order: list[str] = []

    async def _handle_text(update: object) -> None:
        order.append("text-start")
        await asyncio.sleep(0.02)
        order.append("text-end")

    async def _handle_media(update: object) -> None:
        order.append("media-start")
        await asyncio.sleep(0.02)
        order.append("media-end")

    bot._handle_text = _handle_text  # type: ignore[method-assign]
    bot._handle_media = _handle_media  # type: ignore[method-assign]

    async def scenario() -> None:
        bot._inbox_lock = asyncio.Lock()
        await asyncio.gather(
            bot._on_text(update, None),
            bot._on_media(update, None),
        )

    asyncio.run(scenario())
    assert order in (
        ["text-start", "text-end", "media-start", "media-end"],
        ["media-start", "media-end", "text-start", "text-end"],
    ), order
    print("ok  текст и медиа обрабатываются по очереди")


def test_new_blocked_during_task() -> None:
    """/new не сбрасывает контекст, пока агент ещё работает."""
    import asyncio

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram

    class _Agent:
        def __init__(self) -> None:
            self.resets = 0

        def reset(self) -> None:
            self.resets += 1

    bot.agent = _Agent()
    replies: list[str] = []

    async def _reply(text: str, **_: object) -> None:
        replies.append(text)

    message = SimpleNamespace(reply_text=_reply)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=message,
    )

    async def scenario() -> None:
        bot._task_lock = asyncio.Lock()
        await bot._task_lock.acquire()
        await bot._cmd_new(update, None)

    asyncio.run(scenario())
    assert replies and "дождись" in replies[0]
    assert bot.agent.resets == 0
    print("ok  /new ждёт завершения текущей задачи")


def test_confirmation_bound_to_chat_user() -> None:
    """Кнопка подтверждения работает только в своём чате и у своего автора."""
    import asyncio

    cfg = Config()
    cfg.telegram.allowed_users = [7]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot._pending = {}

    class _Query:
        def __init__(self, data: str, user_id: int, chat_id: int) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)
            self.message = SimpleNamespace(chat_id=chat_id)
            self.answers: list[str] = []
            self.edits: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_text(self, text: str, **_: object) -> None:
            self.edits.append(text)

        async def edit_message_reply_markup(self, **_: object) -> None:
            pass

    def _update(query: _Query) -> SimpleNamespace:
        return SimpleNamespace(
            callback_query=query,
            effective_user=query.from_user,
            effective_chat=SimpleNamespace(id=query.message.chat_id, type="private"),
        )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        good: asyncio.Future = loop.create_future()
        good_key = "42:7:1"
        bot._pending[good_key] = (good, "вопрос")
        good_query = _Query(f"tg:{good_key}:y", 7, 42)
        await bot._on_callback(_update(good_query), None)
        assert good.result() is True
        assert good_query.answers[-1] == "Разрешено"

        bad: asyncio.Future = loop.create_future()
        bad_key = "42:8:2"
        bot._pending[bad_key] = (bad, "вопрос")
        bad_query = _Query(f"tg:{bad_key}:y", 7, 42)
        await bot._on_callback(_update(bad_query), None)
        assert not bad.done()
        assert "не для этого чата" in bad_query.answers[-1]

    asyncio.run(scenario())
    print("ok  подтверждение привязано к чату и пользователю")


def test_reaction_list_complete() -> None:
    """У инструмента реакций весь согласованный список эмодзи, без потерь."""
    expected = (
        "🤣 😁 🤩 😍 🥰 🙂 😐 🤔 🥱 😴 😢 😭 😱 🤬 😡 🤮 🤯 🤪 🥴 😇 "
        "🤓 🧐 🤨 🫡 🤗 😎 😘 😨 ❤️ 💔 ❤️‍🔥 💘 👍 👎 👌 🤝 👏 🙏 🖕 ✍ "
        "💅 🤷 🔥 🎉 💯 🏆 🍾 👀 🤡 💩 🗿 🕊 💊 🎃 🎄 🎅 ☃ 🍓 🍌 🌭 "
        "🌚 😈 👻 🦄 🐳 ⚡ 🆒 👨‍💻"
    ).split()
    assert list(REACTION_EMOJIS) == expected, REACTION_EMOJIS
    enum = ReactTelegramTool.parameters["properties"]["emoji"]["enum"]
    assert enum == expected, enum
    assert all(normalize_reaction(emoji) in _ALLOWED_REACTIONS for emoji in expected)
    print("ok  у react_telegram весь список из 68 эмодзи")


def test_reaction_choice() -> None:
    """Отдельный выбор реакции: JSON, полный список и отказ."""
    assert _reaction_from_reply('{"emoji": "🤣"}') == "🤣"
    assert _reaction_from_reply('```json\n{"emoji": "❤️"}\n```') == "❤"
    assert _reaction_from_reply('{"emoji": null}') == ""
    assert _reaction_from_reply('{"emoji": "✅"}') == ""

    class _FakeReactionLLM:
        def __init__(self) -> None:
            self.messages: list[object] = []

        def complete(self, messages: list[object], **_: object) -> object:
            self.messages = messages
            return SimpleNamespace(content='{"emoji": "😁"}')

    cfg = Config()
    cfg.llm.fast_model = "gemini/gemini-3.5-flash-lite"
    fake = _FakeReactionLLM()
    bot = object.__new__(TelegramBot)
    bot.config = cfg
    bot.core = SimpleNamespace(llm=fake)
    assert bot._choose_reaction("Слушай анекдот") == "😁"
    system = fake.messages[0].content  # type: ignore[attr-defined]
    assert all(emoji in system for emoji in REACTION_EMOJIS), "список обрезан"
    print("ok  ИИ выбирает реакцию отдельным запросом из полного списка")


def test_tool_without_bot() -> None:
    tool = telegram_tools()[0]
    result = tool.execute(path="data/nemus.log")
    assert not result.ok and "не запущен" in result.content.lower(), result.content
    react = ReactTelegramTool()
    result = react.execute(emoji="🔥")
    assert not result.ok and "не запущен" in result.content.lower(), result.content
    assert normalize_reaction("❤️") == "❤"
    assert "❤" in normalize_reaction("".join(REACTION_EMOJIS))
    assert not react.execute(emoji="X").ok
    assert SEND_LIMIT_MB >= 50
    print("ok  без бота send_telegram/react_telegram отвечают понятной ошибкой")


def test_factory_registration() -> None:
    from nemus.core.factory import _apply_telegram

    cfg = Config()
    registry = ToolRegistry()
    _apply_telegram(registry, cfg)
    assert "send_telegram" not in registry, "без токена инструмент не нужен"
    assert "react_telegram" not in registry

    cfg.telegram.enabled = True
    cfg.telegram.token = "123:test"
    cfg.telegram.allowed_users = [42]
    _apply_telegram(registry, cfg)
    assert "send_telegram" in registry, "с настроенным ботом инструмент должен быть"
    assert "react_telegram" in registry, "реакции доступны вместе с ботом"

    cfg.telegram.enabled = False
    _apply_telegram(registry, cfg)
    assert "send_telegram" not in registry, "выключили бота — инструмент убираем"
    assert "react_telegram" not in registry
    print("ok  инструмент появляется и исчезает вместе с настройкой бота")


def test_user_ids_parsing() -> None:
    from nemus.gui.dialogs import _parse_user_ids

    assert _parse_user_ids("123, 456; 789") == [123, 456, 789]
    assert _parse_user_ids(" 42  7 ") == [42, 7]
    assert _parse_user_ids("abc, -5, 0") == []
    assert _parse_user_ids("") == []
    print("ok  ID пользователей из поля настроек парсятся без мусора")


def test_gui_hints_toggle() -> None:
    """Галочка подсказок читается из конфига и пишется обратно при сохранении."""
    from PySide6.QtWidgets import QApplication

    import nemus.gui.dialogs as dialogs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    dlg = dialogs.SettingsDialog(None, core=None)
    old_secret, old_get = dialogs.set_secret, dialogs.get_config
    try:
        assert hasattr(dlg, "chk_tg_hints"), "нет галочки подсказок"
        dlg.config.telegram.hints_enabled = True
        dlg._load()
        assert dlg.chk_tg_hints.isChecked()

        dlg.config.telegram.hints_enabled = False
        dlg._load()
        assert not dlg.chk_tg_hints.isChecked()

        dlg.chk_tg_hints.setChecked(True)
        captured = []
        dialogs.set_secret = lambda *a, **k: None
        dialogs.get_config = lambda *a, **k: None
        dlg._write_yaml = lambda cfg: captured.append(cfg)
        dlg._apply_tg_autostart = lambda: True
        dlg.accept = lambda: None
        dlg._save()
        assert captured and captured[0].telegram.hints_enabled is True
    finally:
        dialogs.set_secret, dialogs.get_config = old_secret, old_get
        dlg.done(0)
    print("ok  галочка подсказок Telegram читается и сохраняется")


def test_status() -> None:
    status = telegram_status()
    assert status["running"] is False, status
    print("ok  статус без запущенного бота — «выключен»")


def main() -> int:
    test_config_defaults()
    test_message_split()
    test_safe_names()
    test_kind_detection()
    test_ffmpeg_video_note()
    test_authorization()
    test_formatting()
    test_reminder_delivery()
    test_send_text_chunks()
    test_stt_import_without_numpy()
    test_cloud_stt_without_numpy()
    test_start_timeout_stops_hung_thread()
    test_stop_watchdog_kills_zombie_loop()
    test_typing_and_reactions()
    test_ux_texts()
    test_start_help_respect_hints()
    test_ux_examples_callback()
    test_attachment_actions()
    test_pending_attachments_are_per_chat()
    test_attachment_callback_runs_task()
    test_attachment_without_caption()
    test_stt_failure_does_not_ask_attachment()
    test_download_limit_is_twenty_mb()
    test_send_file_retries_and_falls_back()
    test_application_concurrent_updates()
    test_inbox_lock_serializes_text_and_media()
    test_new_blocked_during_task()
    test_confirmation_bound_to_chat_user()
    test_reaction_list_complete()
    test_reaction_choice()
    test_tool_without_bot()
    test_factory_registration()
    test_user_ids_parsing()
    test_gui_hints_toggle()
    test_status()
    print("\nTELEGRAM TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
