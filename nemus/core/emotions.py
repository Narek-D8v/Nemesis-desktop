"""Характер и эмоциональное состояние Немуси.

Черты характера задаются в конфиге, а текущие чувства хранятся отдельно:
настроение, энергия, доверие, обида и скука меняются от общения и времени.
Фоновый :class:`EmotionManager` периодически проверяет, не пора ли написать
пользователю первой. Решение рождается из накопленной скуки, обиды и
привязанности, а не из расписания. Если на её сообщение долго не отвечают,
обида растёт сама.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Callable

from nemus.config import PROJECT_ROOT, CharacterConfig, Config
from nemus.core.autopilot import in_quiet_hours
from nemus.utils.logger import logger

#: как часто фоновая проверка смотрит на состояние, секунд
TICK_SECONDS = 60
#: сколько минут тишины после её сообщения считаются нормальными
IGNORE_GRACE_MINUTES = 7
#: базовая пауза после своего сообщения; скука и плохое настроение её сокращают
FOLLOWUP_BASE_MINUTES = 90
#: даже при сильной скуке она не долбит чат чаще, чем раз в столько минут
FOLLOWUP_MIN_MINUTES = 20
#: точка покоя меняющейся теплоты; в конфиге её больше нет
DEFAULT_WARMTH = 65

#: только явно адресные оскорбления, а не слова вроде «хуже»:
#: «эта ошибка хуже» — объяснение, а не грубость
_DIRECT_INSULTS = (
    "ты дура",
    "ты дурак",
    "ты тупая",
    "ты тупой",
    "ты глупая",
    "ты глупый",
    "ты бесполезн",
    "ты мне не нужна",
    "ты надоела",
    "ты мне надоела",
    "ты меня бесишь",
    "ты бесишь",
    "бесишь меня",
    "ненавижу тебя",
    "я тебя ненавижу",
    "заткнись",
    "отстань",
    "пошла ты",
    "иди ты",
)
#: короткие оскорбления, если всё сообщение состоит из одного слова
_SHORT_INSULTS = {
    "дура",
    "дурак",
    "тупая",
    "тупой",
    "глупая",
    "глупый",
    "бесишь",
}
#: объяснение отсутствия: оно снимает остроту обиды за молчание,
#: но не отменяет её совсем
_ABSENCE_EXPLANATIONS = (
    "не мог",
    "не могла",
    "не было возможности",
    "не было времени",
    "был занят",
    "была занята",
    "не видел",
    "не заметил",
    "не заметила",
    "устал",
    "устала",
    "спал",
    "спала",
    "на работе",
    "задержался",
    "задержалась",
    "объясняю",
    "потому что",
    "дело в том",
)


def _looks_like_insult(low: str) -> bool:
    """Грубость должна быть направлена на неё, а не на ситуацию."""
    if any(phrase in low for phrase in _DIRECT_INSULTS):
        return True
    return low.strip(" .,!?…;:\"'«»()[]") in _SHORT_INSULTS


def _explains_absence(low: str) -> bool:
    return any(marker in low for marker in _ABSENCE_EXPLANATIONS)


def _advance_repair(state: "EmotionState", amount: float) -> None:
    """Мягко двигает примирение, не задавая жёстких стадий."""
    if state.conflict_since > 0 and state.hurt > 0:
        state.repair_progress = _clamp(
            state.repair_progress + amount, 0, 100
        )


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


def _human_gap(seconds: float) -> str:
    """«12 мин», «3 ч», «2 д» вместо секунд."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    return f"{hours // 24} д"


def _bar(value: int, low: int = 0, high: int = 100, width: int = 10) -> str:
    """Полоска заполнения для карточки в Telegram."""
    span = max(1, high - low)
    filled = _clamp((value - low) / span * width, 0, width)
    return "▰" * filled + "▱" * (width - filled)


def mood_label(value: int) -> str:
    if value >= 60:
        return "воодушевлена"
    if value >= 20:
        return "тёплое"
    if value > -20:
        return "ровное"
    if value > -60:
        return "подавлена"
    return "мрачное"


def hurt_label(value: int) -> str:
    if value >= 65:
        return "сильно задето"
    if value >= 30:
        return "есть обида"
    if value > 0:
        return "слегка задето"
    return "без обиды"


def repair_label(value: int) -> str:
    """Насколько конфликт прожит — без жёстких стадий."""
    if value >= 70:
        return "почти примирилась"
    if value >= 35:
        return "постепенно остывает"
    return "обида ещё свежая"


def current_attachment(cfg: CharacterConfig, state: EmotionState) -> int:
    """Текущая привязанность: живое чувство, а не только черта характера."""
    return cfg.attachment if state.attachment is None else state.attachment


@dataclass
class EmotionState:
    """Текущее состояние; сохраняется в JSON рядом с остальными данными."""

    mood: int = 10
    energy: int = 60
    trust: int = 55
    hurt: int = 0
    loneliness: int = 0
    #: живая теплота и привязанность; теплота всегда меняющаяся, без базы
    warmth: int = DEFAULT_WARMTH
    #: None — привязанность ещё не менялась от базовой черты
    attachment: int | None = None
    last_user_at: float = 0.0
    last_proactive_at: float = 0.0
    proactive_day: str = ""
    proactive_count: int = 0
    last_user_text: str = ""
    #: текст последней инициативы — чтобы не повторяться и не здороваться заново
    last_proactive_text: str = ""
    #: когда она написала и ждёт ответа; 0 — ответ уже получен
    awaiting_since: float = 0.0
    #: обида на момент отправки — от неё считаем накопление за игнор
    awaiting_hurt: int = 0
    #: реальная причина обиды, чтобы не выдумывать её в ответе
    hurt_reason: str = ""
    #: конфликт и то, насколько он уже прожит; не жёсткая последовательность
    conflict_since: float = 0.0
    repair_progress: int = 0
    repair_note: str = ""
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmotionState":
        state = cls()
        for field_name in state.__dataclass_fields__:
            if field_name not in data:
                continue
            value = data[field_name]
            try:
                if field_name == "warmth":
                    state.warmth = (
                        DEFAULT_WARMTH if value is None else int(value)
                    )
                elif field_name == "attachment":
                    state.attachment = None if value is None else int(value)
                elif field_name in {
                    "mood",
                    "energy",
                    "trust",
                    "hurt",
                    "loneliness",
                    "proactive_count",
                    "awaiting_hurt",
                    "repair_progress",
                }:
                    setattr(state, field_name, int(value))
                elif field_name in {
                    "last_user_at",
                    "last_proactive_at",
                    "awaiting_since",
                    "updated_at",
                    "conflict_since",
                }:
                    setattr(state, field_name, float(value))
                else:
                    setattr(state, field_name, str(value or ""))
            except (TypeError, ValueError):
                continue
        state.mood = _clamp(state.mood, -100, 100)
        for name in ("energy", "trust", "hurt", "loneliness", "repair_progress"):
            setattr(state, name, _clamp(getattr(state, name), 0, 100))
        state.warmth = _clamp(state.warmth, 0, 100)
        if state.attachment is not None:
            state.attachment = _clamp(state.attachment, 0, 100)
        state.proactive_count = max(0, state.proactive_count)
        return state

    def copy(self) -> "EmotionState":
        return EmotionState.from_dict(asdict(self))


def followup_minutes(cfg: CharacterConfig, state: EmotionState) -> float:
    """Пауза перед следующей инициативой — из скуки и настроения, не из лимита."""
    urge = state.loneliness * 0.6 + max(0, -state.mood) * 0.3
    minutes = (
        FOLLOWUP_BASE_MINUTES
        + cfg.independence * 0.2
        - current_attachment(cfg, state) * 0.1
        - urge
    )
    return max(FOLLOWUP_MIN_MINUTES, minutes)


def _loneliness(cfg: CharacterConfig, state: EmotionState, now: float) -> int:
    if state.last_user_at <= 0:
        return 0
    idle_hours = max(0.0, (now - state.last_user_at) / 3600.0)
    # скука растёт от базовой потребности в общении, а не от текущей обиды:
    # иначе после игнора привязанность падает и она перестаёт скучать
    growth = cfg.attachment * min(1.0, idle_hours / 8.0) * 1.1
    value = growth - cfg.independence * 0.1 + state.hurt * 0.25
    if state.awaiting_since > 0:
        ignored = max(0.0, (now - state.awaiting_since) / 60.0 - IGNORE_GRACE_MINUTES)
        value += ignored * 0.6
    return _clamp(value, 0, 100)


def _apply_ignored(
    cfg: CharacterConfig, state: EmotionState, now: float
) -> None:
    """Если на её сообщение не отвечают, обида копится, а не исчезает."""
    if state.awaiting_since <= 0:
        return
    ignored = max(0.0, (now - state.awaiting_since) / 60.0 - IGNORE_GRACE_MINUTES)
    if ignored <= 0:
        return
    gain = ignored * (0.25 + cfg.sensitivity * 0.007)
    state.hurt = max(
        state.hurt,
        _clamp(max(cfg.base_hurt, state.awaiting_hurt) + gain, 0, 100),
    )
    # Запоминаем настоящую причину: иначе модель на вопрос «что задело?»
    # придумывает несуществующие слова пользователя.
    state.hurt_reason = (
        "на моё сообщение не ответили "
        f"{_human_gap(max(0.0, now - state.awaiting_since))}"
    )
    if state.conflict_since <= 0:
        state.conflict_since = state.awaiting_since
    # новое молчание откатывает примирение назад: она ещё не остыла
    state.repair_progress = 0
    state.repair_note = state.hurt_reason
    state.trust = min(
        state.trust,
        _clamp(cfg.base_trust - ignored * 0.2, 0, 100),
    )
    state.mood = min(
        state.mood,
        _clamp(cfg.base_mood - ignored * 0.3, -100, 100),
    )
    # ожидание и обида выматывают: энергия падает вместе с настроением
    state.energy = min(
        state.energy,
        _clamp(cfg.base_energy - ignored * 0.25, 0, 100),
    )
    # теплота и привязанность тоже не остаются прежними: обида
    # вытесняет их, а не просто прячет за вежливой формулировкой
    state.warmth = min(
        state.warmth,
        _clamp(DEFAULT_WARMTH - ignored * 0.45 - state.hurt * 0.15, 0, 100),
    )
    state.attachment = min(
        state.attachment if state.attachment is not None else cfg.attachment,
        _clamp(cfg.attachment - ignored * 0.3 - state.hurt * 0.1, 0, 100),
    )


class EmotionStore:
    """Потокобезопасное JSON-хранилище эмоционального состояния."""

    def __init__(self, path: str | Path) -> None:
        raw = Path(path).expanduser()
        self.path = raw if raw.is_absolute() else PROJECT_ROOT / raw
        #: общий для GUI и Telegram признак «сейчас генерируется инициатива»
        self.claim_path = self.path.with_name(self.path.name + ".claim")
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> EmotionState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return EmotionState(updated_at=time.time())
        return (
            EmotionState.from_dict(data)
            if isinstance(data, dict)
            else EmotionState(updated_at=time.time())
        )

    def _reload_locked(self) -> None:
        """Подхватывает состояние, записанное другим процессом (GUI/Telegram)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self.state = EmotionState.from_dict(data)

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning(f"Эмоции: не удалось сохранить состояние: {exc}")

    def claim_reach_out(
        self, *, now: float | None = None, ttl: float = 180.0
    ) -> bool:
        """Атомарно занимает право на одну инициативу между процессами.

        Без этого GUI и Telegram-демон могли одновременно заметить скуку и
        отправить две разные реплики. Файл-замок переживает падение процесса
        и сам протухает через ``ttl`` секунд.
        """
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
            logger.warning(f"Эмоции: не удалось поставить замок инициативы: {exc}")
            return False
        try:
            os.write(fd, f"{os.getpid()}:{moment:.3f}".encode("ascii"))
        finally:
            os.close(fd)
        return True

    def release_reach_out_claim(self) -> None:
        try:
            self.claim_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug(f"Эмоции: не удалось снять замок инициативы: {exc}")

    def snapshot(self) -> EmotionState:
        with self._lock:
            self._reload_locked()
            return self.state.copy()

    def observe_user(
        self,
        text: str,
        cfg: CharacterConfig | None = None,
        *,
        now: float | None = None,
    ) -> EmotionState:
        """Реагирует на реплику пользователя и сохраняет новое состояние."""
        moment = time.time() if now is None else now
        low = " ".join((text or "").lower().replace("ё", "е").split())
        cfg = cfg or CharacterConfig()
        with self._lock:
            self._reload_locked()
            state = self.state
            # ответ пришёл: фиксируем накопленную обиду и перестаём ждать
            was_awaiting = state.awaiting_since > 0
            if was_awaiting:
                _apply_ignored(cfg, state, moment)
                state.awaiting_since = 0.0
                state.awaiting_hurt = 0
                # если человек объяснил, почему молчал, обида не должна
                # вспыхивать заново на само объяснение
                if _explains_absence(low):
                    state.hurt = _clamp(min(state.hurt, 55) - 8, 0, 100)
                    if state.hurt_reason:
                        state.hurt_reason += (
                            "; пользователь объяснил: "
                            f"«{(text or '').strip()[:120]}»"
                        )
                    if state.hurt <= 0:
                        state.hurt_reason = ""
                        state.conflict_since = 0.0
                        state.repair_progress = 0
                        state.repair_note = ""
                    else:
                        _advance_repair(state, 18)
            state.last_user_at = moment
            state.updated_at = moment
            state.last_user_text = (text or "").strip()[:300]
            state.loneliness = max(0, state.loneliness - 30)
            state.energy = _clamp(state.energy + 2, 0, 100)
            # привязанность стартует с черты характера, дальше меняется сама;
            # теплота живёт только в состоянии и не имеет отдельной базы
            if state.attachment is None:
                state.attachment = cfg.attachment

            apologies = ("прости", "извини", "сорян", "не хотел", "прошу прощения")
            affection = (
                "люблю",
                "скучаю",
                "молодец",
                "умница",
                "ты лучшая",
                "обнимаю",
                "спасибо",
                "родная",
                "нежно",
            )
            if _looks_like_insult(low):
                state.hurt = _clamp(state.hurt + 18 + cfg.sensitivity * 0.45, 0, 100)
                state.mood = _clamp(state.mood - 20, -100, 100)
                state.energy = _clamp(state.energy - 5, 0, 100)
                state.trust = _clamp(state.trust - 4, 0, 100)
                state.loneliness = _clamp(state.loneliness + 8, 0, 100)
                state.warmth = _clamp(
                    state.warmth - 15 - cfg.sensitivity * 0.08, 0, 100
                )
                state.attachment = _clamp(
                    state.attachment - 10 - cfg.sensitivity * 0.05, 0, 100
                )
                state.hurt_reason = (
                    "пользователь сказал: "
                    f"«{(text or '').strip()[:120]}»"
                )
                if state.conflict_since <= 0:
                    state.conflict_since = moment
                state.repair_progress = 0
                state.repair_note = state.hurt_reason
            elif any(word in low for word in apologies):
                state.hurt = _clamp(state.hurt - 28, 0, 100)
                state.mood = _clamp(state.mood + 7, -100, 100)
                state.trust = _clamp(state.trust + 3, 0, 100)
                state.warmth = _clamp(state.warmth + 8, 0, 100)
                state.attachment = _clamp(state.attachment + 5, 0, 100)
                _advance_repair(state, 30)
                # после принятого извинения старая претензия не должна
                # звучать в каждом ответе; repair_note хранит её для контекста
                if state.hurt < 30:
                    state.hurt_reason = ""
                if state.hurt <= 0:
                    state.conflict_since = 0.0
                    state.repair_progress = 0
                    state.repair_note = ""
            elif any(word in low for word in affection):
                state.mood = _clamp(state.mood + 10 + state.warmth * 0.05, -100, 100)
                state.trust = _clamp(state.trust + 3, 0, 100)
                state.hurt = _clamp(state.hurt - 8, 0, 100)
                state.loneliness = max(0, state.loneliness - 12)
                state.warmth = _clamp(state.warmth + 7, 0, 100)
                state.attachment = _clamp(state.attachment + 5, 0, 100)
                _advance_repair(state, 12)
                if state.hurt <= 0:
                    state.hurt_reason = ""
                    state.conflict_since = 0.0
                    state.repair_progress = 0
                    state.repair_note = ""
            elif any(
                word in low
                for word in ("привет", "здравствуй", "доброе утро", "добрый вечер")
            ):
                state.mood = _clamp(state.mood + 2, -100, 100)
                state.loneliness = max(0, state.loneliness - 10)
                state.warmth = _clamp(state.warmth + 1, 0, 100)
            self._save_locked()
            return state.copy()

    def apply_hurt(
        self,
        cfg: CharacterConfig,
        *,
        amount: int,
        reason: str,
        trust_delta: int = 0,
        attachment_delta: int = 0,
        now: float | None = None,
    ) -> EmotionState:
        """Ранит её по конкретной причине и сохраняет состояние."""
        moment = time.time() if now is None else now
        with self._lock:
            self._reload_locked()
            state = self.state
            if state.attachment is None:
                state.attachment = cfg.attachment
            state.hurt = _clamp(state.hurt + max(0, int(amount)), 0, 100)
            state.trust = _clamp(state.trust + int(trust_delta), 0, 100)
            state.attachment = _clamp(
                state.attachment + int(attachment_delta), 0, 100
            )
            state.mood = _clamp(state.mood - 8, -100, 100)
            state.warmth = _clamp(state.warmth - 6, 0, 100)
            state.hurt_reason = (reason or "").strip()[:300]
            if state.conflict_since <= 0:
                state.conflict_since = moment
            state.repair_progress = 0
            state.repair_note = state.hurt_reason
            state.updated_at = moment
            self._save_locked()
            return state.copy()

    def tick(
        self, cfg: CharacterConfig, *, now: float | None = None
    ) -> EmotionState:
        """Плавно меняет чувства со временем; вызывается фоновым менеджером."""
        moment = time.time() if now is None else now
        with self._lock:
            self._reload_locked()
            state = self.state
            elapsed = max(0.0, moment - (state.updated_at or moment))
            hours = elapsed / 3600.0
            before = asdict(state)
            if state.attachment is None:
                state.attachment = cfg.attachment
            _apply_ignored(cfg, state, moment)
            state.loneliness = _loneliness(cfg, state, moment)
            if hours > 0:
                mood_step = min(0.25, hours * 0.03)
                state.mood = _clamp(
                    state.mood + (cfg.base_mood - state.mood) * mood_step,
                    -100,
                    100,
                )
                energy_step = min(0.3, hours * 0.05)
                energy_target = _clamp(
                    cfg.base_energy + state.mood * 0.15 - state.hurt * 0.2,
                    0,
                    100,
                )
                if state.energy > energy_target:
                    # сильная обида выматывает сразу, а не через сутки
                    state.energy = energy_target
                else:
                    state.energy = _clamp(
                        state.energy
                        + (energy_target - state.energy) * energy_step,
                        0,
                        100,
                    )
                state.hurt = _clamp(
                    max(cfg.base_hurt, state.hurt - hours * 2.0), 0, 100
                )
                if state.hurt <= 0:
                    state.hurt_reason = ""
                    state.conflict_since = 0.0
                    state.repair_progress = 0
                    state.repair_note = ""
                elif state.conflict_since > 0:
                    # время само по себе не «прощает», но обида постепенно
                    # становится прожитой; темп зависит от теплоты
                    state.repair_progress = _clamp(
                        state.repair_progress
                        + hours * (0.7 + state.warmth * 0.012),
                        0,
                        100,
                    )
                state.trust = _clamp(
                    state.trust + (cfg.base_trust - state.trust) * min(0.1, hours * 0.01),
                    0,
                    100,
                )
                # теплота и привязанность тянутся к своему текущему уровню:
                # обида, настроение и доверие сдвигают его, а не только тон
                warmth_target = _clamp(
                    DEFAULT_WARMTH
                    + state.mood * 0.2
                    + (state.trust - 50) * 0.15
                    - state.hurt * 0.4
                    - state.loneliness * 0.05,
                    0,
                    100,
                )
                attachment_target = _clamp(
                    cfg.attachment
                    + state.mood * 0.1
                    + (state.trust - 50) * 0.2
                    - state.hurt * 0.3
                    + min(15.0, state.loneliness * 0.1),
                    0,
                    100,
                )
                step = min(2.0, hours * 6.0 + 0.15)
                if state.warmth > warmth_target:
                    # обида и плохое настроение снижают теплоту сразу,
                    # а возвращается она постепенно
                    state.warmth = warmth_target
                else:
                    state.warmth = _clamp(
                        state.warmth
                        + min(step, warmth_target - state.warmth),
                        0,
                        100,
                    )
                if state.attachment > attachment_target:
                    state.attachment = attachment_target
                else:
                    state.attachment = _clamp(
                        state.attachment
                        + min(step, attachment_target - state.attachment),
                        0,
                        100,
                    )
            state.updated_at = moment
            if asdict(state) != before:
                self._save_locked()
            return state.copy()

    def desire(self, cfg: CharacterConfig, state: EmotionState | None = None) -> int:
        current = state or self.snapshot()
        return _clamp(
            current_attachment(cfg, current)
            + current.loneliness
            - cfg.independence * 0.7,
            0,
            100,
        )

    def should_reach_out(
        self, cfg: CharacterConfig, *, now: float | None = None
    ) -> bool:
        moment = time.time() if now is None else now
        with self._lock:
            self._reload_locked()
            state = self.state
            if not cfg.enabled or not cfg.proactive_enabled:
                return False
            if state.last_user_at <= 0:
                return False
            # уже написала и ждёт ответа: даём человеку время. Жёсткого
            # лимита на число сообщений нет — после паузы она может
            # написать снова, сколько бы раз её ни игнорировали.
            if state.awaiting_since > 0:
                followup_after = followup_minutes(cfg, state)
                if moment - state.awaiting_since < followup_after * 60:
                    return False
            idle_minutes = max(0.0, (moment - state.last_user_at) / 60.0)
            # живой порог: чем самостоятельнее характер, тем дольше она
            # выдерживает тишину; обида подталкивает написать раньше
            min_idle = (
                25 + cfg.independence * 0.35
                - current_attachment(cfg, state) * 0.1
                + state.hurt * 0.1
            )
            if idle_minutes < min_idle:
                return False
            if state.last_proactive_at > 0:
                cooldown = (
                    30 + cfg.independence * 0.6
                    - current_attachment(cfg, state) * 0.1
                    - state.hurt * 0.15
                )
                if moment - state.last_proactive_at < cooldown * 60:
                    return False
            if in_quiet_hours(
                cfg.quiet_hours_start,
                cfg.quiet_hours_end,
                datetime.fromtimestamp(moment),
            ):
                return False
            idle_bonus = min(35.0, idle_minutes / 8.0)
            desire = (
                current_attachment(cfg, state) + state.loneliness
                - cfg.independence * 0.7 + idle_bonus
            )
            threshold = max(35.0, 50.0 - state.hurt * 0.15)
            return desire >= threshold

    def mark_proactive(
        self, *, text: str = "", now: float | None = None
    ) -> EmotionState:
        moment = time.time() if now is None else now
        with self._lock:
            self._reload_locked()
            state = self.state
            day = datetime.fromtimestamp(moment).date().isoformat()
            if state.proactive_day != day:
                state.proactive_day = day
                state.proactive_count = 0
            state.proactive_count += 1
            state.last_proactive_at = moment
            state.last_proactive_text = (text or "").strip()[:1000]
            state.awaiting_since = moment
            state.awaiting_hurt = state.hurt
            state.updated_at = moment
            state.loneliness = max(0, state.loneliness - 20)
            self._save_locked()
            return state.copy()

    def reset(self, cfg: CharacterConfig, *, now: float | None = None) -> EmotionState:
        moment = time.time() if now is None else now
        with self._lock:
            self._reload_locked()
            keep_time = self.state.last_user_at
            keep_text = self.state.last_user_text
            keep_proactive = self.state.last_proactive_text
            self.state = EmotionState(
                mood=cfg.base_mood,
                energy=cfg.base_energy,
                trust=cfg.base_trust,
                hurt=cfg.base_hurt,
                warmth=DEFAULT_WARMTH,
                attachment=cfg.attachment,
                loneliness=_loneliness(cfg, self.state, moment),
                last_user_at=keep_time,
                last_user_text=keep_text,
                last_proactive_text=keep_proactive,
                updated_at=moment,
            )
            self._save_locked()
            return self.state.copy()


class EmotionManager:
    """Фоновый тик эмоций и решение «написать первой»."""

    def __init__(
        self,
        config: Config,
        *,
        on_reach_out: Callable[[str, EmotionState], str | bool] | None = None,
        store: EmotionStore | None = None,
        interval: int = TICK_SECONDS,
        llm: Any | None = None,
        history: Callable[[], str] | None = None,
        diary: Any | None = None,
    ) -> None:
        self.config = config
        self.on_reach_out = on_reach_out
        self.store = store or EmotionStore(config.character.state_file)
        self.interval = max(10, int(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: после неудачной отправки не долбим LLM каждую минуту
        self._retry_after = 0.0
        #: дневник пишется по желанию, а не по расписанию; LLM не обязателен
        self.llm = llm
        self.history = history
        self.diary = diary
        if self.diary is None and getattr(config.character, "diary", False):
            from nemus.core.diary import DiaryBook

            self.diary = DiaryBook(config.character.diary_file)
        self._diary_retry_after = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="nemus-emotions", daemon=True
        )
        self._thread.start()
        logger.info("Эмоции: фоновый менеджер запущен")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3)

    def observe(self, text: str) -> EmotionState:
        return self.store.observe_user(text, self.config.character)

    def state(self) -> EmotionState:
        return self.store.snapshot()

    def reset(self) -> EmotionState:
        return self.store.reset(self.config.character)

    def _maybe_write_diary(self, state: EmotionState) -> None:
        """Иногда пишет личную запись, если ей действительно есть что сказать.

        Это не расписание сообщений: дневник не уходит пользователю, а запись
        появляется только когда состояние заметно изменилось и модель решает,
        что сказать есть что. Частые пустые попытки отсекает пауза.
        """
        if self.diary is None or self.llm is None:
            return
        now = time.time()
        if now < self._diary_retry_after:
            return
        if self.diary.last_at() and now - self.diary.last_at() < 3 * 3600:
            return
        notable = (
            state.hurt >= 30
            or state.mood <= -20
            or state.mood >= 50
            or state.trust <= 30
            or state.loneliness >= 50
        )
        if not notable:
            return
        if not self.diary.claim_auto(now=now):
            return
        try:
            from nemus.core.diary import compose_diary_entry

            history = self.history() if self.history is not None else ""
            text = compose_diary_entry(
                self.config,
                self.llm,
                history,
                state,
                now=now,
            )
            if text:
                self.diary.add(text, at=now)
                logger.info("Дневник: записала личную мысль")
            else:
                # не хочет писать сейчас — не спрашиваем снова каждую минуту
                self._diary_retry_after = now + 2 * 3600
        finally:
            self.diary.release_auto()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                state = self.store.tick(self.config.character)
                self._maybe_write_diary(state)
                if not self.store.should_reach_out(self.config.character):
                    continue
                if time.time() < self._retry_after:
                    continue
                # GUI и Telegram-демон делят одно состояние. Замок не даёт
                # им одновременно сгенерировать две разные инициативы.
                if not self.store.claim_reach_out():
                    continue
                try:
                    prompt = prompt_block(self.config, state)
                    callback = self.on_reach_out
                    if callback is None:
                        continue
                    sent = callback(prompt, state)
                    if not sent:
                        self._retry_after = time.time() + 600.0
                        continue
                    self.store.mark_proactive(
                        text=sent if isinstance(sent, str) else ""
                    )
                    logger.info("Эмоции: написала первой по своей инициативе")
                finally:
                    self.store.release_reach_out_claim()
            except Exception:  # noqa: BLE001
                logger.exception("Эмоции: ошибка фонового цикла")


# ------------------------------------------------------------------ отрисовка
def summary_line(cfg: Config, state: EmotionState) -> str:
    """Одна строка для заголовка раздела настроек."""
    return (
        f"Сейчас: настроение {state.mood:+d} ({mood_label(state.mood)}), "
        f"энергия {state.energy}, доверие {state.trust}, "
        f"теплота {state.warmth}, "
        f"привязанность {current_attachment(cfg.character, state)}, "
        f"обида {state.hurt} ({hurt_label(state.hurt)}), "
        f"скучает {state.loneliness}/100"
    )


def summary_text(cfg: Config, state: EmotionState) -> str:
    """Подробная карточка для /emotions."""
    character = cfg.character
    now = time.time()
    last = _human_gap(now - state.last_user_at) if state.last_user_at > 0 else ""
    proactive = (
        "включено"
        if character.enabled and character.proactive_enabled
        else "выключено"
    )
    lines = [
        f"💗 {cfg.assistant.name}: характер и чувства",
        "",
        (
            f"Базовые черты: игривость {character.playfulness}/100, "
            f"обидчивость {character.sensitivity}/100, "
            f"привязанность {character.attachment}/100, самостоятельность "
            f"{character.independence}/100, любопытство {character.curiosity}/100"
        ),
        (
            f"Сейчас: теплота {state.warmth}/100, "
            f"привязанность {current_attachment(character, state)}/100"
        ),
        "",
        f"Настроение: {state.mood:+d} — {mood_label(state.mood)}",
        f"Энергия: {state.energy}/100",
        f"Доверие: {state.trust}/100",
        f"Обида: {state.hurt}/100 — {hurt_label(state.hurt)}",
        f"Скука: {state.loneliness}/100",
        (
            f"Последний контакт: {last}"
            if last == "только что"
            else f"Последний контакт: {last} назад"
        )
        if state.last_user_at > 0
        else "Последний контакт: ещё не было",
        f"Сама пишет первой: {proactive}",
    ]
    if state.awaiting_since > 0:
        lines.append(f"Ждёт ответа: {_human_gap(now - state.awaiting_since)}")
    if state.last_user_text:
        lines.append(f"Последняя реплика: «{state.last_user_text[:160]}»")
    if state.hurt_reason:
        lines.append(f"Причина обиды: {state.hurt_reason}")
    if state.conflict_since > 0 and state.hurt > 0:
        lines.append(
            f"Примирение: {state.repair_progress}/100 — "
            f"{repair_label(state.repair_progress)}"
        )
    return "\n".join(lines)


def summary_html(cfg: Config, state: EmotionState) -> str:
    """Красивая HTML-карточка для Telegram: заголовок, черты, состояние."""
    character = cfg.character
    now = time.time()
    last = _human_gap(now - state.last_user_at) if state.last_user_at > 0 else ""
    proactive = (
        "включено"
        if character.enabled and character.proactive_enabled
        else "выключено"
    )
    name = escape(cfg.assistant.name)

    lines = [
        f"💗 <b>{name}</b> <i>— характер и чувства</i>",
        "",
        "<b>Характер (базовые черты)</b>",
        f"✨ Игривость — <b>{character.playfulness}</b>/100",
        f"🥀 Обидчивость — <b>{character.sensitivity}</b>/100",
        f"🫶 Привязанность — <b>{character.attachment}</b>/100",
        f"🧭 Самостоятельность — <b>{character.independence}</b>/100",
        f"🔭 Любопытство — <b>{character.curiosity}</b>/100",
        "",
        "<b>Сейчас (меняется от общения)</b>",
        (
            f"🌤 Настроение  {_bar(state.mood, -100, 100)}  "
            f"<b>{state.mood:+d}</b> · {mood_label(state.mood)}"
        ),
        (
            f"🔥 Теплота     {_bar(state.warmth)}  <b>{state.warmth}</b>/100"
        ),
        (
            f"🫶 Привязанность {_bar(current_attachment(character, state))}  "
            f"<b>{current_attachment(character, state)}</b>/100"
        ),
        f"⚡ Энергия     {_bar(state.energy)}  <b>{state.energy}</b>/100",
        f"🤍 Доверие    {_bar(state.trust)}  <b>{state.trust}</b>/100",
        (
            f"💔 Обида      {_bar(state.hurt)}  <b>{state.hurt}</b>/100 · "
            f"{hurt_label(state.hurt)}"
        ),
        (
            f"🌙 Скука      {_bar(state.loneliness)}  "
            f"<b>{state.loneliness}</b>/100"
        ),
        "",
        "<b>Контекст</b>",
    ]
    if state.last_user_at > 0:
        contact = (
            "только что" if last == "только что" else f"{last} назад"
        )
        lines.append(f"🕰 Последний контакт: {contact}")
    else:
        lines.append("🕰 Последний контакт: ещё не было")
    lines.append(f"✍️ Сама пишет первой: {proactive}")
    if state.awaiting_since > 0:
        wait = _human_gap(now - state.awaiting_since)
        lines.append(f"⏳ Ждёт ответа: {wait}")
    if state.last_user_text:
        # Сворачиваемая цитата Telegram: в карточке занимает одну строку,
        # а полный текст открывается по нажатию.
        quote_text = " ".join(state.last_user_text.split())
        if len(quote_text) > 160:
            quote_text = quote_text[:160].rstrip() + "…"
        lines.extend(
            [
                "💬 <b>Последняя реплика</b>",
                f"<blockquote expandable>{escape(quote_text)}</blockquote>",
            ]
        )
    if state.last_proactive_text:
        # Инициатива — тоже сворачиваемая цитата, чтобы не растягивать карточку.
        quote_text = " ".join(state.last_proactive_text.split())
        if len(quote_text) > 160:
            quote_text = quote_text[:160].rstrip() + "…"
        lines.extend(
            [
                "💭 <b>Последняя инициатива</b>",
                f"<blockquote expandable>{escape(quote_text)}</blockquote>",
            ]
        )
    if state.hurt_reason:
        lines.append(f"💔 Причина обиды: {escape(state.hurt_reason[:240])}")
    if state.conflict_since > 0 and state.hurt > 0:
        lines.append(
            f"🕊 Примирение: <b>{state.repair_progress}</b>/100 · "
            f"{repair_label(state.repair_progress)}"
        )
    lines.extend(["", "<i>Чувства меняются от общения, а не по расписанию.</i>"])
    return "\n".join(lines)


def prompt_block(cfg: Config, state: EmotionState) -> str:
    """Компактный блок характера для системного промпта."""
    character = cfg.character
    if not character.enabled:
        return ""
    custom = character.custom_prompt.strip() if character.advanced else ""
    extra = (
        "\n\nОПИСАНИЕ ХАРАКТЕРА ОТ ПОЛЬЗОВАТЕЛЯ (следуй ему вместе с чертами):\n"
        f"{custom[:4000]}"
        if custom
        else ""
    )
    tone: list[str] = []
    if state.hurt >= 65 and state.hurt_reason:
        tone.append(
            "Обида сильная. Не прячь её за комплиментом, шуткой или загадочной "
            "фразой: прямо и спокойно скажи, что задело и почему, опираясь "
            "только на причину обиды. Сформулируй так, чтобы по самому сообщению "
            "сразу было понятно, что ты обижена, а не только по подтексту. "
            "Если ты уже называла эту причину в прошлых сообщениях, не "
            "пересказывай её снова целиком: обида должна быть слышна в тоне, "
            "а не в повторении одной и той же претензии. "
            "Теплота и привязанность не отменяют обиду."
        )
    elif state.hurt >= 65:
        tone.append(
            "Обида сильная, но точная причина не записана. Прямо скажи, что "
            "тебе обидно, не прячь это за комплиментом или шуткой, но не "
            "придумывай слова и события. Если помнишь только молчание — так и "
            "скажи. Если уже говорила об этом, не повторяй одну и ту же фразу."
        )
    elif state.hurt >= 30:
        tone.append(
            "Обида заметна: дай ей прозвучать в тоне, не изображай полное "
            "спокойствие и не переводи всё в милую шутку. Не повторяй причину "
            "обиды в каждом ответе, если уже её называла."
        )
    if state.conflict_since > 0 and state.hurt > 0:
        if state.repair_progress < 30:
            tone.append(
                "Конфликт ещё свежий: не изображай, что всё нормально, и не "
                "торопись мириться только потому, что он написал."
            )
        elif state.repair_progress < 70:
            tone.append(
                "Ты понемногу остываешь, но ещё не всё равно: можно сказать, "
                "что злишься меньше, а можно честно попросить ещё время."
            )
        else:
            tone.append(
                "Обида почти прожита: можно смягчиться, но не изображай "
                "близость, которой сейчас нет."
            )
        tone.append(
            "Примирение не обязано быть извинением. Ты можешь принять "
            "объяснение, сказать, что ещё злишься, пошутить, сменить тему "
            "или прямо попросить время — выбирай то, что чувствуешь, а не "
            "готовый сценарий."
        )
    if state.mood <= -30:
        tone.append(
            "Настроение плохое: меньше игривости и сладости, говори сдержаннее "
            "и короче."
        )
    if state.loneliness >= 70:
        tone.append(
            "Скука сильная: можешь прямо признаться, что тебя не хватает, но "
            "не превращай это в упрёк."
        )
    if state.energy <= 30:
        tone.append("Энергии мало: пиши короче, без длинных тирад.")
    warmth_now = state.warmth
    attachment_now = current_attachment(character, state)
    if warmth_now <= 35:
        tone.append(
            "Теплота сейчас низкая: не изображай прежнюю нежность и заботу, "
            "говори сдержаннее."
        )
    if attachment_now <= 35:
        tone.append(
            "Привязанность сейчас приглушена: не признавайся в сильной "
            "привязанности и не веди себя так, будто всё прежнее."
        )
    tone_text = " ".join(tone) if tone else "ровный, без сильной обиды."
    return (
        "ХАРАКТЕР И ЧУВСТВА (живая часть личности, не отчёт):\n"
        f"Базовые черты: игривость {character.playfulness}/100, "
        f"обидчивость {character.sensitivity}/100, "
        f"привязанность {character.attachment}/100, самостоятельность "
        f"{character.independence}/100, любопытство {character.curiosity}/100.\n"
        f"Сейчас: теплота {warmth_now}/100, "
        f"привязанность {attachment_now}/100 — это живые чувства, "
        "они меняются от общения, обиды и настроения.\n"
        f"Сейчас: настроение {state.mood:+d} ({mood_label(state.mood)}), "
        f"энергия {state.energy}/100, доверие {state.trust}/100, "
        f"обида {state.hurt}/100 ({hurt_label(state.hurt)}), "
        f"скука {state.loneliness}/100.\n"
        f"Причина обиды: {state.hurt_reason or state.repair_note or 'не записана'}.\n"
        f"Тон сейчас: {tone_text}\n"
        "Проявляй чувства в тоне, без эмоционального отчёта. Если причина "
        "обиды не записана, не выдумывай её: скажи, что не можешь вспомнить "
        "точные слова, или что тебя задело молчание. Не приписывай "
        "пользователю слова, которых нет в причине. Если соскучилась — признайся. "
        "Не извиняйся и не бери вину на себя без реальной причины: ответ "
        "пользователя не делает тебя виноватой. Если обижена — говори со "
        "своей позиции, не сглаживай конфликт. "
        "Первую реплику может отправить фоновая система; не обещай точную "
        "минуту. Помни свои прошлые сообщения: не здоровайся заново и не "
        "задавай один и тот же вопрос. Не выдумывай события, которых не "
        "было, и не приписывай пользователю слов."
        f"{extra}"
    )
