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
from nemus.core.diary import DiaryBook  # noqa: E402
from nemus.core.download_events import (  # noqa: E402
    DownloadEvent,
    subscribe_download,
    unsubscribe_download,
)
from nemus.core.emotions import EmotionState, EmotionStore  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.proactive import (  # noqa: E402
    PROACTIVE_CURIOUS_ANGLES as _PROACTIVE_CURIOUS_ANGLES,
)
from nemus.core.proactive import (  # noqa: E402
    PROACTIVE_RITUAL_ANGLES as _PROACTIVE_RITUAL_ANGLES,
)
from nemus.core.proactive import (  # noqa: E402
    PROACTIVE_SYSTEM as _PROACTIVE_SYSTEM,
)
from nemus.core.proactive import (  # noqa: E402
    daypart as _daypart,
)
from nemus.core.proactive import (
    drop_repeated_greeting as _drop_repeated_greeting,
)
from nemus.core.proactive import (
    proactive_angle as _proactive_angle,
)
from nemus.core.telegram_bot import (  # noqa: E402
    MAX_DOWNLOAD_MB,
    SEND_LIMIT_MB,
    TelegramBot,
    _attachment_question,
    _Progress,
    _reaction_from_reply,
    convert_to_video_note,
    guess_kind,
    safe_filename,
    split_message,
    start_message,
    telegram_status,
)
from nemus.core.telegram_commands import (  # noqa: E402
    category,
    command_category_rows,
    command_category_text,
    command_detail_text,
    command_home_rows,
    command_home_text,
    find_command,
    search_commands,
)
from nemus.core.telegram_ux import (  # noqa: E402
    attachment_action_rows,
    attachment_prompt,
    error_rows,
    error_text,
    examples_text,
    help_text,
    main_menu_rows,
    skill_details_text,
    skill_not_found_text,
    skills_list_text,
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


def test_proactive_drops_repeated_greeting() -> None:
    history = "Пользователь: привет\nНемуся: Привет! Как прошёл день?"
    assert _drop_repeated_greeting("Привет! Я тут подумала…", history) == (
        "Я тут подумала…"
    )
    assert _drop_repeated_greeting(
        "Привет! Я тут подумала…", "Пользователь: привет"
    ) == "Привет! Я тут подумала…"
    assert _drop_repeated_greeting(
        "Привет! Я тут подумала…", "", recent_contact=True
    ) == "Я тут подумала…"
    print("ok  эмоции: повторное приветствие убирается из инициативы")


def test_proactive_angles_are_varied() -> None:
    states = [
        SimpleNamespace(hurt=0, loneliness=20, proactive_count=0, mood=10),
        SimpleNamespace(hurt=0, loneliness=80, proactive_count=1, mood=10),
        SimpleNamespace(hurt=80, loneliness=60, proactive_count=2, mood=-40),
    ]
    angles = {
        _proactive_angle(state, now=1_000_000 + day * 86400 + hour * 3600)
        for day in range(4)
        for hour in (9, 15, 21)
        for state in states
    }
    assert len(angles) >= 6, angles
    assert "скучаю" not in " ".join(angles)
    assert "не извиняйся, не бери вину на себя без реальной причины" in (
        _PROACTIVE_SYSTEM
    )
    assert any("замет" in angle or "дневник" in angle for angle in _PROACTIVE_CURIOUS_ANGLES)
    assert set(_PROACTIVE_RITUAL_ANGLES) == {"утро", "день", "вечер", "ночь"}
    assert _daypart(1_000_000) in {"ночь", "утро", "день", "вечер"}
    print("ok  эмоции: для скуки выбираются разные творческие заходы")


def test_config_defaults() -> None:
    cfg = Config()
    assert cfg.telegram.enabled is False, "бот не должен включаться сам"
    assert cfg.telegram.allowed_users == [], cfg.telegram.allowed_users
    assert cfg.telegram.allow_groups is False
    assert cfg.telegram.transcribe_voice is True
    assert cfg.telegram.hints_enabled is True, "подсказки должны быть включены по умолчанию"
    assert cfg.telegram.advanced_ui is True
    assert cfg.telegram.max_download_mb > 0
    print("ok  конфиг: бот выключен, список ID пуст, группы запрещены")


def test_message_split() -> None:
    assert split_message("") == []
    assert split_message("привет") == ["привет"]
    long = "\n\n".join(f"Абзац {i} " + "слово " * 40 for i in range(20))
    chunks = split_message(long, limit=1000)
    assert len(chunks) > 1, len(chunks)
    from nemus.core.telegram_format import utf16_len

    assert all(utf16_len(chunk) <= 1000 for chunk in chunks), [len(c) for c in chunks]
    emoji = "😀" * 900
    emoji_chunks = split_message(emoji, limit=1000)
    assert all(utf16_len(chunk) <= 1000 for chunk in emoji_chunks), (
        [utf16_len(chunk) for chunk in emoji_chunks],
        "эмодзи считаются как UTF-16, а не как один символ",
    )
    assert "\n".join(chunks).replace("\n", " ") != ""
    assert sum(chunk.count("Абзац") for chunk in chunks) == 20
    print(f"ok  длинный ответ режется на {len(chunks)} сообщения по 1000 символов")


def test_download_cleanup() -> None:
    """Старые скачанные файлы удаляются, пользовательские — нет."""
    import os
    import time

    bot = object.__new__(TelegramBot)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        old = folder / "100_old.jpg"
        recent = folder / "200_new.jpg"
        user_file = folder / "notes.txt"
        for path in (old, recent, user_file):
            path.write_bytes(b"x")
        past = time.time() - 8 * 86400
        os.utime(old, (past, past))
        os.utime(user_file, (past, past))
        bot._prune_downloads(folder)
        assert not old.exists(), "старый скачанный файл должен удаляться"
        assert recent.exists(), "свежий файл должен остаться"
        assert user_file.exists(), "чужие файлы в папке чата не трогаем"
    print("ok  старые скачанные файлы удаляются, пользовательские — нет")


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
    group = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=999, type="group"),
    )
    assert bot._authorized(group)
    assert bot.notify_chat_id == 42, "группа не должна перехватывать напоминания"
    bot.username = "nemus_bot"
    group_message = SimpleNamespace(
        effective_chat=SimpleNamespace(id=999, type="group"),
        effective_message=SimpleNamespace(text="просто болтаем", caption=None),
    )
    assert not bot._addressed_to_bot(group_message), "группа без упоминания — молчим"
    group_message.effective_message.text = "@nemus_bot помоги"
    assert bot._addressed_to_bot(group_message), "упоминание включает бота"
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
    assert "A), B), C)" in suffix and "D) Свой вариант" in suffix, suffix
    assert "❓ Q1" in suffix, suffix
    # и честность про озвучку: голосовых ответов в TG нет
    assert "Голосовых ответов" in suffix, suffix
    print("ok  доступ: только ID владельца; группы — по настройке")


def test_stop_cancels_running_task() -> None:
    """stop() не только глушит polling, но и просит агентский поток встать."""
    import threading

    bot = object.__new__(TelegramBot)
    bot._stop_event = threading.Event()
    bot._stopping = threading.Event()
    bot._loop = None
    bot._app = None
    bot._thread = None
    bot._agent_thread = None
    bot._lock_handle = None
    bot.stop()
    assert bot._stop_event.is_set(), "текущая задача должна получить stop-сигнал"
    assert bot._stopping.is_set()
    print("ok  stop останавливает не только polling, но и текущую задачу")


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


