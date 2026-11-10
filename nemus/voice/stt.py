"""Распознавание речи (STT): облако OpenAI с локальным faster-whisper.

Голосовые из Telegram — это OGG/Opus. Локальный Whisper на CPU расшифровывает
минуту речи десятки секунд, а при первом запуске ещё и скачивает модель на
сотни мегабайт; если сети нет — пользователь получает ошибку вместо текста.
Поэтому по умолчанию (``engine: auto``) используется облачная расшифровка
OpenAI: она принимает OGG напрямую, отвечает за секунды и не требует ffmpeg.
Локальный движок остаётся фолбэком — и единственным, если ключа нет.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from nemus.utils.logger import logger

if TYPE_CHECKING:  # numpy нужен только локальному Whisper: импорт ленивый,
    # чтобы облачная расшифровка работала в контейнере без numpy (Telegram).
    import numpy as np

#: таймаут одной расшифровки, сек: голосовое в Telegram редко длиннее минуты
CLOUD_TIMEOUT = 120.0


def build_hotwords(config: Any) -> str:
    """Собирает подсказки-слова для STT: имя ассистента + частые команды."""
    words: list[str] = []
    name = getattr(getattr(config, "assistant", None), "name", "")
    if name:
        words.append(name)
    full_name = getattr(getattr(config, "assistant", None), "full_name", "")
    if full_name:
        words.append(full_name)
    words.extend(
        [
            "открой", "закрой", "включи", "выключи", "запусти", "останови",
            "громче", "тише", "поставь", "напиши", "создай", "удали",
            "напомни", "таймер", "часы", "будильник", "музыка", "погода",
        ]
    )
    stt = getattr(getattr(config, "voice", None), "stt", None)
    extra = getattr(stt, "hotwords", "") if stt is not None else ""
    if extra:
        words.extend(w for w in extra.replace(",", " ").split() if w)
    # убираем дубли, сохраняя порядок
    seen: set[str] = set()
    uniq = [w for w in words if not (w in seen or seen.add(w))]
    return " ".join(uniq)


class GeminiTranscriber:
    """Расшифровка аудио через Gemini ``generateContent`` (ключ Gemini).

    Модель ``gemini-3.5-transcribe`` отдаёт текст в поле
    ``audioTranscription``; это дешевле и быстрее локального Whisper, а
    бесплатный tier Gemini у пользователя уже есть. Принимает WAV/OGG/MP3
    как inline-данные — без ffmpeg.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-transcribe",
        language: str = "ru",
        prompt: str = "",
        api_base: str = "",
    ) -> None:
        if not api_key.strip():
            raise ValueError("Не задан API-ключ Gemini для распознавания речи.")
        self.api_key = api_key.strip()
        self.model = (model or "gemini-3.5-transcribe").strip()
        self.language = (language or "").strip()
        self.prompt = prompt.strip()
        self.api_base = (
            api_base or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")

    def transcribe(self, path: str | Path) -> str:
        """Возвращает текст аудиофайла; ошибки API пробрасываются наверх."""
        audio = Path(path)
        if not audio.is_file():
            raise FileNotFoundError(f"аудиофайл не найден: {audio}")
        mime = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
        instruction = (
            "Распознай речь из аудио дословно и верни только текст, без "
            "комментариев и форматирования."
        )
        if self.language == "ru":
            instruction += " Речь на русском языке."
        if self.prompt:
            instruction += f" Подсказки-слова: {self.prompt[:1000]}."
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": instruction},
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(audio.read_bytes()).decode(),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {"temperature": 0},
        }
        response = httpx.post(
            f"{self.api_base}/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json=payload,
            timeout=CLOUD_TIMEOUT,
        )
        if response.status_code >= 400:
            detail = response.text.strip()[:500]
            raise RuntimeError(
                f"Gemini STT вернул {response.status_code}: {detail or 'без текста'}"
            )
        data = response.json()
        for candidate in (data or {}).get("candidates", []) or []:
            for part in candidate.get("content", {}).get("parts", []) or []:
                transcription = part.get("audioTranscription") or {}
                text = transcription.get("text")
                if text:
                    return str(text).strip()
                if part.get("text"):
                    return str(part["text"]).strip()
        return ""


