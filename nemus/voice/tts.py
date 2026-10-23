"""Синтез речи (TTS) с выбором движка и плавным очередным воспроизведением.

Движки:
- ``edge-tts`` — облачный, бесплатный, сотни голосов (по умолчанию);
- ``openai`` — OpenAI TTS (нужен API-ключ llm.providers.openai.api_key);
- ``system`` — системный синтез Windows SAPI (офлайн, без зависимостей).
"""

from __future__ import annotations

import asyncio
import base64
import queue
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from nemus.utils.logger import logger

_SENTENCE_RE = re.compile(r"[^.!?…\n]+[.!?…\n]?", re.UNICODE)

#: Поддерживаемые движки синтеза (порядок — как в списке настроек).
ENGINES = ("edge-tts", "openai", "system")

#: Модель OpenAI TTS по умолчанию (поддерживает десятки языков, включая русский).
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"

#: Голоса OpenAI TTS (фиксированный список API, сеть не нужна).
OPENAI_VOICES: list[dict[str, str]] = [
    {"ShortName": "alloy", "Locale": "", "Gender": ""},
    {"ShortName": "ash", "Locale": "", "Gender": ""},
    {"ShortName": "ballad", "Locale": "", "Gender": ""},
    {"ShortName": "coral", "Locale": "", "Gender": ""},
    {"ShortName": "echo", "Locale": "", "Gender": ""},
    {"ShortName": "fable", "Locale": "", "Gender": ""},
    {"ShortName": "marin", "Locale": "", "Gender": ""},
    {"ShortName": "nova", "Locale": "", "Gender": ""},
    {"ShortName": "onyx", "Locale": "", "Gender": ""},
    {"ShortName": "sage", "Locale": "", "Gender": ""},
    {"ShortName": "shimmer", "Locale": "", "Gender": ""},
    {"ShortName": "verse", "Locale": "", "Gender": ""},
]


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    """Дробит текст на фразы, чтобы речь начиналась быстрее и не рвалась."""
    parts: list[str] = []
    for match in _SENTENCE_RE.finditer(text):
        piece = match.group().strip()
        if not piece:
            continue
        if len(piece) > max_len:
            for i in range(0, len(piece), max_len):
                parts.append(piece[i : i + max_len])
        else:
            parts.append(piece)
    return parts or ([text.strip()] if text.strip() else [])


def _percent(raw: Any) -> int:
    """«+10%» / «-15» / мусор → целые проценты (по умолчанию 0)."""
    try:
        return int(str(raw).strip().rstrip("%").replace("+", ""))
    except (TypeError, ValueError):
        return 0


def resolve_openai_key(providers: Any) -> str:
    """Достаёт API-ключ OpenAI из ``config.llm.providers`` (или пустую строку)."""
    try:
        provider = (providers or {}).get("openai")
    except AttributeError:
        return ""
    return str(getattr(provider, "api_key", "") or "").strip()