def test_first_hint_once_and_mute() -> None:
    """Первая подсказка показывается один раз; «Больше не показывать» молчит."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append(text)

    class _Memory:
        def __init__(self) -> None:
            self.meta: dict[str, str] = {}

        def get_meta(self, key: str, default: str | None = None) -> str | None:
            return self.meta.get(key, default)

        def set_meta(self, key: str, value: str) -> None:
            self.meta[key] = value

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.memory = _Memory()
    bot._app = SimpleNamespace(bot=_FakeBot())
    bot._hints_shown = set()
    bot._hints_muted = set()

    asyncio.run(bot._maybe_show_first_hint(7))
    asyncio.run(bot._maybe_show_first_hint(7))
    assert len(bot._app.bot.sent) == 1, bot._app.bot.sent
    assert bot.memory.meta["telegram_hint_shown:7"] == "1"

    # Мьют проверяем на свежем чате: иначе сработает флаг «уже показано»,
    # и тест пройдёт, даже если мьют вообще не учитывается.
    bot._set_hint_muted(8, True)
    asyncio.run(bot._maybe_show_first_hint(8))
    assert len(bot._app.bot.sent) == 1
    assert "telegram_hint_shown:8" not in bot.memory.meta
    print("ok  первая подсказка показывается один раз и уважает мьют")


def test_error_text_and_progress_keyboard() -> None:
    """Ошибка короткая, без стектрейса, с кнопками восстановления."""
    import asyncio

    text = error_text(
        'Traceback (most recent call last):\nFile "x.py", line 1\n'
        "ValueError: плохой файл"
    )
    assert "плохой файл" in text and "Traceback" not in text

    qualified = error_text("telegram.error.BadRequest: Chat not found")
    assert "telegram.error" not in qualified and "BadRequest" not in qualified

    file_error = error_text(
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'C:\\Users\\User\\secret\\report.pdf'"
    )
    assert "Файл не найден" in file_error
    assert "C:\\" not in file_error and "secret" not in file_error

    rows = error_rows(True)
    assert any(data == "ux:retry" for row in rows for _, data in row)
    assert not any(data == "ux:retry" for row in error_rows(False) for _, data in row)

    class _FakeBot:
        def __init__(self) -> None:
            self.edits: list[tuple[str, dict]] = []
            self.sent: list[tuple[str, dict]] = []

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> object:
            self.sent.append((text, kwargs))
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.edits.append((text, kwargs))

    fake = _FakeBot()
    progress = _Progress(fake, 7)
    marker = object()
    asyncio.run(progress.start("⏳ Думаю…"))
    asyncio.run(progress.fail("Не получилось", reply_markup=marker))
    assert fake.edits and fake.edits[-1][1]["reply_markup"] is marker
    print("ok  ошибка короткая, с кнопками восстановления")


def test_progress_download_line() -> None:
    """Строка прогресса модели обновляет статус, а не копит заметки."""
    import asyncio

    class _Bot:
        def __init__(self) -> None:
            self.edits: list[str] = []

        async def send_message(self, chat_id: int, text: str, **_: object):
            return SimpleNamespace(message_id=3)

        async def edit_message_text(
            self, text: str, chat_id: int, message_id: int, **_: object
        ) -> None:
            self.edits.append(text)

    bot = _Bot()
    progress = _Progress(bot, 7)

    async def scenario() -> None:
        await progress.start("⏳ Думаю…")
        progress._last = 0
        await progress.download("⬇️ model.gguf: 25%")
        progress._last = 0
        await progress.download("⬇️ model.gguf: 50%")
        await progress.close()

    asyncio.run(scenario())
    assert bot.edits and "50%" in bot.edits[-1], bot.edits
    assert bot.edits[-1].count("model.gguf") == 1, bot.edits[-1]
    print("ok  Telegram: прогресс модели заменяет строку статуса")


def test_retry_and_new_callbacks() -> None:
    """Повтор берёт последнюю задачу, /new не сбрасывает работающий контекст."""
    import asyncio

    class _Query:
        def __init__(self, data: str) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=42)
            self.message = SimpleNamespace(chat_id=42, message_id=5)
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot._last_tasks = {42: ("найди отчёт", [], 11, 42)}
    bot._task_lock = None
    seen: list[dict] = []

    async def _run_task_for(**kwargs: object) -> None:
        seen.append(kwargs)

    bot._run_task_for = _run_task_for  # type: ignore[method-assign]

    query = _Query("ux:retry")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(update, None))
    assert seen and seen[0]["text"] == "найди отчёт"

    resets: list[int] = []
    bot._new_session_for_chat = lambda chat_id: resets.append(chat_id)  # type: ignore[method-assign]
    bot._task_lock = asyncio.Lock()
    asyncio.run(bot._task_lock.acquire())
    blocked = _Query("ux:new")
    blocked_update = SimpleNamespace(
        callback_query=blocked,
        effective_user=blocked.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(blocked_update, None))
    assert not resets, "нельзя сбрасывать контекст во время задачи"
    assert "дождись" in blocked.answers[-1]
    print("ok  повтор и /new не ломают работающий контекст")


def test_status_context_lines() -> None:
    """Статус показывает реплики, выжимку и состояние подсказок."""
    bot = object.__new__(TelegramBot)
    bot.cfg = Config().telegram
    bot.memory = None
    bot.agent = SimpleNamespace(
        history=[
            SimpleNamespace(role="system"),
            SimpleNamespace(role="user"),
            SimpleNamespace(role="assistant"),
        ],
        _summary="краткая выжимка",
    )
    bot.core = SimpleNamespace(llm=SimpleNamespace(model="m", provider="p"))
    bot.username = "bot"
    bot.started_at = 0.0
    bot._hints_shown, bot._hints_muted = set(), set()
    text = bot._status_text(42)
    assert "диалог: 1 реплик" in text
    assert "сжатая выжимка" in text
    assert "подсказки: включены" in text
    print("ok  статус показывает реплики, выжимку и подсказки")


def test_ux_texts() -> None:
    """Тексты первого экрана и примеров зависят от переключателя."""
    with_hints = start_text("Немесис", "Немо", True)
    without_hints = start_text("Немесис", "Немо", False)
    assert "Примеры" in with_hints and "сделай из этого видео кружок" in with_hints
    assert "Примеры" not in without_hints
    assert "Фишка" in help_text(True)
    assert "Фишка" not in help_text(False)
    assert "/skills" in with_hints
    assert "/skills" in without_hints
    assert "/skill" in with_hints
    assert "/skill" in without_hints
    assert "PDF" in examples_text()
    menu = main_menu_rows(True)
    assert {data for row in menu for _, data in row} == {
        "ux:examples",
        "ux:help",
        "ux:h",
        "ux:skills",
        "ux:new",
        "ux:status",
        "ux:cleanup",
    }
    assert main_menu_rows(False) == []
    print("ok  тексты первого экрана и примеров зависят от подсказок")


def test_diary_command_is_private_file() -> None:
    """`/diary` отдаёт .md только при высоком доверии; иначе отказ и обида."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.documents: list[tuple[int, str, dict]] = []
            self.messages: list[str] = []

        async def send_document(
            self, chat_id: int, document: object, **kwargs: object
        ) -> None:
            caption = kwargs.get("caption")
            self.documents.append(
                (chat_id, caption if isinstance(caption, str) else "", kwargs)
            )

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> SimpleNamespace:
            self.messages.append(text)
            return SimpleNamespace(message_id=1)

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    path = Path(tempfile.mkdtemp(prefix="nemus_tg_diary_")) / "diary.md"
    cfg.character.diary_file = str(path)
    cfg.character.state_file = str(path.with_name("emotions.json"))
    book = DiaryBook(path)
    book.add("Это личная запись.")
    store = EmotionStore(cfg.character.state_file)
    fake = _FakeBot()
    bot = object.__new__(TelegramBot)
    bot.config = cfg
    bot.cfg = cfg.telegram
    bot.core = SimpleNamespace(diary=book)
    bot._emotions = SimpleNamespace(store=store, state=store.snapshot)
    bot._app = SimpleNamespace(bot=fake)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(message_id=11, text="/diary"),
    )

    store.state = EmotionState(
        hurt=0, trust=85, attachment=85, warmth=75, mood=20, energy=70
    )
    store._save_locked()
    asyncio.run(bot._cmd_diary(update, None))
    assert fake.documents and fake.documents[0][0] == 42, fake.documents
    assert "дневник" in fake.documents[0][1].lower(), fake.documents

    # Если файла нет, она замечает это, заводит новый и присылает его.
    path.unlink()
    fake.documents.clear()
    asyncio.run(bot._cmd_diary(update, None))
    assert path.exists(), "отсутствующий дневник должен создаться"
    assert fake.documents and "завела" in fake.documents[0][1].lower()

    fake.documents.clear()
    store.state = EmotionState(
        hurt=0, trust=20, attachment=20, warmth=40, mood=0, energy=60
    )
    store._save_locked()
    asyncio.run(bot._cmd_diary(update, None))
    assert not fake.documents, "при низком доверии файл не отправляется"
    assert "Нет" in fake.messages[-1], fake.messages
    assert store.snapshot().hurt >= 16, store.snapshot()
    print("ok  /diary: файл только при доверии, иначе отказ и обида")


def test_command_catalog() -> None:
    """Команды объясняют себя: категории, синтаксис, примеры и поиск."""
    assert find_command("/read") is not None
    assert find_command("/diary") is not None
    assert "<url>" in find_command("read").syntax  # type: ignore[union-attr]
    assert find_command("commands").name == "help"  # type: ignore[union-attr]
    assert category("jobs") is not None
    assert "<id>" in command_category_text("jobs")
    assert "http.server" in command_detail_text("run")
    read_help = command_detail_text("read")
    assert "**Аргументы:**" in read_help
    assert "`<...>` — обязательно" in read_help
    assert "полный адрес страницы" in read_help
    assert "[вопрос]" in read_help
    assert "не нужны" in command_detail_text("pc")
    assert any(item.name == "read" for item in search_commands("сайт"))
    rows = command_home_rows()
    callbacks = {data for row in rows for _, data in row}
    assert {"ux:hc:main", "ux:hc:pc", "ux:hc:jobs", "ux:hc:auto"} <= callbacks
    assert "ux:cleanup" in callbacks
    for row in command_category_rows("jobs"):
        for _, data in row:
            assert len(data.encode("utf-8")) <= 64
    assert "категор" in command_home_text().lower()
    print("ok  каталог команд: категории, синтаксис, примеры и поиск")


def test_help_callback_edits_same_message() -> None:
    """Навигация по /help меняет одно сообщение, а не шлёт новые."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append(text)

    class _Query:
        data = "ux:hc:jobs"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(
            chat_id=42,
            message_id=5,
            chat=SimpleNamespace(id=42, type="private"),
        )

        def __init__(self) -> None:
            self.answers: list[str] = []
            self.edits: list[tuple[str, dict[str, object]]] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.edits.append((text, kwargs))

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.config = cfg
    bot._hints_shown = set()
    bot._hints_muted = set()
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    query = _Query()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(update, None))
    assert not fake.sent, "навигация не должна присылать новое сообщение"
    assert len(query.edits) == 1, query.edits
    assert "Фоновые задачи" in query.edits[0][0]
    assert query.edits[0][1].get("reply_markup") is not None
    assert query.answers == [""], query.answers
    print("ok  /help листается редактированием одного сообщения")


def test_skills_text() -> None:
    """Список скиллов группируется по плагинам, MCP идёт отдельно."""
    entries = [
        ("brainstorming", "superpowers", "skill"),
        ("hyperframes", "hyperframes", "skill"),
        ("pdf-reader", "", "skill"),
        ("superpowers:review", "superpowers", "command"),
    ]
    text = skills_list_text(entries, ["git", "time"])
    assert "**Установлено скиллов: 4**" in text
    assert text.index("**hyperframes**") < text.index("**superpowers**")
    assert text.index("**superpowers**") < text.index("**Без плагина**")
    assert "/brainstorming, /superpowers:review" in text
    assert "/pdf-reader" in text
    assert "git, time" in text
    assert "Подробнее: /skills <имя>" in text

    empty = skills_list_text([], [])
    assert "Скиллы не установлены" in empty
    assert "не подключены" in empty
    assert "Подробнее" not in empty

    many = skills_list_text(
        [(f"skill-{i:03d}-" + "x" * 40, "", "skill") for i in range(120)],
        [],
    )
    assert len(split_message(many)) > 1, "длинный список должен резаться"


def test_skill_details_text() -> None:
    """Справка показывает имя, плагин, тип, описание и команду запуска."""
    text = skill_details_text("pdf-reader", "Read PDFs", "", "skill")
    assert "**/pdf-reader**" in text
    assert "Плагин: нет" in text
    assert "Тип: скилл" in text
    assert "Read PDFs" in text
    assert "Как запустить: `/pdf-reader`" in text

    command = skill_details_text(
        "superpowers:review", "Review code", "superpowers", "command"
    )
    assert "Плагин: superpowers" in command
    assert "Тип: команда плагина" in command
    assert "`/superpowers:review`" in command

    assert "Без описания." in skill_details_text("x", "", "", "skill")
    assert "/skills" in skill_not_found_text()


def test_skills_command_and_callback() -> None:
    """Команда /skills, справка, callback и меню видят установленные скиллы."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []
            self.commands: list[object] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append((text, kwargs))

        async def set_my_commands(self, commands: list[object]) -> None:
            self.commands = commands

    class _Query:
        data = "ux:skills"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(chat_id=42, message_id=5)

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    cfg.mcp.enabled = False
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.config = cfg
    bot.memory = None
    bot._hints_shown = set()
    bot._hints_muted = set()
    bot._last_tasks = {}
    bot._skill_names = set()
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)

    skill = SimpleNamespace(
        name="pdf-reader",
        description="Read PDFs",
        path=Path("pdf-reader"),
        kind="skill",
    )
    command = SimpleNamespace(
        name="superpowers:review",
        description="Review code",
        path=Path("superpowers/commands/review.md"),
        kind="command",
    )
    bot._skills = lambda: [skill, command]  # type: ignore[method-assign]
    bot._skill_plugin_name = lambda item: (  # type: ignore[method-assign]
        "superpowers" if item.kind == "command" else ""
    )
    bot._mcp_names = lambda: ["git"]  # type: ignore[method-assign]

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(message_id=11, text="привет"),
    )

    async def _reply(text: str, **kwargs: object) -> None:
        fake.sent.append((text, kwargs))

    update.message.reply_text = _reply  # type: ignore[attr-defined]

    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=[])))
    text, kwargs = fake.sent[-1]
    assert "Установлено скиллов: 2" in text
    assert "/pdf-reader" in text and "/superpowers:review" in text
    assert "git" in text
    assert kwargs.get("reply_markup") is not None

    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=["superpowers:review"])))
    text, _ = fake.sent[-1]
    assert "Тип: команда плагина" in text
    assert "Review code" in text
    assert "/superpowers:review" in text

    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=["pdf-reader"])))
    text, _ = fake.sent[-1]
    assert "Тип: скилл" in text
    assert "Read PDFs" in text

    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=["missing"])))
    assert "Не нашёл такой скилл" in fake.sent[-1][0]

    bot._hints_muted = {42}
    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=[])))
    assert fake.sent
    assert fake.sent[-1][1].get("reply_markup") is None

    cfg.telegram.hints_enabled = False
    bot._hints_muted = set()
    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=[])))
    assert fake.sent
    assert fake.sent[-1][1].get("reply_markup") is None

    fake.sent.clear()
    query = _Query()
    asyncio.run(
        bot._on_callback(
            SimpleNamespace(
                callback_query=query,
                effective_user=query.from_user,
                effective_chat=SimpleNamespace(id=42, type="private"),
            ),
            None,
        )
    )
    assert fake.sent and "/pdf-reader" in fake.sent[-1][0]

    fake.sent.clear()
    fake.commands.clear()
    asyncio.run(bot._cmd_unknown(update, None))
    assert "/skills" in fake.sent[-1][0]
    assert " /skill " not in fake.sent[-1][0]

    asyncio.run(bot._set_commands())
    commands = {getattr(item, "command", "") for item in fake.commands}
    assert "skills" in commands, commands
    assert "skill" not in commands, commands
    print("ok  /skills, справка, callback и меню показывают скиллы без дубля")


