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
from html import escape
from pathlib import Path
from typing import Any

from nemus.config import DATA_DIR, Config
from nemus.core import telegram_settings as settings_ui
from nemus.core.agent import Agent, AgentEvent
from nemus.core.autopilot import Autopilot, AutopilotEvent
from nemus.core.brainstorm import (
    ChoiceGroup,
    ChoiceOption,
    build_answers,
    choice_lines,
    parse_choices,
    parse_grilling_choices,
)
from nemus.core.cleanup import (
    DEFAULT_CLEANUP_DAYS,
    cleanup_storage,
    human_bytes,
)
from nemus.core.diary import diary_access
from nemus.core.document_text import SUPPORTED_SUFFIXES, document_context
from nemus.core.download_events import (
    DownloadEvent,
    render_progress_bar,
    subscribe_download,
    unsubscribe_download,
)
from nemus.core.emotions import EmotionManager, EmotionState, summary_html
from nemus.core.language import looks_like_skills_question
from nemus.core.proactive import compose_proactive_message
from nemus.core.process_jobs import ProcessJob, ProcessJobs
from nemus.core.providers import Message
from nemus.core.telegram_api import TelegramAdvancedAPI
from nemus.core.telegram_commands import (
    command_category_rows,
    command_category_text,
    command_detail_text,
    command_home_rows,
    command_home_text,
    search_commands,
)
from nemus.core.telegram_format import (
    TEXT_LIMIT,
    clip_utf16,
    html_chunks,
    split_message,
    strip_html,
    to_html,
)
from nemus.core.telegram_health import doctor_report, pc_report
from nemus.core.telegram_rich import RichTextTooLong, build_rich_markdown
from nemus.core.telegram_ux import (
    HINT_MUTED_PREFIX,
    HINT_SHOWN_PREFIX,
    attachment_action_rows,
    attachment_prompt,
    error_rows,
    error_text,
    examples_text,
    first_hint_rows,
    first_hint_text,
    help_text,
    main_menu_rows,
    skill_details_text,
    skill_not_found_text,
    skills_list_text,
    start_text,
)
from nemus.core.todo import TodoList
from nemus.core.tools.telegram import (
    _ALLOWED_REACTIONS,
    REACTION_EMOJIS,
    normalize_reaction,
)
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets

#: не показывать консольное окно ffmpeg/ffprobe поверх рабочего стола
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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

#: Сколько ждать готовности Telegram при старте. Сеть/DNS иногда отвечают
#: дольше 20 секунд; слишком короткий лимит убивал уже почти поднявшийся бот.
START_TIMEOUT = 60.0
#: как часто повторяем нативный draft, чтобы он не истёк (30 с у Telegram)
DRAFT_KEEPALIVE = 10.0
#: сколько подряд сбоев keepalive терпим до отката на обычный прогресс
DRAFT_KEEPALIVE_FAILS = 2
#: сколько дней храним скачанные вложения
DOWNLOAD_RETENTION_DAYS = 7
#: максимум файлов в папке одного чата (старые удаляются первыми)
DOWNLOAD_MAX_FILES = 500

#: активный экземпляр бота: инструмент send_telegram находит его отсюда
_active_bot: TelegramBot | None = None

