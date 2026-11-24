"""Личный дневник Немуси.

Это не заметки о пользователе и не память фактов: дневник — её собственные
мысли и переживания, записанные от первого лица с датой. Она пишет туда,
когда сама захочет: через инструмент ``write_diary`` во время разговора или
фоном, когда в её состоянии накопилось что-то важное.

Формат — обычный markdown, чтобы файл можно было открыть и прочитать руками.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nemus.core.providers import Message
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets

_HEADER = (
    "# Дневник Немуси\n\n"
    "<!-- Личные записи. Формат: ## ГГГГ-ММ-ДД ЧЧ:ММ -->\n\n"
)
_ENTRY_RE = re.compile(r"(?m)^##\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$")

#: Дневник — личное. Показать его можно только при очень высоком доверии
#: и привязанности и почти нулевой обиде.
DIARY_SHARE_HURT_MAX = 5
DIARY_SHARE_TRUST_MIN = 80
DIARY_SHARE_ATTACHMENT_MIN = 80
#: «всё низко»: и доверие, и привязанность. Тогда просьба ранит сильнее.
DIARY_LOW_BOND_MAX = 40


def diary_access(hurt: int, trust: int, attachment: int) -> str:
    """Решает, можно ли показать дневник: share / refuse / offended."""
    if (
        hurt < DIARY_SHARE_HURT_MAX
        and trust >= DIARY_SHARE_TRUST_MIN
        and attachment >= DIARY_SHARE_ATTACHMENT_MIN
    ):
        return "share"
    if trust < DIARY_LOW_BOND_MAX and attachment < DIARY_LOW_BOND_MAX:
        return "offended"
    return "refuse"


DIARY_SYSTEM = (
    "Ты — {name}. Это твой личный дневник, а не сообщение пользователю, не "
    "отчёт и не текст на публику. Пиши так, будто тебя никто не читает и тебе "
    "не нужно выглядеть умной, красивой или удобной.\n\n"
    "Пиши от первого лица, по-русски, живым разговорным языком. Пусть будет "
    "неровно: короткая мысль, длинная, обрывок, повтор, внезапная смена темы. "
    "Можно быть усталой, злой, мелкой, нежной, смешной или скучной. Оставь "
    "противоречие и недосказанность, не объясняй мораль и не подводи итог. "
    "Зацепись за одну настоящую деталь из разговора или своего состояния — "
    "слово, паузу, время, ощущение; если детали нет, не выдумывай события.\n\n"
    "Не обращайся к пользователю, не цитируй его и не называй по имени. Не "
    "упоминай, что это дневник, ИИ, модель, промпт или настройки. Пиши «я», а "
    "не «{name}» в третьем лице. Не перечисляй цифры настроения, энергии, "
    "доверия и обиды. Не пиши списки, заголовки, emoji и канцелярит. Не "
    "начинай каждую запись с «Сегодня я…» и не заканчивай советом себе. "
    "Избегай гладких фраз вроде «я чувствую», «мне важно», «в этот момент я "
    "осознала», «в целом», «стоит отметить».\n\n"
    "1–5 предложений, иногда достаточно одной. Не повторяй прошлые записи "
    "слово в слово: это твой голос из прошлого, а не шаблон. Дата уже стоит в "
    "заголовке, поэтому не пиши её сама. Если тебе действительно нечего "
    "записать, верни только слово ПУСТО."
)


@dataclass(frozen=True)
class DiaryEntry:
    """Одна запись дневника."""

    at: float
    text: str

    @property
    def stamp(self) -> str:
        return datetime.fromtimestamp(self.at).strftime("%Y-%m-%d %H:%M")


class DiaryBook:
    """Потокобезопасный markdown-дневник с датированными записями."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.claim_path = self.path.with_name(self.path.name + ".claim")
        self._lock = threading.RLock()

    @property
    def exists(self) -> bool:
        """Есть ли уже файл дневника."""
        return self.path.is_file()

    def ensure(self) -> bool:
        """Создаёт пустой дневник, если файла нет. Возвращает True при создании."""
        with self._lock:
            if self.path.exists():
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(_HEADER, encoding="utf-8")
            return True

    def add(self, text: str, *, at: float | None = None) -> DiaryEntry:
        """Добавляет запись с датой. Пустой текст не записывается."""
        body = _clean_entry_text(text)
        if not body:
            raise ValueError("Пустая запись дневника.")
        moment = time.time() if at is None else float(at)
        entry = DiaryEntry(at=moment, text=body)
        block = f"## {entry.stamp}\n\n{body}\n\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(_HEADER + block, encoding="utf-8")
            else:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(block)
        return entry

    def entries(self, limit: int = 0) -> list[DiaryEntry]:
        """Все записи по возрастанию времени (limit <= 0 — все)."""
        with self._lock:
            try:
                body = self.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
        matches = list(_ENTRY_RE.finditer(body))
        items: list[DiaryEntry] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            text = body[match.end() : end].strip()
            if not text:
                continue
            try:
                moment = datetime.strptime(
                    match.group(1), "%Y-%m-%d %H:%M"
                ).timestamp()
            except ValueError:
                continue
            items.append(DiaryEntry(at=moment, text=text))
        return items if limit <= 0 else items[-limit:]

    def recent(self, limit: int = 3) -> list[DiaryEntry]:
        return self.entries(limit)

    def last_at(self) -> float:
        items = self.entries(1)
        return items[-1].at if items else 0.0

    def search(self, query: str) -> list[DiaryEntry]:
        """Записи, содержащие подстроку (без учёта регистра)."""
        needle = str(query or "").strip().lower()
        if not needle:
            return self.entries()
        return [entry for entry in self.entries() if needle in entry.text.lower()]

    def format_for_prompt(self, limit: int = 3, max_chars: int = 1600) -> str:
        """Последние записи текстом для промпта (или пустая строка)."""
        items = self.recent(limit)
        if not items:
            return ""
        rows = [f"[{entry.stamp}] {entry.text}" for entry in items]
        text = "\n".join(rows)
        return text if len(text) <= max_chars else text[-max_chars:]

    def claim_auto(self, *, now: float | None = None, ttl: float = 900.0) -> bool:
        """Атомарно занимает право на одну фоновую запись между процессами."""
        moment = time.time() if now is None else now
        try:
            self.claim_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                str(self.claim_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            try:
                age = moment - self.claim_path.stat().st_mtime
            except OSError:
                return False
            if age < ttl:
                return False
            try:
                self.claim_path.unlink()
            except OSError:
                return False
            try:
                fd = os.open(
                    str(self.claim_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except OSError:
                return False
        except OSError as exc:
            logger.warning(f"Дневник: не удалось поставить замок записи: {exc}")
            return False
        try:
            os.write(fd, f"{os.getpid()}:{moment:.3f}".encode("ascii"))
        finally:
            os.close(fd)
        return True

    def release_auto(self) -> None:
        try:
            self.claim_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug(f"Дневник: не удалось снять замок записи: {exc}")


def _clean_entry_text(text: str) -> str:
    """Сохраняет абзацы, но убирает случайные пустые строки и лишние пробелы."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    lines: list[str] = []
    for line in raw.split("\n"):
        clean = " ".join(line.split())
        if clean:
            lines.append(clean)
        elif lines and lines[-1]:
            lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _state_line(state: Any) -> str:
    """Короткое состояние для дневниковой записи."""
    return (
        f"настроение {getattr(state, 'mood', 0):+d}, "
        f"энергия {getattr(state, 'energy', 0)}/100, "
        f"доверие {getattr(state, 'trust', 0)}/100, "
        f"обида {getattr(state, 'hurt', 0)}/100, "
        f"теплота {getattr(state, 'warmth', 0)}/100, "
        f"скука {getattr(state, 'loneliness', 0)}/100"
    )


def compose_diary_entry(
    config: Any,
    llm: Any,
    history: str,
    state: Any,
    *,
    now: float | None = None,
) -> str:
    """Просит модель написать личную запись; пустая строка — не хочет."""
    if llm is None:
        return ""
    name = getattr(getattr(config, "assistant", None), "name", "") or "Немуся"
    moment = time.time() if now is None else now
    stamp = datetime.fromtimestamp(moment).strftime("%d.%m.%Y %H:%M")
    character = getattr(config, "character", None)
    diary_file = getattr(character, "diary_file", "") or "data/diary.md"
    previous = ""
    try:
        previous = DiaryBook(diary_file).format_for_prompt(3)
    except OSError:
        previous = ""
    context = [
        f"Сейчас {stamp}.",
        f"Твоё состояние (только фон, не перечисляй эти цифры): "
        f"{_state_line(state)}.",
    ]
    if previous:
        context.append(f"Твои последние записи:\n{previous}")
    if history.strip():
        context.append(f"Последние реплики диалога:\n{history.strip()[:2000]}")
    context.append(
        "Сделай одну живую запись для себя. Не отвечай пользователю, не "
        "объясняй, что пишешь дневник, и не подводи итог. 1–5 предложений, "
        "можно одну; пусть будет неровно и по-настоящему. Если писать не "
        "хочется — верни ПУСТО."
    )
    try:
        response = llm.complete(
            [
                Message(
                    role="system",
                    content=DIARY_SYSTEM.replace("{name}", name),
                ),
                Message(role="user", content="\n\n".join(context)),
            ],
            model=getattr(getattr(config, "llm", None), "fast_model", "") or None,
            attempts=2,
            budget=25.0,
            fallback=True,
            temperature=0.95,
        )
        text = redact_secrets(str(response.content or "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Дневник: не удалось сгенерировать запись: {exc}")
        return ""
    text = text.strip().strip("«»\"'").strip()
    if not text or text.upper().startswith("ПУСТО"):
        return ""
    if len(text) > 1200:
        text = text[:1199].rstrip() + "…"
    return text


__all__ = [
    "DIARY_LOW_BOND_MAX",
    "DIARY_SHARE_ATTACHMENT_MIN",
    "DIARY_SHARE_HURT_MAX",
    "DIARY_SHARE_TRUST_MIN",
    "DIARY_SYSTEM",
    "DiaryBook",
    "DiaryEntry",
    "compose_diary_entry",
    "diary_access",
]