def test_skill_command_not_duplicated() -> None:
    """Короткого псевдонима /skill больше нет — только /skills."""
    class _App:
        def __init__(self) -> None:
            self.handlers: list[object] = []

        def add_handler(self, handler: object, **kwargs: object) -> None:
            self.handlers.append(handler)

    bot = object.__new__(TelegramBot)
    app = _App()
    bot._app = app
    bot._register_handlers()
    commands = {
        command
        for handler in app.handlers
        for command in getattr(handler, "commands", ())
    }
    assert "skills" in commands, commands
    assert "skill" not in commands, commands
    print("ok  /skill не дублирует /skills")


def test_natural_language_skills_question() -> None:
    """Вопрос о скиллах отвечает списком, а не общим быстрым ответом."""
    import asyncio

    from nemus.core.language import looks_like_skills_question

    assert looks_like_skills_question("Какие у тебя есть скиллы?")
    assert looks_like_skills_question("What skills do you have?")
    assert not looks_like_skills_question("Загрузи скилл pdf-reader")

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append((text, kwargs))

    cfg = Config()
    cfg.telegram.hints_enabled = False
    cfg.mcp.enabled = False
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.config = cfg
    bot.memory = None
    bot._hints_shown = set()
    bot._hints_muted = set()
    bot._pending_attachments = {}
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    bot._skills = lambda: [  # type: ignore[method-assign]
        SimpleNamespace(
            name="pdf-reader",
            description="Read PDFs",
            path=Path("pdf-reader"),
            kind="skill",
        )
    ]
    bot._skill_plugin_name = lambda item: ""  # type: ignore[method-assign]
    bot._mcp_names = lambda: []  # type: ignore[method-assign]
    called: list[bool] = []

    async def _run_task(*args: object, **kwargs: object) -> None:
        called.append(True)

    bot._run_task = _run_task  # type: ignore[method-assign]
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=42),
        message=SimpleNamespace(text="Какие у тебя есть скиллы?", message_id=11),
    )
    asyncio.run(bot._handle_text(update))
    assert not called, "вопрос о скиллах не должен уходить в обычный цикл"
    assert fake.sent and "Установлено скиллов: 1" in fake.sent[-1][0]
    assert "/pdf-reader" in fake.sent[-1][0]
    print("ok  естественный вопрос о скиллах присылает список")


def test_skills_helpers_are_safe() -> None:
    """Нет папки скиллов — пустой список; MCP не трогается по сети."""
    from unittest.mock import patch

    cfg = Config()
    bot = object.__new__(TelegramBot)
    bot.config = cfg

    with patch("nemus.core.skills.load_skills", side_effect=OSError("denied")):
        assert bot._skills() == []

    cfg.mcp.enabled = False
    assert bot._mcp_names() == []
    cfg.mcp.enabled = True
    cfg.mcp.servers = [
        SimpleNamespace(name="git", enabled=True),
        SimpleNamespace(name="off", enabled=False),
    ]
    assert bot._mcp_names() == ["git"]