class OpenAITranscriber:
    """Расшифровка аудиофайла через ``/v1/audio/transcriptions`` OpenAI.

    Отправляем исходный файл (OGG/MP3/M4A), а не PCM: Telegram отдаёт OGG/Opus,
    API понимает его без перекодирования. Это убирает зависимость от ffmpeg и
    экономит время на декодирование.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini-transcribe",
        language: str = "ru",
        prompt: str = "",
        api_base: str = "",
    ) -> None:
        if not api_key.strip():
            raise ValueError("Не задан API-ключ OpenAI для распознавания речи.")
        self.api_key = api_key.strip()
        self.model = (model or "gpt-4o-mini-transcribe").strip()
        self.language = (language or "").strip()
        self.prompt = prompt.strip()
        self.api_base = (api_base or "https://api.openai.com/v1").rstrip("/")

    def transcribe(self, path: str | Path) -> str:
        """Возвращает текст аудиофайла; ошибки сети/API пробрасываются наверх."""
        audio = Path(path)
        if not audio.is_file():
            raise FileNotFoundError(f"аудиофайл не найден: {audio}")
        mime = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
        models = [self.model]
        if self.model != "whisper-1":
            # запасной вариант: новая модель может быть недоступна ключу
            models.append("whisper-1")
        last_error = ""
        for model in models:
            data: dict[str, str] = {"model": model}
            if self.language:
                data["language"] = self.language
            if self.prompt:
                data["prompt"] = self.prompt[:1000]
            # response_format=text не нужен: json даёт ещё и служебные поля
            with audio.open("rb") as fh:
                response = httpx.post(
                    f"{self.api_base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files={"file": (audio.name, fh, mime)},
                    timeout=CLOUD_TIMEOUT,
                )
            if response.status_code < 400:
                payload = response.json()
                text = payload.get("text") if isinstance(payload, dict) else ""
                return str(text or "").strip()
            last_error = response.text.strip()[:500]
            # 404/400 про модель — пробуем whisper-1, остальные ошибки сразу
            if response.status_code not in (400, 404) or "model" not in (
                last_error.lower()
            ):
                break
        raise RuntimeError(
            f"OpenAI STT вернул ошибку: {last_error or 'без текста'}"
        )


class STTEngine:
    """Лениво загружаемая модель faster-whisper с прогревом и быстрым decode."""

    def __init__(
        self,
        model: str = "small",
        language: str = "ru",
        device: str = "auto",
        beam_size: int = 5,
        hotwords: str = "",
        initial_prompt: str = "Это короткая голосовая команда ассистенту.",
    ) -> None:
        self.model_size = model
        self.language = language
        self.device = device
        # beam_size>1 заметно снижает ошибки распознавания коротких команд
        self.beam_size = max(1, int(beam_size))
        # подсказки-слова (hotwords) повышают вероятность верного варианта
        self.hotwords = hotwords.strip()
        self.initial_prompt = initial_prompt
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            device = self.device
            compute = "int8"
            if device in ("auto", ""):
                try:
                    import ctranslate2  # noqa: F401

                    device = (
                        "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                    )
                except Exception:  # noqa: BLE001
                    device = "cpu"
            if device == "cuda":
                compute = "float16"
            cpu_threads = max(1, (os.cpu_count() or 4) - 1)
            logger.info(
                f"Загружаю STT-модель '{self.model_size}' на {device} ({compute}), "
                f"потоков CPU: {cpu_threads}…"
            )
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute,
                cpu_threads=cpu_threads,
            )
            return self._model

    def warmup(self) -> bool:
        """Заранее грузит модель, чтобы первая команда обрабатывалась быстро."""
        try:
            self._load()
            logger.info("STT-модель прогрета.")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Не удалось прогреть STT: {exc}")
            return False

    def transcribe(self, audio: np.ndarray | bytes) -> str:
        """Распознаёт PCM-аудио (int16 @16кГц или float32) в текст."""
        import numpy as np  # ленивый импорт: облачному STT numpy не нужен

        if isinstance(audio, bytes):
            audio = np.frombuffer(audio, dtype=np.int16)
        if audio.size == 0:
            return ""
        # faster-whisper ожидает float32 0..1
        if audio.dtype == np.int16:
            arr = audio.astype(np.float32) / 32768.0
        else:
            arr = audio.astype(np.float32)
        model = self._load()
        kwargs: dict[str, Any] = {
            "language": self.language,
            "beam_size": self.beam_size,
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 300},
            "condition_on_previous_text": False,
            "temperature": 0.0,
            "initial_prompt": self.initial_prompt,
        }
        if self.hotwords:
            kwargs["hotwords"] = self.hotwords
        segments, _info = model.transcribe(arr, **kwargs)
        # шум и тишина: whisper выдаёт галлюцинации («спасибо», «включите
        # субтитры», фразы на других языках) — фильтруем по no_speech_prob
        parts = [
            seg.text.strip()
            for seg in segments
            if getattr(seg, "no_speech_prob", 0.0) < 0.7 and seg.text.strip()
        ]
        return " ".join(parts).strip()
