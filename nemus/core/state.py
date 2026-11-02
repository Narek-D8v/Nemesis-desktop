"""Общие состояния ассистента, разделяемые между ядром и GUI."""

from __future__ import annotations

from enum import Enum


class AssistState(str, Enum):
    """Состояние ассистента (влияет на визуализацию и логику)."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"

    @property
    def label_ru(self) -> str:
        return {
            AssistState.IDLE: "Готова",
            AssistState.LISTENING: "Слушаю…",
            AssistState.THINKING: "Думаю…",
            AssistState.SPEAKING: "Говорю…",
            AssistState.ERROR: "Ошибка",
        }[self]