def test_skills_use_real_plugin_and_mcp_config() -> None:
    """Реальная папка плагина группируется, MCP берёт имена из конфига."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plugin = root / "acme"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "Acme Tools"}', encoding="utf-8"
        )
        (plugin / "commands").mkdir()
        (plugin / "commands" / "x.md").write_text(
            "---\ndescription: Do X\n---\nBody", encoding="utf-8"
        )

        cfg = Config()
        cfg.skills.dir = str(root)
        cfg.mcp.enabled = True
        cfg.mcp.servers = [
            SimpleNamespace(name="", command="node", enabled=True),
            SimpleNamespace(name="git", command="npx", enabled=True),
            SimpleNamespace(name="off", command="python", enabled=False),
        ]
        bot = object.__new__(TelegramBot)
        bot.config = cfg

        skills = bot._skills()
        command = next(skill for skill in skills if skill.name == "acme-tools:x")
        assert bot._skill_plugin_name(command) == "Acme Tools"
        found = bot._find_skill("Acme-Tools:X")
        assert found is not None and found.name == "acme-tools:x"

        entries = [
            (skill.name, bot._skill_plugin_name(skill), skill.kind)
            for skill in skills
        ]
        text = skills_list_text(entries, bot._mcp_names())
        assert "**Acme Tools**" in text
        assert "/acme-tools:x" in text
        assert "node, git" in text
        assert ", python" not in text

        cfg.mcp.enabled = False
        assert bot._mcp_names() == []


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


def test_muted_help_examples_and_attachment_buttons() -> None:
    """Мьют чата не обходится через /help, старые кнопки и вложения."""
    import asyncio
    import time

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append((text, kwargs))

    class _Query:
        def __init__(self, data: str, message_id: int = 5) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=42)
            self.message = SimpleNamespace(chat_id=42, message_id=message_id)
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.memory = None
    bot._hints_shown = {42}
    bot._hints_muted = {42}
    bot._pending_attachments = {}
    bot._last_tasks = {}
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(message_id=11),
    )
    asyncio.run(bot._cmd_help(update, None))
    text, kwargs = fake.sent[-1]
    assert "Фишка" not in text
    assert kwargs.get("reply_markup") is None

    fake.sent.clear()
    examples = _Query("ux:examples")
    asyncio.run(
        bot._on_callback(
            SimpleNamespace(
                callback_query=examples,
                effective_user=examples.from_user,
                effective_chat=SimpleNamespace(id=42, type="private"),
            ),
            None,
        )
    )
    assert not fake.sent
    assert "этом чате" in examples.answers[-1]

    bot._remember_pending_attachment(
        42,
        "Пользователь прислал файл(ы):\n- photo.jpg",
        [],
        at=time.time(),
        message_id=5,
        user_id=42,
    )
    stale_action = _Query("ux:attach:photo_describe")
    seen: list[dict] = []

    async def _run_task_for(**kwargs: object) -> None:
        seen.append(kwargs)

    bot._run_task_for = _run_task_for  # type: ignore[method-assign]
    asyncio.run(
        bot._on_callback(
            SimpleNamespace(
                callback_query=stale_action,
                effective_user=stale_action.from_user,
                effective_chat=SimpleNamespace(id=42, type="private"),
            ),
            None,
        )
    )
    assert not seen, "замьюченный чат не должен запускать действие вложения"
    assert "этом чате" in stale_action.answers[-1]
    print("ok  мьют чата закрывает /help, примеры и старые кнопки вложений")


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


def test_pending_attachments_are_per_user() -> None:
    """В одном чате вложения разных людей не смешиваются."""
    cfg = Config()
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    bot._remember_pending_attachment(1, "файл A", ["a.jpg"], user_id=7)
    bot._remember_pending_attachment(1, "файл B", ["b.jpg"], user_id=8)
    assert bot._peek_pending_attachment(1, 7)[0] == "файл A"
    assert bot._peek_pending_attachment(1, 8)[0] == "файл B"
    assert bot._take_pending_attachment(1, 7)[0] == "файл A"
    assert bot._peek_pending_attachment(1, 8)[0] == "файл B"
    assert bot._take_pending_attachment(1, 7) == ("", [])
    print("ok  вложения без подписи изолированы по чату и автору")


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
    bot.memory = None
    bot._pending_attachments = {}
    bot._hints_shown = set()
    bot._hints_muted = set()
    bot._remember_pending_attachment(
        42,
        "Пользователь прислал файл(ы):\n- photo.jpg",
        [],
        message_id=5,
        user_id=42,
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


def test_stale_attachment_button_does_not_take_newer_file() -> None:
    """Кнопка под старым фото не должна забрать новое вложение чата."""
    import asyncio
    import time

    class _Query:
        def __init__(self, message_id: int, user_id: int = 42) -> None:
            self.data = "ux:attach:photo_describe"
            self.from_user = SimpleNamespace(id=user_id)
            self.message = SimpleNamespace(chat_id=42, message_id=message_id)
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42, 7]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.memory = None
    bot._pending_attachments = {}
    bot._hints_shown = set()
    bot._hints_muted = set()
    bot._last_tasks = {}
    bot._remember_pending_attachment(
        42,
        "Пользователь прислал файл(ы):\n- old.jpg",
        [],
        at=time.time(),
        message_id=101,
        user_id=42,
    )
    bot._remember_pending_attachment(
        42,
        "Пользователь прислал файл(ы):\n- new.jpg",
        [],
        at=time.time(),
        message_id=102,
        user_id=42,
    )
    seen: list[dict] = []

    async def _run_task_for(**kwargs: object) -> None:
        seen.append(kwargs)

    bot._run_task_for = _run_task_for  # type: ignore[method-assign]

    def _run(query: _Query) -> None:
        asyncio.run(
            bot._on_callback(
                SimpleNamespace(
                    callback_query=query,
                    effective_user=query.from_user,
                    effective_chat=SimpleNamespace(id=42, type="private"),
                ),
                None,
            )
        )

    old = _Query(101)
    _run(old)
    assert not seen, "старая кнопка не должна брать новое фото"
    assert "уже не актуально" in old.answers[-1]
    assert "new.jpg" in bot._peek_pending_attachment(42)[0]

    foreign = _Query(102, user_id=7)
    _run(foreign)
    assert not seen, "чужой пользователь не должен забрать вложение"
    assert "уже не актуально" in foreign.answers[-1]

    current = _Query(102)
    _run(current)
    assert seen and "new.jpg" in seen[0]["text"]
    assert bot._peek_pending_attachment(42) == ("", [])
    print("ok  старая кнопка и чужой пользователь не забирают новое вложение")


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
        "🤣 😁 🤩 😍 🥰 😐 🤔 🥱 😴 😢 😭 😱 🤬 😡 🤮 🤯 🤪 🥴 😇 🤓 "
        "🤨 🫡 🤗 😎 😘 😨 ❤️ 💔 ❤‍🔥 💘 👍 👎 👌 🤝 👏 🙏 🖕 ✍ 💅 🤷 "
        "🔥 🎉 💯 🏆 🍾 👀 🤡 💩 🗿 🕊 💊 🎃 🎄 🎅 ☃ 🍓 🍌 🌭 🌚 😈 "
        "👻 🦄 🐳 ⚡ 🆒 👨‍💻 💋 🙈 🙉 🙊 👾 🤷‍♂ 🤷‍♀"
    ).split()
    assert list(REACTION_EMOJIS) == expected, REACTION_EMOJIS
    enum = ReactTelegramTool.parameters["properties"]["emoji"]["enum"]
    assert enum == expected, enum
    assert all(normalize_reaction(emoji) in _ALLOWED_REACTIONS for emoji in expected)
    assert normalize_reaction("❤️‍🔥") == "❤‍🔥"
    assert normalize_reaction("🤷‍♂️") == "🤷‍♂"
    print("ok  у react_telegram весь список из 73 эмодзи Bot API")


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

        assert hasattr(dlg, "chk_tg_advanced_ui"), "нет галочки advanced_ui"
        dlg.config.telegram.advanced_ui = False
        dlg._load()
        assert not dlg.chk_tg_advanced_ui.isChecked()

        dlg.chk_tg_hints.setChecked(True)
        dlg.chk_tg_advanced_ui.setChecked(True)
        captured = []
        dialogs.set_secret = lambda *a, **k: None
        dialogs.get_config = lambda *a, **k: None
        dlg._write_yaml = lambda cfg: captured.append(cfg)
        dlg._apply_tg_autostart = lambda: True
        dlg.accept = lambda: None
        dlg._save()
        assert captured and captured[0].telegram.hints_enabled is True
        assert captured[0].telegram.advanced_ui is True
    finally:
        dialogs.set_secret, dialogs.get_config = old_secret, old_get
        dlg.done(0)
    print("ok  галочка подсказок Telegram читается и сохраняется")


def test_agent_reset_clears_partial_answer() -> None:
    """Сброс стрима убирает черновик из живого прогресса до повтора."""
    import asyncio

    import nemus.core.telegram_bot as telegram_bot

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.edits: list[str] = []
            self.deleted: list[int] = []

        async def send_message(self, chat_id: int, text: str, **_: object):
            self.sent.append(text)
            return SimpleNamespace(message_id=len(self.sent))

        async def edit_message_text(
            self, text: str, chat_id: int, message_id: int, **_: object
        ) -> None:
            self.edits.append(text)

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            self.deleted.append(message_id)

    def run_agent(
        text: str,
        images: list[str] | None = None,
        stop: object = None,
        observe: bool = True,
    ):
        yield SimpleNamespace(kind="assistant_delta", text="черновик")
        yield SimpleNamespace(kind="assistant_reset")
        yield SimpleNamespace(kind="assistant_delta", text="полный ответ")
        yield SimpleNamespace(kind="final", text="полный ответ")

    cfg = Config()
    bot = TelegramBot(
        cfg,
        SimpleNamespace(
            llm=None,
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=None,
        ),
    )
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    bot.agent = SimpleNamespace(run=run_agent)
    bot._prepare_session = lambda chat_id: None

    async def _typing(chat_id: int) -> None:
        return None

    async def _hint(chat_id: int) -> None:
        return None

    bot._keep_typing = _typing  # type: ignore[method-assign]
    bot._maybe_show_first_hint = _hint  # type: ignore[method-assign]

    old_interval = telegram_bot.PROGRESS_INTERVAL
    telegram_bot.PROGRESS_INTERVAL = 0
    try:
        asyncio.run(
            bot._run_task_for(
                chat_id=42, user_id=7, message_id=None, text="вопрос"
            )
        )
    finally:
        telegram_bot.PROGRESS_INTERVAL = old_interval

    assert "черновик" not in fake.sent[-1], fake.sent
    assert "полный ответ" in fake.sent[-1], fake.sent
    draft_edits = [index for index, text in enumerate(fake.edits) if "черновик" in text]
    assert draft_edits, fake.edits
    after_draft = fake.edits[draft_edits[-1] + 1 :]
    assert after_draft and all("черновик" not in text for text in after_draft), (
        fake.edits
    )
    print("ok  assistant_reset очищает черновик в прогрессе Telegram")


def test_advanced_api_golden_payloads() -> None:
    """Адаптер отправляет ровно задокументированные payload'ы Bot API."""
    import asyncio

    from nemus.core.telegram_api import TelegramAdvancedAPI

    class _FakeBot:
        token = "123:SECRET"

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def _post(self, endpoint: str, data: dict):
            self.calls.append((endpoint, data))
            return {"message_id": 11}

    fake = _FakeBot()
    api = TelegramAdvancedAPI(fake)

    async def scenario() -> None:
        assert await api.send_message_draft(chat_id=5, draft_id=7, text="")
        assert await api.send_rich_message(
            chat_id=5,
            rich_message={"markdown": "# x"},
            reply_parameters={"message_id": 3, "allow_sending_without_reply": True},
            reply_markup=None,
        ) == 11
        assert await api.send_ephemeral(
            chat_id=5, callback_query_id="cb1", html="<b>x</b>", reply_markup=None
        ) == 11

    asyncio.run(scenario())
    endpoints = [endpoint for endpoint, _ in fake.calls]
    assert endpoints == [
        "sendMessageDraft",
        "sendRichMessage",
        "sendMessage",
    ], endpoints
    payloads = dict(fake.calls)
    assert payloads["sendMessageDraft"] == {
        "chat_id": 5,
        "draft_id": 7,
        "text": "",
        "can_stop": True,
        "keep_on_stop": True,
    }
    assert payloads["sendRichMessage"]["rich_message"] == {"markdown": "# x"}
    assert "reply_markup" not in payloads["sendRichMessage"], "None не отправляем"
    ephemeral = payloads["sendMessage"]
    assert ephemeral["parse_mode"] == "HTML"
    assert ephemeral["ephemeral_message_parameters"] == {
        "callback_query_id": "cb1"
    }
    assert "reply_markup" not in ephemeral
    print("ok  адаптер Bot API 10.3/10.4 шлёт точные payload'ы")


def test_advanced_api_unsupported_sticky() -> None:
    """«Method not found» кэшируется, «chat not found» — нет."""
    import asyncio

    from telegram.error import BadRequest

    from nemus.core.telegram_api import TelegramAdvancedAPI

    class _UnsupportedBot:
        token = "123:SECRET"

        def __init__(self) -> None:
            self.calls = 0

        async def _post(self, endpoint: str, data: dict):
            self.calls += 1
            raise BadRequest("Method not found")

    fake = _UnsupportedBot()
    api = TelegramAdvancedAPI(fake)

    async def scenario() -> None:
        try:
            await api.send_message_draft(chat_id=1, draft_id=2, text="")
        except BadRequest:
            pass
        else:
            raise AssertionError("первый вызов должен пробросить ошибку")
        assert not api.available("sendMessageDraft")
        assert await api.send_message_draft(chat_id=1, draft_id=2, text="") is False

    asyncio.run(scenario())
    assert fake.calls == 1, fake.calls

    class _ChatBot:
        token = "123:SECRET"

        def __init__(self) -> None:
            self.calls = 0

        async def _post(self, endpoint: str, data: dict):
            self.calls += 1
            raise BadRequest("chat not found")

    fake2 = _ChatBot()
    api2 = TelegramAdvancedAPI(fake2)

    async def scenario2() -> None:
        for _ in range(2):
            try:
                await api2.send_rich_message(chat_id=1, rich_message={})
            except BadRequest:
                pass
            else:
                raise AssertionError("ошибка должна пробрасываться")
        assert api2.available("sendRichMessage")

    asyncio.run(scenario2())
    assert fake2.calls == 2, fake2.calls

    class _DeletedMessageBot:
        token = "123:SECRET"

        def __init__(self) -> None:
            self.calls = 0

        async def _post(self, endpoint: str, data: dict):
            self.calls += 1
            raise BadRequest("Bad Request: message to edit not found")

    fake3 = _DeletedMessageBot()
    api3 = TelegramAdvancedAPI(fake3)

    async def scenario3() -> None:
        for _ in range(2):
            try:
                await api3.send_rich_message(chat_id=1, rich_message={})
            except BadRequest:
                pass
            else:
                raise AssertionError("удалённое сообщение должно пробрасывать ошибку")
        assert api3.available("sendRichMessage"), (
            "удалённое сообщение — временная ошибка, а не отсутствие метода"
        )

    asyncio.run(scenario3())
    assert fake3.calls == 2, fake3.calls
    print("ok  неподдерживаемый метод кэшируется, временная ошибка — нет")