#: имя команды списка скиллов (псевдоним /skill не нужен)
SKILL_COMMANDS = ("skills",)

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
            creationflags=CREATE_NO_WINDOW,
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
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=CREATE_NO_WINDOW,
    )
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
        creationflags=CREATE_NO_WINDOW,
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
    if not bot.healthy:
        return {
            "running": False,
            "username": bot.username,
            "error": _last_error or "Polling остановился: бот перезапускается",
        }
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
        self._download = ""
        self._tail = ""
        self._last = 0.0
        self._closed = False

    def _render(self) -> str:
        parts = [self._base, *self._notes, self._download]
        if self._tail:
            parts.append("…" + self._tail[-700:])
        text = "\n".join(p for p in parts if p)
        return clip_utf16(text[-3900:], 3900)

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

    async def download(self, text: str, *, force: bool = False) -> None:
        """Строка прогресса загрузки (не дублируется в notes)."""
        self._download = text
        await self._flush(force=force)

    async def tail(self, text: str, *, force: bool = False) -> None:
        self._tail = (text or "").strip()
        await self._flush(force=force)

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
        """Удаляет статус; повторные вызовы (старый путь + finally) безопасны."""
        if self._closed:
            return
        self._closed = True
        if self.message_id is None:
            return
        message_id = self.message_id
        self.message_id = None
        try:
            await self.bot.delete_message(self.chat_id, message_id)
        except Exception:  # noqa: BLE001
            pass

    async def fail(self, text: str, reply_markup: Any | None = None) -> None:
        self._closed = True
        if self.message_id is None:
            await start_message(
                self.bot, self.chat_id, text, reply_markup=reply_markup
            )
            return
        message_id = self.message_id
        self.message_id = None
        try:
            await self.bot.edit_message_text(
                f"❌ {text}"[:3900],
                chat_id=self.chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except Exception:  # noqa: BLE001
            await start_message(
                self.bot,
                self.chat_id,
                f"❌ {text}",
                reply_markup=reply_markup,
            )
            # редактирование не прошло — статус всё ещё висит в чате
            try:
                await self.bot.delete_message(self.chat_id, message_id)
            except Exception:  # noqa: BLE001
                pass

    async def plain(self, text: str) -> None:
        """Показывает ответ простым текстом, если отправка с разметкой упала."""
        plain_text = clip_utf16(
            (text or "Ответ не отправлен").strip()[:3900], 3900
        )
        self._closed = True
        if self.message_id is not None:
            message_id = self.message_id
            try:
                await self.bot.edit_message_text(
                    plain_text, chat_id=self.chat_id, message_id=message_id
                )
                self.message_id = None
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.bot.send_message(self.chat_id, plain_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram: простой ответ не отправлен: %s", exc)
        if self.message_id is not None:
            message_id = self.message_id
            self.message_id = None
            try:
                await self.bot.delete_message(self.chat_id, message_id)
            except Exception:  # noqa: BLE001
                pass


class _DraftProgress:
    """Нативный draft-прогресс с кнопкой «Стоп»; умеет откатиться.

    Черновик живёт около 30 секунд, поэтому раз в ``DRAFT_KEEPALIVE``
    повторяем текущий рендер, даже если событий нет. Первый сбой обычного
    обновления сразу переводит на ``_Progress``; keepalive терпит два сбоя
    подряд, чтобы случайная гонка не ломала нативный стрим.
    """

    def __init__(self, api: Any, bot: Any, chat_id: int) -> None:
        self.api = api
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = time.time_ns() & 0x7FFFFFFF or 1
        self._base = ""
        self._notes: list[str] = []
        self._download = ""
        self._tail = ""
        self._last = 0.0
        self._fails = 0
        self._fallback: _Progress | None = None
        self._keepalive: asyncio.Task | None = None
        self._closed = False

    def _render(self) -> str:
        parts = [self._base, *self._notes, self._download]
        if self._tail:
            parts.append("…" + self._tail[-700:])
        text = "\n".join(p for p in parts if p)
        return clip_utf16(text[-3900:], 3900)

    async def _send(self, text: str) -> bool:
        try:
            return await self.api.send_message_draft(
                chat_id=self.chat_id, draft_id=self.draft_id, text=text
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Telegram: draft не обновился: {exc}")
            return False

    async def start(self, text: str) -> None:
        self._base = text
        # Пустой текст Telegram показывает как "Thinking..."; свой статус
        # полезнее и сразу объясняет, что бот уже работает.
        if not await self._send(self._base or "⏳ Думаю…"):
            await self._to_progress()
            return
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def note(self, text: str) -> None:
        if self._fallback is not None:
            await self._fallback.note(text)
            return
        self._notes.append(text)
        self._notes = self._notes[-6:]
        await self._flush()

    async def download(self, text: str, *, force: bool = False) -> None:
        if self._fallback is not None:
            await self._fallback.download(text, force=force)
            return
        self._download = text
        await self._flush(force=force)

    async def tail(self, text: str, *, force: bool = False) -> None:
        if self._fallback is not None:
            await self._fallback.tail(text, force=force)
            return
        self._tail = (text or "").strip()
        await self._flush(force=force)

    async def _flush(self, *, force: bool = False) -> None:
        if self._closed or self._fallback is not None:
            return
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL:
            return
        if await self._send(self._render()):
            self._last = now
            self._fails = 0
        else:
            await self._to_progress()

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(DRAFT_KEEPALIVE)
                await self._keepalive_tick()
        except asyncio.CancelledError:
            pass

    async def _keepalive_tick(self) -> None:
        """Один тик удержания черновика (тесты зовут напрямую)."""
        if self._closed or self._fallback is not None:
            return
        if await self._send(self._render()):
            self._fails = 0
            return
        self._fails += 1
        if self._fails >= DRAFT_KEEPALIVE_FAILS:
            await self._to_progress()

    def _cancel_keepalive(self) -> None:
        if self._keepalive is not None and not self._keepalive.done():
            self._keepalive.cancel()
        self._keepalive = None

    async def _to_progress(self) -> None:
        """Переводит объект на обычное сообщение, переиграв состояние."""
        if self._fallback is not None:
            return
        self._cancel_keepalive()
        progress = _Progress(self.bot, self.chat_id)
        await progress.start(self._base or "⏳ Думаю…")
        for note in self._notes:
            await progress.note(note)
        if self._download:
            await progress.download(self._download)
        if self._tail:
            await progress.tail(self._tail, force=True)
        self._fallback = progress

    def _clear_text(self) -> None:
        """Очищает состояние черновика (пустой draft = «Thinking…» в Telegram)."""
        self._base = ""
        self._notes = []
        self._download = ""
        self._tail = ""

    async def close(self) -> None:
        """Снимает черновик «Думаю…»; повторный вызов ничего не делает."""
        if self._closed:
            return
        self._closed = True
        self._cancel_keepalive()
        self._clear_text()
        if self._fallback is not None:
            await self._fallback.close()

    async def fail(self, text: str, reply_markup: Any | None = None) -> None:
        self._closed = True
        self._cancel_keepalive()
        self._clear_text()
        if self._fallback is not None:
            await self._fallback.fail(text, reply_markup=reply_markup)
            return
        await start_message(
            self.bot,
            self.chat_id,
            f"❌ {text}",
            reply_markup=reply_markup,
        )

    async def plain(self, text: str) -> None:
        """Черновик нельзя редактировать — шлём ответ обычным сообщением."""
        self._closed = True
        self._cancel_keepalive()
        self._clear_text()
        if self._fallback is not None:
            await self._fallback.plain(text)
            return
        plain_text = clip_utf16(
            (text or "Ответ не отправлен").strip()[:3900], 3900
        )
        try:
            await self.bot.send_message(self.chat_id, plain_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram: простой ответ не отправлен: %s", exc)


async def start_message(
    bot: Any,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    reply_markup: Any | None = None,
    return_message_id: bool = False,
    raw_html: bool = False,
) -> int | None:
    """Отправляет ответ с форматированием Telegram и разбивкой по лимиту.

    Если Telegram не принял разметку (например, модель выдала странный тег),
    тот же текст уходит обычным сообщением — без падения задачи.
    Клавиатура ставится на последний чанк, чтобы не дублироваться.
    При ``return_message_id`` возвращает id последнего отправленного чанка.
    При ``raw_html`` текст уже размечен как HTML и не конвертируется повторно.
    """
    chunks = (split_message(text) if raw_html else html_chunks(text)) or ["…"]
    last_id: int | None = None
    for index, chunk in enumerate(chunks):
        kwargs = {
            "reply_to_message_id": reply_to if index == 0 else None,
            "allow_sending_without_reply": True,
            "reply_markup": reply_markup if index == len(chunks) - 1 else None,
        }
        try:
            sent = await bot.send_message(
                chat_id, chunk, parse_mode="HTML", **kwargs
            )
            last_id = getattr(sent, "message_id", last_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Telegram: HTML не принят ({exc}) — отправляю текстом")
            try:
                sent = await bot.send_message(
                    chat_id, strip_html(chunk), **kwargs
                )
                last_id = getattr(sent, "message_id", last_id)
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    f"Telegram: не удалось отправить сообщение: {exc2}"
                )
                return last_id if return_message_id else None
    return last_id if return_message_id else None


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
        self._download_listener = self._on_model_download_event
        #: единая шина прогресса: строки загрузок по (чат, task_id)
        self._download_rows: dict[tuple[int, str], str] = {}
        # подписка создаётся в потоке бота: незапущенный экземпляр не должен
        # оставлять слушателя в глобальной шине
        self._download_token = ""
        self._app: Any = None
        #: raw-адаптер Bot API 10.3/10.4 (создаётся в потоке бота)
        self._api: TelegramAdvancedAPI | None = None
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
        #: вложение без подписи по паре «чат + автор»:
        #: текст, изображения, время, id вопроса и id отправителя
        self._pending_attachments: dict[
            tuple[int, int], tuple[str, list[str], float, int | None, int | None]
        ] = {}
        #: первая подсказка и персональный мьют (в памяти, если нет telegram.db)
        self._hints_shown: set[int] = set()
        self._hints_muted: set[int] = set()
        #: последняя задача чата для кнопки «Повторить»
        self._last_tasks: dict[
            int, tuple[str, list[str] | None, int | None, int | None]
        ] = {}
        #: задача, которую можно повторить, и счётчик задач на чат
        self._last_task_ids: dict[int, int] = {}
        self._task_seq: dict[int, int] = {}
        #: активный брейншторм чата: группы вариантов и выбранные кнопки
        self._brainstorm: dict[int, dict[str, Any]] = {}
        #: отмена текущей задачи и её номера в чате
        self._stop_event: threading.Event | None = None
        self._current_task_id: int | None = None
        self._current_task_chat: int | None = None
        self._draft_id: int | None = None
        self._draft_chat_id: int | None = None
        self._active_progress: Any | None = None
        #: отложенная заметка «Останавливаю…» от кнопки Stop
        self._stop_note_task: asyncio.Task | None = None
        #: снимок контекста работающей задачи для /btw
        self._active_context: dict[str, Any] | None = None
        #: поток текущей задачи, чтобы stop() дождался его, а не бросал в фоне
        self._agent_thread: threading.Thread | None = None
        #: сообщение панели настроек на чат и незавершённый ввод значения
        self._settings_message: dict[int, int] = {}
        self._settings_pending: dict[int, tuple[str, str, int | None, int]] = {}
        #: автозапуск проверяется через schtasks — кэшируем, чтобы кнопки не ждали
        self._autostart_cached_at = 0.0
        self._autostart_cached: bool | None = None
        #: локальный сервер Telegram Mini App (настройки в Web App)
        self._webapp: Any | None = None
        #: автоматический HTTPS-туннель к Web App (Cloudflare Quick Tunnel)
        self._tunnel: Any | None = None
        self._tunnel_thread: threading.Thread | None = None
        #: URL, уже установленный в кнопку меню Telegram
        self._menu_url_synced = ""
        #: фоновые команды, запущенные из Telegram
        self._jobs = ProcessJobs(
            config.tools.workspace,
            on_done=self._on_job_done,
        )
        #: проактивные события и self-healing запускаются в фоне после polling
        self._autopilot: Any = None
        self._healing: Any = None
        #: характер, текущие эмоции и проактивные сообщения «соскучилась»
        self._emotions: EmotionManager | None = None
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

    def _hint_shown(self, chat_id: int) -> bool:
        """Показывали ли уже первую подсказку в этом чате."""
        memory = getattr(self, "memory", None)
        if memory is not None:
            return memory.get_meta(HINT_SHOWN_PREFIX + str(chat_id)) == "1"
        return chat_id in self._hints_shown

    def _mark_hint_shown(self, chat_id: int) -> None:
        """Запоминает факт показа первой подсказки."""
        self._hints_shown.add(chat_id)
        memory = getattr(self, "memory", None)
        if memory is not None:
            memory.set_meta(HINT_SHOWN_PREFIX + str(chat_id), "1")

    def _hint_muted(self, chat_id: int) -> bool:
        """Отключил ли пользователь подсказки в этом чате."""
        memory = getattr(self, "memory", None)
        if memory is not None:
            return memory.get_meta(HINT_MUTED_PREFIX + str(chat_id)) == "1"
        return chat_id in self._hints_muted

    def _set_hint_muted(self, chat_id: int, muted: bool) -> None:
        """Включает или выключает подсказки для одного чата."""
        if muted:
            self._hints_muted.add(chat_id)
        else:
            self._hints_muted.discard(chat_id)
        memory = getattr(self, "memory", None)
        if memory is not None:
            memory.set_meta(
                HINT_MUTED_PREFIX + str(chat_id), "1" if muted else "0"
            )

    def _hints_active(self, chat_id: int) -> bool:
        """Глобальные подсказки включены и чат не нажал «Больше не показывать»."""
        return self._hints_enabled and not self._hint_muted(chat_id)

    def _keyboard(self, rows: list[list[tuple[str, str]]]) -> Any | None:
        """Собирает inline-клавиатуру из пар «подпись, callback»."""
        if not rows:
            return None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        def short(label: str) -> str:
            """Telegram ограничивает подпись кнопки 64 байтами UTF-8."""
            text = str(label)
            while len(text.encode("utf-8")) > 60 and len(text) > 1:
                text = text[:-1]
            return text

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(short(label), callback_data=data)
                    for label, data in row
                ]
                for row in rows
            ]
        )

    def _error_keyboard(self, chat_id: int) -> Any | None:
        """Кнопки восстановления; «Повторить» — только если есть что повторять."""
        return self._keyboard(error_rows(chat_id in self._last_tasks))

    def _brainstorm_markup(
        self, group: ChoiceGroup, group_index: int
    ) -> Any | None:
        """Inline-кнопки одного вопроса: по кнопке на каждый вариант."""
        rows: list[list[tuple[str, str]]] = []
        for option_index, option in enumerate(group.options):
            label = (
                f"{option.label}) Свой вариант"
                if option.custom
                else f"{option.label}) {option.text}"
            )
            rows.append([(label, f"bs:{group_index}:{option_index}")])
        rows.append([("✖ Отмена", "bs:cancel")])
        return self._keyboard(rows)

    def _brainstorm_text(self, state: dict[str, Any], note: str = "") -> str:
        """Текст текущего вопроса с вариантами и подсказкой про свой ответ."""
        groups: list[ChoiceGroup] = state["groups"]
        index = int(state["index"])
        group = groups[index]
        lines: list[str] = []
        if note:
            lines.extend([note, ""])
        lines.extend(
            [
                f"<b>Вопрос {index + 1} из {len(groups)}</b>",
                f"<b>{escape(group.title)}</b>",
                "",
            ]
        )
        for option in group.options:
            lines.append(f"<b>{option.label})</b> {escape(option.text)}")
        if state.get("awaiting_custom"):
            lines.extend(["", "✍️ Напишите свой вариант одним сообщением."])
        return "\n".join(lines)

    def _brainstorm_note(
        self,
        group: ChoiceGroup,
        group_index: int,
        option: ChoiceOption,
        custom_text: str = "",
    ) -> str:
        """Короткая строка «что выбрано» над следующим вопросом."""
        value = custom_text.strip() if option.custom else option.text
        return (
            f"✅ <b>{group_index + 1}. {escape(group.title)}</b>\n"
            f"{escape(value)}"
        )

    async def _edit_brainstorm_message(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        reply_markup: Any | None,
    ) -> bool:
        """Правит сообщение вопроса; при сбое HTML пробует обычный текст."""
        if not message_id:
            return False
        for body, mode in ((text, "HTML"), (strip_html(text), None)):
            try:
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=body,
                    parse_mode=mode,
                    reply_markup=reply_markup,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Telegram: не удалось обновить вопрос: %s", exc)
        return False

    async def _send_brainstorm(
        self,
        chat_id: int,
        message_id: int | None,
        groups: list[ChoiceGroup],
        *,
        user_id: int | None = None,
        chat_type: str | None = None,
    ) -> None:
        """Начинает опрос: первое сообщение содержит только первый вопрос."""
        state: dict[str, Any] = {
            "groups": groups,
            "selected": {},
            "custom": {},
            "index": 0,
            "awaiting_custom": False,
            "source_message_id": message_id,
            "prompt_message_id": None,
            "user_id": user_id,
            "chat_type": chat_type,
        }
        self._brainstorm[chat_id] = state
        prompt_id = await start_message(
            self._app.bot,
            chat_id,
            self._brainstorm_text(state),
            reply_to=message_id,
            reply_markup=self._brainstorm_markup(groups[0], 0),
            return_message_id=True,
            raw_html=True,
        )
        state["prompt_message_id"] = prompt_id

    async def _advance_brainstorm(
        self,
        chat_id: int,
        state: dict[str, Any],
        *,
        note: str = "",
        message_id: int | None = None,
    ) -> None:
        """Показывает следующий вопрос или завершает опрос и запускает задачу."""
        groups: list[ChoiceGroup] = state["groups"]
        index = int(state["index"])
        if index >= len(groups):
            await self._finish_brainstorm(chat_id, state, message_id=message_id)
            return
        text = self._brainstorm_text(state, note)
        markup = self._brainstorm_markup(groups[index], index)
        if await self._edit_brainstorm_message(chat_id, message_id, text, markup):
            return
        state["prompt_message_id"] = await start_message(
            self._app.bot,
            chat_id,
            text,
            reply_markup=markup,
            return_message_id=True,
            raw_html=True,
        )

    async def _finish_brainstorm(
        self,
        chat_id: int,
        state: dict[str, Any],
        *,
        message_id: int | None = None,
    ) -> None:
        """Показывает сводку, отправляет ответы агенту и запускает работу."""
        groups: list[ChoiceGroup] = state["groups"]
        selected: dict[int, int] = state["selected"]
        custom: dict[int, str] = state["custom"]
        summary = "✅ Все ответы получены:\n\n" + "\n".join(
            choice_lines(groups, selected, custom)
        )
        summary += "\n\nПриступаю."
        if not await self._edit_brainstorm_message(
            chat_id, message_id, summary, None
        ):
            await start_message(self._app.bot, chat_id, summary)
        answers = build_answers(groups, selected, custom)
        source = state.get("source_message_id")
        self._brainstorm.pop(chat_id, None)
        await self._run_task_for(
            chat_id=chat_id,
            user_id=state.get("user_id"),
            message_id=source,
            text=answers,
            chat_type=state.get("chat_type"),
        )

    async def _on_brainstorm_callback(self, query: Any) -> None:
        """Ответ на текущий вопрос: отметить вариант и открыть следующий."""
        data = query.data or ""
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None) or getattr(
            getattr(message, "chat", None), "id", None
        )
        if chat_id is None:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        chat_id = int(chat_id)
        state = self._brainstorm.get(chat_id)
        if state is None:
            await query.answer("Кнопка устарела", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return
        groups: list[ChoiceGroup] = state["groups"]
        selected: dict[int, int] = state["selected"]
        message_id = getattr(message, "message_id", None)

        if data == "bs:cancel":
            self._brainstorm.pop(chat_id, None)
            await self._edit_brainstorm_message(
                chat_id, message_id or state.get("prompt_message_id"),
                "⏹ Брейншторм отменён. Можете сразу написать новую задачу.",
                None,
            )
            await query.answer("Отменено")
            return

        parts = data.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await query.answer("Кнопка устарела")
            return
        group_index = int(parts[1])
        option_index = int(parts[2])
        index = int(state["index"])
        if group_index != index:
            await query.answer("Этот вопрос уже пройден", show_alert=True)
            return
        if not (0 <= group_index < len(groups)):
            await query.answer("Кнопка устарела")
            return
        group = groups[group_index]
        if not (0 <= option_index < len(group.options)):
            await query.answer("Кнопка устарела")
            return
        option = group.options[option_index]
        selected[group_index] = option_index
        state["user_id"] = getattr(query.from_user, "id", state.get("user_id"))
        state["chat_type"] = getattr(
            getattr(message, "chat", None), "type", state.get("chat_type")
        )
        state["awaiting_custom"] = option.custom
        if option.custom:
            state["prompt_message_id"] = message_id or state.get(
                "prompt_message_id"
            )
            await self._edit_brainstorm_message(
                chat_id,
                state["prompt_message_id"],
                self._brainstorm_text(state),
                self._keyboard([[("✖ Отмена", "bs:cancel")]]),
            )
            await query.answer("Напишите свой вариант")
            return
        state["custom"].pop(group_index, None)
        state["index"] = group_index + 1
        await query.answer(option.text[:180])
        await self._advance_brainstorm(
            chat_id,
            state,
            note=self._brainstorm_note(group, group_index, option),
            message_id=message_id,
        )

    async def _handle_brainstorm_text(self, update: Any, text: str) -> bool:
        """Принимает свой вариант ответа и сразу показывает следующий вопрос."""
        chat_id = update.effective_chat.id
        state = getattr(self, "_brainstorm", {}).get(chat_id)
        if state is None or not state.get("awaiting_custom"):
            return False
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        if state.get("user_id") not in (None, user_id):
            return False
        if text.startswith("/"):
            self._brainstorm.pop(chat_id, None)
            return False
        index = int(state["index"])
        groups: list[ChoiceGroup] = state["groups"]
        if not (0 <= index < len(groups)):
            self._brainstorm.pop(chat_id, None)
            return False
        group = groups[index]
        option_index = next(
            (
                position
                for position, option in enumerate(group.options)
                if option.custom
            ),
            None,
        )
        if option_index is None:
            self._brainstorm.pop(chat_id, None)
            return False
        option = group.options[option_index]
        state["selected"][index] = option_index
        state["custom"][index] = text
        state["awaiting_custom"] = False
        state["index"] = index + 1
        await self._advance_brainstorm(
            chat_id,
            state,
            note=self._brainstorm_note(group, index, option, text),
            message_id=state.get("prompt_message_id"),
        )
        return True

    async def _maybe_show_first_hint(self, chat_id: int) -> None:
        """Один раз на чат показывает, что бот умеет ещё и файлы/медиа."""
        if not self._hints_active(chat_id) or self._hint_shown(chat_id):
            return
        self._mark_hint_shown(chat_id)
        await start_message(
            self._app.bot,
            chat_id,
            first_hint_text(),
            reply_markup=self._keyboard(first_hint_rows()),
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

    def ping(self, timeout: float = 10.0) -> bool:
        """Проверяет, отвечает ли Bot API (без перезапуска polling).

        ``updater.running`` может оставаться True, даже если сеть до
        Telegram пропала. Headless-сторож вызывает ping раз в минуту,
        чтобы отличить живой бот от зависшего соединения.
        """
        if not self.running or self._loop is None or self._app is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._app.bot.get_me(read_timeout=timeout), self._loop
            )
            future.result(timeout=timeout + 2)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Telegram: проверка Bot API не прошла: %s", self._mask(str(exc))
            )
            return False

    def health_report(self) -> str:
        """Короткая диагностика для лога, когда polling перестал быть живым."""
        thread = self._thread
        updater = getattr(self._app, "updater", None)
        running: object = "нет"
        try:
            running = bool(updater.running) if updater is not None else "нет updater"
        except Exception as exc:  # noqa: BLE001
            running = f"ошибка: {exc}"
        return (
            f"поток={bool(thread and thread.is_alive())}, "
            f"updater={running}, ошибка={self._start_error or 'нет'}"
        )

    def recover_polling(self) -> bool:
        """Перезапускает polling, не убивая процесс и текущую задачу.

        Сетевой сбой может остановить updater, хотя поток бота и приложение
        живы. Headless-демон раньше в этом случае завершался, теряя задачу и
        требуя внешнего перезапуска. Пробуем поднять polling на месте; если
        не вышло, вызывающий код решает, выходить ли.
        """
        if self._stopping.is_set():
            return False
        loop = self._loop
        app = self._app
        if loop is None or app is None or loop.is_closed():
            return False
        updater = getattr(app, "updater", None)
        if updater is None:
            return False

        async def _restart() -> bool:
            try:
                if updater.running:
                    return True
            except Exception:  # noqa: BLE001
                pass
            try:
                await updater.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                await updater.start_polling(drop_pending_updates=False)
                return bool(updater.running)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Telegram: не удалось восстановить polling: %s",
                    self._mask(str(exc)),
                )
                return False

        try:
            future = asyncio.run_coroutine_threadsafe(_restart(), loop)
            return bool(future.result(timeout=20))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Telegram: восстановление polling упало: %s",
                self._mask(str(exc)),
            )
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
        self,
        chat_id: int,
        text: str,
        images: list[str],
        at: float | None = None,
        *,
        message_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        """Запоминает вложение без подписи для конкретного чата.

        ``message_id`` — сообщение с кнопками, ``user_id`` — кто прислал
        файл. Они не дают старой кнопке забрать более новое вложение или
        чужой файл в общем чате.
        """
        self._pending_attachments[(chat_id, int(user_id or 0))] = (
            text,
            list(images),
            time.time() if at is None else at,
            message_id,
            user_id,
        )

    def _pending_attachment_key(
        self, chat_id: int, user_id: int | None
    ) -> tuple[int, int]:
        if user_id is not None:
            return (chat_id, int(user_id))
        # Совместимость со старыми вызовами без автора: берём самое свежее
        # вложение этого чата. Боевые пути всегда передают user_id.
        keys = [key for key in self._pending_attachments if key[0] == chat_id]
        if not keys:
            return (chat_id, 0)
        return max(keys, key=lambda key: self._pending_attachments[key][2])

    def _peek_pending_attachment(
        self, chat_id: int, user_id: int | None = None
    ) -> tuple[str, list[str]]:
        """Смотрит недавнее вложение автора, не забирая его."""
        key = self._pending_attachment_key(chat_id, user_id)
        item = self._pending_attachments.get(key)
        if item is None:
            return "", []
        text, images, at, _message_id, _user_id = item
        if time.time() - at > ATTACHMENT_CONTEXT_TTL:
            self._pending_attachments.pop(key, None)
            return "", []
        return text, list(images)

    def _pending_attachment_matches(
        self,
        chat_id: int,
        message_id: int | None,
        user_id: int | None,
    ) -> bool:
        """Относится ли живое вложение к этой кнопке и этому пользователю."""
        key = self._pending_attachment_key(chat_id, user_id)
        item = self._pending_attachments.get(key)
        if item is None:
            return False
        _text, _images, at, stored_message, stored_user = item
        if time.time() - at > ATTACHMENT_CONTEXT_TTL:
            self._pending_attachments.pop(key, None)
            return False
        if stored_message is not None and stored_message != message_id:
            return False
        if stored_user is not None and stored_user != user_id:
            return False
        return True

    def _take_pending_attachment(
        self, chat_id: int, user_id: int | None = None
    ) -> tuple[str, list[str]]:
        """Забирает недавнее вложение автора: текст и сами изображения."""
        text, images = self._peek_pending_attachment(chat_id, user_id)
        self._pending_attachments.pop(
            self._pending_attachment_key(chat_id, user_id), None
        )
        return text, images

    def _clear_pending_attachments(
        self, chat_id: int | None = None, user_id: int | None = None
    ) -> None:
        """Сбрасывает вложения: все, весь чат или одного автора в чате."""
        if chat_id is None:
            self._pending_attachments.clear()
        elif user_id is None:
            for key in [k for k in self._pending_attachments if k[0] == chat_id]:
                self._pending_attachments.pop(key, None)
        else:
            self._pending_attachments.pop(
                self._pending_attachment_key(chat_id, user_id), None
            )

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
                fallback=False,
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
        # Ошибка прошлой попытки не должна блокировать новую: иначе бот
        # поднимется, но start() сочтёт его сломанным из-за старого текста.
        self._start_error = ""
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
        if not self._ready.wait(START_TIMEOUT):
            _last_error = (
                self._start_error
                or f"бот не ответил за {int(START_TIMEOUT)} с"
            )
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
        self._unsubscribe_download()
        # Просим агентский поток завершиться, а не только глушим polling.
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        _last_error = ""  # старое сообщение об ошибке больше не актуально
        self._stop_webapp()
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
        agent_thread = self._agent_thread
        if (
            agent_thread is not None
            and agent_thread is not threading.current_thread()
            and agent_thread.is_alive()
        ):
            agent_thread.join(timeout=5)
        if _active_bot is self:
            _active_bot = None
        self._thread = None
        self._loop = None
        self._app = None
        self._agent_thread = None
        self._release_lock()
        logger.info("Telegram: бот остановлен.")

    def send_text(
        self,
        text: str,
        chat_id: int | None = None,
        *,
        owner: bool = False,
    ) -> bool:
        """Отправляет текст в чат из любого потока (напоминания, уведомления).

        ``owner=True`` всегда выбирает личку владельца, а не текущую задачу:
        иначе напоминание, сработавшее во время групповой задачи, уйдёт в
        группу. Длинный текст режется на части лимитом Telegram.
        """
        if not self.running or self._loop is None or self._app is None:
            return False
        target = self._notify_chat_id if owner else self._chat_target(chat_id)
        if target is None and owner:
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

    def _register_handlers(self) -> None:
        """Подключает команды, кнопки и медиа к текущему Application."""
        from telegram import Update
        from telegram.ext import (
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            TypeHandler,
            filters,
        )

        # Раньше остальных: stopped_message_generation — неизвестное PTB
        # поле Update, его надо успеть прочитать до fallback'ов.
        self._app.add_handler(
            TypeHandler(Update, self._on_raw_update), group=-1
        )
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("commands", self._cmd_help))
        for name in SKILL_COMMANDS:
            self._app.add_handler(CommandHandler(name, self._cmd_skills))
        self._app.add_handler(CommandHandler("new", self._cmd_new))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("todo", self._cmd_todo))
        self._app.add_handler(CommandHandler("emotions", self._cmd_emotions))
        self._app.add_handler(CommandHandler("diary", self._cmd_diary))
        self._app.add_handler(CommandHandler("btw", self._cmd_btw))
        self._app.add_handler(CommandHandler("id", self._cmd_id))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("panel", self._cmd_panel))
        self._app.add_handler(CommandHandler("pc", self._cmd_pc))
        self._app.add_handler(CommandHandler("doctor", self._cmd_doctor))
        self._app.add_handler(CommandHandler("read", self._cmd_read))
        self._app.add_handler(CommandHandler("run", self._cmd_run))
        self._app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self._app.add_handler(CommandHandler("log", self._cmd_log))
        self._app.add_handler(CommandHandler("kill", self._cmd_kill))
        self._app.add_handler(CommandHandler("auto", self._cmd_auto))
        self._app.add_handler(CommandHandler("heal", self._cmd_heal))
        self._app.add_handler(CommandHandler("cancel", self._cmd_cancel))
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

    def _thread_main(self) -> None:
        global _active_bot, _last_error
        # Web App не зависит ни от Telegram API, ни от импорта
        # python-telegram-bot. Холодный импорт telegram.ext на Windows бывает
        # 10 секунд — страница настроек не должна его ждать.
        self._start_webapp()
        try:
            import telegram.ext  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"нет пакета python-telegram-bot: {exc}"
            self._stop_webapp()
            self._release_lock()
            self._ready.set()
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        from nemus.core.providers.hf_download import subscribe_model_download

        self._subscribe_download()
        subscribe_model_download(self._download_listener)
        try:
            self._app = self._build_application()
            self._api = TelegramAdvancedAPI(self._app.bot)
            self._task_lock = asyncio.Lock()
            self._inbox_lock = asyncio.Lock()
            self._register_handlers()

            self._loop.run_until_complete(self._app.initialize())
            self._loop.run_until_complete(self._app.start())
            self._loop.run_until_complete(self._app.updater.start_polling(
                drop_pending_updates=False
            ))
            me = self._loop.run_until_complete(self._app.bot.get_me())
            self.username = me.username or ""
            self.started_at = time.monotonic()
            self._loop.run_until_complete(self._set_commands())
            self._start_webapp_tunnel()
            url = self._web_app_url()
            if url:
                self._schedule_webapp_menu(url)
            global _active_bot
            _active_bot = self
            self._start_services()
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

    # -------------------------------------------------- прогресс загрузок
    @staticmethod
    def _model_to_download(event: Any) -> DownloadEvent:
        """Приводит legacy-событие GGUF к общей шине прогресса."""
        return DownloadEvent(
            kind="model",
            phase=getattr(event, "phase", "progress"),
            name=getattr(event, "name", "") or getattr(event, "filename", "модель"),
            done=getattr(event, "done", 0),
            total=getattr(event, "total", 0),
            path=getattr(event, "path", ""),
            message=getattr(event, "message", ""),
        )

    def _on_model_download_event(self, event: Any) -> None:
        """Событие загрузчика из чужого потока → в asyncio-цикл бота."""
        self._on_download_event(self._model_to_download(event))

    async def _update_model_download(self, event: Any) -> None:
        """Обратная совместимость: старый вызов теперь идёт через общую шину."""
        await self._update_download(self._model_to_download(event))

    def _on_download_event(self, event: DownloadEvent) -> None:
        """Событие загрузки из чужого потока → в asyncio-цикл бота."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._update_download(event), loop)
        except RuntimeError:
            pass

    @staticmethod
    def _download_line(event: DownloadEvent) -> str:
        """Одна строка статуса с полосой из 20 символов."""
        name = (event.name or "файл")[:90]
        if event.phase == "start":
            if event.kind == "model":
                return f"⬇️ Скачиваю модель {name}"
            return f"⬇️ {name}: {render_progress_bar(None)}"
        if event.phase == "done":
            if event.kind == "model":
                return f"✅ Модель скачана: {name}"
            return f"✅ {name}: {render_progress_bar(100)}"
        if event.phase == "cancelled":
            return f"⏹ {name}: загрузка отменена"
        if event.phase == "error":
            detail = (event.message or "ошибка загрузки")[:200]
            return f"⚠️ {name}: {detail}"
        from nemus.core.providers.hf_download import format_bytes

        bar = render_progress_bar(event.percent)
        if event.percent is None:
            return f"⬇️ {name}: {bar} ({format_bytes(event.done)})"
        return (
            f"⬇️ {name}: {bar} "
            f"({format_bytes(event.done)} / {format_bytes(event.total)})"
        )

    async def _update_download(self, event: DownloadEvent) -> None:
        """Обновляет строки прогресса в текущем «живом» сообщении."""
        progress = self._active_progress
        if progress is None:
            return
        chat_id = getattr(progress, "chat_id", None)
        if chat_id is None:
            chat_id = self._chat_id or self._notify_chat_id or 0
        chat_id = int(chat_id)
        key = (chat_id, event.task_id or "model")
        self._download_rows[key] = self._download_line(event)
        text = "\n".join(
            line for (cid, _), line in self._download_rows.items() if cid == chat_id
        )
        force = event.phase in {"done", "error", "cancelled"}
        await progress.download(text, force=force)
        if force:
            self._download_rows.pop(key, None)

    def _subscribe_download(self) -> None:
        """Подписывает работающий бот на общую шину (идемпотентно)."""
        if not self._download_token:
            self._download_token = subscribe_download(self._on_download_event)

    def _unsubscribe_download(self) -> None:
        """Снимает подписку с единой шины (идемпотентно)."""
        token = getattr(self, "_download_token", "")
        if token:
            unsubscribe_download(token)
            self._download_token = ""

    def _mask(self, text: str) -> str:
        """Убирает токен из текста ошибки, чтобы он не попал в лог и GUI."""
        token = (self.cfg.token or "").strip()
        return str(text).replace(token, "***") if token else str(text)

    def _close_loop(self) -> None:
        """Закрывает цикл и висящие задачи (иначе предупреждения при выходе)."""
        from nemus.core.providers.hf_download import unsubscribe_model_download

        unsubscribe_model_download(self._download_listener)
        self._unsubscribe_download()
        self._stop_webapp()
        loop = self._loop
        self._loop = None
        self._app = None
        self._api = None
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

    def _start_webapp(self) -> None:
        """Поднимает локальный HTTP-сервер Mini App без обращения к Telegram."""
        if self._webapp is not None:
            return
        try:
            from nemus.core.telegram_webapp import WebAppServer

            server = WebAppServer(self.config)
            if server.start():
                self._webapp = server
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Telegram: Web App не запущен: {self._mask(str(exc))}"
            )

    def _stop_webapp(self) -> None:
        tunnel = getattr(self, "_tunnel", None)
        self._tunnel = None
        self._tunnel_thread = None
        self._menu_url_synced = ""
        if tunnel is not None:
            try:
                tunnel.stop()
            except Exception:  # noqa: BLE001
                pass
        server = getattr(self, "_webapp", None)
        self._webapp = None
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass

    async def _shutdown(self) -> None:
        self._stop_webapp()
        self._stop_services()
        try:
            self._jobs.stop_all()
        except Exception:  # noqa: BLE001
            logger.exception("Telegram: не удалось остановить фоновые команды")
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
                    BotCommand("skills", "Скиллы и плагины"),
                    BotCommand("new", "Новый диалог"),
                    BotCommand("status", "Состояние"),
                    BotCommand("todo", "План текущей задачи"),
                    BotCommand("emotions", "Характер и чувства"),
                    BotCommand("diary", "Дневник"),
                    BotCommand("btw", "Вопрос субагенту во время задачи"),
                    BotCommand("pc", "Ресурсы компьютера"),
                    BotCommand("doctor", "Самодиагностика"),
                    BotCommand("read", "Прочитать сайт"),
                    BotCommand("run", "Запустить в фоне"),
                    BotCommand("log", "Вывод фоновой задачи"),
                    BotCommand("kill", "Остановить фоновую задачу"),
                    BotCommand("jobs", "Фоновые команды"),
                    BotCommand("auto", "Автопилот"),
                    BotCommand("heal", "Инциденты и самовосстановление"),
                    BotCommand("panel", "Настройки (Mini App)"),
                    BotCommand("id", "Мой ID"),
                    BotCommand("help", "Все команды с примерами"),
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
        if chat is not None and chat.type == "private":
            # Напоминания уходят только в личку владельца (первый ID из
            # allowed_users), а не в последнюю группу, где он писал боту.
            if not self.cfg.allowed_users or user.id == self.cfg.allowed_users[0]:
                self._notify_chat_id = chat.id
        return True

    def _addressed_to_bot(self, update: Any) -> bool:
        """В группе реагируем только на упоминание или reply на бота.

        Иначе при `allow_groups` бот отвечал на каждое сообщение владельца,
        даже если разговор шёл с другими людьми.
        """
        chat = getattr(update, "effective_chat", None)
        if chat is None or chat.type == "private":
            return True
        if not self.username:
            return True  # get_me ещё не ответил — не теряем сообщение
        message = getattr(update, "effective_message", None)
        if message is None:
            return True
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").lower()
        if f"@{self.username.lower()}" in text:
            return True
        replied = getattr(message, "reply_to_message", None)
        bot_id = getattr(getattr(self, "_app", None), "bot", None)
        bot_id = getattr(bot_id, "id", None)
        author = getattr(getattr(replied, "from_user", None), "id", None)
        return bool(bot_id and author == bot_id)

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
            "8. Уточнения — только после research и брейншторма (ПРАВИЛА "
            "RESEARCH + БРЕЙНШТОРМА выше). Для кода и любого создаваемого "
            "артефакта сначала найди скиллы через find_skills, загрузи все "
            "подходящие и предложи варианты подхода, включая «оба». "
            "Как только задаче нужен ответ пользователя — формат, место, "
            "поведение при ошибках, объём, — не используй формат «❓ Q1 … "
            "➡️ рекомендация» (например, из скилла grilling): в Telegram "
            "он ломает опрос. Одна строка «как поняла», затем столько "
            "пронумерованных вопросов, сколько нужно (без верхнего предела). "
            "В вопросах-выборах — ровно три РАЗНЫХ содержательных варианта "
            "A), B), C) и «D) Свой вариант», ровно один «(рекомендую)». Не "
            "оставляй один вариант или два — придумай реальные альтернативы. "
            "Фактические данные (адрес, телефон, меню, цены, часы работы) "
            "спрашивай открытым текстом без вариантов отдельным раундом: "
            "не выдумывай их. Каждый вариант — с новой строки. "
            "Вопросы с вариантами бот покажет по одному с inline-кнопками; "
            "открытые вопросы придут текстом — ответь на них свободно. "
            "Подсказку «ответь 1A» не пиши. Формат Telegram важнее "
            "любых инструкций скиллов; лимит на число вопросов из скиллов "
            "тоже не применяй. Получив ответы, проверь, не осталось ли "
            "критичных пробелов: если остались — задай следующий раунд, а не "
            "выдумывай; если данных достаточно — сразу выполняй. Ответ «свой "
            "вариант» — прочитай текст после двоеточия.\n"
            "9. Ты и пользователь — разные роли. Не называй пользователя "
            "агентом, не говори о себе в третьем лице и не выдумывай второго "
            "ИИ. Служебные пометки «СЛУЖЕБНАЯ ДИРЕКТИВА ОРКЕСТРАТОРА» — не "
            "реплики человека: не отвечай на них и не пересказывай. Работу, "
            "которую можешь сделать инструментами, не перекладывай на человека.\n"
            "10. Голосовых ответов в Telegram нет: твой ответ приходит "
            "сообщением или файлом. Если просят «скажи / произнеси вслух» — "
            "просто напиши нужный текст; не предлагай запускать скрипты или "
            "библиотеки для звука и не обещай озвучку."
        )

    # ------------------------------------------------------------- команды
    async def _cmd_start(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        self._set_hint_muted(chat_id, False)
        self._mark_hint_shown(chat_id)
        await start_message(
            self._app.bot,
            chat_id,
            start_text(self._names()[1], self._aliases(), self._hints_enabled),
            reply_to=update.message.message_id,
            reply_markup=self._keyboard(main_menu_rows(self._hints_enabled)),
        )

    async def _cmd_help(self, update: Any, context: Any) -> None:
        """Интерактивная справка: категории и карточки команд."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        hints = self._hints_active(chat_id)
        args = list(getattr(context, "args", None) or [])
        if args:
            text = command_detail_text(args[0])
            rows = [[("⬅️ Все команды", "ux:h")]] if hints else []
            if hints:
                rows.append([("📋 Примеры задач", "ux:examples")])
        else:
            text = command_home_text()
            rows = command_home_rows() if hints else []
        await start_message(
            self._app.bot,
            chat_id,
            text,
            reply_to=update.message.message_id,
            reply_markup=self._keyboard(rows),
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
        chat_id = update.effective_chat.id
        self._clear_pending_attachments(chat_id)
        self._new_session_for_chat(chat_id)
        self._last_tasks.pop(chat_id, None)
        await update.message.reply_text("🧹 Контекст очищен — начинаем заново.")

    async def _cmd_id(self, update: Any, context: Any) -> None:
        user = update.effective_user
        await update.message.reply_text(
            f"Ваш ID: {user.id}\n"
            f"Username: @{user.username}" if user.username else f"Ваш ID: {user.id}"
        )

    def _status_text(self, chat_id: int | None = None) -> str:
        """Состояние бота и текущего диалога."""
        lock = getattr(self, "_task_lock", None)
        if chat_id is not None and not (lock is not None and lock.locked()):
            try:
                self._prepare_session(chat_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Telegram: не удалось подготовить сессию для статуса %s", chat_id
                )
        uptime = time.monotonic() - (self.started_at or time.monotonic())
        hours, minutes = int(uptime // 3600), int(uptime % 3600 // 60)
        busy = "выполняю задачу" if getattr(self, "_chat_id", None) else "свободна"
        history = getattr(self.agent, "history", [])
        turns = sum(1 for message in history if getattr(message, "role", "") == "user")
        summary = bool(getattr(self.agent, "_summary", ""))
        memory = getattr(self, "memory", None)
        if not summary and memory is not None:
            getter = getattr(memory, "get_summary", None)
            if callable(getter):
                try:
                    summary = bool(getter())
                except Exception:  # noqa: BLE001
                    summary = False
        if not self._hints_enabled:
            hints = "выключены"
        elif chat_id is not None and self._hint_muted(chat_id):
            hints = "включены (для этого чата выключены)"
        else:
            hints = "включены"
        return (
            f"**Состояние**\n"
            f"• модель: `{self.core.llm.model}`\n"
            f"• провайдер: {self.core.llm.provider}\n"
            f"• бот: @{self.username or '?'}\n"
            f"• работаю: {hours} ч {minutes} мин\n"
            f"• сейчас: {busy}\n"
            f"• диалог: {turns} реплик\n"
            f"• память: {'сжатая выжимка' if summary else 'обычная'}\n"
            f"• подсказки: {hints}"
        )

    def _history_excerpt(self, limit: int = 16) -> str:
        """Короткий снимок диалога для субагента /btw."""
        rows: list[str] = []
        history = getattr(self.agent, "history", []) or []
        for message in history[-limit * 2 :]:
            role = getattr(message, "role", "")
            if role not in ("user", "assistant"):
                continue
            content = getattr(message, "content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            text = redact_secrets(str(content or "")).strip()
            if not text:
                continue
            who = "Пользователь" if role == "user" else "Немуся"
            if len(text) > 1200:
                text = text[:1200] + "…"
            rows.append(f"{who}: {text}")
        return "\n".join(rows[-limit:])

    def _todo_for_chat(self, chat_id: int) -> TodoList:
        """План этого чата, даже если основная задача идёт в другом."""
        lock = getattr(self, "_task_lock", None)
        busy = lock is not None and lock.locked()
        if busy and getattr(self, "_current_task_chat", None) == chat_id:
            return self.agent.todo
        if busy:
            todo = TodoList()
            memory = getattr(self, "memory", None)
            if memory is not None:
                try:
                    sid = memory.get_meta(self._session_key(chat_id))
                    if sid:
                        todo.load(memory.get_meta(f"todo:{int(sid)}"))
                except (TypeError, ValueError, OSError):
                    pass
            return todo
        try:
            self._prepare_session(chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram: не удалось открыть сессию для /todo")
        return self.agent.todo

    def _btw_context(self, chat_id: int) -> str:
        """Собирает задачу, план и недавний чат для вопроса /btw."""
        lock = getattr(self, "_task_lock", None)
        busy = lock is not None and lock.locked()
        active = self._active_context if busy else None
        parts: list[str] = []
        if active and active.get("chat_id") == chat_id:
            task = str(active.get("task") or "").strip()
            if task:
                parts.append("Текущая задача: " + task[:2000])
            history = str(active.get("history") or "").strip()
            if history:
                parts.append("Недавний диалог:\n" + history)
            todo = self.agent.todo.render().strip()
            if todo and "Плана пока нет" not in todo:
                parts.append(todo)
        else:
            memory = getattr(self, "memory", None)
            rows: list[dict[str, Any]] = []
            todo = TodoList()
            if memory is not None:
                try:
                    sid = memory.get_meta(self._session_key(chat_id))
                    if sid:
                        rows = memory.session_messages(int(sid), limit=20)
                        todo.load(memory.get_meta(f"todo:{int(sid)}"))
                except (TypeError, ValueError, OSError):
                    rows = []
            history: list[str] = []
            for row in rows:
                if row.get("role") not in ("user", "assistant"):
                    continue
                text = redact_secrets(str(row.get("content") or "")).strip()
                if not text:
                    continue
                who = "Пользователь" if row.get("role") == "user" else "Немуся"
                if len(text) > 1200:
                    text = text[:1200] + "…"
                history.append(f"{who}: {text}")
            if history:
                parts.append("Недавний диалог:\n" + "\n".join(history[-16:]))
            plan = todo.render().strip()
            if plan and "Плана пока нет" not in plan:
                parts.append(plan)
        return "\n\n".join(parts)[:14000]

    def _btw_answer(self, chat_id: int, question: str) -> str:
        """Быстрый ответ отдельного субагента, без инструментов и очереди."""
        llm = getattr(self.core, "llm", None)
        if llm is None:
            raise RuntimeError("Модель не подключена.")
        context = self._btw_context(chat_id)
        system = (
            "Ты — быстрый субагент Немуси. Пользователь задаёт тебе отдельный "
            "вопрос по контексту чата, возможно, пока основная Немуся занята "
            "задачей. Отвечай только на вопрос, "
            "опираясь на контекст ниже; не выполняй задачу, не вызывай "
            "инструменты и не обещай действий. Если ответа в контексте нет, "
            "честно скажи об этом. Кратко: до 6 предложений, без вступлений. "
            "Отвечай на языке вопроса."
        )
        user = (
            ("КОНТЕКСТ ЧАТА:\n" + (context or "Контекста пока нет."))
            + "\n\nВОПРОС:\n"
            + question
        )
        response = llm.complete(
            [
                Message(role="system", content=system),
                Message(role="user", content=user),
            ],
            attempts=1,
            budget=25.0,
        )
        answer = redact_secrets(str(getattr(response, "content", "") or "")).strip()
        if not answer:
            raise RuntimeError("Субагент не вернул ответ.")
        return answer

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            self._status_text(update.effective_chat.id),
            reply_to=update.message.message_id,
        )

    async def _cmd_emotions(self, update: Any, context: Any) -> None:
        """`/emotions` — характер и текущее эмоциональное состояние."""
        if not self._authorized(update):
            await self._deny(update)
            return
        try:
            manager = self._emotions
            if manager is not None:
                state = manager.state()
            else:
                from nemus.core.emotions import EmotionStore

                state = EmotionStore(self.config.character.state_file).snapshot()
            text = summary_html(self.config, state)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: /emotions: {exc}")
            await update.message.reply_text("Не удалось прочитать эмоции.")
            return
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            text,
            reply_to=update.message.message_id,
            raw_html=True,
        )

    async def _cmd_diary(self, update: Any, context: Any) -> None:
        """`/diary` — присылает личный дневник файлом, если она доверяет."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        reply_to = getattr(getattr(update, "message", None), "message_id", None)
        try:
            book = getattr(self.core, "diary", None)
            if book is None:
                from nemus.core.diary import DiaryBook

                book = DiaryBook(self.config.character.diary_file)
            # если файла нет, она замечает это и заводит новый дневник
            created = book.ensure()
            manager = self._emotions
            if manager is not None:
                store = manager.store
                state = manager.state()
            else:
                from nemus.core.emotions import EmotionStore

                store = EmotionStore(self.config.character.state_file)
                state = store.snapshot()
            attachment = (
                state.attachment
                if state.attachment is not None
                else self.config.character.attachment
            )
            access = diary_access(state.hurt, state.trust, attachment)
            if access == "offended":
                store.apply_hurt(
                    self.config.character,
                    amount=16,
                    reason=(
                        "попросил показать мой личный дневник, хотя я ему "
                        "не доверяю"
                    ),
                    trust_delta=-4,
                    attachment_delta=-6,
                )
                text = (
                    "Нет. Дневник — это слишком личное, а я тебе не настолько "
                    "доверяю. И мне неприятно, что ты вообще попросил."
                )
            elif access != "share":
                if state.hurt >= 5:
                    text = (
                        "Не сейчас. Я ещё обижена, а дневник не показывают, "
                        "чтобы помириться."
                    )
                else:
                    text = (
                        "Не покажу. Это слишком личное: я ещё не готова "
                        "открывать тебе дневник."
                    )
            else:
                has_entries = bool(book.entries())
                if not has_entries:
                    caption = (
                        "Я только что завела дневник. Пока он пуст."
                        if created
                        else "Дневник пока пуст."
                    )
                else:
                    caption = (
                        "Держи. Это мой дневник. Только не смейся и не пересылай."
                    )
                from telegram import InputFile

                with book.path.open("rb") as fh:
                    await self._app.bot.send_document(
                        chat_id,
                        document=InputFile(fh, filename="diary.md"),
                        caption=caption,
                        reply_to_message_id=reply_to,
                        allow_sending_without_reply=True,
                    )
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: /diary: {exc}")
            await update.message.reply_text("Не удалось прочитать дневник.")
            return
        await start_message(
            self._app.bot,
            chat_id,
            text,
            reply_to=reply_to,
        )

    async def _cmd_todo(self, update: Any, context: Any) -> None:
        """`/todo` — план текущей или последней задачи."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        try:
            todo = self._todo_for_chat(chat_id)
            text = todo.render()
            items = todo.items
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: /todo: {exc}")
            await update.message.reply_text("Не удалось прочитать план задачи.")
            return
        lock = getattr(self, "_task_lock", None)
        active = (
            lock is not None
            and lock.locked()
            and getattr(self, "_current_task_chat", None) == chat_id
            and self._active_context
        )
        if active:
            task = str(active.get("task") or "").replace("\n", " ").strip()
            if task:
                if len(task) > 180:
                    task = task[:180] + "…"
                text = f"🎯 Сейчас: {task}\n\n{text}"
        elif items and any(item.status != "completed" for item in items):
            text = "📌 Последний план\n\n" + text
        await start_message(
            self._app.bot,
            chat_id,
            text,
            reply_to=update.message.message_id,
        )

    async def _cmd_btw(self, update: Any, context: Any) -> None:
        """`/btw` — короткий ответ субагента, пока основная задача идёт."""
        if not self._authorized(update):
            await self._deny(update)
            return
        question = " ".join(getattr(context, "args", None) or []).strip()
        if not question:
            await update.message.reply_text(
                "Напиши вопрос после команды, например:\n"
                "/btw что ты уже проверила и что осталось?"
            )
            return
        chat_id = update.effective_chat.id
        lock = getattr(self, "_task_lock", None)
        active = (
            lock is not None
            and lock.locked()
            and getattr(self, "_current_task_chat", None) == chat_id
        )
        try:
            answer = await asyncio.to_thread(self._btw_answer, chat_id, question)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: /btw: {exc}")
            await update.message.reply_text(
                "Субагент не смог ответить: " + self._mask(str(exc))
            )
            return
        prefix = "💬 Пока задача идёт\n\n" if active else "💬 /btw\n\n"
        await start_message(
            self._app.bot,
            chat_id,
            prefix + answer,
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

    # ---------------------------------------------------- autopilot / healing
    def _start_services(self) -> None:
        """Поднимает автопилот и self-healing после старта polling."""
        if self._autopilot is None:
            try:
                self._autopilot = Autopilot(
                    self.config,
                    on_event=self._on_autopilot_event,
                    autonomy_message=self._autonomy_message,
                )
                self._autopilot.start()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram: автопилот не запущен")
                self._autopilot = None
        if self._healing is None:
            try:
                from nemus.core.self_healing import HealingManager

                self._healing = HealingManager(
                    self.config,
                    self.core.llm,
                    on_incident=self._on_healing_incident,
                )
                self._healing.start()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram: self-healing не запущен")
                self._healing = None
        if self._emotions is None:
            try:
                self._emotions = EmotionManager(
                    self.config,
                    on_reach_out=self._on_emotion_reach_out,
                    llm=self.core.llm,
                    history=lambda: self._history_excerpt(8),
                    diary=self.core.diary,
                )
                self._emotions.start()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram: эмоции не запущены")
                self._emotions = None

    def _stop_services(self) -> None:
        emotions, self._emotions = self._emotions, None
        if emotions is not None:
            try:
                emotions.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram: ошибка остановки эмоций")
        healing, self._healing = self._healing, None
        if healing is not None:
            try:
                healing.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram: ошибка остановки self-healing")
        autopilot, self._autopilot = self._autopilot, None
        if autopilot is not None:
            try:
                autopilot.close()
            except Exception:  # noqa: BLE001
                logger.exception("Telegram: ошибка остановки автопилота")

    def _autonomy_message(self, context: dict[str, Any]) -> str:
        """Живая самостоятельная реплика; вызывается из потока автопилота."""
        from nemus.core.autonomy import compose_autonomy_message

        return compose_autonomy_message(self.config, self.core.llm, context)

    def _on_autopilot_event(self, event: AutopilotEvent) -> None:
        """Событие автопилота: сообщение в Telegram и, если нужно, задача."""
        if event.kind == "autonomy":
            text = event.message
            self.send_text(text, owner=True)
            try:
                from nemus.core.emotions import EmotionStore

                EmotionStore(self.config.character.state_file).mark_proactive(
                    text=text
                )
            except Exception:  # noqa: BLE001
                logger.debug("Автономность: не удалось обновить чувства", exc_info=True)
            try:
                self.agent.record_proactive(text)
            except Exception:  # noqa: BLE001
                logger.debug("Автономность: не удалось записать реплику", exc_info=True)
            return
        text = f"**🤖 Автопилот · {event.title}**\n\n{event.message}"
        self.send_text(text, owner=True)
        if not event.auto_run or not event.prompt.strip():
            return
        loop = self._loop
        chat_id = self._notify_chat_id
        if loop is None or chat_id is None or not self.running:
            logger.warning("Автопилот: некому запустить задачу — бот не готов")
            return
        user_id = self.cfg.allowed_users[0] if self.cfg.allowed_users else None
        future = asyncio.run_coroutine_threadsafe(
            self._run_task_for(
                chat_id=chat_id,
                user_id=user_id,
                message_id=None,
                text=event.prompt,
                chat_type="private",
                observe=False,
            ),
            loop,
        )

        def _done(result: Any) -> None:
            try:
                result.result()
            except Exception:  # noqa: BLE001
                logger.exception("Автопилот: задача агента упала")

        future.add_done_callback(_done)

    def _on_emotion_reach_out(
        self, prompt: str, state: EmotionState
    ) -> str | bool:
        """Генерирует и отправляет сообщение по собственной инициативе."""
        if not self.running:
            return False
        recent = self._history_excerpt(8)
        text = compose_proactive_message(
            self.config, self.core.llm, recent, state, prompt
        )
        if not text:
            # Никакой шаблонной «обиженной» фразы: при сбое модели лучше
            # промолчать и повторить позже, чем отправлять один и тот же
            # текст, который выглядит как ответ ИИ, но им не является.
            logger.warning("Эмоции: пустой ответ модели — инициативу отложила")
            return False
        if not self.send_text(text, owner=True):
            return False
        # теперь следующая реплика помнит, что она уже писала
        self.agent.record_proactive(text)
        return text

    def _on_healing_incident(self, incident: Any) -> None:
        """Новый инцидент или готовый патч — уведомляем владельца."""
        try:
            manager = self._healing
            body = (
                manager.incident_text(incident.id)
                if manager is not None
                else incident.error
            )
            hint = (
                f"\n\nПрименить: `/heal apply {incident.id}`\n"
                f"Отклонить: `/heal reject {incident.id}`"
            )
            self.send_text(body + hint, owner=True)
        except Exception:  # noqa: BLE001
            logger.exception("Self-healing: не удалось отправить инцидент")

    async def _cmd_auto(self, update: Any, context: Any) -> None:
        """Управление проактивным автопилотом."""
        if not self._authorized(update):
            await self._deny(update)
            return
        if self._autopilot is None:
            await update.message.reply_text("Автопилот не инициализирован.")
            return
        args = [part.lower() for part in (context.args or [])]
        try:
            if args and args[0] in ("on", "off", "вкл", "выкл"):
                value = args[0] in ("on", "вкл")
                self._autopilot.set_enabled(value)
                await update.message.reply_text(
                    "Автопилот включён." if value else "Автопилот выключен."
                )
                return
            if args and args[0] in ("check", "проверить"):
                sent = await asyncio.to_thread(self._autopilot.check_now)
                await update.message.reply_text(
                    f"Проверка завершена, новых событий: {sent}."
                )
                return
            if args and args[0] in ("events", "события"):
                text = self._autopilot.events_text()
            elif args and args[0] in ("rules", "правила"):
                text = self._autopilot.rules_text()
            else:
                text = self._autopilot.status_text()
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Автопилот: {exc}")
            return
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            text,
            reply_to=update.message.message_id,
        )

    async def _cmd_heal(self, update: Any, context: Any) -> None:
        """Список инцидентов, диагноз, применение и откат патча."""
        if not self._authorized(update):
            await self._deny(update)
            return
        manager = self._healing
        if manager is None:
            await update.message.reply_text("Self-healing не инициализирован.")
            return
        args = [part for part in (context.args or [])]
        command = args[0].lower() if args else ""
        if command in ("on", "вкл"):
            self.config.healing.enabled = True
            manager.start()
            await update.message.reply_text("Self-healing включён.")
            return
        if command in ("off", "выкл"):
            self.config.healing.enabled = False
            manager.stop()
            await update.message.reply_text("Self-healing выключен.")
            return
        if command in ("scan", "скан"):
            count = await asyncio.to_thread(manager.scan_now)
            await update.message.reply_text(f"Новых инцидентов: {count}.")
            return
        if command in ("show", "показать") and len(args) > 1:
            text = manager.incident_text(args[1])
        elif command in ("apply", "применить") and len(args) > 1:
            ok, text = await asyncio.to_thread(manager.apply, args[1])
            if ok:
                await update.message.reply_text(f"✅ {text}")
                return
            await update.message.reply_text(f"❌ {text}")
            return
        elif command in ("reject", "отклонить") and len(args) > 1:
            ok, text = await asyncio.to_thread(manager.reject, args[1])
            await update.message.reply_text(("✅ " if ok else "❌ ") + text)
            return
        elif command in ("rollback", "откат") and len(args) > 1:
            ok, text = await asyncio.to_thread(manager.rollback, args[1])
            await update.message.reply_text(("✅ " if ok else "❌ ") + text)
            return
        elif command in ("list", "список") or not command:
            text = manager.status_text() + "\n\n" + manager.list_text()
        else:
            text = "Формат: `/heal`, `/heal show ID`, `/heal apply ID`, "
            "`/heal reject ID`, `/heal rollback ID`, `/heal scan`."
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            text,
            reply_to=update.message.message_id,
        )

    async def _cmd_pc(self, update: Any, context: Any) -> None:
        """Ресурсы компьютера: CPU, RAM, диски, батарея, процессы."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        await self._app.bot.send_chat_action(chat_id, "typing")
        report = await asyncio.to_thread(pc_report, self.config.tools.workspace)
        await start_message(
            self._app.bot,
            chat_id,
            report,
            reply_to=update.message.message_id,
        )

    async def _cmd_doctor(self, update: Any, context: Any) -> None:
        """Самодиагностика: Telegram, Web App, туннель, LLM, MCP, лог."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        await self._app.bot.send_chat_action(chat_id, "typing")
        report = await asyncio.to_thread(doctor_report, self)
        await start_message(
            self._app.bot,
            chat_id,
            report,
            reply_to=update.message.message_id,
        )

    async def _cmd_read(self, update: Any, context: Any) -> None:
        """`/read URL [вопрос]` — прочитать и разобрать страницу."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        raw = (update.message.text or "").strip()
        payload = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""
        if not payload:
            await update.message.reply_text(
                "Формат: `/read https://example.com` или "
                "`/read https://example.com коротко перескажи`.",
                parse_mode="Markdown",
            )
            return
        url, _, question = payload.partition(" ")
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url
        notice = await update.message.reply_text("🌐 Читаю страницу…")
        result = await asyncio.to_thread(self._fetch_site, url, not bool(question))
        if not result.ok:
            await notice.edit_text(f"Не получилось: {result.content}")
            return
        if question.strip():
            await notice.edit_text("📄 Страница прочитана, разбираю…")
            task = (
                f"Пользователь попросил прочитать страницу {url} и ответить: "
                f"{question.strip()}\n\n{result.content}"
            )
            await self._run_task(update, task)
            return
        await notice.edit_text("✅ Готово")
        await start_message(
            self._app.bot,
            chat_id,
            result.content,
            reply_to=update.message.message_id,
        )

    @staticmethod
    def _fetch_site(url: str, include_links: bool = False) -> Any:
        """Синхронно читает сайт инструментом с SSRF-защитой."""
        from nemus.core.tools.web import FetchUrlTool

        return FetchUrlTool().execute(
            url=url, max_chars=14_000, include_links=include_links
        )

    async def _cmd_run(self, update: Any, context: Any) -> None:
        """Запускает долгую команду в фоне и присылает уведомление."""
        if not self._authorized(update):
            await self._deny(update)
            return
        raw = (update.message.text or "").strip()
        parts = raw.split(maxsplit=1)
        command = parts[1].strip() if len(parts) > 1 else ""
        if not command:
            await update.message.reply_text(
                "Формат: `/run команда`\n"
                "Например: `/run python -m http.server 8000`\n"
                "Команда выполняется в рабочей папке; `/jobs` — список, "
                "`/log ID` — вывод, `/kill ID` — остановить.",
                parse_mode="Markdown",
            )
            return
        chat_id = update.effective_chat.id
        try:
            job = await asyncio.to_thread(self._jobs.start, command, chat_id)
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Не удалось запустить: {exc}")
            return
        await update.message.reply_text(
            f"▶️ Фоновая команда #{job.job_id} запущена.\n"
            f"Узнать вывод: /log {job.job_id}\n"
            f"Остановить: /kill {job.job_id}"
        )

    async def _cmd_jobs(self, update: Any, context: Any) -> None:
        """Список фоновых команд."""
        if not self._authorized(update):
            await self._deny(update)
            return
        jobs = self._jobs.list(limit=15)
        if not jobs:
            await update.message.reply_text(
                "Фоновых команд нет. Запустить: `/run команда`.",
                parse_mode="Markdown",
            )
            return
        lines = ["**Фоновые команды**"]
        for job in jobs:
            snap = job.snapshot()
            if snap.running:
                state = "▶️ выполняется"
            elif snap.exit_code == 0:
                state = "✅ готово"
            else:
                state = f"❌ код {snap.exit_code}"
            started = time.strftime("%d.%m %H:%M", time.localtime(snap.started_at))
            short = job.command.replace("`", "'")
            if len(short) > 90:
                short = short[:87] + "…"
            lines.append(f"• #{snap.job_id} — {state}, {started}\n  `{short}`")
        lines.append("\n`/log ID` — вывод, `/kill ID` — остановить")
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            "\n".join(lines),
            reply_to=update.message.message_id,
        )

    async def _cmd_log(self, update: Any, context: Any) -> None:
        """Хвост вывода фоновой команды."""
        if not self._authorized(update):
            await self._deny(update)
            return
        parts = (update.message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Формат: `/log ID [строк]`.")
            return
        job = self._jobs.get(int(parts[1]))
        if job is None:
            await update.message.reply_text("Такой фоновой команды нет. Список: /jobs")
            return
        lines = 60
        if len(parts) > 2 and parts[2].isdigit():
            lines = max(1, min(200, int(parts[2])))
        snap = job.snapshot()
        state = "выполняется" if snap.running else f"завершена (код {snap.exit_code})"
        text = f"**Команда #{snap.job_id}** — {state}\n```\n{job.tail(lines)}\n```"
        await start_message(
            self._app.bot,
            update.effective_chat.id,
            text,
            reply_to=update.message.message_id,
        )

    async def _cmd_kill(self, update: Any, context: Any) -> None:
        """Останавливает фоновую команду."""
        if not self._authorized(update):
            await self._deny(update)
            return
        parts = (update.message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Формат: `/kill ID`. Список: /jobs")
            return
        stopped = await asyncio.to_thread(self._jobs.stop, int(parts[1]))
        if stopped:
            await update.message.reply_text(
                f"🛑 Команда #{parts[1]} остановлена."
            )
        else:
            await update.message.reply_text(
                "Не нашла такую команду или она уже завершилась. Список: /jobs"
            )

    def _on_job_done(self, job: ProcessJob) -> None:
        """Уведомляет чат из потока процесса о завершении команды."""
        if job.exit_code == 0:
            title = f"✅ Фоновая команда #{job.job_id} завершена"
        else:
            title = (
                f"❌ Фоновая команда #{job.job_id} завершилась "
                f"с кодом {job.exit_code}"
            )
        command = job.command.replace("`", "'")
        if len(command) > 200:
            command = command[:197] + "…"
        self.send_text(
            f"{title}\n`{command}`\n```\n{job.tail(15)}\n```",
            chat_id=job.chat_id,
        )

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
        token = text.lstrip().split(maxsplit=1)[0].lstrip("/") if text else ""
        guesses = search_commands(token, limit=3)
        if guesses:
            rows = [
                [(f"/{item.name}", f"ux:hx:{item.name}") for item in guesses]
            ]
            await update.message.reply_text(
                "Не знаю такую команду. Возможно, ты имела в виду:\n"
                + "\n".join(f"• /{item.name} — {item.title}" for item in guesses)
                + "\n\nВсе команды: /help, скиллы: /skills.",
                reply_markup=self._keyboard(rows),
            )
            return
        await update.message.reply_text(
            "Не знаю такой команды. Открой /help — там все команды "
            "по категориям, с синтаксисом и примерами. Быстрый доступ: "
            "/skills, /panel, /status."
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

    def _skills(self) -> list[Any]:
        """Установленные скиллы; битая папка не должна ломать /skills."""
        cfg = getattr(self.config, "skills", None)
        try:
            from nemus.core.skills import load_skills

            return load_skills(
                getattr(cfg, "dir", None) if cfg else None,
                disabled=getattr(cfg, "disabled", ()) if cfg else (),
            )
        except OSError as exc:
            logger.warning(f"Telegram: не удалось прочитать скиллы: {exc}")
            return []

    def _skill_plugin_name(self, skill: Any) -> str:
        """Имя плагина скилла или пустая строка."""
        cfg = getattr(self.config, "skills", None)
        try:
            from nemus.core.skills import skill_plugin

            return skill_plugin(
                getattr(skill, "path", Path()),
                getattr(cfg, "dir", None) if cfg else None,
            )
        except OSError:
            return ""

    def _mcp_names(self) -> list[str]:
        """Только включённые MCP-серверы из конфига, без сети."""
        mcp = getattr(self.config, "mcp", None)
        if mcp is None or not getattr(mcp, "enabled", False):
            return []
        names: list[str] = []
        for server in getattr(mcp, "servers", ()):
            if not getattr(server, "enabled", False):
                continue
            name = str(getattr(server, "name", "") or "").strip()
            if not name:
                command = str(getattr(server, "command", "") or "").strip()
                name = Path(command).name if command else ""
            if name:
                names.append(name)
        return names

    def _find_skill(self, name: str) -> Any | None:
        """Точное имя скилла без учёта регистра; ведущий / не мешает."""
        wanted = (name or "").strip().lstrip("/").lower()
        for skill in self._skills():
            if str(getattr(skill, "name", "")).lower() == wanted:
                return skill
        return None

    def _skills_text(self) -> str:
        """Текст списка скиллов — один и тот же для чата и ephemeral."""
        entries = [
            (
                skill.name,
                self._skill_plugin_name(skill),
                getattr(skill, "kind", "skill"),
            )
            for skill in self._skills()
        ]
        return skills_list_text(entries, self._mcp_names())

    async def _send_skills(
        self,
        chat_id: int,
        reply_to: int | None = None,
        reply_markup: Any | None = None,
    ) -> None:
        """Список установленных скиллов и MCP-серверов."""
        await start_message(
            self._app.bot,
            chat_id,
            self._skills_text(),
            reply_to=reply_to,
            reply_markup=reply_markup,
        )

    async def _cmd_skills(self, update: Any, context: Any) -> None:
        """`/skills` — список; `/skills имя` — короткая справка."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        args = list(getattr(context, "args", None) or ())
        keyboard = self._keyboard(main_menu_rows(self._hints_active(chat_id)))
        if not args:
            await self._send_skills(
                chat_id,
                reply_to=update.message.message_id,
                reply_markup=keyboard,
            )
            return
        skill = self._find_skill(str(args[0]))
        if skill is None:
            text = skill_not_found_text()
        else:
            text = skill_details_text(
                skill.name,
                getattr(skill, "description", ""),
                self._skill_plugin_name(skill),
                getattr(skill, "kind", "skill"),
            )
        await start_message(
            self._app.bot,
            chat_id,
            text,
            reply_to=update.message.message_id,
            reply_markup=keyboard,
        )

    # ------------------------------------------------------------- приём
    async def _on_text(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if not self._addressed_to_bot(update):
            return
        if self._inbox_lock is None:
            await self._handle_text(update)
            return
        async with self._inbox_lock:
            await self._handle_text(update)

    async def _handle_text(self, update: Any) -> None:
        text = (update.message.text or "").strip()
        if not text:
            return
        if await self._handle_brainstorm_text(update, text):
            return
        if await self._apply_settings_text(update, text):
            return
        chat_id = update.effective_chat.id
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        pending, pending_images = self._take_pending_attachment(chat_id, user_id)
        if pending:
            text = f"{pending}\n\nЗадача пользователя: {text}"
        if not pending and not pending_images and looks_like_skills_question(text):
            await self._send_skills(
                chat_id,
                reply_to=update.message.message_id,
                reply_markup=self._keyboard(
                    main_menu_rows(self._hints_active(chat_id))
                ),
            )
            return
        await self._run_task(update, text, images=pending_images)

    async def _on_media(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if not self._addressed_to_bot(update):
            return
        if self._inbox_lock is None:
            await self._handle_media(update)
            return
        async with self._inbox_lock:
            await self._handle_media(update)

    async def _document_text(self, paths: list[Path]) -> str:
        """Текст документов для промпта, без блокировки loop."""
        documents = [
            path for path in paths if path.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        if not documents:
            return ""
        try:
            return await asyncio.to_thread(document_context, documents)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram: не удалось извлечь текст документа: {exc}")
            return ""

    async def _handle_media(self, update: Any) -> None:
        message = update.message
        chat_id = update.effective_chat.id
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
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
        document_text = await self._document_text(saved)
        attachment_info = _attachment_context(saved)
        if document_text:
            attachment_info += "\n\n" + document_text
        caption = (message.caption or "").strip()
        if saved and not caption and not voice_text:
            # Вложение без подписи — это не задача. Не отдаём его агенту:
            # иначе модель домысливает действие (например, делает чиби).
            # Запоминаем путь и само изображение: следующая задача будет
            # знать, о чём речь, и модель действительно увидит фото.
            rows = (
                attachment_action_rows(guess_kind(saved[0]))
                if len(saved) == 1 and self._hints_active(chat_id)
                else []
            )
            question = await message.reply_text(
                _attachment_question(saved, short=bool(rows)),
                reply_markup=self._keyboard(rows),
            )
            self._remember_pending_attachment(
                chat_id,
                attachment_info,
                image_urls,
                message_id=getattr(question, "message_id", None),
                user_id=getattr(update.effective_user, "id", None),
            )
            return
        parts: list[str] = []
        if voice_text:
            parts.append(voice_text)
        if caption:
            parts.append(caption)
        images: list[str] = []
        if saved:
            pending, pending_images = self._take_pending_attachment(chat_id, user_id)
            images.extend(pending_images)
            if pending:
                parts.append(pending)
            parts.append(attachment_info)
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

    def _prune_downloads(self, folder: Path, keep: Path | None = None) -> None:
        """Удаляет старые скачанные файлы, чтобы папка чата не росла вечно."""
        cutoff = time.time() - DOWNLOAD_RETENTION_DAYS * 86400
        try:
            files = sorted(
                (
                    item
                    for item in folder.iterdir()
                    if item.is_file() and item.name.split("_", 1)[0].isdigit()
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for index, item in enumerate(files):
            if item == keep:
                continue
            try:
                if index >= DOWNLOAD_MAX_FILES or item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                pass

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
        # Наносекунды не дают двум photo.jpg из альбома перезаписать друг друга.
        path = folder / f"{time.time_ns()}_{name}"
        file = await attachment.get_file()
        await file.download_to_drive(custom_path=path)
        self._prune_downloads(folder, keep=path)
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
            chat_type=getattr(update.effective_chat, "type", None),
        )

    async def _send_final(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        *,
        stopped: bool = False,
        rich: bool = True,
    ) -> None:
        """Финальный ответ: rich-сообщение, иначе текущий HTML-путь.

        Rich-кнопки повторяют обычные «Повторить / Новый / Помощь», поэтому
        привычное восстановление после ошибки не меняется.
        """
        rows = error_rows(True)
        api = self._api
        advanced = bool(getattr(self.cfg, "advanced_ui", True))
        if rich and advanced and api is not None and api.available("sendRichMessage"):
            try:
                markdown = build_rich_markdown(text, rows)
                reply_parameters = None
                if message_id:
                    reply_parameters = {
                        "message_id": message_id,
                        "allow_sending_without_reply": True,
                    }
                sent = await api.send_rich_message(
                    chat_id=chat_id,
                    rich_message={"markdown": markdown},
                    reply_parameters=reply_parameters,
                )
                if sent is not None:
                    return
                logger.info(
                    "Telegram: rich-ответ не принят — отправляю HTML-чанками"
                )
            except RichTextTooLong as exc:
                logger.info("Telegram: %s — отправляю HTML-чанками", exc)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "Telegram: rich-ответ не отправлен (%s) — HTML-чанками",
                    self._mask(str(exc)),
                )
        await start_message(
            self._app.bot,
            chat_id,
            text,
            reply_to=message_id,
            reply_markup=self._keyboard(rows),
        )

    async def _send_final_or_plain(
        self,
        progress: Any,
        chat_id: int,
        message_id: int | None,
        text: str,
        *,
        stopped: bool = False,
        rich: bool = True,
    ) -> None:
        """Отправляет финал; если разметка не ушла — оставляет простой текст."""
        try:
            await self._send_final(
                chat_id, message_id, text, stopped=stopped, rich=rich
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Telegram: финальный ответ не отправлен (%s) — показываю текстом",
                self._mask(str(exc)),
            )
            await progress.plain(text)
        else:
            await progress.close()

    async def _run_task_for(
        self,
        chat_id: int,
        user_id: int | None,
        message_id: int | None,
        text: str,
        images: list[str] | None = None,
        chat_type: str | None = None,
        retry: bool = False,
        observe: bool = True,
    ) -> None:
        if self._task_lock is None:
            self._task_lock = asyncio.Lock()
        if self._task_lock.locked():
            await self._app.bot.send_message(
                chat_id,
                "⏳ Ещё выполняю предыдущую задачу — это сообщение в очереди.",
            )
        async with self._task_lock:
            # новая задача делает старые кнопки брейншторма неактуальными
            self._brainstorm.pop(chat_id, None)
            try:
                self._prepare_session(chat_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Telegram: не удалось переключить сессию чата %s", chat_id
                )
            # Номер задачи и «что повторять» обновляем только когда задача
            # реально вошла в lock: queued-сообщение не должно перетирать
            # кнопку «Повторить» у ещё работающей задачи.
            task_id = self._task_seq.get(chat_id, 0) + 1
            self._task_seq[chat_id] = task_id
            self._last_tasks[chat_id] = (
                text,
                list(images or []),
                message_id,
                user_id,
            )
            self._last_task_ids[chat_id] = task_id
            self._current_task_id = task_id
            self._current_task_chat = chat_id
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._chat_id = chat_id
            self._user_id = user_id
            self._message_id = message_id
            self._reaction_set = False
            self._notify_chat_id = chat_id
            # завершённый план прошлой задачи не должен висеть над новой:
            # незавершённый оставляем, чтобы «продолжай» не терял контекст
            try:
                previous = self.agent.todo.items
                if previous and all(item.status == "completed" for item in previous):
                    self.agent.todo.clear()
            except Exception:  # noqa: BLE001
                pass
            # снимок нужен /btw: агентский поток уже будет менять history
            self._active_context = {
                "chat_id": chat_id,
                "task": text,
                "history": self._history_excerpt(),
            }
            advanced = bool(getattr(self.cfg, "advanced_ui", True))
            use_draft = (
                chat_type == "private"
                and advanced
                and not retry
                and self._api is not None
                and self._api.available("sendMessageDraft")
            )
            progress: Any
            if use_draft:
                progress = _DraftProgress(self._api, self._app.bot, chat_id)
                self._draft_id = progress.draft_id
                self._draft_chat_id = chat_id
            else:
                progress = _Progress(self._app.bot, chat_id)
            self._active_progress = progress
            typing: asyncio.Task | None = None
            agent_thread: threading.Thread | None = None
            # Всё выполнение задачи — включая финальную отправку и ранние
            # return — живёт внутри outer finally: статус не должен залипать
            # ни при ошибке, ни при отмене, ни при падении форматирования.
            try:
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
                        for event in self.agent.run(
                            text, images=images, stop=stop_event, observe=observe
                        ):
                            push(event)
                    except Exception as exc:  # noqa: BLE001
                        push(AgentEvent(kind="error", error=str(exc)))
                    finally:
                        push(None)

                agent_thread = threading.Thread(
                    target=work, name="telegram-agent", daemon=True
                )
                self._agent_thread = agent_thread
                agent_thread.start()

                final_text = ""
                error = ""
                stopped = False
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    if event.kind == "assistant_delta":
                        final_text += event.text
                        await progress.tail(final_text)
                    elif event.kind == "assistant_reset":
                        # провайдер оборвал ответ после части текста — не
                        # склеиваем черновик с ответом после повтора
                        final_text = ""
                        await progress.tail("", force=True)
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
                    elif event.kind == "stopped":
                        stopped = True
                        final_text = event.text or final_text
                    elif event.kind == "error":
                        error = event.error or event.text

                if stopped:
                    final_text = (final_text or "").strip()
                    stopped_text = (
                        f"{final_text}\n\n⏹ Остановлено."
                        if final_text
                        else "⏹ Остановила задачу."
                    )
                    await self._send_final_or_plain(
                        progress,
                        chat_id,
                        message_id,
                        stopped_text,
                        stopped=True,
                        rich=chat_type == "private",
                    )
                    return
                if error:
                    await progress.fail(
                        error_text(self._mask(error)),
                        reply_markup=self._error_keyboard(chat_id),
                    )
                    return
                final_text = (final_text or "").strip()
                if not final_text:
                    await progress.fail(
                        error_text(
                            "Пустой ответ модели. Попробуйте переформулировать задачу."
                        ),
                        reply_markup=self._error_keyboard(chat_id),
                    )
                    return
                groups = parse_choices(final_text) or parse_grilling_choices(
                    final_text
                )
                if groups:
                    await self._send_brainstorm(
                        chat_id,
                        message_id,
                        groups,
                        user_id=user_id,
                        chat_type=chat_type,
                    )
                    return
                await self._send_final_or_plain(
                    progress,
                    chat_id,
                    message_id,
                    final_text,
                    rich=chat_type == "private",
                )
                await self._maybe_show_first_hint(chat_id)
                # Агент в общем наборе инструментов часто отвечает эмодзи в тексте,
                # а не вызывает react_telegram. Спрашиваем отдельно и ставим сами,
                # только если агент уже не поставил реакцию.
                if message_id and not self._reaction_set:
                    asyncio.create_task(
                        self._auto_react(text, final_text, chat_id, message_id)
                    )
            finally:
                if typing is not None:
                    typing.cancel()
                    try:
                        await typing
                    except asyncio.CancelledError:
                        pass
                await self._cancel_stop_note()
                await progress.close()
                self._chat_id = None
                self._user_id = None
                self._message_id = None
                self._active_progress = None
                self._active_context = None
                self._draft_id = None
                self._draft_chat_id = None
                self._stop_event = None
                self._current_task_id = None
                self._current_task_chat = None
                if agent_thread is not None and self._agent_thread is agent_thread:
                    self._agent_thread = None

    # ---------------------------------------------------------- настройки
    def _autostart_state(self, *, force: bool = False) -> bool | None:
        """Состояние автозапуска демона; None — платформа не поддерживает."""
        now = time.monotonic()
        if not force and now - self._autostart_cached_at < 10.0:
            return self._autostart_cached
        try:
            from nemus import telegram_autostart

            if not telegram_autostart.supported():
                value = None
            else:
                value = telegram_autostart.status()[0]
        except Exception:  # noqa: BLE001
            value = None
        self._autostart_cached = value
        self._autostart_cached_at = now
        return value

    def _settings_markup(
        self,
        section_id: str,
        *,
        page: int = 0,
        pending: bool = False,
    ) -> Any | None:
        if section_id == "root":
            return self._keyboard(settings_ui.root_keyboard())
        return self._keyboard(
            settings_ui.section_keyboard(
                self.config,
                section_id,
                page=page,
                skills=(
                    settings_ui.skills_entries(self.config)
                    if section_id == "skills"
                    else None
                ),
                pending=pending,
                autostart=(
                    self._autostart_state()
                    if section_id == "telegram"
                    else None
                ),
            )
        )

    def _settings_text(
        self,
        section_id: str,
        *,
        page: int = 0,
        pending: tuple[str, str] | None = None,
    ) -> str:
        return settings_ui.render(
            self.config,
            section_id,
            page=page,
            skills=(
                settings_ui.skills_entries(self.config)
                if section_id == "skills"
                else None
            ),
            pending=pending,
            autostart=(
                self._autostart_state() if section_id == "telegram" else None
            ),
        )

    async def _show_settings(
        self,
        chat_id: int,
        section_id: str,
        *,
        page: int = 0,
        query: Any | None = None,
        message_id: int | None = None,
        pending: tuple[str, str] | None = None,
        toast: str = "",
    ) -> None:
        """Рисует страницу настроек, по возможности редактируя то же сообщение."""
        text = self._settings_text(section_id, page=page, pending=pending)
        markup = self._settings_markup(section_id, page=page, pending=bool(pending))
        if query is not None:
            # Сначала гасим «часики» на кнопке, потом редактируем сообщение:
            # иначе ответ Telegram ждёт сети и кнопка кажется зависшей.
            await query.answer(toast[:200])
            message_id = getattr(getattr(query, "message", None), "message_id", None)
            try:
                await query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=markup
                )
            except Exception:  # noqa: BLE001
                # Старое сообщение могло исчезнуть/устареть: присылаем новое,
                # иначе кнопка выглядит «мёртвой».
                await self._show_settings(
                    chat_id, section_id, page=page, pending=pending
                )
                return
            if message_id is not None:
                self._settings_message[chat_id] = int(message_id)
            return
        if message_id is not None:
            try:
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                self._settings_message[chat_id] = message_id
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            sent = await self._app.bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=markup
            )
        except Exception:  # noqa: BLE001
            sent = await self._app.bot.send_message(
                chat_id, strip_html(text), reply_markup=markup
            )
        if sent is not None and getattr(sent, "message_id", None) is not None:
            self._settings_message[chat_id] = int(sent.message_id)

    async def _show_edit_prompt(
        self,
        chat_id: int,
        section_id: str,
        field_id: str,
        *,
        index: int | None = None,
        page: int = 0,
        label: str,
        query: Any | None = None,
    ) -> None:
        """Переводит панель в режим ожидания текстового значения."""
        self._settings_pending[chat_id] = (section_id, field_id, index, page)
        text = (
            f"<b>✏️ {label}</b>\n\n"
            "Отправь новое значение одним сообщением.\n"
            "Отмена — /cancel или кнопка ниже."
        )
        markup = self._keyboard([[("✖️ Отменить ввод", "pn:x")]])
        if query is not None:
            await query.answer("Жду значение")
            try:
                await query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=markup
                )
            except Exception:  # noqa: BLE001
                await self._app.bot.send_message(
                    chat_id, text, parse_mode="HTML", reply_markup=markup
                )
            return
        await self._app.bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=markup
        )

    def _save_settings(self, mutate: Any) -> tuple[bool, str]:
        """Применяет изменение к живому конфигу и сразу пишет копию в YAML.

        Без ``get_config(reload=True)``: перечитывание файла, keyring и
        переменных окружения на каждое нажатие кнопки было заметно медленным.
        """
        before = self.config.model_copy(deep=True)
        ok, message = mutate(self.config)
        if not ok:
            return False, message
        disk = self.config.model_copy(deep=True)
        try:
            settings_ui.save(disk)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Telegram: не удалось сохранить настройки")
            self.config = before
            self.cfg = before.telegram
            if hasattr(self, "agent"):
                self.agent.config = before
            return False, f"Не удалось сохранить: {exc}"
        self.cfg = self.config.telegram
        if hasattr(self, "agent"):
            self.agent.config = self.config
        self._skill_names = None
        return True, message

    async def _cmd_cancel(self, update: Any, context: Any) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id
        pending = self._settings_pending.pop(chat_id, None)
        if pending is None:
            await update.message.reply_text("Сейчас нечего отменять.")
            return
        section_id, _field_id, _index, page = pending
        await self._show_settings(
            chat_id,
            section_id,
            page=page,
            message_id=self._settings_message.get(chat_id),
            toast="Ввод отменён",
        )

    def _start_webapp_tunnel(self) -> None:
        """Поднимает публичный туннель выбранного провайдера."""
        if self._webapp is None:
            return
        if not bool(getattr(self.cfg, "web_app_tunnel", True)):
            return
        provider = str(
            getattr(self.cfg, "web_app_tunnel_provider", "cloudflared")
            or "cloudflared"
        ).strip().lower()
        manual_url = (getattr(self.cfg, "web_app_url", "") or "").strip()
        if provider != "ngrok" and manual_url:
            return
        ngrok_token = str(
            getattr(self.cfg, "web_app_ngrok_authtoken", "") or ""
        ).strip()
        if provider == "ngrok" and not ngrok_token:
            logger.warning(
                "ngrok: не задан authtoken — туннель не запущен. "
                "Укажи telegram.web_app_ngrok_authtoken."
            )
            return
        try:
            from nemus.core.webapp_tunnel import CloudflaredTunnel, NgrokTunnel

            local_url = f"http://127.0.0.1:{self._webapp.bound_port}"
            timeout = float(
                getattr(self.cfg, "web_app_tunnel_timeout", 45) or 45
            )
            download = bool(getattr(self.cfg, "web_app_tunnel_download", True))

            def make_tunnel() -> Any:
                if provider == "ngrok":
                    return NgrokTunnel(
                        local_url,
                        expected_url=manual_url,
                        authtoken=ngrok_token,
                        binary=str(
                            getattr(self.cfg, "web_app_ngrok_bin", "ngrok")
                            or "ngrok"
                        ),
                        download=download,
                        timeout=timeout,
                    )
                return CloudflaredTunnel(
                    local_url,
                    binary=str(
                        getattr(self.cfg, "web_app_tunnel_bin", "cloudflared")
                        or "cloudflared"
                    ),
                    download=download,
                    timeout=timeout,
                )

            tunnel = make_tunnel()
            self._tunnel = tunnel

            def _bring_up() -> None:
                nonlocal tunnel
                attempt = 0
                while not self._stopping.is_set():
                    attempt += 1
                    if attempt > 1:
                        time.sleep(10)
                    if self._stopping.is_set() or self._tunnel is not tunnel:
                        return
                    try:
                        if not tunnel.start():
                            time.sleep(20)
                            continue
                        deadline = time.monotonic() + timeout
                        while (
                            time.monotonic() < deadline
                            and not self._stopping.is_set()
                            and self._tunnel is tunnel
                        ):
                            if tunnel.public_url or tunnel.failed:
                                break
                            time.sleep(1)
                        if tunnel.public_url:
                            self._schedule_webapp_menu(tunnel.public_url)
                        last_health = time.monotonic()
                        unhealthy = 0
                        while (
                            not self._stopping.is_set()
                            and self._tunnel is tunnel
                            and tunnel.public_url
                            and not tunnel.failed
                        ):
                            now = time.monotonic()
                            if now - last_health >= 30.0:
                                last_health = now
                                if tunnel.check_health():
                                    unhealthy = 0
                                else:
                                    unhealthy += 1
                                    if unhealthy >= 2:
                                        logger.warning(
                                            "Туннель: публичный адрес не отвечает — "
                                            "перезапускаю."
                                        )
                                        tunnel.stop()
                                        break
                            time.sleep(2)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Туннель: не удалось запустить: {exc}")
                    if self._stopping.is_set() or self._tunnel is not tunnel:
                        return
                    logger.warning("Туннель: перезапускаю через 10 секунд.")
                    tunnel.stop()
                    tunnel = make_tunnel()
                    self._tunnel = tunnel

            self._tunnel_thread = threading.Thread(
                target=_bring_up, name="webapp-tunnel", daemon=True
            )
            self._tunnel_thread.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Туннель: не удалось создать: {exc}")

    def _schedule_webapp_menu(self, url: str) -> None:
        """Обновляет кнопку меню из фонового потока туннеля."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._app is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._sync_webapp_menu(url), loop)
        except RuntimeError:
            pass

    async def _sync_webapp_menu(self, url: str) -> None:
        """Ставит актуальный URL в Web App-кнопку меню Telegram.

        Quick Tunnel меняет адрес при каждом перезапуске. Старая кнопка
        остаётся в чате и даёт ERR_NAME_NOT_RESOLVED, поэтому обновляем её,
        но только если пользователь уже выбрал именно Web App-меню.
        """
        target = (url or "").strip()
        if not target or target == self._menu_url_synced:
            return
        from telegram import MenuButtonWebApp, WebAppInfo

        updated = 0
        # Кнопка может быть задана как общая, так и отдельно для владельца.
        targets: list[int | None] = [None, *self.cfg.allowed_users]
        for chat_id in targets:
            try:
                current = await self._app.bot.get_chat_menu_button(chat_id=chat_id)
                if not isinstance(current, MenuButtonWebApp):
                    continue
                if current.web_app.url == target:
                    continue
                await self._app.bot.set_chat_menu_button(
                    chat_id=chat_id,
                    menu_button=MenuButtonWebApp(
                        text=current.text or "Настройки",
                        web_app=WebAppInfo(url=target),
                    ),
                )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Telegram: не удалось обновить кнопку Web App: "
                    f"{self._mask(str(exc))}"
                )
        if updated:
            self._menu_url_synced = target
            logger.info(f"Telegram: кнопка Web App обновлена на {target}")

    def _web_app_url(self) -> str:
        """Публичный HTTPS-адрес Mini App: конфиг или авттуннель."""
        url = (getattr(self.cfg, "web_app_url", "") or "").strip()
        if url.lower().startswith(("https://", "http://")):
            return url
        tunnel = getattr(self, "_tunnel", None)
        if tunnel is not None:
            public = getattr(tunnel, "public_url", "") or ""
            if public:
                return public
        return ""

    async def _cmd_panel(self, update: Any, context: Any) -> None:
        """`/panel` — настройки в Telegram Mini App."""
        if not self._authorized(update):
            await self._deny(update)
            return
        chat = update.effective_chat
        if getattr(chat, "type", None) != "private":
            await update.message.reply_text(
                "Панель настроек доступна только в личке с ботом."
            )
            return
        chat_id = chat.id
        self._settings_pending.pop(chat_id, None)
        url = self._web_app_url()
        tunnel = getattr(self, "_tunnel", None)
        if not url and tunnel is not None and getattr(tunnel, "starting", False):
            await update.message.reply_text(
                "⏳ Поднимаю HTTPS-туннель для Web App. Попробуй /panel через "
                "несколько секунд."
            )
            return
        if not url and tunnel is not None and getattr(tunnel, "failed", False):
            await update.message.reply_text(
                "⚠️ Автотуннель не поднялся: "
                f"{getattr(tunnel, 'error', '')[:250]}. Открываю inline-резерв."
            )
        if url:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚙️ Открыть настройки",
                            web_app=WebAppInfo(url=url),
                        )
                    ]
                ]
            )
            try:
                await update.message.reply_text(
                    "⚙️ <b>Настройки Немуси</b>\n\nОткрой Mini App — это те же "
                    "разделы, что в GUI.",
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Telegram: Web App не отправлен ({self._mask(str(exc))}) — inline fallback")
        await self._show_settings(chat_id, "root")

    async def _apply_settings_text(
        self, update: Any, text: str
    ) -> bool:
        """Применяет ответ на «Отправь новое значение»; False — обычная задача."""
        chat = getattr(update, "effective_chat", None)
        if chat is None or getattr(chat, "type", None) != "private":
            return False
        chat_id = chat.id
        pending = self._settings_pending.get(chat_id)
        if pending is None:
            return False
        section_id, field_id, index, page = pending
        user_id = getattr(getattr(update, "effective_user", None), "id", None)

        def mutate(cfg: Config) -> tuple[bool, str]:
            if field_id == "__mcp_add__":
                return settings_ui.add_mcp_server(cfg, text)
            if index is not None:
                return settings_ui.mcp_apply_text(cfg, index, field_id, text)
            item = settings_ui.find_field(section_id, field_id)
            if item is None:
                return False, "Поле не найдено."
            if item.path == ("telegram", "token") and not text.strip():
                return False, "Токен не может быть пустым."
            ok, message = settings_ui.apply_text(cfg, item, text)
            if not ok:
                return False, message
            if item.path == ("telegram", "allowed_users") and user_id not in cfg.telegram.allowed_users:
                return False, "Нельзя убрать свой ID — потеряешь доступ к боту."
            return True, message

        ok, message = self._save_settings(mutate)
        if not ok:
            await update.message.reply_text(f"❌ {message}")
            return True

        self._settings_pending.pop(chat_id, None)
        if field_id == "__mcp_add__":
            await self._show_settings(chat_id, "mcp", message_id=self._settings_message.get(chat_id))
        elif index is not None:
            await self._show_settings(
                chat_id, "mcp", message_id=self._settings_message.get(chat_id)
            )
        else:
            await self._show_settings(
                chat_id,
                section_id,
                page=page,
                message_id=self._settings_message.get(chat_id),
            )
        return True

    async def _on_settings_callback(self, query: Any) -> None:
        """Кнопки панели настроек: обычные callback-запросы, без виджетов."""
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None)
        chat_id = getattr(message, "chat_id", None) or getattr(chat, "id", None)
        if chat_id is None or getattr(chat, "type", None) != "private":
            await query.answer("Панель доступна только в личке", show_alert=True)
            return
        chat_id = int(chat_id)
        parts = str(getattr(query, "data", "") or "").split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "c":
            self._settings_pending.pop(chat_id, None)
            self._settings_message.pop(chat_id, None)
            await query.answer("Панель закрыта")
            try:
                await message.delete()
            except Exception:  # noqa: BLE001
                pass
            return
        if action == "r":
            self._settings_pending.pop(chat_id, None)
            await self._show_settings(chat_id, "root", query=query)
            return
        if action == "x":
            pending = self._settings_pending.pop(chat_id, None)
            section_id, page = ("root", 0)
            if pending is not None:
                section_id, page = pending[0], pending[3]
            await self._show_settings(
                chat_id, section_id, page=page, query=query, toast="Ввод отменён"
            )
            return
        if action == "s" and len(parts) >= 3:
            section_id = parts[2]
            try:
                page = int(parts[3]) if len(parts) > 3 else 0
            except ValueError:
                page = 0
            await self._show_settings(chat_id, section_id, page=page, query=query)
            return
        if action == "ske":
            def mutate_skills(cfg: Config) -> tuple[bool, str]:
                cfg.skills.enabled = not cfg.skills.enabled
                return True, "Скиллы включены" if cfg.skills.enabled else "Скиллы выключены"

            ok, message = self._save_settings(mutate_skills)
            await self._show_settings(
                chat_id, "skills", query=query, toast=message[:180]
            )
            return
        if action == "cleanup":
            await self._cleanup_preview(
                query, yes="pn:cleanup_yes", back="pn:s:tools:0"
            )
            return
        if action == "cleanup_yes":
            await self._cleanup_run(query, back="pn:s:tools:0")
            return
        if action == "auto":
            state = self._autostart_state(force=True)
            if state is None:
                await query.answer("Автозапуск доступен только в Windows", show_alert=True)
                return
            try:
                from nemus import telegram_autostart

                ok, error = telegram_autostart.set_enabled(not state)
            except Exception as exc:  # noqa: BLE001
                ok, error = False, str(exc)
            self._autostart_cached_at = 0.0
            await self._show_settings(
                chat_id,
                "telegram",
                query=query,
                toast="Автозапуск изменён" if ok else error[:180],
            )
            return
        if action in ("t", "y", "+", "-") and len(parts) >= 4:
            section_id, field_id = parts[2], parts[3]
            item = settings_ui.find_field(section_id, field_id)
            if item is None:
                await query.answer("Поле не найдено", show_alert=True)
                return

            def mutate(cfg: Config) -> tuple[bool, str]:
                if action == "t":
                    return settings_ui.toggle_field(cfg, item)
                if action == "y":
                    return settings_ui.cycle_field(cfg, item)
                return settings_ui.step_field(cfg, item, 1 if action == "+" else -1)

            ok, message = self._save_settings(mutate)
            await self._show_settings(
                chat_id,
                section_id,
                query=query,
                toast=message if ok else message[:180],
            )
            return
        if action == "e" and len(parts) >= 4:
            section_id, field_id = parts[2], parts[3]
            item = settings_ui.find_field(section_id, field_id)
            if item is None:
                await query.answer("Поле не найдено", show_alert=True)
                return
            await self._show_edit_prompt(
                chat_id,
                section_id,
                field_id,
                label=item.label,
                query=query,
            )
            return
        if action == "k" and len(parts) >= 4:
            try:
                page, index = int(parts[2]), int(parts[3])
            except ValueError:
                await query.answer("Кнопка устарела", show_alert=True)
                return
            entries = settings_ui.skills_entries(self.config)
            if not 0 <= index < len(entries):
                await query.answer("Список скиллов изменился", show_alert=True)
                return
            name = entries[index][0]

            def mutate(cfg: Config) -> tuple[bool, str]:
                return settings_ui.toggle_skill(cfg, name)

            ok, message = self._save_settings(mutate)
            await self._show_settings(
                chat_id, "skills", page=page, query=query, toast=message if ok else message[:180]
            )
            return
        if action == "ma":
            await self._show_edit_prompt(
                chat_id,
                "mcp",
                "__mcp_add__",
                label="Команда MCP-сервера (например: npx -y @modelcontextprotocol/server-filesystem .)",
                query=query,
            )
            return
        if action == "m" and len(parts) >= 3:
            try:
                index = int(parts[2])
            except ValueError:
                await query.answer("Кнопка устарела", show_alert=True)
                return
            text = settings_ui.mcp_detail_text(self.config, index)
            if text is None:
                await query.answer("Сервер исчез", show_alert=True)
                return
            await query.answer()
            keyboard = self._keyboard(settings_ui.mcp_detail_keyboard(index))
            try:
                await query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=keyboard
                )
            except Exception:  # noqa: BLE001
                await self._app.bot.send_message(
                    chat_id, text, parse_mode="HTML", reply_markup=keyboard
                )
            return
        if action == "mt" and len(parts) >= 3:
            try:
                index = int(parts[2])
            except ValueError:
                await query.answer("Кнопка устарела", show_alert=True)
                return

            def mutate(cfg: Config) -> tuple[bool, str]:
                if not 0 <= index < len(cfg.mcp.servers):
                    return False, "Сервер исчез."
                cfg.mcp.servers[index].enabled = not cfg.mcp.servers[index].enabled
                return True, "Сохранено."

            ok, message = self._save_settings(mutate)
            await query.answer(message[:180])
            text = settings_ui.mcp_detail_text(self.config, index)
            if text is not None:
                keyboard = self._keyboard(settings_ui.mcp_detail_keyboard(index))
                try:
                    await query.edit_message_text(
                        text, parse_mode="HTML", reply_markup=keyboard
                    )
                except Exception:  # noqa: BLE001
                    await self._app.bot.send_message(
                        chat_id, text, parse_mode="HTML", reply_markup=keyboard
                    )
            return
        if action == "me" and len(parts) >= 4:
            try:
                index = int(parts[2])
            except ValueError:
                await query.answer("Кнопка устарела", show_alert=True)
                return
            labels = {
                "name": "Название сервера",
                "command": "Команда запуска",
                "args": "Аргументы (через пробел)",
                "cwd": "Рабочая папка",
                "env": "Переменные: KEY=значение, KEY2=значение",
            }
            field_id = parts[3]
            await self._show_edit_prompt(
                chat_id,
                "mcp",
                field_id,
                index=index,
                label=labels.get(field_id, field_id),
                query=query,
            )
            return
        if action == "md" and len(parts) >= 3:
            try:
                index = int(parts[2])
            except ValueError:
                await query.answer("Кнопка устарела", show_alert=True)
                return

            def mutate(cfg: Config) -> tuple[bool, str]:
                if not 0 <= index < len(cfg.mcp.servers):
                    return False, "Сервер исчез."
                cfg.mcp.servers.pop(index)
                return True, "Сервер удалён."

            ok, message = self._save_settings(mutate)
            await self._show_settings(
                chat_id, "mcp", query=query, toast=message[:180]
            )
            return
        await query.answer("Кнопка устарела")

    # -------------------------------------------------------------- отмена
    def _request_stop(
        self,
        chat_id: int,
        *,
        draft_id: int | None = None,
        task_id: int | None = None,
    ) -> bool:
        """Ставит отмену текущей задачи, если сигнал относится именно к ней."""
        event = self._stop_event
        if (
            event is None
            or self._current_task_chat != chat_id
            or self._current_task_id is None
        ):
            return False
        if task_id is not None and task_id != self._current_task_id:
            return False
        if draft_id is not None and draft_id != self._draft_id:
            return False
        if event.is_set():
            return True
        event.set()
        self._deny_pending_confirmations(chat_id)
        progress = self._active_progress
        if progress is not None:
            try:
                self._stop_note_task = asyncio.get_running_loop().create_task(
                    progress.note("⏹ Останавливаю…")
                )
            except RuntimeError:
                pass  # задача уже завершилась
        return True

    async def _cancel_stop_note(self) -> None:
        """Гасит заметку Stop до закрытия прогресса, чтобы не было гонки."""
        task = getattr(self, "_stop_note_task", None)
        self._stop_note_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def _deny_pending_confirmations(self, chat_id: int) -> None:
        """Отклоняет все ждущие подтверждения этого чата."""
        prefix = f"{chat_id}:"
        for key, (future, _text) in list(self._pending.items()):
            if key.startswith(prefix) and not future.done():
                future.set_result(False)

    async def _on_raw_update(self, update: Any, context: Any) -> None:
        """Неизвестные PTB поля Update: остановка генерации."""
        kwargs = getattr(update, "api_kwargs", None) or {}
        stopped = kwargs.get("stopped_message_generation")
        if isinstance(stopped, dict):
            chat = stopped.get("chat")
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            if chat_id is None:
                chat_id = getattr(
                    getattr(update, "effective_chat", None), "id", None
                )
            if chat_id is not None:
                draft_id = stopped.get("draft_id")
                if isinstance(draft_id, str) and draft_id.isdigit():
                    draft_id = int(draft_id)
                if not isinstance(draft_id, int):
                    draft_id = None
                self._request_stop(int(chat_id), draft_id=draft_id)

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
            # Ждём короткими шагами: Stop должен освободить агентский поток
            # сразу, а не через CONFIRM_TIMEOUT.
            deadline = time.monotonic() + CONFIRM_TIMEOUT + 30
            while True:
                if self._stop_event is not None and self._stop_event.is_set():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    return bool(future.result(timeout=min(0.25, remaining)))
                except TimeoutError:
                    continue
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
        # Регистрируем future до отправки: иначе очень быстрый клик по кнопке
        # увидит «кнопка устарела» и агент будет ждать до таймаута.
        self._pending[key] = (future, text[:3500])
        try:
            message = await self._app.bot.send_message(
                chat_id, text[:3900], reply_markup=keyboard
            )
        except Exception:
            self._pending.pop(key, None)
            raise
        stopped = False
        try:
            deadline = self._loop.time() + CONFIRM_TIMEOUT
            while True:
                if self._stop_event is not None and self._stop_event.is_set():
                    stopped = True
                    if not future.done():
                        future.set_result(False)
                    return False
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    allowed = bool(
                        await asyncio.wait_for(
                            asyncio.shield(future),
                            timeout=min(0.25, remaining),
                        )
                    )
                except asyncio.TimeoutError:
                    continue
                if not allowed and (
                    self._stop_event is not None and self._stop_event.is_set()
                ):
                    stopped = True
                return allowed
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
            if stopped:
                try:
                    await message.edit_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass

    def _ask_torrent(
        self, chat_id: int, user_id: int, text: str
    ) -> asyncio.Future[bool]:
        """Спрашивает согласие на торрент инлайн-кнопками ``td:``.

        Возвращает future сразу, чтобы вызывающий мог ждать её в своём потоке.
        Ключ в ``_pending`` привязан к чату и человеку; тот же ключ разбирает
        ``_on_callback``. Таймаут и Stop обрабатывает ``_wait_torrent``.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise RuntimeError(
                    "Нет event loop для инлайн-согласия на торрент"
                ) from exc
        key = f"{int(chat_id)}:{int(user_id)}:{time.time_ns()}"
        future: asyncio.Future[bool] = loop.create_future()
        original = f"⬇️ Скачать через торрент?\n\n{str(text or '')}"[:3500]
        markup = self._keyboard([
            [
                ("⬇️ Скачать через торрент", f"td:{key}:y"),
                ("❌ Отмена", f"td:{key}:n"),
            ]
        ])
        # Регистрируем future до отправки: быстрый клик не должен видеть
        # «кнопка устарела» и заставлять пользователя ждать таймаута.
        self._pending[key] = (future, original)
        try:
            asyncio.run_coroutine_threadsafe(
                self._wait_torrent(chat_id, key, future, original, markup, loop),
                loop,
            )
        except RuntimeError:
            self._pending.pop(key, None)
            if not future.done():
                future.set_result(False)
            raise
        return future

    async def _wait_torrent(
        self,
        chat_id: int,
        key: str,
        future: asyncio.Future,
        original: str,
        markup: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Отправляет запрос и гасит его по кнопке, Stop или таймауту."""
        message: Any = None
        stopped = False
        timed_out = False
        try:
            try:
                message = await self._app.bot.send_message(
                    chat_id, original[:3900], reply_markup=markup
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Telegram: не удалось спросить согласие на торрент: {exc}")
                if not future.done():
                    future.set_result(False)
                return False

            deadline = loop.time() + CONFIRM_TIMEOUT
            while True:
                if self._stop_event is not None and self._stop_event.is_set():
                    stopped = True
                    if not future.done():
                        future.set_result(False)
                    return False
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    if not future.done():
                        future.set_result(False)
                    return False
                try:
                    return bool(
                        await asyncio.wait_for(
                            asyncio.shield(future), timeout=min(0.25, remaining)
                        )
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            self._pending.pop(key, None)
            if timed_out:
                try:
                    await self._app.bot.send_message(
                        chat_id,
                        "⌛ Время на согласие истекло — торрент не запущен.",
                    )
                except Exception:  # noqa: BLE001
                    pass
            if (stopped or timed_out) and message is not None:
                try:
                    await message.edit_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass

    async def _callback_message(
        self, query: Any, text: str, reply_markup: Any | None = None
    ) -> None:
        """Ответ на кнопку: в группе — ephemeral, иначе обычное сообщение.

        Telegram требует отдельно ответить на callback query. В группе
        сначала отправляем ephemeral и только при успехе отвечаем на
        callback; при ошибке — один alert, без публичного спама.
        """
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        chat_id = getattr(message, "chat_id", None) or getattr(chat, "id", None)
        api = getattr(self, "_api", None)
        advanced = bool(getattr(self.cfg, "advanced_ui", True))
        if (
            chat_type in ("group", "supergroup")
            and advanced
            and api is not None
            and api.available("sendMessage")
            and chat_id is not None
        ):
            try:
                sent = await api.send_ephemeral(
                    chat_id=int(chat_id),
                    callback_query_id=str(getattr(query, "id", "")),
                    html=to_html(text),
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "Telegram: ephemeral не отправлен (%s) — отвечаю alert'ом",
                    self._mask(str(exc)),
                )
                await query.answer(text[:200], show_alert=True)
                return
            if sent is not None:
                await query.answer()
                return
            await query.answer(text[:200], show_alert=True)
            return
        await query.answer()
        await start_message(
            self._app.bot, int(chat_id), text, reply_markup=reply_markup
        )

    async def _callback_edit(
        self, query: Any, text: str, reply_markup: Any | None = None
    ) -> None:
        """Показывает следующий экран справки в том же сообщении.

        В личке редактируем исходное сообщение, чтобы навигация не плодила
        новые сообщения. Если отредактировать нельзя (старое сообщение,
        несколько чанков, группа) — отправляем обычный ответ.
        """
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        chat_id = getattr(message, "chat_id", None) or getattr(chat, "id", None)
        edit = getattr(query, "edit_message_text", None)
        if (
            chat_type in ("group", "supergroup")
            or edit is None
            or chat_id is None
        ):
            await self._callback_message(query, text, reply_markup)
            return
        await query.answer()
        chunks = html_chunks(text) or ["…"]
        if len(chunks) != 1:
            await start_message(
                self._app.bot, int(chat_id), text, reply_markup=reply_markup
            )
            return
        try:
            await edit(chunks[0], parse_mode="HTML", reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc).lower()
            if "not modified" in reason:
                return
            logger.debug(f"Telegram: не удалось изменить сообщение: {exc}")
            await start_message(
                self._app.bot, int(chat_id), text, reply_markup=reply_markup
            )

    async def _cleanup_preview(
        self, query: Any, *, yes: str, back: str
    ) -> None:
        """Показывает, сколько старых файлов найдено, и спрашивает про удаление."""
        chat = getattr(getattr(query, "message", None), "chat", None)
        if getattr(chat, "type", None) != "private":
            await query.answer(
                "Очистка доступна только в личке", show_alert=True
            )
            return
        workspace = Path(self.config.tools.workspace)
        result = await asyncio.to_thread(
            cleanup_storage,
            workspace,
            days=DEFAULT_CLEANUP_DAYS,
            dry_run=True,
            tmp_dir=DATA_DIR / "tmp",
        )
        if result.candidates == 0:
            await self._callback_edit(
                query,
                f"🧹 Старых файлов старше {DEFAULT_CLEANUP_DAYS} дней не найдено.",
                self._keyboard([[("⬅️ Назад", back)]]),
            )
            return
        text = (
            "🧹 <b>Очистка старых файлов</b>\n\n"
            f"Старше {DEFAULT_CLEANUP_DAYS} дней: <b>{result.candidates}</b> "
            f"файлов, {human_bytes(result.candidate_bytes)}.\n\n"
            "Удалить их?"
        )
        await self._callback_edit(
            query,
            text,
            self._keyboard(
                [[("🧹 Удалить", yes)], [("⬅️ Назад", back)]]
            ),
        )

    async def _cleanup_run(self, query: Any, *, back: str) -> None:
        """Удаляет старые файлы и показывает итог."""
        chat = getattr(getattr(query, "message", None), "chat", None)
        if getattr(chat, "type", None) != "private":
            await query.answer(
                "Очистка доступна только в личке", show_alert=True
            )
            return
        workspace = Path(self.config.tools.workspace)
        result = await asyncio.to_thread(
            cleanup_storage,
            workspace,
            days=DEFAULT_CLEANUP_DAYS,
            tmp_dir=DATA_DIR / "tmp",
        )
        text = (
            "✅ <b>Очистка завершена</b>\n\n"
            f"Удалено файлов: <b>{result.removed}</b>\n"
            f"Освобождено: {human_bytes(result.freed)}"
        )
        if result.failed:
            text += (
                f"\n\n⚠️ Не удалось удалить файлов: {result.failed} — "
                "возможно, они заняты другой программой."
            )
        await self._callback_edit(
            query, text, self._keyboard([[("⬅️ Назад", back)]])
        )

    async def _on_ux_callback(self, update: Any, query: Any) -> None:
        """Кнопки подсказок: действия вложений, помощь и примеры."""
        action = (query.data or "")[3:]
        if action == "cleanup":
            await self._cleanup_preview(
                query, yes="ux:cleanup_yes", back="ux:help"
            )
            return
        if action == "cleanup_yes":
            await self._cleanup_run(query, back="ux:help")
            return
        if action == "h":
            await self._callback_edit(
                query,
                command_home_text(),
                self._keyboard(command_home_rows()),
            )
            return
        if action.startswith("hc:"):
            category_id = action.split(":", 1)[1]
            await self._callback_edit(
                query,
                command_category_text(category_id),
                self._keyboard(command_category_rows(category_id)),
            )
            return
        if action.startswith("hx:"):
            name = action.split(":", 1)[1]
            await self._callback_edit(
                query,
                command_detail_text(name),
                self._keyboard([[("⬅️ Все команды", "ux:h")]]),
            )
            return
        if action == "skills":
            chat_id = query.message.chat_id
            await self._callback_message(
                query,
                self._skills_text(),
                self._keyboard(main_menu_rows(self._hints_active(chat_id))),
            )
            return
        if action.startswith("attach:"):
            key = action.split(":", 1)[1]
            prompt = attachment_prompt(key)
            chat_id = query.message.chat_id
            user_id = getattr(query.from_user, "id", None)
            message_id = getattr(query.message, "message_id", None)
            if prompt is None:
                await query.answer("Неизвестное действие")
                return
            if not self._hints_active(chat_id):
                await query.answer(
                    "Подсказки выключены в этом чате", show_alert=True
                )
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
                return
            if not self._pending_attachment_matches(
                chat_id, message_id, user_id
            ):
                await query.answer(
                    "Вложение уже не актуально — пришли его ещё раз",
                    show_alert=True,
                )
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
                return
            pending, images = self._take_pending_attachment(chat_id, user_id)
            await query.answer("Запускаю")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            text = f"{pending}\n\nЗадача пользователя: {prompt}"
            await self._run_task_for(
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                text=text,
                images=images,
                chat_type=getattr(
                    getattr(query.message, "chat", None), "type", None
                ),
            )
            return
        if action == "mute":
            chat_id = query.message.chat_id
            self._set_hint_muted(chat_id, True)
            self._mark_hint_shown(chat_id)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            await query.answer("Больше не буду показывать подсказки в этом чате")
            return
        if action == "new":
            chat_id = query.message.chat_id
            lock = getattr(self, "_task_lock", None)
            if lock is not None and lock.locked():
                await query.answer(
                    "Сначала дождись завершения текущей задачи",
                    show_alert=True,
                )
                return
            self._clear_pending_attachments(chat_id)
            self._new_session_for_chat(chat_id)
            self._last_tasks.pop(chat_id, None)
            self._last_task_ids.pop(chat_id, None)
            await self._callback_message(
                query,
                "🧹 Контекст очищен — начинаем заново.",
                self._keyboard(main_menu_rows(self._hints_active(chat_id))),
            )
            return
        if action == "status":
            chat_id = query.message.chat_id
            await self._callback_message(query, self._status_text(chat_id))
            return
        if action == "retry":
            chat_id = query.message.chat_id
            last = self._last_tasks.get(chat_id)
            if last is None:
                await query.answer("Пока нечего повторять", show_alert=True)
                return
            text, images, message_id, user_id = last
            await query.answer("Повторяю")
            await self._run_task_for(
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                text=text,
                images=images,
                chat_type=getattr(
                    getattr(query.message, "chat", None), "type", None
                ),
                retry=True,
            )
            return
        if action == "help":
            chat_id = query.message.chat_id
            hints = self._hints_active(chat_id)
            await self._callback_message(
                query,
                help_text(hints),
                self._keyboard(main_menu_rows(hints)),
            )
            return
        if action == "examples":
            chat_id = query.message.chat_id
            if not self._hints_active(chat_id):
                await query.answer(
                    "Подсказки выключены в этом чате", show_alert=True
                )
                return
            await self._callback_message(
                query,
                examples_text(),
                self._keyboard(main_menu_rows(True)),
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
        if data.startswith("bs:"):
            await self._on_brainstorm_callback(query)
            return
        if data.startswith("pn:"):
            await self._on_settings_callback(query)
            return
        if data.startswith("ux:"):
            await self._on_ux_callback(update, query)
            return
        if data.startswith("td:"):
            await self._on_torrent_callback(query, data)
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

    async def _on_torrent_callback(self, query: Any, data: str) -> None:
        """Разбирает ``td:<chat>:<user>:<ns>:y|n`` и решает future согласия."""
        parts = data.split(":")
        if len(parts) != 5 or parts[0] != "td" or parts[4] not in ("y", "n"):
            await query.answer("Кнопка устарела")
            return
        try:
            chat_id, user_id, namespace = (int(part) for part in parts[1:4])
        except ValueError:
            await query.answer("Кнопка устарела")
            return
        actual_chat = getattr(query.message, "chat_id", None)
        if actual_chat is None:
            actual_chat = getattr(getattr(query.message, "chat", None), "id", None)
        actual_user = getattr(query.from_user, "id", None)
        if actual_chat != chat_id or actual_user != user_id:
            await query.answer("Кнопка не для этого чата", show_alert=True)
            return
        key = f"{chat_id}:{user_id}:{namespace}"
        entry = self._pending.get(key)
        if entry is None:
            await query.answer("Кнопка устарела")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return
        future, original = entry
        allowed = parts[4] == "y"
        if not future.done():
            future.set_result(allowed)
        await query.answer("Разрешено" if allowed else "Отклонено")
        try:
            await query.edit_message_text(
                f"{original}\n\n"
                + ("✅ Скачиваем через торрент" if allowed else "❌ Отмена"),
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