# --------------------------------------------------------------- синтез edge
def _synthesize_edge(text: str, voice: str, rate: str, volume: str, out: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    asyncio.run(communicate.save(str(out)))


# -------------------------------------------------------------- синтез openai
def _synthesize_openai(
    text: str, voice: str, rate: str, model: str, api_key: str, out: Path
) -> None:
    if not api_key:
        raise RuntimeError(
            "Для движка OpenAI TTS нужен API-ключ: Настройки → LLM → "
            "провайдер openai (llm.providers.openai.api_key)."
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("Не установлен пакет openai. Выполните: pip install openai") from exc

    speed = max(0.25, min(4.0, 1.0 + _percent(rate) / 100.0))
    client = OpenAI(api_key=api_key, timeout=60.0)
    response = client.audio.speech.create(
        model=model or OPENAI_TTS_MODEL, voice=voice, input=text, speed=speed
    )
    out.write_bytes(response.content)


# -------------------------------------------------------------- синтез system
def _ps_quote(value: str) -> str:
    """Экранирование строкового литерала PowerShell (одинарная кавычка)."""
    return value.replace("'", "''")


def _run_powershell(script: str) -> str:
    """Запускает скрипт PowerShell без окна, возвращает stdout (UTF-8)."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        timeout=60,
        creationflags=flags,
    )
    return (proc.stdout or b"").decode("utf-8", errors="replace")


_PS_HEADER = (
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
    "Add-Type -AssemblyName System.Speech\n"
)


def _system_supported() -> bool:
    return sys.platform.startswith("win")


def _synthesize_system(text: str, voice: str, rate: str, volume: str, out: Path) -> None:
    if not _system_supported():
        raise RuntimeError("Системный синтез речи доступен только в Windows.")
    rate_value = max(-10, min(10, round(_percent(rate) / 10)))
    volume_value = max(0, min(100, 100 + _percent(volume)))
    script = (
        _PS_HEADER
        + "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        + f"try {{ $synth.SelectVoice('{_ps_quote(voice)}') }} catch {{ }}\n"
        + f"$synth.Rate = {rate_value}\n"
        + f"$synth.Volume = {volume_value}\n"
        + f"$synth.SetOutputToWaveFile('{_ps_quote(str(out))}')\n"
        + f"$synth.Speak('{_ps_quote(text)}')\n"
        + "$synth.Dispose()\n"
    )
    try:
        _run_powershell(script)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Системный синтез недоступен: {exc}") from exc
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(
            "Системный синтез не вернул звук. Проверьте, что голоса Windows "
            "установлены (Параметры → Время и язык → Речь)."
        )


# ------------------------------------------------------------------ диспетчер
def synthesize_text(
    engine: str,
    text: str,
    voice: str,
    rate: str,
    volume: str,
    model: str = "",
    api_key: str = "",
) -> Path:
    """Синтезирует текст указанным движком и возвращает путь к файлу.

    ``edge-tts``/``openai`` → mp3, ``system`` → wav. Неизвестный движок — ValueError.
    """
    engine = (engine or "").strip()
    if engine == "system":
        out = Path(tempfile.gettempdir()) / f"nemus_tts_{uuid.uuid4().hex}.wav"
        _synthesize_system(text, voice, rate, volume, out)
        return out
    out = Path(tempfile.gettempdir()) / f"nemus_tts_{uuid.uuid4().hex}.mp3"
    if engine == "edge-tts":
        _synthesize_edge(text, voice, rate, volume, out)
        return out
    if engine == "openai":
        _synthesize_openai(text, voice, rate, model, api_key, out)
        return out
    raise ValueError(f"Неизвестный движок синтеза: {engine!r} (доступны: {', '.join(ENGINES)})")


def list_engine_voices(engine: str, api_key: str = "") -> list[dict[str, str]]:
    """Возвращает голоса движка в формате ``list_voices`` edge-tts.

    Для edge-tts — полный облачный список (нужен интернет), для openai —
    фиксированный список API, для system — установленные голоса Windows.
    """
    engine = (engine or "").strip()
    if engine == "edge-tts":
        import edge_tts

        return list(asyncio.run(edge_tts.list_voices()))
    if engine == "openai":
        return [dict(v) for v in OPENAI_VOICES]
    if engine == "system":
        if not _system_supported():
            return []
        script = (
            _PS_HEADER
            + "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            + "foreach ($v in $synth.GetInstalledVoices()) {\n"
            + "  if ($v.Enabled) {\n"
            + "    $i = $v.VoiceInfo\n"
            + "    Write-Output ($i.Name + [char]9 + $i.Culture.Name + [char]9 + $i.Gender.ToString())\n"
            + "  }\n"
            + "}\n"
            + "$synth.Dispose()\n"
        )
        try:
            raw = _run_powershell(script)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"Список системных голосов недоступен: {exc}")
            return []
        voices: list[dict[str, str]] = []
        for line in raw.splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[0]:
                voices.append(
                    {"ShortName": parts[0], "Locale": parts[1], "Gender": parts[2] if len(parts) > 2 else ""}
                )
        return voices
    return []


class TTSEngine:
    """Синтезирует и воспроизводит речь строго последовательно (без наложений)."""

    def __init__(
        self,
        voice: str = "ru-RU-DmitryNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        engine: str = "edge-tts",
        model: str = "",
        api_key: str = "",
    ) -> None:
        engine = (engine or "").strip()
        if engine not in ENGINES:
            logger.warning(f"Неизвестный движок синтеза {engine!r} — использую edge-tts.")
            engine = "edge-tts"
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.engine = engine
        self.model = model
        self.api_key = api_key

        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        self._stop_flag = threading.Event()
        self._speaking = threading.Event()
        self._play_lock = threading.Lock()

    # ------------------------------------------------------------- synthesize
    def synthesize(self, text: str) -> Path:
        """Синтезирует текст выбранным движком и возвращает путь к файлу."""
        return synthesize_text(
            self.engine,
            text,
            self.voice,
            self.rate,
            self.volume,
            model=self.model,
            api_key=self.api_key,
        )

    # ---------------------------------------------------------------- playback
    @staticmethod
    def _decode(path: Path) -> tuple[Any, int]:
        import av
        import numpy as np

        container = av.open(str(path))
        stream = container.streams.audio[0]
        sample_rate = stream.rate or 24000
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        chunks: list[Any] = []
        for frame in container.decode(stream):
            frame.pts = None
            for resampled in resampler.resample(frame):
                arr = resampled.to_ndarray()
                if arr.ndim == 2:
                    arr = arr[0]
                chunks.append(arr)
        container.close()
        if not chunks:
            return np.zeros(0, dtype=np.float32), sample_rate
        return np.concatenate(chunks), sample_rate

    # ------------------------------------------------------------------ worker
    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True, name="tts")
        self._worker.start()

    def _loop(self) -> None:
        while self._running:
            try:
                text = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if text is None:
                break
            try:
                with self._play_lock:
                    self._play_text(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"TTS сбой: {exc}")

    # --------------------------------------------------------------- speaking
    def _play_text(self, text: str) -> None:
        import sounddevice as sd

        self._stop_flag.clear()
        sentences = split_sentences(text)
        if not sentences:
            return

        current: Path | None = None
        try:
            self._speaking.set()
            for i, sentence in enumerate(sentences):
                if self._stop_flag.is_set():
                    break
                # синтез текущего предложения (первое — сразу, дальше предзагрузка)
                if current is None:
                    current = self.synthesize(sentence)
                # предзагрузка следующего предложения параллельно воспроизведению
                nxt: dict[str, Any] = {}
                if i + 1 < len(sentences):
                    nxt["path"] = self.synthesize(sentences[i + 1])
                audio, sample_rate = self._decode(current)
                if audio.size and not self._stop_flag.is_set():
                    sd.play(audio, sample_rate)
                    sd.wait()
                current.unlink(missing_ok=True)
                current = nxt.get("path")
                if self._stop_flag.is_set():
                    if current is not None:
                        Path(current).unlink(missing_ok=True)
                    current = None
                    break
        finally:
            self._speaking.clear()
            if current is not None:
                Path(current).unlink(missing_ok=True)

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    # ------------------------------------------------------------------ speak
    def speak(self, text: str, block: bool = False) -> None:
        """Ставит текст в очередь (block=False) или озвучивает сразу (block=True)."""
        if not text.strip():
            return
        if block:
            with self._play_lock:
                self._play_text(text)
            return
        self._ensure_worker()
        self._queue.put(text)

    def stop(self) -> None:
        """Обрывает текущую речь и очищает очередь (barge-in)."""
        self._stop_flag.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        self._speaking.clear()

    def shutdown(self) -> None:
        self.stop()
        self._running = False
        self._queue.put(None)