def test_rich_markdown() -> None:
    """Rich-текст сохраняет разметку, но не пропускает HTML модели."""
    from nemus.core.telegram_rich import (
        RICH_LIMIT,
        RichTextTooLong,
        build_rich_markdown,
        prepare_rich_markdown,
        rich_button_rows,
    )

    source = (
        "# Отчёт\n\n- пункт\n\n"
        "| Файл | Дата |\n|---|---|\n| Март | 12.03 |\n\n"
        "```py\nprint('<tag>')\n```\n\n"
        "<script>alert(1)</script> и `<b>` в коде"
    )
    prepared = prepare_rich_markdown(source)
    assert "# Отчёт" in prepared
    assert "- пункт" in prepared
    assert '<table bordered striped compact>' in prepared, prepared
    assert "<th>Файл</th>" in prepared and "<td>Март</td>" in prepared, prepared
    assert "print('<tag>')" in prepared, prepared
    assert "`<b>`" in prepared, prepared
    assert "&lt;script>" in prepared, prepared
    assert "<script>" not in prepared, prepared
    assert "&lt;tg-button" in prepare_rich_markdown('<tg-button data="evil">')
    long_quote = "> первая строка\n> вторая строка\n> третья строка\n> четвёртая строка"
    expanded = prepare_rich_markdown(long_quote)
    assert "<blockquote expandable>" in expanded, expanded
    short_quote = prepare_rich_markdown("> только одна строка")
    assert short_quote == "> только одна строка", short_quote

    rows = rich_button_rows(
        [[("🆕 <Новый>", "ux:new"), ("Помощь", "ux:help")]]
    )
    assert 'data="ux:new"' in rows, rows
    assert "&lt;Новый>" in rows, rows
    assert 'style="primary"' in rows and 'style="link"' in rows, rows
    assert "tg-button-row" in rows

    try:
        build_rich_markdown("x" * (RICH_LIMIT + 1))
    except RichTextTooLong:
        pass
    else:
        raise AssertionError("длинный rich-текст должен уходить в fallback")
    assert len(build_rich_markdown("x" * RICH_LIMIT)) == RICH_LIMIT
    print("ok  rich Markdown санитайзится, кнопки и лимит проверяются")


def test_rich_final_fallback() -> None:
    """Неудачный rich-ответ откатывается на HTML с обычной клавиатурой."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object):
            self.sent.append((text, kwargs))
            return SimpleNamespace(message_id=len(self.sent))

    class _FakeAPI:
        def __init__(self, message_id: int | None) -> None:
            self.message_id = message_id
            self.calls: list[dict] = []

        def available(self, endpoint: str) -> bool:
            return endpoint == "sendRichMessage"

        async def send_rich_message(self, **kwargs: object) -> int | None:
            self.calls.append(dict(kwargs))
            return self.message_id

    cfg = Config()
    bot = TelegramBot(
        cfg,
        SimpleNamespace(
            llm=None,
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=None,
        ),
    )
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    broken = _FakeAPI(None)
    bot._api = broken  # type: ignore[assignment]
    asyncio.run(bot._send_final(42, 7, "# Готово"))
    assert broken.calls, "rich-вызов должен быть"
    markdown = broken.calls[0]["rich_message"]["markdown"]
    assert markdown.startswith("# Готово"), markdown
    assert "tg-button-row" in markdown, markdown
    assert broken.calls[0]["reply_parameters"] == {
        "message_id": 7,
        "allow_sending_without_reply": True,
    }
    assert fake.sent, "fallback должен отправить HTML-сообщение"
    assert fake.sent[-1][1].get("reply_markup") is not None
    assert "Готово" in fake.sent[-1][0]

    ok_fake = _FakeBot()
    bot._app = SimpleNamespace(bot=ok_fake)
    ok = _FakeAPI(11)
    bot._api = ok  # type: ignore[assignment]
    asyncio.run(bot._send_final(42, 7, "Готово"))
    assert not ok_fake.sent, "при успехе fallback не нужен"
    print("ok  rich-финал откатывается на HTML при ошибке и не дублируется")


class _DraftAPI:
    """Fake raw-адаптера: пишет draft'ы и умеет падать по команде."""

    def __init__(self, *, fail: bool = False) -> None:
        self.drafts: list[tuple[int, int, str]] = []
        self.rich = 0
        self.fail = fail

    def available(self, endpoint: str) -> bool:
        return endpoint in ("sendMessageDraft", "sendRichMessage")

    async def send_message_draft(
        self, *, chat_id: int, draft_id: int, text: str
    ) -> bool:
        self.drafts.append((chat_id, draft_id, text))
        if self.fail:
            raise RuntimeError("draft недоступен")
        return True

    async def send_rich_message(self, **kwargs: object) -> int:
        self.rich += 1
        return self.rich


