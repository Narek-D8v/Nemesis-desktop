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
                self.replies: list[str] = []

            async def reply_text(self, text: str, **_: object) -> None:
                self.replies.append(text)

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

        async def _run_task(
            update: object, text: str, images: list[str] | None = None
        ) -> None:
            called.append(text)
            seen_images.append(images or [])

        fake_bot = _FakeBot()
        bot._app = SimpleNamespace(bot=fake_bot)
        bot._download = _download  # type: ignore[method-assign]
        bot._run_task = _run_task  # type: ignore[method-assign]
        asyncio.run(bot._on_media(update, None))
        assert not called, "вложение без подписи не должно уходить агенту"
        assert message.replies and "не поняла задачу" in message.replies[-1]
        assert not fake_bot.reactions, "👀 как статус больше не ставится"

        # следующая текстовая задача должна знать путь к фото и видеть его
        message.text = "опиши"  # type: ignore[attr-defined]
        asyncio.run(bot._on_text(update, None))
        assert called and image.name in called[-1], called
        assert "опиши" in called[-1], called[-1]
        assert seen_images and seen_images[-1], "фото должно уйти модели как изображение"
        assert seen_images[-1][0].startswith("data:image/"), seen_images[-1][0][:40]
    print("ok  фото без подписи: бот спрашивает задачу и передаёт изображение")


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
    test_typing_and_reactions()
    test_attachment_without_caption()
    test_reaction_list_complete()
    test_reaction_choice()
    test_tool_without_bot()
    test_factory_registration()
    test_user_ids_parsing()
    test_status()
    print("\nTELEGRAM TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
