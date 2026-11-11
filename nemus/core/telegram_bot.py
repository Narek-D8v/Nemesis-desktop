"""Telegram-бот Немуси: управление ноутбуком из чата.

Бот работает в отдельном потоке (свой asyncio-loop), поднимает long polling
через python-telegram-bot и отдаёт задачи тому же агенту, что и GUI, но со
своей историей диалога и своим подтверждением опасных инструментов.

Что умеет:
- принимать текст, файлы, фото, видео, голосовые (локальный STT);
- складывать вложения в ``<workspace>/telegram/<chat_id>`` и говорить агенту,
  где они лежат;
- показывать прогресс в «живом» сообщении и присылать итог;
- спрашивать разрешение на опасные действия inline-кнопками;
- отправлять файлы обратно в чат (инструмент ``send_telegram``), включая
  видео-кружки: ffmpeg сам обрезает ролик до квадрата 512×512 и минуты.

Безопасность: бот слушает только ID из ``telegram.allowed_users``; пустой
список — бот не запускается. Группы по умолчанию отключены.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from nemus.config import Config
from nemus.core.agent import Agent, AgentEvent
from nemus.core.providers import Message
from nemus.core.telegram_format import (
    TEXT_LIMIT,
    html_chunks,
    split_message,
    strip_html,
)
from nemus.core.telegram_ux import (
    attachment_action_rows,
    attachment_prompt,
    examples_text,
    help_text,
    main_menu_rows,
    start_text,
)
from nemus.core.tools.telegram import (
    _ALLOWED_REACTIONS,
    REACTION_EMOJIS,
    normalize_reaction,
)
from nemus.utils.logger import logger

#: предел подписи к файлу
CAPTION_LIMIT = 1024
#: сколько ждём нажатия кнопки подтверждения, сек
CONFIRM_TIMEOUT = 300.0
#: больше этого Bot API не примет файл без локального сервера
SEND_LIMIT_MB = 50
#: больше этого облачный Bot API не отдаёт файл через get_file
MAX_DOWNLOAD_MB = 20
#: сколько секунд держим «живое» сообщение с прогрессом
PROGRESS_INTERVAL = 1.5

#: активный экземпляр бота: инструмент send_telegram находит его отсюда
_active_bot: TelegramBot | None = None

#: выбор реакции: отдельный короткий запрос, потому что в общем наборе из
#: десятков инструментов модель почти не вызывает react_telegram
_REACTION_SYSTEM = (
    "Ты ставишь одну реакцию на сообщение пользователя в Telegram. Это твоё "
    "отношение к сообщению, а не статус работы: никогда не выбирай 👀, ✅ или "
    "❌ как «прочитала/работаю/готово».\n"
    "Выбирай эмодзи только по смыслу: шутка или анекдот — 🤣 или 😁, грустное — "
    "😢 или 💔, хорошая новость — 🎉 или 🔥, поддержка — ❤️, согласие — 👍, "
    "недоумение — 🤔 или 🤨. Если сообщение нейтральное, служебное или "
    "реакция неуместна — верни null. Не ставь реакцию только ради галочки.\n"
    "Верни строго JSON без пояснений: {\"emoji\": \"🤣\"} или "
    "{\"emoji\": null}.\n"
    f"Доступные эмодзи: {' '.join(REACTION_EMOJIS)}."
)


def _reaction_from_reply(reply: str) -> str:
    """Достаёт эмодзи из JSON-ответа модели и проверяет, что оно допустимо."""
    text = (reply or "").strip()
    if not text:
        return ""
    match = re.search(r"\{.*\}", text, re.S)
    raw = ""
    if match:
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            value = data.get("emoji")
            if isinstance(value, str):
                raw = value.strip()
    if not raw and len(text) <= 8:
        raw = text
    emoji = normalize_reaction(raw)
    if emoji and emoji in _ALLOWED_REACTIONS:
        return emoji
    if raw and raw.lower() not in {"null", "none"}:
        logger.warning(f"Telegram: модель выбрала недопустимую реакцию «{raw}»")
    return ""

#: сколько секунд помним вложение, к которому ещё не написали задачу
ATTACHMENT_CONTEXT_TTL = 1800.0
#: расширения, которые можно отдать модели как изображение
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
#: максимальная сторона изображения для vision-модели
IMAGE_MAX_SIDE = 1600


def active_bot() -> TelegramBot | None:
    """Запущенный бот (или None) — точка входа для инструмента отправки."""
    return _active_bot


# ---------------------------------------------------------------- утилиты
def safe_filename(name: str, fallback: str = "file") -> str:
    """Имя файла без путей и недопустимых символов."""
    raw = Path(str(name or "")).name.strip()
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in raw
    ).strip("._")
    return cleaned or fallback


def human_size(size: int) -> str:
    """Размер в человекочитаемом виде."""
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def guess_kind(path: Path) -> str:
    """Тип отправки по расширению файла."""
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    if suffix in {".mp3", ".ogg", ".m4a", ".wav", ".flac"}:
        return "audio"
    return "document"


def _image_data_url(path: Path) -> str | None:
    """Готовит изображение для vision-модели: уменьшает и кодирует в data URL."""
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    try:
        from io import BytesIO

        from PIL import Image, ImageOps

        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.split()[-1])
            elif image.mode != "RGB":
                image = image.convert("RGB")
            buffer = BytesIO()
            if path.suffix.lower() == ".png":
                image.save(buffer, "PNG", optimize=True)
                mime = "image/png"
            else:
                image.save(buffer, "JPEG", quality=88, optimize=True)
                mime = "image/jpeg"
            data = buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Telegram: изображение не подготовлено ({path.name}): {exc}")
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    logger.info(
        f"Telegram: изображение {path.name} передано модели "
        f"({human_size(len(data))})"
    )
    return f"data:{mime};base64,{encoded}"


def _attachment_context(paths: list[Path]) -> str:
    """Строка с путями вложений — чтобы следующая задача знала, о чём речь."""
    lines = []
    for path in paths:
        size = human_size(path.stat().st_size) if path.exists() else "?"
        lines.append(f"- {path} ({size})")
    return "Пользователь прислал файл(ы):\n" + "\n".join(lines)


def _attachment_question(paths: list[Path], short: bool = False) -> str:
    """Вопрос для вложения без подписи: не угадывать задачу."""
    if len(paths) == 1:
        path = paths[0]
        kind = guess_kind(path)
        if short:
            names = {
                "photo": "изображение",
                "video": "видео",
                "audio": "аудио",
            }
            if kind in names:
                return f"Я получила {names[kind]}. Что с ним сделать?"
            return f"Я получила файл «{path.name}». Что с ним сделать?"
        if kind == "photo":
            return (
                "Я получила изображение, но не поняла задачу. Что с ним "
                "сделать — описать, обработать, распознать текст или "
                "что-то другое?"
            )
        if kind == "video":
            return (
                "Я получила видео, но не поняла задачу. Сделать кружок, "
                "обрезать, извлечь звук или что-то другое?"
            )
        if kind == "audio":
            return (
                "Я получила аудио, но не поняла задачу. Распознать, "
                "обработать или что-то другое?"
            )
        return (
            f"Я получила файл «{path.name}», но не поняла задачу. "
            "Прочитать, обработать, отправить обратно или что-то другое?"
        )
    names = ", ".join(path.name for path in paths[:5])
    return (
        f"Я получила файлы: {names}. Что с ними сделать? "
        "Опиши задачу одной фразой."
    )


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg не найден в PATH")
    return exe


def media_duration(path: Path) -> float:
    """Длительность медиафайла, сек (0 — если не удалось определить)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return 0.0
    try:
        proc = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(proc.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def convert_to_video_note(src: Path, dst: Path) -> None:
    """Готовит видео-кружок: квадрат 512×512, не длиннее минуты.

    Telegram принимает video_note только с соотношением 1:1 и длительностью
    до 60 с, поэтому вертикальные и широкие ролики обрезаются по центру.
    """
    cmd = [
        _ffmpeg(),
        "-y",
        "-i",
        str(src),
        "-t",
        "59",
        "-vf",
        "scale=512:512:force_original_aspect_ratio=increase,crop=512:512",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not dst.exists():
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("; ".join(tail) or "ffmpeg вернул ошибку")


def _pcm_16k(path: Path) -> bytes:
    """Декодирует аудио в PCM int16 16 кГц моно для локального STT."""
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-v",
            "error",
            "-i",
            str(path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
        raise RuntimeError("; ".join(tail) or "ffmpeg не смог прочитать аудио")
    return proc.stdout


def telegram_status() -> dict[str, Any]:
    """Состояние бота для GUI: работает ли и под каким именем."""
    bot = _active_bot
    if bot is None or not bot.running:
        return {"running": False, "username": "", "error": _last_error}
    return {"running": True, "username": bot.username, "error": ""}


_last_error = ""


# ------------------------------------------------------------ прогресс
class _Progress:
    """Одно редактируемое сообщение: «Думаю…», инструменты, хвост ответа."""

    def __init__(self, bot: Any, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id: int | None = None
        self._base = ""
        self._notes: list[str] = []
        self._tail = ""
        self._last = 0.0

    def _render(self) -> str:
        parts = [self._base, *self._notes]
        if self._tail:
            parts.append("…" + self._tail[-700:])
        text = "\n".join(p for p in parts if p)
        return text[-3900:]

    async def start(self, text: str) -> None:
        self._base = text
        try:
            message = await self.bot.send_message(self.chat_id, text)
            self.message_id = message.message_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: не удалось отправить статус: {exc}")

    async def note(self, text: str) -> None:
        self._notes.append(text)
        self._notes = self._notes[-6:]
        await self._flush()

    async def tail(self, text: str) -> None:
        self._tail = (text or "").strip()
        await self._flush()

    async def _flush(self, force: bool = False) -> None:
        if self.message_id is None:
            return
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL:
            return
        self._last = now
        try:
            await self.bot.edit_message_text(
                self._render(), chat_id=self.chat_id, message_id=self.message_id
            )
        except Exception:  # noqa: BLE001
            pass  # «message is not modified» и гонки правок — не повод падать

    async def close(self) -> None:
        if self.message_id is None:
            return
        try:
            await self.bot.delete_message(self.chat_id, self.message_id)
        except Exception:  # noqa: BLE001
            pass
        self.message_id = None

    async def fail(self, text: str) -> None:
        if self.message_id is None:
            await start_message(self.bot, self.chat_id, text)
            return
        try:
            await self.bot.edit_message_text(
                f"❌ {text}"[:3900], chat_id=self.chat_id, message_id=self.message_id
            )
        except Exception:  # noqa: BLE001
            await start_message(self.bot, self.chat_id, f"❌ {text}")


async def start_message(
    bot: Any,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    reply_markup: Any | None = None,
) -> None:
    """Отправляет ответ с форматированием Telegram и разбивкой по лимиту.

    Если Telegram не принял разметку (например, модель выдала странный тег),
    тот же текст уходит обычным сообщением — без падения задачи.
    Клавиатура ставится на последний чанк, чтобы не дублироваться.
    """
    chunks = html_chunks(text) or ["…"]
    for index, chunk in enumerate(chunks):
        kwargs = {
            "reply_to_message_id": reply_to if index == 0 else None,
            "allow_sending_without_reply": True,
            "reply_markup": reply_markup if index == len(chunks) - 1 else None,
        }
        try:
            await bot.send_message(chat_id, chunk, parse_mode="HTML", **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Telegram: HTML не принят ({exc}) — отправляю текстом")
            try:
                await bot.send_message(chat_id, strip_html(chunk), **kwargs)
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    f"Telegram: не удалось отправить сообщение: {exc2}"
                )
                return


# ------------------------------------------------------------------- бот
class TelegramBot:
    """Long polling бот Немуси и мост «Telegram ↔ агент»."""

    def __init__(self, config: Config, core: Any) -> None:
        self.config = config
        self.core = core
        self.cfg = config.telegram
        if self.cfg.max_download_mb > MAX_DOWNLOAD_MB:
            logger.warning(
                "Telegram: max_download_mb=%s больше лимита Bot API (%s МБ) — "
                "использую %s МБ.",
                self.cfg.max_download_mb,
                MAX_DOWNLOAD_MB,
                MAX_DOWNLOAD_MB,
            )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._start_error = ""
        self._pending: dict[str, tuple[asyncio.Future, str]] = {}
        self._task_lock: asyncio.Lock | None = None
        #: сериализует приём текста/медиа, чтобы вложение и подпись не разъехались
        self._inbox_lock: asyncio.Lock | None = None
        self._chat_id: int | None = None
        #: автор текущей задачи — подтверждение привязано к чату и человеку
        self._user_id: int | None = None
        #: сообщение текущей задачи — на него ставится реакция
        self._message_id: int | None = None
        #: агент уже поставил реакцию — второй раз не ставим
        self._reaction_set = False
        #: вложение без подписи по чатам: текст, изображения, время
        self._pending_attachments: dict[int, tuple[str, list[str], float]] = {}
        #: чат для фоновых уведомлений (напоминания): не сбрасывается после
        #: ответа, в отличие от _chat_id текущей задачи
        self._notify_chat_id: int | None = (
            self.cfg.allowed_users[0] if self.cfg.allowed_users else None
        )
        self._stt: Any = None
        self._stt_lock = threading.Lock()
        #: имена скиллов для маршрутизации /имя (лениво, один раз)
        self._skill_names: set[str] | None = None
        #: файл-замок, чтобы GUI и служба не запускали двух ботов с одним токеном
        self._lock_handle: Any = None
        self.username = ""
        self.started_at = 0.0
        memory = getattr(core, "telegram_memory", None)
        if memory is None:
            store = getattr(core, "telegram_memory_store", None)
            if callable(store):
                try:
                    memory = store()
                except Exception:  # noqa: BLE001
                    logger.exception("Telegram: не удалось открыть telegram.db")
        self.memory = memory
        self.agent = Agent(
            config=config,
            llm=core.llm,
            registry=core.registry,
            permissions=core.permissions,
            confirm=self._confirm,
            memory=self.memory,
            system_suffix=self._system_suffix(),
        )

    @property
    def _hints_enabled(self) -> bool:
        """Включены ли подсказки глобально."""
        return bool(getattr(self.cfg, "hints_enabled", True))

    def _keyboard(self, rows: list[list[tuple[str, str]]]) -> Any | None:
        """Собирает inline-клавиатуру из пар «подпись, callback»."""
        if not rows:
            return None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(label, callback_data=data)
                    for label, data in row
                ]
                for row in rows
            ]
        )

    def _session_key(self, chat_id: int) -> str:
        """Ключ meta с session_id для конкретного Telegram-чата."""
        return f"telegram_session:{chat_id}"

    def _prepare_session(self, chat_id: int) -> None:
        """Переключает агента на сохранённую сессию чата (или создаёт её)."""
        if self.memory is None:
            return
        key = self._session_key(chat_id)
        raw = self.memory.get_meta(key)
        try:
            session_id = int(raw) if raw else 0
        except (TypeError, ValueError):
            session_id = 0
        if session_id:
            try:
                self.agent.switch_session(session_id)
                return
            except ValueError:
                logger.info(
                    "Telegram: сессия %s для чата %s не найдена — создаю новую",
                    session_id,
                    chat_id,
                )
        session_id = self.agent.new_session(f"Telegram {chat_id}")
        self.memory.set_meta(key, str(session_id))

    def _new_session_for_chat(self, chat_id: int) -> None:
        """/new: новый диалог только для этого чата."""
        if self.memory is None:
            self.agent.reset()
            return
        session_id = self.agent.new_session(f"Telegram {chat_id}")
        self.memory.set_meta(self._session_key(chat_id), str(session_id))

    # ----------------------------------------------------------- публичное
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._app)

    @property
    def healthy(self) -> bool:
        """Живы ли поток бота и polling, а не только объект приложения.

        ``running`` проверяет поток и ``Application``, но updater может
        остановиться сам после фатальной ошибки сети. Для headless-режима
        это сигнал перезапустить процесс.
        """
        if not self.running or self._start_error:
            return False
        try:
            updater = getattr(self._app, "updater", None)
            return bool(updater is None or updater.running)
        except Exception:  # noqa: BLE001
            return False

    @property
    def current_chat_id(self) -> int | None:
        """Чат, из которого пришла текущая задача (для отправки файлов)."""
        return self._chat_id

    @property
    def notify_chat_id(self) -> int | None:
        """Постоянный чат для фоновых уведомлений и напоминаний."""
        return self._notify_chat_id

    def _chat_target(self, chat_id: int | None = None) -> int | None:
        """Куда отправить: явный чат → текущая задача → постоянный чат."""
        return chat_id or self._chat_id or self._notify_chat_id

    def _remember_pending_attachment(
        self, chat_id: int, text: str, images: list[str], at: float | None = None
    ) -> None:
        """Запоминает вложение без подписи для конкретного чата."""
        self._pending_attachments[chat_id] = (
            text,
            list(images),
            time.time() if at is None else at,
        )

    def _peek_pending_attachment(self, chat_id: int) -> tuple[str, list[str]]:
        """Смотрит недавнее вложение чата, не забирая его."""
        item = self._pending_attachments.get(chat_id)
        if item is None:
            return "", []
        text, images, at = item
        if time.time() - at > ATTACHMENT_CONTEXT_TTL:
            self._pending_attachments.pop(chat_id, None)
            return "", []
        return text, list(images)

    def _take_pending_attachment(self, chat_id: int) -> tuple[str, list[str]]:
        """Забирает недавнее вложение без подписи: текст и сами изображения."""
        text, images = self._peek_pending_attachment(chat_id)
        self._pending_attachments.pop(chat_id, None)
        return text, images

    def _clear_pending_attachments(self, chat_id: int | None = None) -> None:
        """Сбрасывает вложения без подписи: все или только одного чата."""
        if chat_id is None:
            self._pending_attachments.clear()
        else:
            self._pending_attachments.pop(chat_id, None)

    def react_current(self, emoji: str) -> tuple[bool, str]:
        """Ставит реакцию на сообщение текущей задачи из любого потока."""
        if not self.running or self._loop is None:
            return False, "Telegram-бот не запущен — поставить реакцию нельзя."
        chat_id = self._chat_id
        message_id = self._message_id
        if not chat_id or not message_id:
            return False, (
                "Реакция доступна только во время задачи из Telegram."
            )
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._react(chat_id, message_id, emoji), self._loop
            )
            ok = bool(future.result(timeout=15))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: не удалось поставить реакцию {emoji}: {exc}")
            return False, f"Не удалось поставить реакцию {emoji}: {exc}"
        if not ok:
            return False, f"Telegram не принял реакцию {emoji}."
        self._reaction_set = True
        return True, f"Реакция {emoji} поставлена."

    async def _auto_react(
        self, text: str, answer: str, chat_id: int, message_id: int
    ) -> None:
        """Выбирает и ставит реакцию фоном, не задерживая ответ."""
        try:
            emoji = await asyncio.to_thread(self._choose_reaction, text, answer)
            if emoji and await self._react(chat_id, message_id, emoji):
                logger.info(f"Telegram: реакция {emoji} поставлена по смыслу.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: реакция не поставлена: {exc}")

    def _choose_reaction(self, text: str, answer: str = "") -> str:
        """Просит ИИ выбрать реакцию на сообщение (пустая строка — не ставить)."""
        body = f"Сообщение пользователя:\n{text[:2000]}"
        if answer:
            body += f"\n\nОтвет ассистента (для контекста):\n{answer[:1500]}"
        try:
            response = self.core.llm.complete(
                [
                    Message(role="system", content=_REACTION_SYSTEM),
                    Message(role="user", content=body),
                ],
                model=getattr(self.config.llm, "fast_model", "") or None,
                attempts=1,
                budget=15.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: не удалось выбрать реакцию: {exc}")
            return ""
        emoji = _reaction_from_reply(response.content or "")
        if emoji:
            logger.info(f"Telegram: ИИ выбрал реакцию {emoji}")
        else:
            logger.debug("Telegram: реакция не выбрана")
        return emoji

    def start(self, wait: bool = True) -> bool:
        """Поднимает бота в фоне.

        ``wait=False`` — не держать запуск приложения: поток сам сообщит об
        ошибке в лог и в :func:`telegram_status`.
        """
        global _active_bot, _last_error
        if self.running:
            return True
        _last_error = ""
        token = (self.cfg.token or "").strip()
        if not self.cfg.enabled:
            return False
        if not token:
            _last_error = "не задан токен бота"
            logger.warning("Telegram: бот включён, но токен не задан.")
            return False
        if not self.cfg.allowed_users:
            _last_error = "не задан ID пользователя"
            logger.warning(
                "Telegram: бот не запущен — не указан ни один ID пользователя "
                "(иначе команды смог бы отдавать любой человек)."
            )
            return False
        if not self._acquire_lock():
            _last_error = (
                "бот уже запущен в другом процессе (GUI или автозапуск) — "
                "останови один из них"
            )
            logger.warning(f"Telegram: {_last_error}")
            return False
        self._ready.clear()
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="telegram-bot", daemon=True
        )
        self._thread.start()
        if not wait:
            return True
        if not self._ready.wait(20):
            _last_error = self._start_error or "бот не ответил за 20 с"
            logger.error(f"Telegram: {_last_error}")
            # Поток мог зависнуть на сетевом вызове и ожить после таймаута.
            # Если не подать стоп-сигнал, он начнёт поллинг уже без нашего
            # присмотра (ссылка потеряна, замок снят) — получится зомби,
            # который конфликтует с новым ботом за getUpdates (409).
            self._stopping.set()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
            self._thread = None
            self._release_lock()
            return False
        if self._start_error or not self.running:
            _last_error = self._start_error or "бот не запустился"
            logger.error(f"Telegram: {_last_error}")
            self._stopping.set()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
            self._thread = None
            self._release_lock()
            return False
        _active_bot = self
        logger.info(f"Telegram: бот @{self.username} слушает сообщения.")
        return True

    def stop(self) -> None:
        """Останавливает polling и закрывает поток бота."""
        global _active_bot, _last_error
        self._stopping.set()
        _last_error = ""  # старое сообщение об ошибке больше не актуально
        if self._loop is None or self._app is None:
            _active_bot = None if _active_bot is self else _active_bot
            self._release_lock()
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            future.result(timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: остановка бота: {exc}")
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
        if _active_bot is self:
            _active_bot = None
        self._thread = None
        self._loop = None
        self._app = None
        self._release_lock()
        logger.info("Telegram: бот остановлен.")

    def send_text(self, text: str, chat_id: int | None = None) -> bool:
        """Отправляет текст в чат из любого потока (напоминания, уведомления).

        Длинный текст режется на части лимитом Telegram — один send_message
        на 10 000 символов падает с ошибкой, и уведомление теряется.
        """
        if not self.running or self._loop is None or self._app is None:
            return False
        target = self._chat_target(chat_id)
        if target is None:
            return False
        chunks = html_chunks(text) or ["…"]

        async def _send() -> None:
            for chunk in chunks:
                try:
                    await self._app.bot.send_message(
                        chat_id=target, text=chunk, parse_mode="HTML"
                    )
                except Exception:  # noqa: BLE001
                    await self._app.bot.send_message(
                        chat_id=target, text=strip_html(chunk)
                    )

        try:
            future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
            future.result(timeout=30)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: не удалось отправить текст: {exc}")
            return False

    def send_file(
        self,
        path: str | Path,
        kind: str = "auto",
        caption: str = "",
        chat_id: int | None = None,
    ) -> tuple[bool, str]:
        """Отправляет файл в чат из любого потока (вызывает инструмент)."""
        if not self.running or self._loop is None:
            return False, "Telegram-бот не запущен."
        target = self._chat_target(chat_id)
        if target is None:
            return False, "Отправка в Telegram доступна только из чата с ботом."
        file_path = Path(str(path)).expanduser()
        if not file_path.is_absolute():
            file_path = Path(self.config.tools.workspace) / file_path
        if not file_path.exists():
            return False, f"Файл не найден: {file_path}"
        if not file_path.is_file():
            return False, f"Это не файл: {file_path}"
        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > SEND_LIMIT_MB:
            return False, (
                f"Файл {size_mb:.1f} МБ — Telegram Bot API принимает до "
                f"{SEND_LIMIT_MB} МБ. Сожмите или отправьте ссылку."
            )
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_file(target, file_path, kind, caption), self._loop
            )
            return future.result(timeout=300)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: отправка файла: {exc}")
            return False, f"Не удалось отправить файл: {exc}"

    # ------------------------------------------------------------- замок
    def _lock_path(self) -> Path:
        """Файл-замок рядом с базой: один токен — один polling-процесс."""
        return Path(self.config.memory.database).parent / "telegram.lock"

    def _acquire_lock(self) -> bool:
        """Захватывает межпроцессный замок (Windows msvcrt / POSIX flock).

        Telegram допускает только один getUpdates на токен: если GUI и
        автозапуск поднимут бота одновременно, второй получит 409 и будет
        молча простаивать. Замок снимается ОС при падении процесса, поэтому
        «вечных» блокировок не остаётся.
        """
        if self._lock_handle is not None:
            return True
        handle: Any = None
        try:
            path = self._lock_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
            handle.seek(0)
            handle.write(" ")
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            return False
        except Exception:  # noqa: BLE001
            logger.exception("Telegram: не удалось создать файл-замок")
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            return False
        self._lock_handle = handle
        return True

    def _release_lock(self) -> None:
        """Освобождает замок и закрывает файл (вызывается из stop/ошибок)."""
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass

    # ---------------------------------------------------------- поток/цикл
    def _build_application(self) -> Any:
        """Собирает Application с таймаутами, достаточными для видео."""
        from telegram.ext import ApplicationBuilder

        builder = (
            ApplicationBuilder()
            .token(self.cfg.token)
            # Подтверждения — отдельные callback-апдейты: при строгой
            # последовательной обработке они ждут конца задачи (до 300 с).
            .concurrent_updates(4)
            .connect_timeout(10)
            .read_timeout(30)
            .write_timeout(120)
            .pool_timeout(5)
        )
        # В PTB 21 этого метода ещё нет, в 22 — есть. Без него большие
        # медиафайлы живут на дефолтном таймауте и падают с TimedOut.
        media_timeout = getattr(builder, "media_write_timeout", None)
        if callable(media_timeout):
            builder = media_timeout(120)
        return builder.build()

    def _thread_main(self) -> None:
        global _active_bot, _last_error
        try:
            from telegram.ext import (
                CallbackQueryHandler,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"нет пакета python-telegram-bot: {exc}"
            self._release_lock()
            self._ready.set()
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._app = self._build_application()
            self._task_lock = asyncio.Lock()
            self._inbox_lock = asyncio.Lock()
            self._app.add_handler(CommandHandler("start", self._cmd_start))
            self._app.add_handler(CommandHandler("help", self._cmd_help))
            self._app.add_handler(CommandHandler("new", self._cmd_new))
            self._app.add_handler(CommandHandler("status", self._cmd_status))
            self._app.add_handler(CommandHandler("id", self._cmd_id))
            self._app.add_handler(CommandHandler("stop", self._cmd_stop))
            self._app.add_handler(CallbackQueryHandler(self._on_callback))
            self._app.add_handler(
                MessageHandler(
                    filters.PHOTO
                    | filters.Document.ALL
                    | filters.VIDEO
                    | filters.ANIMATION
                    | filters.AUDIO
                    | filters.VOICE
                    | filters.VIDEO_NOTE,
                    self._on_media,
                )
            )
            self._app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text)
            )
            self._app.add_handler(MessageHandler(filters.COMMAND, self._cmd_unknown))

            self._loop.run_until_complete(self._app.initialize())
            self._loop.run_until_complete(self._app.start())
            self._loop.run_until_complete(self._app.updater.start_polling(
                drop_pending_updates=False
            ))
            me = self._loop.run_until_complete(self._app.bot.get_me())
            self.username = me.username or ""
            self.started_at = time.monotonic()
            self._loop.run_until_complete(self._set_commands())
            global _active_bot
            _active_bot = self
            self._ready.set()
            if self._stopping.is_set():
                self._loop.run_until_complete(self._shutdown())
                self._close_loop()
                return
            # Сторож: если stop() (или таймаут старта) пришёл после проверки
            # выше, цикл остановится сам — иначе поток ожил бы зомби и поллил
            # без присмотра, конфликтуя с новым ботом за getUpdates (409).
            self._loop.create_task(self._stop_watchdog())
            self._loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            # В тексте InvalidToken лежит сам токен — в лог и статус только ***
            safe = self._mask(str(exc))
            _last_error = safe
            self._start_error = safe
            logger.error(f"Telegram: не удалось запустить бота: {safe}")
            self._close_loop()
            self._release_lock()
            self._ready.set()
            return
        finally:
            if not self._ready.is_set():
                self._ready.set()

    def _mask(self, text: str) -> str:
        """Убирает токен из текста ошибки, чтобы он не попал в лог и GUI."""
        token = (self.cfg.token or "").strip()
        return str(text).replace(token, "***") if token else str(text)

    def _close_loop(self) -> None:
        """Закрывает цикл и висящие задачи (иначе предупреждения при выходе)."""
        loop = self._loop
        self._loop = None
        self._app = None
        if loop is not None and not loop.is_closed():
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    async def _stop_watchdog(self) -> None:
        """Гасит бота, если стоп-сигнал пришёл уже после проверки в старте.

        ``start()`` после таймаута ставит ``_stopping`` и уходит, не дожидаясь
        зависшего потока. Без сторожа поток мог бы ожить и поллить вечно.
        """
        while not self._stopping.is_set():
            await asyncio.sleep(0.25)
        try:
            await self._shutdown()
        finally:
            loop = self._loop
            if loop is not None:
                loop.stop()

    async def _shutdown(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._app.stop()
            await self._app.shutdown()
        except Exception:  # noqa: BLE001
            pass

    async def _set_commands(self) -> None:
        """Меню команд в Telegram (кнопка «/»)."""
        from telegram import BotCommand

        try:
            await self._app.bot.set_my_commands(
                [
                    BotCommand("start", "Начать работу"),
                    BotCommand("new", "Новый диалог"),
                    BotCommand("status", "Состояние"),
                    BotCommand("id", "Мой ID"),
                    BotCommand("help", "Помощь"),
                    BotCommand("stop", "Остановить бота"),
                ]
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Telegram: set_my_commands: {exc}")

    # -------------------------------------------------------------- доступ
    def _authorized(self, update: Any) -> bool:
        """Пускать только владельца и только в личку (если группы запрещены)."""
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if user is None or user.id not in self.cfg.allowed_users:
            logger.warning(
                f"Telegram: отклонён пользователь {getattr(user, 'id', '?')} "
                f"({getattr(user, 'username', '')})"
            )
            return False
        if chat is not None and chat.type != "private" and not self.cfg.allow_groups:
            logger.info("Telegram: сообщение из группы проигнорировано.")
            return False
        if chat is not None:
            # Запоминаем чат навсегда: напоминания приходят и после ответа,
            # когда _chat_id текущей задачи уже сброшен.
            self._notify_chat_id = chat.id
        return True

    async def _deny(self, update: Any) -> None:
        """Вежливый отказ с ID — владелец сможет добавить человека в настройках."""
        user = getattr(update, "effective_user", None)
        message = getattr(update, "effective_message", None)
        if message is None or user is None:
            return
        try:
            await message.reply_text(
                f"⛔ Доступ запрещён. Ваш ID: {user.id} — попросите владельца "
                "добавить его в настройках Немуси."
            )
        except Exception:  # noqa: BLE001
            pass

    def _names(self) -> tuple[str, str]:
        """Короткое и полное имя: «Немуся» и «Немесис» из конфига."""
        short = (self.config.assistant.name or "Немуся").strip()
        full = (self.config.assistant.full_name or short).strip()
        return short, full

    def _aliases(self) -> str:
        """Как к ней обращаться: «Немо или Немуся», без дублей."""
        short, full = self._names()
        names = [name for name in ("Немо", short) if name and name != full]
        return " или ".join(dict.fromkeys(names))

    def _system_suffix(self) -> str:
        workspace = Path(self.config.tools.workspace)
        inbox = workspace / "telegram"
        full = self._names()[1]
        return (
            "СЕЙЧАС ТЫ РАБОТАЕШЬ В TELEGRAM.\n"
            f"Тебя зовут {full}; пользователь зовёт тебя {self._aliases()}. "
            "Ты девушка — говори о себе в женском роде.\n"
            f"Сообщения приходят из чата Telegram, файлы пользователя лежат в "
            f"{inbox} (путь указывается в сообщении).\n"
            "Правила:\n"
            "1. Отвечай живо и по-человечески, как в переписке: коротко, без "
            "канцелярита и без «я не могу» — задачу выполняй инструментами.\n"
            "2. Оформляй ответ в Markdown, я превращаю его в форматирование "
            "Telegram: **жирный**, *курсив*, `код`, ```блок кода```, > цитата, "
            "||спойлер||, [текст](https://ссылка). HTML-теги не пиши. "
            "Цитатой выделяй главный вывод или важное предупреждение.\n"
            "3. Чтобы отправить пользователю файл, фото, видео или кружок, "
            "вызови инструмент send_telegram(path=..., caption=...). Для "
            "видео-кружка передай as=\"video_note\" — инструмент сам обрежет "
            "видео до квадрата и минуты.\n"
            "4. Результат сохраняй в файл и отправляй его, если текст длиннее "
            "нескольких абзацев; большие простыни в чат не пиши.\n"
            "5. Подтверждение опасных действий приходит кнопками в Telegram — "
            "не считай, что пользователя нет рядом.\n"
            "6. Голосовые сообщения уже распознаны в текст; файлы и картинки "
            "приходят как пути на диске.\n"
            "7. Реакция — это твоё отношение к сообщению, а не статус работы. "
            "Не ставь 👀, ✅ или ❌ как «приняла/работаю/готово»: для статуса "
            "есть «печатает…» и прогресс. Если реакция уместна, вызови "
            "react_telegram(emoji=...): анекдот или шутка — 🤣 или 😁, "
            "грустное — 😢 или 💔, хорошая новость — 🎉 или 🔥, вопрос — 🤔. "
            "Это только примеры: доступен весь список из описания инструмента, "
            "выбирай любой подходящий по смыслу. Не ставь реакцию на каждое "
            "сообщение; сомневаешься — не ставь.\n"
            "8. Уточнения — только брейнштормом (ПРАВИЛО БРЕЙНШТОРМА выше). "
            "Как только задаче нужен ответ пользователя — формат, место, "
            "поведение при ошибках, объём, — не задавай открытый вопрос "
            "одним списком. Одна строка «как поняла», затем 3–5 пронумерованных "
            "вопросов с вариантами A), B), C) и «D) Свой вариант»; в каждом "
            "вопросе ровно один «(рекомендую)». Панели в Telegram нет, поэтому "
            "добавь: «ответь коротко: 1A, 2B» — пользователь может написать "
            "это текстом или голосом. Получив «1A, 2B» (или «всё A»), сразу "
            "выполняй без второго круга вопросов; ответ «свой вариант» — "
            "прочитай текст после двоеточия.\n"
            "9. Голосовых ответов в Telegram нет: твой ответ приходит "
            "сообщением или файлом. Если просят «скажи / произнеси вслух» — "
            "просто напиши нужный текст; не предлагай запускать скрипты или "
            "библиотеки для звука и не обещай озвучку."
        )

    # ------------------------------------------------------------- команды
    async def _cmd_start(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            start_text(self._names()[1], self._aliases(), self._hints_enabled),
            reply_to=update.message.message_id,
            reply_markup=self._keyboard(main_menu_rows(self._hints_enabled)),
        )

    async def _cmd_help(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            help_text(self._hints_enabled),
            reply_to=update.message.message_id,
            reply_markup=self._keyboard(main_menu_rows(self._hints_enabled)),
        )

    async def _cmd_new(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if self._task_lock is not None and self._task_lock.locked():
            await update.message.reply_text(
                "⏳ Сначала дождись завершения текущей задачи — потом /new."
            )
            return
        self._clear_pending_attachments(update.effective_chat.id)
        self._new_session_for_chat(update.effective_chat.id)
        await update.message.reply_text("🧹 Контекст очищен — начинаем заново.")

    async def _cmd_id(self, update: Any, context: Any) -> None:
        user = update.effective_user
        await update.message.reply_text(
            f"Ваш ID: {user.id}\n"
            f"Username: @{user.username}" if user.username else f"Ваш ID: {user.id}"
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        uptime = time.monotonic() - (self.started_at or time.monotonic())
        hours, minutes = int(uptime // 3600), int(uptime % 3600 // 60)
        busy = "выполняю задачу" if self._chat_id else "свободна"
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            f"**Состояние**\n"
            f"• модель: `{self.core.llm.model}`\n"
            f"• провайдер: {self.core.llm.provider}\n"
            f"• бот: @{self.username or '?'}\n"
            f"• работаю: {hours} ч {minutes} мин\n"
            f"• сейчас: {busy}",
            reply_to=update.message.message_id,
        )

    async def _cmd_stop(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        await update.message.reply_text(
            "Останавливаю бота. Включить снова можно галочкой в настройках Немуси."
        )
        threading.Thread(target=self.stop, name="telegram-stop", daemon=True).start()

    async def _cmd_unknown(self, update: Any, context: Any) -> None:
        """Неизвестная команда: команды скиллов уходят агенту, остальные — отказ.

        Telegram считает командой любое сообщение с ``/``, поэтому раньше
        ``/i-have-adhd`` и ``/pptx-design-styles`` до агента не доходили и
        отвечали «не знаю такой команды». Теперь известные имена скиллов
        передаются в обычную задачу: агент подгрузит инструкцию и выполнит её.
        """
        if not self._authorized(update):
            await self._deny(update)
            return
        text = (update.message.text or "").strip()
        if text and await asyncio.to_thread(self._is_skill_command, text):
            await self._run_task(update, text)
            return
        await update.message.reply_text(
            "Не знаю такой команды. Список: /start, /new, /status, /id, /help."
        )

    def _is_skill_command(self, text: str) -> bool:
        """Первый токен — имя установленного скилла (``/имя``)?"""
        token = text.lstrip().split(maxsplit=1)[0].lstrip("/").strip().lower()
        if not token:
            return False
        if self._skill_names is None:
            try:
                from nemus.core.skills import load_skills

                cfg = getattr(self.config, "skills", None)
                skills = load_skills(
                    getattr(cfg, "dir", ".agents/skills"),
                    disabled=getattr(cfg, "disabled", ()) if cfg else (),
                )
                self._skill_names = {skill.name.lower() for skill in skills}
            except OSError as exc:
                logger.warning(f"Telegram: не удалось прочитать скиллы: {exc}")
                self._skill_names = set()
        return token in self._skill_names

    # ------------------------------------------------------------- приём
    async def _on_text(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if self._inbox_lock is None:
            await self._handle_text(update)
            return
        async with self._inbox_lock:
            await self._handle_text(update)

    async def _handle_text(self, update: Any) -> None:
        text = (update.message.text or "").strip()
        if text:
            chat_id = update.effective_chat.id
            pending, pending_images = self._take_pending_attachment(chat_id)
            if pending:
                text = f"{pending}\n\nЗадача пользователя: {text}"
            await self._run_task(update, text, images=pending_images)

    async def _on_media(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if self._inbox_lock is None:
            await self._handle_media(update)
            return
        async with self._inbox_lock:
            await self._handle_media(update)

    async def _handle_media(self, update: Any) -> None:
        message = update.message
        chat_id = update.effective_chat.id
        await self._app.bot.send_chat_action(chat_id, "typing")
        saved: list[Path] = []
        voice_text = ""
        try:
            if message.voice is not None:
                path = await self._download(chat_id, message.voice, "voice.ogg")
                saved.append(path)
                if self.cfg.transcribe_voice:
                    notice = await message.reply_text("🎧 Распознаю голосовое…")
                    try:
                        voice_text = await asyncio.to_thread(
                            self._transcribe, path
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Telegram: голосовое не распознано: {exc}")
                        await notice.edit_text(
                            "🎧 Не удалось распознать голосовое. "
                            "Проверьте интернет и ключи Gemini/OpenAI или "
                            "пришлите текстом."
                        )
                        # Не проваливаемся в «вложение без подписи»: иначе
                        # после ошибки STT бот спросит, что делать с аудио.
                        return
                    else:
                        if voice_text:
                            await notice.edit_text(f"🗣 «{voice_text}»")
                        else:
                            await notice.edit_text(
                                "🎧 Речь не разобрана — пришлите задачу текстом."
                            )
                            return
            for attachment, default in self._attachments(message):
                saved.append(await self._download(chat_id, attachment, default))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: вложение не скачано: {exc}")
            await message.reply_text(f"Не удалось скачать файл: {exc}")
            return

        image_urls = [url for path in saved if (url := _image_data_url(path))]
        caption = (message.caption or "").strip()
        if saved and not caption and not voice_text:
            # Вложение без подписи — это не задача. Не отдаём его агенту:
            # иначе модель домысливает действие (например, делает чиби).
            # Запоминаем путь и само изображение: следующая задача будет
            # знать, о чём речь, и модель действительно увидит фото.
            self._remember_pending_attachment(
                chat_id, _attachment_context(saved), image_urls
            )
            rows = (
                attachment_action_rows(guess_kind(saved[0]))
                if len(saved) == 1 and self._hints_enabled
                else []
            )
            await message.reply_text(
                _attachment_question(saved, short=bool(rows)),
                reply_markup=self._keyboard(rows),
            )
            return
        parts: list[str] = []
        if voice_text:
            parts.append(voice_text)
        if caption:
            parts.append(caption)
        images: list[str] = []
        if saved:
            pending, pending_images = self._take_pending_attachment(chat_id)
            images.extend(pending_images)
            if pending:
                parts.append(pending)
            parts.append(_attachment_context(saved))
        images.extend(image_urls)
        text = "\n\n".join(parts).strip()
        if not text:
            await message.reply_text(
                "В этом сообщении нет текста или файла, который я умею принять."
            )
            return
        await self._run_task(update, text, images=images)

    def _attachments(self, message: Any) -> list[tuple[Any, str]]:
        """Пары «вложение → имя по умолчанию» для всех типов сообщений."""
        items: list[tuple[Any, str]] = []
        if message.document is not None:
            items.append((message.document, message.document.file_name or "file"))
        if message.video is not None:
            items.append((message.video, message.video.file_name or "video.mp4"))
        if message.video_note is not None:
            items.append((message.video_note, "circle.mp4"))
        if message.animation is not None:
            items.append(
                (message.animation, message.animation.file_name or "animation.mp4")
            )
        if message.audio is not None:
            items.append((message.audio, message.audio.file_name or "audio.mp3"))
        if message.photo:
            items.append((message.photo[-1], "photo.jpg"))
        return items

    async def _download(self, chat_id: int, attachment: Any, default: str) -> Path:
        """Скачивает вложение в рабочую папку и возвращает путь."""
        size = int(getattr(attachment, "file_size", 0) or 0)
        limit_mb = min(int(self.cfg.max_download_mb or MAX_DOWNLOAD_MB), MAX_DOWNLOAD_MB)
        if size and size > limit_mb * 1024 * 1024:
            raise RuntimeError(
                f"файл {human_size(size)} больше лимита {limit_mb} МБ "
                f"(Telegram Bot API отдаёт файлы до {MAX_DOWNLOAD_MB} МБ)"
            )
        name = safe_filename(
            getattr(attachment, "file_name", "") or default, fallback=default
        )
        folder = Path(self.config.tools.workspace) / "telegram" / str(chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{int(time.time())}_{name}"
        file = await attachment.get_file()
        await file.download_to_drive(custom_path=path)
        logger.info(f"Telegram: файл сохранён {path} ({human_size(size)})")
        return path

    def _transcribe(self, path: Path) -> str:
        """Распознаёт голосовое: облако OpenAI, при сбое — локальный Whisper.

        Облако включено в ``engine: auto`` и не требует ffmpeg/скачивания
        модели. Если ключа нет или запрос не удался, локальный движок остаётся
        последним рубежом — голосовое всё равно будет расшифровано.
        """
        from nemus.voice.stt import (
            GeminiTranscriber,
            OpenAITranscriber,
            STTEngine,
            build_hotwords,
        )

        stt_cfg = self.config.voice.stt
        engine = str(getattr(stt_cfg, "engine", "auto") or "auto").lower()
        hotwords = build_hotwords(self.config)
        providers = self.config.llm.providers
        gemini = providers.get("gemini")
        openai = providers.get("openai")

        if engine in ("auto", "gemini"):
            if gemini is not None and gemini.api_key:
                try:
                    return GeminiTranscriber(
                        api_key=gemini.api_key,
                        model=getattr(
                            stt_cfg, "gemini_model", "gemini-3.5-transcribe"
                        ),
                        language=stt_cfg.language,
                        prompt=hotwords,
                    ).transcribe(path)
                except Exception as exc:  # noqa: BLE001
                    if engine == "gemini":
                        raise
                    logger.warning(
                        f"Gemini STT недоступен ({exc}); пробую следующий движок"
                    )
            elif engine == "gemini":
                raise RuntimeError(
                    "Распознавание Gemini выбрано, но ключ llm.providers.gemini "
                    "не задан."
                )

        if engine in ("auto", "openai"):
            if openai is not None and openai.api_key:
                try:
                    return OpenAITranscriber(
                        api_key=openai.api_key,
                        model=getattr(stt_cfg, "cloud_model", ""),
                        language=stt_cfg.language,
                        prompt=hotwords,
                        api_base=openai.api_base or "",
                    ).transcribe(path)
                except Exception as exc:  # noqa: BLE001
                    if engine == "openai":
                        raise
                    logger.warning(
                        f"OpenAI STT недоступен ({exc}); перехожу на локальный "
                        "faster-whisper"
                    )
            elif engine == "openai":
                raise RuntimeError(
                    "Распознавание OpenAI выбрано, но ключ llm.providers.openai "
                    "не задан."
                )

        with self._stt_lock:
            if self._stt is None:
                self._stt = STTEngine(
                    model=stt_cfg.model,
                    language=stt_cfg.language,
                    device=stt_cfg.device,
                    beam_size=stt_cfg.beam_size,
                    hotwords=hotwords,
                )
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "Локальный faster-whisper требует numpy: установи numpy или "
                "включи облачное распознавание (voice.stt.engine: gemini/openai)."
            ) from exc
        pcm = _pcm_16k(path)
        audio = np.frombuffer(pcm, dtype=np.int16)
        return self._stt.transcribe(audio)

    # ------------------------------------------------------------ задача
    async def _react(
        self, chat_id: int, message_id: int | None, emoji: str
    ) -> bool:
        """Ставит реакцию на сообщение — как живой человек, если чат разрешает."""
        if not message_id:
            return False
        try:
            from telegram import ReactionTypeEmoji

            await self._app.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: реакция {emoji} не поставлена: {exc}")
            return False

    async def _keep_typing(self, chat_id: int) -> None:
        """Держит статус «печатает…», пока агент думает и работает.

        Telegram сбрасывает действие через ~5 секунд, поэтому обновляем его
        каждые 4 секунды. Задача отменяется, когда ответ отправлен.
        """
        while True:
            try:
                await self._app.bot.send_chat_action(chat_id, "typing")
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(4.0)

    async def _run_task(
        self, update: Any, text: str, images: list[str] | None = None
    ) -> None:
        await self._run_task_for(
            chat_id=update.effective_chat.id,
            user_id=getattr(update.effective_user, "id", None),
            message_id=getattr(update.message, "message_id", None),
            text=text,
            images=images,
        )

    async def _run_task_for(
        self,
        chat_id: int,
        user_id: int | None,
        message_id: int | None,
        text: str,
        images: list[str] | None = None,
    ) -> None:
        if self._task_lock is None:
            self._task_lock = asyncio.Lock()
        if self._task_lock.locked():
            await self._app.bot.send_message(
                chat_id,
                "⏳ Ещё выполняю предыдущую задачу — это сообщение в очереди.",
            )
        async with self._task_lock:
            try:
                self._prepare_session(chat_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Telegram: не удалось переключить сессию чата %s", chat_id
                )
            self._chat_id = chat_id
            self._user_id = user_id
            self._message_id = message_id
            self._reaction_set = False
            self._notify_chat_id = chat_id
            progress = _Progress(self._app.bot, chat_id)
            await progress.start("⏳ Думаю…")
            typing = asyncio.create_task(self._keep_typing(chat_id))
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def push(event: Any) -> None:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:
                    pass  # бота остановили посреди задачи

            def work() -> None:
                try:
                    for event in self.agent.run(text, images=images):
                        push(event)
                except Exception as exc:  # noqa: BLE001
                    push(AgentEvent(kind="error", error=str(exc)))
                finally:
                    push(None)

            threading.Thread(
                target=work, name="telegram-agent", daemon=True
            ).start()

            final_text = ""
            error = ""
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    if event.kind == "assistant_delta":
                        final_text += event.text
                        await progress.tail(final_text)
                    elif event.kind == "assistant_text":
                        final_text = final_text or event.text
                    elif event.kind == "tool_call":
                        await progress.note(f"🔧 {event.tool_name}")
                    elif event.kind == "tool_result":
                        if event.result is not None and not event.result.ok:
                            detail = event.result.content.replace("\n", " ")[:160]
                            await progress.note(f"⚠️ {event.tool_name}: {detail}")
                    elif event.kind == "final":
                        final_text = event.text or final_text
                    elif event.kind == "error":
                        error = event.error or event.text
            finally:
                typing.cancel()
                try:
                    await typing
                except asyncio.CancelledError:
                    pass
                self._chat_id = None
                self._user_id = None
                self._message_id = None

            if error:
                await progress.fail(error[:3000])
                return
            final_text = (final_text or "").strip()
            if not final_text:
                await progress.fail(
                    "Пустой ответ модели. Попробуйте переформулировать задачу."
                )
                return
            await progress.close()
            await start_message(
                self._app.bot, chat_id, final_text, reply_to=message_id
            )
            # Агент в общем наборе инструментов часто отвечает эмодзи в тексте,
            # а не вызывает react_telegram. Спрашиваем отдельно и ставим сами,
            # только если агент уже не поставил реакцию.
            if message_id and not self._reaction_set:
                asyncio.create_task(
                    self._auto_react(text, final_text, chat_id, message_id)
                )

    # -------------------------------------------------------- подтверждение
    def _confirm(self, tool: Any, args: dict[str, Any]) -> bool:
        """Спрашивает разрешение кнопками (вызывается из потока агента)."""
        chat_id = self._chat_id
        user_id = self._user_id
        if chat_id is None or user_id is None or self._loop is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._ask_confirm(chat_id, user_id, tool, args), self._loop
            )
            return bool(future.result(timeout=CONFIRM_TIMEOUT + 30))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: подтверждение не получено: {exc}")
            return False

    async def _ask_confirm(
        self, chat_id: int, user_id: int, tool: Any, args: dict[str, Any]
    ) -> bool:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        # Ключ привязан к чату и автору: чужую кнопку нельзя нажать из
        # другого чата или под другой учёткой, даже если она ещё жива.
        key = f"{chat_id}:{user_id}:{time.time_ns()}"
        future: asyncio.Future = self._loop.create_future()
        detail = json.dumps(args, ensure_ascii=False, default=str)
        if len(detail) > 2500:
            detail = detail[:2500] + "…"
        text = f"⚠️ Разрешить действие?\n\n{tool.name}\n{detail}"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Разрешить", callback_data=f"tg:{key}:y"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"tg:{key}:n"),
                ]
            ]
        )
        message = await self._app.bot.send_message(
            chat_id, text[:3900], reply_markup=keyboard
        )
        self._pending[key] = (future, text[:3500])
        try:
            return bool(await asyncio.wait_for(future, timeout=CONFIRM_TIMEOUT))
        except asyncio.TimeoutError:
            await self._app.bot.send_message(
                chat_id, "⌛ Время на подтверждение истекло — действие отменено."
            )
            try:
                await message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return False
        finally:
            self._pending.pop(key, None)

    async def _on_ux_callback(self, update: Any, query: Any) -> None:
        """Кнопки подсказок: действия вложений, помощь и примеры."""
        action = (query.data or "")[3:]
        if action.startswith("attach:"):
            key = action.split(":", 1)[1]
            prompt = attachment_prompt(key)
            chat_id = query.message.chat_id
            if prompt is None:
                await query.answer("Неизвестное действие")
                return
            pending, images = self._take_pending_attachment(chat_id)
            if not pending:
                await query.answer(
                    "Вложение уже не актуально — пришли его ещё раз",
                    show_alert=True,
                )
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
                return
            await query.answer("Запускаю")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            text = f"{pending}\n\nЗадача пользователя: {prompt}"
            await self._run_task_for(
                chat_id=chat_id,
                user_id=getattr(query.from_user, "id", None),
                message_id=getattr(query.message, "message_id", None),
                text=text,
                images=images,
            )
            return
        if action == "help":
            await query.answer()
            await start_message(
                self._app.bot,
                query.message.chat_id,
                help_text(self._hints_enabled),
                reply_markup=self._keyboard(main_menu_rows(self._hints_enabled)),
            )
            return
        if action == "examples":
            if not self._hints_enabled:
                await query.answer("Подсказки выключены в настройках", show_alert=True)
                return
            await query.answer()
            await start_message(
                self._app.bot,
                query.message.chat_id,
                examples_text(),
                reply_markup=self._keyboard(main_menu_rows(True)),
            )
            return
        await query.answer("Кнопка устарела")

    async def _on_callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        data = query.data or ""
        if data.startswith("ux:"):
            await self._on_ux_callback(update, query)
            return
        parts = data.split(":")
        if len(parts) < 5 or parts[0] != "tg":
            await query.answer()
            return
        key = ":".join(parts[1:-1])
        choice = parts[-1]
        chat_id = getattr(query.message, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(getattr(query.message, "chat", None), "id", None)
        user_id = getattr(query.from_user, "id", None)
        if not key.startswith(f"{chat_id}:{user_id}:"):
            await query.answer("Кнопка не для этого чата", show_alert=True)
            return
        entry = self._pending.get(key)
        if entry is None:
            await query.answer("Кнопка устарела")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return
        future, original = entry
        allowed = choice == "y"
        if not future.done():
            future.set_result(allowed)
        await query.answer("Разрешено" if allowed else "Отклонено")
        try:
            await query.edit_message_text(
                f"{original}\n\n"
                + ("✅ Разрешено" if allowed else "❌ Отклонено"),
                reply_markup=None,
            )
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- отправка
    async def _send_file(
        self, chat_id: int, path: Path, kind: str, caption: str
    ) -> tuple[bool, str]:
        """Отправляет файл, повторяя таймауты и падая в документ.

        Большие видео на медленном канале штатно упираются в write timeout.
        Один повтор часто проходит, а если нет — документ отправляется без
        серверной обработки видео и почти всегда доходит.
        """
        from telegram.error import NetworkError, TimedOut

        kind = (kind or "auto").strip().lower()
        if kind == "auto":
            kind = guess_kind(path)
        caption = (caption or "").strip()[:CAPTION_LIMIT]
        note_path: Path | None = None
        try:
            if kind == "video_note":
                note_path = path.with_name(f"{path.stem}_note.mp4")
                try:
                    await asyncio.to_thread(convert_to_video_note, path, note_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Telegram: видео-кружок не готов: {exc}")
                    return False, f"Не удалось отправить файл: {exc}"

            for attempt in (1, 2):
                try:
                    await self._send_once(chat_id, path, kind, caption, note_path)
                    return True, f"Отправлено в Telegram: {path.name}"
                except Exception as exc:  # noqa: BLE001
                    if attempt == 1 and isinstance(exc, (TimedOut, NetworkError)):
                        logger.warning(
                            "Telegram: файл %s не отправлен (попытка 1/2): %s — "
                            "повторяю через секунду",
                            path.name,
                            exc,
                        )
                        await asyncio.sleep(1.0)
                        continue

                    if kind != "document":
                        # Видео/фото/аудио после таймаута часто проходит
                        # документом: меньше серверной обработки.
                        try:
                            await self._send_once(
                                chat_id, path, "document", caption, note_path
                            )
                            logger.info(
                                "Telegram: %s отправлен документом после ошибки: %s",
                                path.name,
                                exc,
                            )
                            return True, (
                                f"Отправлено документом в Telegram: {path.name}"
                            )
                        except Exception as fallback_exc:  # noqa: BLE001
                            logger.warning(
                                "Telegram: файл %s не отправлен даже документом: %s",
                                path.name,
                                fallback_exc,
                            )
                            return False, (
                                f"Не удалось отправить файл: {fallback_exc}"
                            )

                    logger.warning(f"Telegram: файл {path} не отправлен: {exc}")
                    return False, f"Не удалось отправить файл: {exc}"
        finally:
            if note_path is not None:
                try:
                    note_path.unlink()
                except OSError:
                    pass

    async def _send_once(
        self,
        chat_id: int,
        path: Path,
        kind: str,
        caption: str,
        note_path: Path | None,
    ) -> None:
        from telegram import InputFile

        if kind == "video_note":
            if note_path is None:
                raise RuntimeError("видео-кружок не подготовлен")
            duration = int(max(1, min(59, media_duration(note_path))))
            with note_path.open("rb") as note:
                await self._app.bot.send_video_note(
                    chat_id,
                    video_note=InputFile(note, filename="note.mp4"),
                    duration=duration,
                    length=512,
                )
            return

        with path.open("rb") as fh:
            if kind == "photo":
                await self._app.bot.send_photo(
                    chat_id,
                    photo=InputFile(fh, filename=path.name),
                    caption=caption,
                )
            elif kind == "video":
                await self._app.bot.send_video(
                    chat_id,
                    video=InputFile(fh, filename=path.name),
                    caption=caption,
                    supports_streaming=True,
                )
            elif kind in ("audio", "voice"):
                await self._app.bot.send_audio(
                    chat_id,
                    audio=InputFile(fh, filename=path.name),
                    caption=caption,
                )
            else:
                await self._app.bot.send_document(
                    chat_id,
                    document=InputFile(fh, filename=path.name),
                    caption=caption,
                )


__all__ = [
    "CAPTION_LIMIT",
    "CONFIRM_TIMEOUT",
    "SEND_LIMIT_MB",
    "TEXT_LIMIT",
    "TelegramBot",
    "active_bot",
    "convert_to_video_note",
    "guess_kind",
    "human_size",
    "media_duration",
    "safe_filename",
    "split_message",
    "telegram_status",
]