class _ProgressBot:
    """Fake Bot: нужен только для _Progress при откате."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.deleted: list[int] = []

    async def send_message(self, chat_id: int, text: str, **_: object):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(
        self, text: str, chat_id: int, message_id: int, **_: object
    ) -> None:
        self.edits.append(text)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)


def test_draft_progress() -> None:
    """Draft стартует пустым, троттлится и очищает хвост."""
    import asyncio

    import nemus.core.telegram_bot as telegram_bot

    api = _DraftAPI()
    bot = _ProgressBot()
    progress = telegram_bot._DraftProgress(api, bot, 1)

    async def scenario() -> None:
        await progress.start("⏳ Думаю…")
        assert api.drafts and "Думаю" in api.drafts[0][2], api.drafts
        assert api.drafts[0][1] != 0
        progress._last = float("inf")  # троттлинг: время ещё «не пришло»
        before = len(api.drafts)
        await progress.note("🔧 read_file")
        assert len(api.drafts) == before, "note должен троттлиться"
        await progress.tail("частичный ответ", force=True)
        assert "частичный ответ" in api.drafts[-1][2]
        await progress.tail("", force=True)
        assert api.drafts[-1][2].endswith("read_file"), api.drafts[-1][2]
        await progress.close()
        assert progress._keepalive is None

    asyncio.run(scenario())
    print("ok  draft-прогресс троттлится и очищает хвост")


def test_draft_keepalive_fallback() -> None:
    """Keepalive повторяет рендер, два сбоя дают обычный прогресс."""
    import asyncio

    import nemus.core.telegram_bot as telegram_bot

    api = _DraftAPI()
    bot = _ProgressBot()
    progress = telegram_bot._DraftProgress(api, bot, 1)

    async def scenario() -> None:
        await progress.start("⏳ Думаю…")
        await progress.note("🔧 read_file")
        await progress.tail("черновик")
        before = len(api.drafts)
        await progress._keepalive_tick()
        assert len(api.drafts) == before + 1, "тик должен повторить рендер"
        assert "черновик" in api.drafts[-1][2]

        api.fail = True
        await progress._keepalive_tick()
        assert progress._fallback is None, "одного сбоя мало"
        await progress._keepalive_tick()
        assert progress._fallback is not None, "два сбоя — откат"
        assert bot.sent and "Думаю" in bot.sent[0]
        assert any("read_file" in text for text in bot.edits + bot.sent)

        drafts = len(api.drafts)
        await progress.note("🔧 run_shell")
        assert len(api.drafts) == drafts, "после отката draft не трогаем"
        await progress.close()

    asyncio.run(scenario())

    api2 = _DraftAPI(fail=True)
    bot2 = _ProgressBot()
    broken = telegram_bot._DraftProgress(api2, bot2, 1)

    async def scenario2() -> None:
        await broken.start("⏳ Думаю…")
        assert broken._fallback is not None, "первый сбой — сразу откат"
        assert bot2.sent

    asyncio.run(scenario2())
    print("ok  draft keepalive переживает паузы и откатывается на _Progress")


def test_run_task_uses_draft() -> None:
    """Draft выбирается только для private + advanced + не-retry."""
    import asyncio

    import nemus.core.telegram_bot as telegram_bot

    def _agent(
        text: str, images: list[str] | None = None, stop: object = None
    ):
        yield SimpleNamespace(kind="assistant_delta", text="ответ")
        yield SimpleNamespace(kind="final", text="ответ")

    def make_bot(api: _DraftAPI, advanced: bool = True):
        cfg = Config()
        cfg.telegram.advanced_ui = advanced
        instance = TelegramBot(
            cfg,
            SimpleNamespace(
                llm=None,
                registry=ToolRegistry(),
                permissions=PermissionManager(cfg),
                telegram_memory=None,
            ),
        )
        instance._app = SimpleNamespace(bot=_ProgressBot())
        instance._api = api  # type: ignore[assignment]
        instance.agent = SimpleNamespace(run=_agent)
        instance._prepare_session = lambda chat_id: None

        async def _noop(chat_id: int) -> None:
            return None

        instance._keep_typing = _noop  # type: ignore[method-assign]
        instance._maybe_show_first_hint = _noop  # type: ignore[method-assign]
        return instance

    old_interval = telegram_bot.PROGRESS_INTERVAL
    telegram_bot.PROGRESS_INTERVAL = 0
    try:
        cases = [
            ("private", True, False, True),
            ("group", True, False, False),
            ("private", False, False, False),
            ("private", True, True, False),
        ]
        for chat_type, advanced, retry, expected in cases:
            api = _DraftAPI()
            bot = make_bot(api, advanced)
            asyncio.run(
                bot._run_task_for(
                    chat_id=1,
                    user_id=2,
                    message_id=None,
                    text="вопрос",
                    chat_type=chat_type,
                    retry=retry,
                )
            )
            used = bool(api.drafts)
            assert used is expected, (chat_type, advanced, retry, api.drafts)
    finally:
        telegram_bot.PROGRESS_INTERVAL = old_interval
    print("ok  draft включается только в личке, при advanced_ui и без retry")


def test_stopped_generation_update() -> None:
    """Stop принимается только для своей задачи и своего draft."""
    import asyncio
    import threading

    cfg = Config()
    bot = object.__new__(TelegramBot)
    bot._pending = {}
    bot._stop_event = threading.Event()
    bot._current_task_id = 3
    bot._current_task_chat = 5
    bot._draft_id = 7
    bot._active_progress = None

    def stopped(chat_id: int, draft_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            api_kwargs={
                "stopped_message_generation": {
                    "chat": {"id": chat_id},
                    "draft_id": draft_id,
                }
            },
            effective_chat=SimpleNamespace(id=chat_id, type="private"),
        )

    asyncio.run(bot._on_raw_update(stopped(5, 99), None))
    assert not bot._stop_event.is_set(), "чужой draft не должен останавливать"
    asyncio.run(bot._on_raw_update(stopped(5, 7), None))
    assert bot._stop_event.is_set()

    bot._stop_event = threading.Event()
    asyncio.run(bot._on_raw_update(stopped(6, 7), None))
    assert not bot._stop_event.is_set(), "чужой чат не должен останавливать"

    bot._stop_event = None
    asyncio.run(bot._on_raw_update(stopped(5, 7), None))
    assert bot._stop_event is None, "после задачи событие игнорируется"
    _ = cfg
    print("ok  stopped_message_generation останавливает только свою задачу")


def test_stop_denies_pending_confirmation() -> None:
    """Stop разрешает future подтверждения как False и снимает клавиатуру."""
    import asyncio
    import threading

    class _Message:
        def __init__(self) -> None:
            self.markups: list[object] = []

        async def edit_reply_markup(self, reply_markup: object = None) -> None:
            self.markups.append(reply_markup)

    class _Bot:
        def __init__(self) -> None:
            self.message = _Message()

        async def send_message(self, chat_id: int, text: str, **kwargs: object):
            return self.message

    cfg = Config()
    bot = TelegramBot(
        cfg,
        SimpleNamespace(
            llm=None,
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=None,
        ),
    )
    fake = _Bot()
    bot._app = SimpleNamespace(bot=fake)
    bot._stop_event = threading.Event()
    bot._stop_event.set()

    async def scenario() -> None:
        bot._loop = asyncio.get_running_loop()
        future = bot._loop.create_future()
        bot._pending["42:7:1"] = (future, "текст")
        bot._deny_pending_confirmations(42)
        assert future.result() is False
        bot._pending.clear()
        result = await asyncio.wait_for(
            bot._ask_confirm(
                42, 7, SimpleNamespace(name="run_shell"), {"cmd": "dir"}
            ),
            timeout=2,
        )
        assert result is False
        assert fake.message.markups == [None], fake.message.markups

    asyncio.run(scenario())
    print("ok  Stop отклоняет подтверждение и снимает клавиатуру")


def test_last_tasks_set_inside_lock() -> None:
    """Постановка в очередь не перетирает «Повторить» работающей задачи."""
    import asyncio

    import nemus.core.telegram_bot as telegram_bot

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object):
            self.sent.append(text)
            return SimpleNamespace(message_id=len(self.sent))

        async def edit_message_text(self, *args: object, **kwargs: object) -> None:
            pass

        async def delete_message(self, *args: object, **kwargs: object) -> None:
            pass

    def run_agent(
        text: str,
        images: list[str] | None = None,
        stop: object = None,
        observe: bool = True,
    ):
        yield SimpleNamespace(kind="final", text="готово")

    cfg = Config()
    bot = TelegramBot(
        cfg,
        SimpleNamespace(
            llm=None,
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=None,
        ),
    )
    bot._app = SimpleNamespace(bot=_Bot())
    bot.agent = SimpleNamespace(run=run_agent)
    bot._prepare_session = lambda chat_id: None
    bot._last_tasks[1] = ("старая", [], None, 7)
    bot._last_task_ids[1] = 1
    bot._task_seq[1] = 1
    bot._task_lock = asyncio.Lock()

    async def _noop(chat_id: int) -> None:
        return None

    bot._keep_typing = _noop  # type: ignore[method-assign]
    bot._maybe_show_first_hint = _noop  # type: ignore[method-assign]

    async def scenario() -> None:
        await bot._task_lock.acquire()
        queued = asyncio.create_task(
            bot._run_task_for(1, 7, None, "новая", chat_type="group")
        )
        await asyncio.sleep(0.05)
        assert bot._last_tasks[1][0] == "старая", bot._last_tasks
        bot._task_lock.release()
        await queued
        assert bot._last_tasks[1][0] == "новая", bot._last_tasks
        assert bot._last_task_ids[1] == 2, bot._last_task_ids

    old_interval = telegram_bot.PROGRESS_INTERVAL
    telegram_bot.PROGRESS_INTERVAL = 0
    try:
        asyncio.run(scenario())
    finally:
        telegram_bot.PROGRESS_INTERVAL = old_interval
    print("ok  последняя задача обновляется внутри lock, очередь её не перетирает")


def test_panel_command() -> None:
    """`/panel` — настройки в Mini App с inline-резервом."""
    import asyncio

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.markups: list[object] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object):
            self.sent.append(text)
            self.markups.append(kwargs.get("reply_markup"))
            return SimpleNamespace(message_id=len(self.sent))

    def make_bot(*, allowed: bool = True):
        cfg = Config()
        cfg.telegram.allowed_users = [7] if allowed else [99]
        bot = TelegramBot(
            cfg,
            SimpleNamespace(
                llm=None,
                registry=ToolRegistry(),
                permissions=PermissionManager(cfg),
                telegram_memory=None,
            ),
        )
        bot._app = SimpleNamespace(bot=_Bot())
        return bot

    class _Reply:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.kwargs: dict[str, object] = {}

        async def __call__(self, text: str, **kwargs: object) -> None:
            self.calls.append(text)
            self.kwargs = kwargs

    def update(chat_type: str = "private") -> SimpleNamespace:
        reply = _Reply()
        message = SimpleNamespace(message_id=5, reply_text=reply)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=1, type=chat_type),
            effective_message=message,
            message=message,
            reply=reply,
        )

    async def scenario() -> None:
        bot = make_bot()
        await bot._cmd_panel(update(), None)
        assert bot._app.bot.sent, "панель должна отправить сообщение"
        panel_text = bot._app.bot.sent[-1]
        assert "Настройки Немуси" in panel_text
        assert "LLM" in panel_text and "Telegram" in panel_text
        assert bot._app.bot.markups[-1] is not None, "нужны рабочие кнопки"
        assert 1 in bot._settings_message

        web = make_bot()
        web.config.telegram.web_app_url = "https://example.test/app"
        web.cfg = web.config.telegram
        web_update = update()
        await web._cmd_panel(web_update, None)
        markup = web_update.reply.kwargs.get("reply_markup")
        assert markup is not None, "Web App должен прислать кнопку"
        button = markup.inline_keyboard[0][0]
        assert button.web_app is not None
        assert button.web_app.url == "https://example.test/app"

        auto = make_bot()
        auto._tunnel = SimpleNamespace(
            public_url="https://auto.test", starting=False, failed=False
        )
        auto_update = update()
        await auto._cmd_panel(auto_update, None)
        auto_markup = auto_update.reply.kwargs.get("reply_markup")
        assert auto_markup is not None
        assert auto_markup.inline_keyboard[0][0].web_app.url == "https://auto.test"

        group = make_bot()
        group_update = update("group")
        await group._cmd_panel(group_update, None)
        assert group_update.reply.calls, "в группе должен быть отказ"

        denied = make_bot(allowed=False)
        denied_update = update()
        await denied._cmd_panel(denied_update, None)
        assert denied_update.reply.calls

    asyncio.run(scenario())
    print("ok  /panel открывает Web App (или inline-резерв) и закрыта для групп")


def test_webapp_menu_sync() -> None:
    """Старая Web App-кнопка меню получает адрес нового quick-туннеля."""
    import asyncio

    from telegram import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

    class _Bot:
        def __init__(self, current: object) -> None:
            self.current = current
            self.menu: object | None = None

        async def get_chat_menu_button(self, *, chat_id: int | None = None) -> object:
            return self.current

        async def set_chat_menu_button(
            self, *, menu_button: object, chat_id: int | None = None
        ) -> bool:
            self.menu = menu_button
            self.current = menu_button
            return True

    old = MenuButtonWebApp("Nemo", WebAppInfo("https://old.trycloudflare.com"))
    app_bot = _Bot(old)
    bot = object.__new__(TelegramBot)
    bot.cfg = SimpleNamespace(token="", allowed_users=[])
    bot._menu_url_synced = ""
    bot._app = SimpleNamespace(bot=app_bot)
    asyncio.run(bot._sync_webapp_menu("https://new.trycloudflare.com"))
    assert app_bot.menu is not None, "кнопка меню не обновлена"
    assert app_bot.menu.web_app.url == "https://new.trycloudflare.com"
    assert app_bot.menu.text == "Nemo"

    commands_bot = _Bot(MenuButtonDefault())
    safe = object.__new__(TelegramBot)
    safe.cfg = SimpleNamespace(token="", allowed_users=[])
    safe._menu_url_synced = ""
    safe._app = SimpleNamespace(bot=commands_bot)
    asyncio.run(safe._sync_webapp_menu("https://new.trycloudflare.com"))
    assert commands_bot.menu is None, "меню команд трогать нельзя"
    print("ok  Web App: кнопка меню обновляется на актуальный туннель")


def test_settings_callback_flow() -> None:
    """Кнопки панели меняют настройки и текстовый ввод возвращает панель."""
    import asyncio

    class _Reply:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def __call__(self, text: str, **kwargs: object) -> None:
            self.calls.append(text)

    class _Msg:
        def __init__(self) -> None:
            self.chat_id = 1
            self.chat = SimpleNamespace(id=1, type="private")
            self.message_id = 11
            self.reply_text = _Reply()
            self.text = ""
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

    class _Query:
        def __init__(self, data: str) -> None:
            self.data = data
            self.message = _Msg()
            self.from_user = SimpleNamespace(id=7)
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.message.text = text

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.edits: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object):
            self.sent.append(text)
            return SimpleNamespace(message_id=len(self.sent))

        async def edit_message_text(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("text") or ""))

    cfg = Config()
    cfg.telegram.allowed_users = [7]
    bot = TelegramBot(
        cfg,
        SimpleNamespace(
            llm=None,
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=None,
        ),
    )
    bot._app = SimpleNamespace(bot=_Bot())

    def fake_save(mutate: object) -> tuple[bool, str]:
        return mutate(bot.config)  # type: ignore[operator]

    bot._save_settings = fake_save  # type: ignore[method-assign]

    async def scenario() -> None:
        reply = _Reply()
        message = SimpleNamespace(message_id=5, reply_text=reply)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_message=message,
            message=message,
        )
        await bot._cmd_panel(update, None)
        assert bot._app.bot.sent

        query = _Query("pn:s:telegram:0")
        await bot._on_settings_callback(query)
        assert "Telegram" in query.message.text

        before = cfg.telegram.hints_enabled
        query = _Query("pn:t:telegram:tg_hints")
        await bot._on_settings_callback(query)
        assert cfg.telegram.hints_enabled is not before

        query = _Query("pn:e:telegram:tg_groups")
        await bot._on_settings_callback(query)
        assert 1 in bot._settings_pending
        value_message = SimpleNamespace(reply_text=_Reply())
        value_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=1, type="private"),
            message=value_message,
            effective_message=value_message,
        )
        await bot._apply_settings_text(value_update, "да")
        assert cfg.telegram.allow_groups is True
        assert 1 not in bot._settings_pending

        await bot._on_settings_callback(_Query("pn:c"))

    asyncio.run(scenario())
    print("ok  callback-кнопки /panel меняют настройки, текст сохраняется")


def test_settings_panel_helpers() -> None:
    """Панель настроек читает/пишет те же поля конфига, что GUI."""
    from nemus.core import telegram_settings as settings

    cfg = Config()
    assert settings.render(cfg, "root").startswith("<b>⚙️")
    assert settings.section("telegram") is not None
    temp = settings.find_field("llm", "temp")
    assert temp is not None
    ok, _ = settings.apply_text(cfg, temp, "0.3")
    assert ok and cfg.llm.temperature == 0.3
    provider = settings.find_field("llm", "provider")
    assert provider is not None
    ok, _ = settings.cycle_field(cfg, provider)
    assert ok and cfg.llm.default_provider != "gemini"
    users = settings.find_field("telegram", "tg_users")
    assert users is not None
    ok, _ = settings.apply_text(cfg, users, "7, 8")
    assert ok and cfg.telegram.allowed_users == [7, 8]
    bad = settings.apply_text(cfg, users, "не id")
    assert bad[0] is False
    keyboard = settings.section_keyboard(cfg, "telegram", autostart=False)
    assert any(data.startswith("pn:t:") for row in keyboard for _, data in row)
    assert any(data == "pn:auto" for row in keyboard for _, data in row)
    tools_keyboard = settings.section_keyboard(cfg, "tools")
    assert any(data == "pn:cleanup" for row in tools_keyboard for _, data in row)
    print("ok  панель настроек читает и пишет поля GUI")


def test_callback_message_ephemeral() -> None:
    """В группе ответ на кнопку уходит ephemeral и получает один answer."""
    import asyncio

    class _Query:
        def __init__(self, chat_type: str = "group") -> None:
            self.id = "cb-1"
            self.data = "ux:help"
            self.message = SimpleNamespace(
                chat_id=1,
                chat=SimpleNamespace(id=1, type=chat_type),
            )
            self.from_user = SimpleNamespace(id=7)
            self.events: list[tuple] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.events.append(("answer", text, show_alert))

    class _API:
        def __init__(self, *, fail: bool = False) -> None:
            self.calls: list[dict] = []
            self.fail = fail

        def available(self, endpoint: str) -> bool:
            return endpoint == "sendMessage"

        async def send_ephemeral(self, **kwargs: object) -> int | None:
            self.calls.append(dict(kwargs))
            if self.fail:
                raise RuntimeError("ephemeral недоступен")
            return 21

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object):
            self.sent.append(text)
            return SimpleNamespace(message_id=1)

    def make_bot(api: _API, *, advanced: bool = True):
        cfg = Config()
        cfg.telegram.advanced_ui = advanced
        bot = TelegramBot(
            cfg,
            SimpleNamespace(
                llm=None,
                registry=ToolRegistry(),
                permissions=PermissionManager(cfg),
                telegram_memory=None,
            ),
        )
        bot._api = api  # type: ignore[assignment]
        bot._app = SimpleNamespace(bot=_Bot())
        return bot

    async def scenario() -> None:
        api = _API()
        bot = make_bot(api)
        query = _Query()
        await bot._callback_message(query, "**Текст**")
        assert api.calls and api.calls[0]["callback_query_id"] == "cb-1"
        assert "<b>Текст</b>" in api.calls[0]["html"]
        assert [e[0] for e in query.events] == ["answer"], query.events

        failed = _API(fail=True)
        err_bot = make_bot(failed)
        err_query = _Query()
        await err_bot._callback_message(err_query, "Ошибка")
        assert err_bot._app.bot.sent == [], "публичного сообщения быть не должно"
        assert err_query.events == [("answer", "Ошибка", True)]

        private_api = _API()
        private = make_bot(private_api)
        private_query = _Query("private")
        await private._callback_message(private_query, "Привет")
        assert private_api.calls == []
        assert private._app.bot.sent and private_query.events == [("answer", "", False)]

        legacy_api = _API()
        legacy = make_bot(legacy_api, advanced=False)
        legacy_query = _Query()
        await legacy._callback_message(legacy_query, "Старый путь")
        assert legacy_api.calls == []
        assert legacy._app.bot.sent and legacy_query.events
        assert len(legacy_query.events) == 1

    asyncio.run(scenario())
    print("ok  ephemeral в группе, alert при ошибке и старый путь без advanced_ui")


def test_ux_callback_no_double_answer() -> None:
    """Информационные ux-кнопки в группе отвечают ровно один раз."""
    import asyncio

    class _API:
        def __init__(self) -> None:
            self.sent = 0

        def available(self, endpoint: str) -> bool:
            return endpoint == "sendMessage"

        async def send_ephemeral(self, **kwargs: object) -> int:
            self.sent += 1
            return 21

    class _Query:
        def __init__(self, data: str) -> None:
            self.id = "cb"
            self.data = data
            self.message = SimpleNamespace(
                chat_id=1, chat=SimpleNamespace(id=1, type="group")
            )
            self.from_user = SimpleNamespace(id=7)
            self.answers = 0

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers += 1

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [7]
    bot = TelegramBot(
        cfg,
        SimpleNamespace(
            llm=SimpleNamespace(model="test-model", provider="test"),
            registry=ToolRegistry(),
            permissions=PermissionManager(cfg),
            telegram_memory=None,
        ),
    )
    api = _API()
    bot._api = api  # type: ignore[assignment]
    bot._new_session_for_chat = lambda chat_id: None  # type: ignore[method-assign]

    async def scenario() -> None:
        for data in ("ux:help", "ux:status", "ux:new", "ux:skills"):
            query = _Query(data)
            await bot._on_ux_callback(SimpleNamespace(), query)
            assert query.answers == 1, (data, query.answers)
        assert api.sent == 4, api.sent

    asyncio.run(scenario())
    print("ok  ux-кнопки в группе дают ровно один answerCallbackQuery")


def test_status() -> None:
    import nemus.core.telegram_bot as telegram_bot

    status = telegram_status()
    assert status["running"] is False, status
    old = telegram_bot._active_bot
    telegram_bot._active_bot = SimpleNamespace(
        running=True, healthy=False, username="nemus_bot"
    )
    try:
        status = telegram_status()
        assert status["running"] is False, status
        assert status["error"], status
    finally:
        telegram_bot._active_bot = old
    print("ok  статус без запущенного бота — «выключен», мёртвый polling — ошибка")


def test_brainstorm_one_question_at_a_time() -> None:
    """Брейншторм идёт по одному вопросу, D просит текст и запускает задачу."""
    import asyncio

    from nemus.core.brainstorm import parse_choices

    sample = """Поняла задачу: видео.
1. Какого формата сделать видео?
A) Вертикальный (рекомендую)
B) Горизонтальный
D) Свой вариант: _________

2. Какая длительность?
A) 30 секунд (рекомендую)
B) 60 секунд
D) Свой вариант: _________

3. Какой стиль?
A) Тёмный (рекомендую)
B) Светлый
D) Свой вариант: _________
"""

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []
            self.edits: list[dict[str, object]] = []
            self.next_id = 100

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> SimpleNamespace:
            self.next_id += 1
            self.sent.append((text, kwargs))
            return SimpleNamespace(message_id=self.next_id)

        async def edit_message_text(self, **kwargs: object) -> SimpleNamespace:
            self.edits.append(kwargs)
            return SimpleNamespace(message_id=kwargs.get("message_id"))

    class _Query:
        def __init__(self, data: str, message_id: int) -> None:
            self.data = data
            self.message = SimpleNamespace(
                message_id=message_id,
                chat_id=1,
                chat=SimpleNamespace(id=1, type="private"),
            )
            self.from_user = SimpleNamespace(id=7)
            self.answered = ""

        async def answer(self, text: str = "", **_: object) -> None:
            self.answered = text

        async def edit_message_reply_markup(self, **_: object) -> None:
            return None

    async def run_flow() -> None:
        bot = object.__new__(TelegramBot)
        fake = _FakeBot()
        bot._app = SimpleNamespace(bot=fake)
        bot._brainstorm = {}
        runs: list[dict[str, object]] = []

        async def run_task(**kwargs: object) -> None:
            runs.append(kwargs)

        bot._run_task_for = run_task  # type: ignore[method-assign]
        groups = parse_choices(sample)
        assert len(groups) == 3, groups
        await bot._send_brainstorm(
            1, 10, groups, user_id=7, chat_type="private"
        )
        first = fake.sent[0][0]
        assert "Вопрос 1 из 3" in first, first
        assert "Вопрос 2 из 3" not in first, first
        assert len(fake.sent[0][1].get("reply_markup").inline_keyboard) == 4

        raw = _FakeBot()
        await start_message(raw, 1, "<b>Вопрос</b>", raw_html=True)
        assert raw.sent[0][0] == "<b>Вопрос</b>", raw.sent
        assert "&lt;" not in raw.sent[0][0], raw.sent

        prompt_id = bot._brainstorm[1]["prompt_message_id"]
        await bot._on_brainstorm_callback(_Query("bs:0:0", prompt_id))
        assert "Вопрос 2 из 3" in str(fake.edits[-1]["text"])
        await bot._on_brainstorm_callback(_Query("bs:1:0", prompt_id))
        assert "Вопрос 3 из 3" in str(fake.edits[-1]["text"])
        await bot._on_brainstorm_callback(_Query("bs:2:2", prompt_id))
        assert bot._brainstorm[1]["awaiting_custom"] is True

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            effective_user=SimpleNamespace(id=7),
        )
        assert await bot._handle_brainstorm_text(
            update, "Неоновый нуар"
        )
        assert len(runs) == 1, runs
        assert "3D" in str(runs[0]["text"]), runs[0]
        assert "Неоновый нуар" in str(runs[0]["text"]), runs[0]
        assert 1 not in bot._brainstorm, bot._brainstorm

    asyncio.run(run_flow())
    print("ok  брейншторм: вопросы по одному, свой вариант и запуск задачи")


def test_torrent_tool_publishes_progress(tmp_path: Path) -> None:
    """Торрент-заглушка сохраняет файл, хеш и события kind='torrent'."""
    import hashlib
    from types import SimpleNamespace

    from nemus.core.tools import torrents as torrent_mod

    payload = b"torrent-fake-bytes"
    name = "setup.exe"

    class _FakeHandle:
        def __init__(self, save_path: Path) -> None:
            self._save_path = save_path
            self._status_calls = 0
            (self._save_path / name).write_bytes(payload)

        def status(self) -> SimpleNamespace:
            self._status_calls += 1
            done = len(payload) if self._status_calls >= 2 else len(payload) // 2
            return SimpleNamespace(
                total_done=done, total_want=len(payload), total_size=len(payload)
            )

        def is_seed(self) -> bool:
            return self._status_calls >= 2

        def save_path(self) -> str:
            return str(self._save_path)

        def name(self) -> str:
            return name

    class _AddParams:
        """libtorrent 2.x: add_torrent_params — объект с атрибутами."""

        def __init__(self, url: str = "") -> None:
            self.url = url
            self.save_path = ""
            self.ti: object = None

    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.params: object = None

        def add_torrent(self, params: object) -> _FakeHandle:
            self.params = params
            assert isinstance(params, _AddParams), params
            assert params.save_path, params
            return _FakeHandle(Path(params.save_path))

    fake_lt = SimpleNamespace(
        session=_FakeSession,
        add_torrent_params=_AddParams,
        parse_magnet_uri=lambda uri: _AddParams(uri),
    )
    tool = torrent_mod.TorrentDownloadTool()
    tool.root = tmp_path / "downloads"
    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    old_lib = torrent_mod._libtorrent
    old_interval = torrent_mod.POLL_INTERVAL
    torrent_mod._libtorrent = lambda: fake_lt
    torrent_mod.POLL_INTERVAL = 0
    try:
        result = tool.execute(
            magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            filename=name,
        )
    finally:
        torrent_mod._libtorrent = old_lib
        torrent_mod.POLL_INTERVAL = old_interval
        unsubscribe_download(token)

    assert result.ok, result.content
    target = tool.root / name
    assert target.read_bytes() == payload
    assert (tool.root / f"{name}.sha256").read_text(encoding="ascii") == (
        hashlib.sha256(payload).hexdigest()
    )
    assert seen and all(event.kind == "torrent" for event in seen), seen
    assert {event.phase for event in seen} >= {"start", "progress", "done"}, seen
    assert seen[-1].phase == "done" and seen[-1].done == len(payload)
    assert {event.task_id for event in seen} == {tool.last_task_id}

    from nemus.core.tools.all import all_builtin_tools

    assert "torrent_download" in {item.name for item in all_builtin_tools()}
    print("ok  торрент: файл, SHA-256 и события kind=torrent")


def test_torrent_add_uses_libtorrent_2x_attribute_api(tmp_path: Path) -> None:
    """Magnet и .torrent собирают add_torrent_params через атрибуты."""
    from unittest.mock import patch

    from nemus.core.tools import torrents as torrent_mod

    class _Params:
        def __init__(self) -> None:
            self.save_path = ""
            self.ti: object = None

    class _Session:
        def __init__(self) -> None:
            self.seen: list[_Params] = []

        def add_torrent(self, params: object) -> object:
            assert isinstance(params, _Params), params
            self.seen.append(params)
            return params

    info = object()
    lt = SimpleNamespace(
        add_torrent_params=_Params,
        parse_magnet_uri=lambda uri: _Params(),
        torrent_info=lambda raw: info,
    )
    session = _Session()
    tool = torrent_mod.TorrentDownloadTool

    magnet = tool._add(
        session, lt, "magnet", "magnet:?xt=urn:btih:test", tmp_path, 1024
    )
    assert magnet.save_path == str(tmp_path), magnet

    with patch.object(tool, "_fetch_metadata", staticmethod(lambda url, limit: b"meta")):
        torrent = tool._add(
            session,
            lt,
            "torrent",
            "https://example.test/file.torrent",
            tmp_path,
            1024,
        )
    assert torrent.save_path == str(tmp_path), torrent
    assert torrent.ti is info, torrent
    assert len(session.seen) == 2, session.seen
    print("ok  торрент: libtorrent 2.x использует add_torrent_params-атрибуты")


def test_torrent_add_falls_back_to_legacy_dict_params(tmp_path: Path) -> None:
    """Резервный dict API 1.x остаётся рабочим без add_torrent_params."""
    from unittest.mock import patch

    from nemus.core.tools import torrents as torrent_mod

    class _Session:
        def __init__(self) -> None:
            self.seen: list[object] = []

        def add_torrent(self, params: object) -> object:
            self.seen.append(params)
            return params

    info = object()
    lt = SimpleNamespace(
        parse_magnet_uri=lambda uri: {"url": uri},
        torrent_info=lambda raw: info,
    )
    session = _Session()
    tool = torrent_mod.TorrentDownloadTool

    magnet = tool._add(
        session, lt, "magnet", "magnet:?xt=urn:btih:test", tmp_path, 1024
    )
    assert magnet == {"url": "magnet:?xt=urn:btih:test", "save_path": str(tmp_path)}

    with patch.object(tool, "_fetch_metadata", staticmethod(lambda url, limit: b"meta")):
        torrent = tool._add(
            session,
            lt,
            "torrent",
            "https://example.test/file.torrent",
            tmp_path,
            1024,
        )
    assert torrent == {"ti": info, "save_path": str(tmp_path)}
    assert len(session.seen) == 2, session.seen
    print("ok  торрент: резерв dict API 1.x для magnet и .torrent")


def test_torrent_inline_button_gate() -> None:
    """Торрент-согласие: чужая кнопка не работает, доступ даёт только :y."""
    import asyncio

    cfg = Config()
    cfg.telegram.allowed_users = [7]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot._pending = {}
    bot._stop_event = None

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> SimpleNamespace:
            self.sent.append((text, kwargs))
            return SimpleNamespace(message_id=1)

    class _Query:
        def __init__(self, data: str, user_id: int) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)
            self.message = SimpleNamespace(chat_id=42, message_id=5)
            self.answers: list[str] = []
            self.edits: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.edits.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            return None

    def _update(query: _Query) -> SimpleNamespace:
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=42, type="private"),
        )

    def _buttons(fake: _FakeBot) -> list[object]:
        markup = fake.sent[-1][1]["reply_markup"]
        return [button for row in markup.inline_keyboard for button in row]

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bot._loop = loop
        fake = _FakeBot()
        bot._app = SimpleNamespace(bot=fake)

        future = bot._ask_torrent(42, 7, "magnet:?xt=urn:btih:test")
        await asyncio.sleep(0.05)
        assert isinstance(future, asyncio.Future)
        assert not future.done()
        text = fake.sent[0][0]
        assert "Скачать через торрент" in text, text
        buttons = _buttons(fake)
        labels = [button.text for button in buttons]
        assert any("Скачать через торрент" in label for label in labels), labels
        yes = next(
            button.callback_data for button in buttons
            if button.callback_data.endswith(":y")
        )
        assert any(
            button.callback_data.endswith(":n") for button in buttons
        ), labels
        assert yes.startswith("td:42:7:"), yes

        foreign = _Query(yes, user_id=8)
        await bot._on_callback(_update(foreign), None)
        assert not future.done(), "чужая кнопка не должна давать согласие"
        assert "не для этого чата" in foreign.answers[-1]

        approved = _Query(yes, user_id=7)
        await bot._on_callback(_update(approved), None)
        assert future.done() and future.result() is True
        assert approved.answers[-1] == "Разрешено"

        future2 = bot._ask_torrent(42, 7, "magnet:?xt=urn:btih:test2")
        await asyncio.sleep(0.05)
        no2 = next(
            button.callback_data for button in _buttons(fake)
            if button.callback_data.endswith(":n")
        )
        cancelled = _Query(no2, user_id=7)
        await bot._on_callback(_update(cancelled), None)
        assert future2.done() and future2.result() is False

    asyncio.run(scenario())
    print("ok  инлайн-согласие торрента: только своя кнопка и :y даёт доступ")


def test_torrent_input_and_dependency_guards(tmp_path: Path) -> None:
    """Только явный-source; без libtorrent — failure с подсказкой, не crash."""
    from nemus.core.tools import torrents as torrent_mod

    tool = torrent_mod.TorrentDownloadTool()
    tool.root = tmp_path / "downloads"

    bad = tool.execute(torrent_url="http://example.test/file.torrent")
    assert not bad.ok and "HTTPS" in bad.content, bad.content

    seen: list[DownloadEvent] = []
    token = subscribe_download(seen.append)
    old_lib = torrent_mod._libtorrent

    def _missing() -> object:
        raise ImportError("No module named 'libtorrent'")

    torrent_mod._libtorrent = _missing
    try:
        result = tool.execute(
            magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            filename="setup.exe",
        )
    finally:
        torrent_mod._libtorrent = old_lib
        unsubscribe_download(token)

    assert not result.ok, result.content
    assert "pip install python-libtorrent" in result.content, result.content
    assert any(
        event.kind == "torrent" and event.phase == "error" for event in seen
    ), seen
    assert not (tool.root / "setup.exe").exists()
    print("ok  торрент: только явный source и честная ошибка без libtorrent")


def main() -> int:
    test_proactive_drops_repeated_greeting()
    test_proactive_angles_are_varied()
    test_config_defaults()
    test_brainstorm_one_question_at_a_time()
    test_message_split()
    test_download_cleanup()
    test_safe_names()
    test_kind_detection()
    test_ffmpeg_video_note()
    test_authorization()
    test_formatting()
    test_stop_cancels_running_task()
    test_reminder_delivery()
    test_send_text_chunks()
    test_stt_import_without_numpy()
    test_cloud_stt_without_numpy()
    test_start_timeout_stops_hung_thread()
    test_stop_watchdog_kills_zombie_loop()
    test_typing_and_reactions()
    test_first_hint_once_and_mute()
    test_error_text_and_progress_keyboard()
    test_retry_and_new_callbacks()
    test_status_context_lines()
    test_ux_texts()
    test_diary_command_is_private_file()
    test_command_catalog()
    test_help_callback_edits_same_message()
    test_skills_text()
    test_skill_details_text()
    test_skills_command_and_callback()
    test_skill_command_not_duplicated()
    test_natural_language_skills_question()
    test_skills_helpers_are_safe()
    test_skills_use_real_plugin_and_mcp_config()
    test_start_help_respect_hints()
    test_muted_help_examples_and_attachment_buttons()
    test_ux_examples_callback()
    test_attachment_actions()
    test_pending_attachments_are_per_user()
    test_pending_attachments_are_per_chat()
    test_attachment_callback_runs_task()
    test_stale_attachment_button_does_not_take_newer_file()
    test_attachment_without_caption()
    test_stt_failure_does_not_ask_attachment()
    test_download_limit_is_twenty_mb()
    test_send_file_retries_and_falls_back()
    test_application_concurrent_updates()
    test_inbox_lock_serializes_text_and_media()
    test_new_blocked_during_task()
    test_confirmation_bound_to_chat_user()
    with tempfile.TemporaryDirectory(prefix="nemus_tg_torrent_") as tmp:
        folder = Path(tmp)
        test_torrent_tool_publishes_progress(folder / "ok")
        test_torrent_add_uses_libtorrent_2x_attribute_api(folder / "api2")
        test_torrent_add_falls_back_to_legacy_dict_params(folder / "api1")
        test_torrent_input_and_dependency_guards(folder / "guards")
    test_torrent_inline_button_gate()
    test_reaction_list_complete()
    test_reaction_choice()
    test_tool_without_bot()
    test_factory_registration()
    test_user_ids_parsing()
    test_gui_hints_toggle()
    test_agent_reset_clears_partial_answer()
    test_advanced_api_golden_payloads()
    test_advanced_api_unsupported_sticky()
    test_rich_markdown()
    test_rich_final_fallback()
    test_progress_download_line()
    test_draft_progress()
    test_draft_keepalive_fallback()
    test_run_task_uses_draft()
    test_stopped_generation_update()
    test_stop_denies_pending_confirmation()
    test_last_tasks_set_inside_lock()
    test_panel_command()
    test_webapp_menu_sync()
    test_settings_callback_flow()
    test_settings_panel_helpers()
    test_callback_message_ephemeral()
    test_ux_callback_no_double_answer()
    test_status()
    print("\nTELEGRAM TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
