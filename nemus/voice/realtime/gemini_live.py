"""Gemini Live API: непрерывный голосовой диалог по WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import traceback
from typing import Any

import websockets

from nemus.config import DEFAULT_GEMINI_VOICE
from nemus.core.language import (
    REPLY_AUTO,
    anti_reasoning_directive,
    detect_language,
    reply_language_directive,
)
from nemus.utils.logger import logger
from nemus.utils.redact import redact_secrets
from nemus.voice.params import SAMPLE_RATE
from nemus.voice.realtime.audio_io import MicStreamer, SpeakerPlayer
from nemus.voice.realtime.base import BaseRealtimeSession, RealtimeState

_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)


class GeminiLiveSession(BaseRealtimeSession):
    """Реализация протокола BidiGenerateContent (Gemini Live API)."""

    provider = "gemini"
    send_rate = SAMPLE_RATE
    receive_rate = 24000
    mic_rate = SAMPLE_RATE

    def __init__(
        self,
        api_key: str,
        model: str,
        audio: bool = True,
        voice: str = DEFAULT_GEMINI_VOICE,
        language: str = "ru-RU",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("Не задан API-ключ Gemini (llm.providers.gemini.api_key).")
        self._key = api_key
        self._model = model if model.startswith("models/") else f"models/{model}"
        self._audio = audio
        self._voice = voice
        self._language = language

        self._ws: Any = None
        self._mic: MicStreamer | None = None
        self._speaker: SpeakerPlayer | None = None
        self._mic_queue: asyncio.Queue[bytes] | None = None
        self._play_queue: asyncio.Queue[bytes] | None = None
        self._user_buf = ""
        self._asst_buf = ""       # расшифровка произнесённого (outputAudioTranscription)
        self._asst_parts_buf = ""  # текст из modelTurn (text-сессии)
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._detected_lang = ""

        # диагностика задержки: конец речи -> первый звук ответа
        self._voice_ts = 0.0
        self._voice_start = 0.0
        self._mic_spoke = False
        self._speech_end_ts: float | None = None
        self._tool_reply_ts: float | None = None
        self._awaiting_model = False

    # ------------------------------------------------------------------ run
    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._emit_state(RealtimeState.CONNECTING)
        if not self._running:
            return
        self._mic_queue = asyncio.Queue()
        self._play_queue = asyncio.Queue()

        uri = f"{_WS_URL}?key={self._key}"
        try:
            async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
                self._ws = ws
                await ws.send(json.dumps({"setup": self._setup_message()}))
                reply = json.loads(await ws.recv())
                if "setup" not in reply and "setupComplete" not in reply:
                    raise RuntimeError(f"Сбой setup Gemini Live: {reply}")

                if self._audio:
                    self._speaker = SpeakerPlayer(self.receive_rate)
                    if not self._speaker.start():
                        self._audio = False
                    else:
                        self._mic = MicStreamer(
                            self.mic_rate,
                            self._on_mic_chunk,
                            cleaner=self.noise_reducer,
                        )
                        if not self._mic.start():
                            self._emit_error("Микрофон недоступен — аудиоввод выключен.")
                self._emit_state(RealtimeState.READY)

                tasks = [asyncio.create_task(self._recv_loop())]
                if self._audio and self._mic is not None:
                    tasks.append(asyncio.create_task(self._mic_loop()))
                    tasks.append(asyncio.create_task(self._play_loop()))
                while self._running:
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED, timeout=0.5
                    )
                    if done:
                        # одна из задач завершилась (например, обрыв соединения)
                        for task in done:
                            exc = task.exception()
                            if exc:
                                raise exc
                        break
                    if not self._running:
                        break
                for task in tasks:
                    task.cancel()
        except websockets.exceptions.ConnectionClosed as exc:
            if self._running:
                self._emit_error(f"Соединение закрыто: {exc}")
        except Exception as exc:  # noqa: BLE001
            if self._running:
                detail = redact_secrets("".join(traceback.format_exception(exc)))
                logger.error(f"Gemini Live: ошибка сессии\n{detail}")
                self._emit_error(str(exc))
        finally:
            if self._mic is not None:
                self._mic.stop()
            if self._speaker is not None:
                self._speaker.stop()

    def _setup_message(self) -> dict[str, Any]:
        setup: dict[str, Any] = {"model": self._model}
        if self._audio:
            setup["generationConfig"] = {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "languageCode": self._language,
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": self._voice}
                    },
                },
            }
            # Без этих полей Gemini Live не присылает расшифровку речи
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}
            # Быстрее завершать реплику пользователя (меньше задержка ответа)
            setup["realtimeInputConfig"] = {
                "automaticActivityDetection": {
                    "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                    "prefixPaddingMs": 20,
                    "silenceDurationMs": 400,
                }
            }

        instruction = self.system_prompt
        # Язык ответов: авто (язык собеседника) либо явно выбранный из настроек.
        instruction = f"{instruction}\n\n{self.language_instruction()}"
        # Скрытые рассуждения модели (thought parts) не должны попадать в ответ.
        instruction = f"{instruction}\n{anti_reasoning_directive()}"
        if instruction.strip():
            setup["systemInstruction"] = {"parts": [{"text": instruction.strip()}]}

        tools = self._tool_declarations()
        if tools:
            setup["tools"] = [{"functionDeclarations": tools}]
        return setup

    def _tool_declarations(self) -> list[dict[str, Any]]:
        decls: list[dict[str, Any]] = []
        for tool in self.registry:
            decls.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            )
        return decls

    # ----------------------------------------------------------- audio hooks
    def _on_mic_chunk(self, chunk: bytes) -> None:
        """Вызывается из потока звука — мостим в цикл asyncio."""
        self._track_voice(chunk)
        loop = self._loop
        if loop is not None and not loop.is_closed() and self._running:
            loop.call_soon_threadsafe(self._mic_queue.put_nowait, chunk)

    def _track_voice(self, chunk: bytes) -> None:
        """Оценивает начало/конец речи по громкости.

        Используется для замера задержки и для статуса «Думаю…»: после паузы
        пользователь ждёт ответ, а не «слушает».
        """
        usable = len(chunk) - len(chunk) % 2
        if usable < 2:
            return
        samples = memoryview(chunk)[:usable].cast("h")
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        now = time.monotonic()
        if rms >= 300:
            if not self._mic_spoke:
                self._voice_start = now
            self._voice_ts = now
            self._mic_spoke = True
            self._speech_end_ts = None
            if self._awaiting_model:
                # пользователь заговорил снова (или шум) — снова слушаем
                self._awaiting_model = False
                self._want_state(RealtimeState.LISTENING)
        elif self._mic_spoke and now - self._voice_ts >= 0.35:
            self._mic_spoke = False
            self._speech_end_ts = self._voice_ts
            # Слишком короткий всплеск (щелчок, стук) за реплику не считаем.
            if self._voice_ts - self._voice_start < 0.3:
                return
            # Реплика закончилась, ответа ещё нет — это «думает».
            if self._speaker is None or not self._speaker.is_playing:
                self._awaiting_model = True
                self._want_state(RealtimeState.THINKING)

    def _want_state(self, state: RealtimeState) -> None:
        """Смена состояния из потока микрофона — через цикл сессии."""
        loop = self._loop
        if loop is not None and not loop.is_closed() and self._running:
            loop.call_soon_threadsafe(self._emit_state, state)

    async def _mic_loop(self) -> None:
        while self._running:
            chunk = await self._mic_queue.get()
            # полудуплекс: при выключенном barge-in не слушаем, пока говорим сами
            if not self.barge_in and self._speaker is not None and self._speaker.is_playing:
                continue
            await self._send_audio(chunk)

    async def _send_audio(self, chunk: bytes) -> None:
        if self._ws is None:
            return
        # Современное поле Live API (mediaChunks устарело и в gemini-3.8-live
        # игнорируется — модель просто не слышит микрофон).
        message = {
            "realtimeInput": {
                "audio": {
                    "mimeType": f"audio/pcm;rate={self.send_rate}",
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
            }
        }
        await self._ws.send(json.dumps(message))

    async def _play_loop(self) -> None:
        while self._running:
            chunk = await self._play_queue.get()
            if self._speaker is not None:
                self._speaker.play(chunk)

    def _drain_play_queue(self) -> None:
        """Убирает ещё не воспроизведённые чанки, чтобы не смешивать ответы."""
        if self._play_queue is None:
            return
        try:
            while True:
                self._play_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    # ------------------------------------------------------------------ recv
    async def _recv_loop(self) -> None:
        while self._running:
            raw = await self._ws.recv()
            message = json.loads(raw)
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "serverContent" in message:
            await self._handle_server_content(message["serverContent"])
        elif "toolCall" in message:
            # Инструменты ходят в сеть секундами (Spotify и т.п.) — не блокируем
            # приём аудио/реплик: иначе ответ модели задерживается на всё время
            # выполнения инструмента.
            self._speech_end_ts = None
            self._awaiting_model = True
            self._emit_state(RealtimeState.THINKING)
            task = asyncio.create_task(self._handle_tool_call(message["toolCall"]))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _handle_server_content(self, content: dict[str, Any]) -> None:
        user_text = content.get("inputTranscription", {}).get("text", "")
        if user_text:
            self._user_buf += user_text
            # подстраиваемся под язык пользователя в режиме авто
            if self.reply_language == REPLY_AUTO:
                detected = detect_language(user_text)
                if detected and detected != self._detected_lang:
                    self._detected_lang = detected
                    try:
                        await self._send(
                            {
                                "clientContent": {
                                    "turns": [
                                        {
                                            "role": "user",
                                            "parts": [
                                                {
                                                    "text": f"[Языковая настройка] {reply_language_directive(REPLY_AUTO, detected)}"
                                                }
                                            ],
                                        }
                                    ],
                                    "turnComplete": True,
                                }
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass
            # финальная расшифровка приходит уже после паузы: не сбиваем
            # «Думаю…» обратно на «Слушаю…»
            if not self._awaiting_model:
                self._emit_state(RealtimeState.LISTENING)

        asst_text = content.get("outputTranscription", {}).get("text", "")
        if asst_text:
            self._asst_buf += asst_text
            self._emit_state(RealtimeState.SPEAKING)

        model_turn = content.get("modelTurn", {})
        for part in model_turn.get("parts", []):
            # скрытые рассуждения модели: в чат и озвучку не попадают
            if part.get("thought"):
                continue
            inline = part.get("inlineData") or part.get("audio")
            if inline and inline.get("data"):
                self._awaiting_model = False
                now = time.monotonic()
                if self._speech_end_ts is not None:
                    latency = (now - self._speech_end_ts) * 1000
                    logger.info(
                        f"Задержка ответа: {latency:.0f} мс "
                        "(конец речи → первый звук)"
                    )
                    self._speech_end_ts = None
                elif self._tool_reply_ts is not None:
                    latency = (now - self._tool_reply_ts) * 1000
                    logger.info(f"Задержка ответа после инструмента: {latency:.0f} мс")
                    self._tool_reply_ts = None
                if self._play_queue is not None:
                    audio = base64.b64decode(inline["data"])
                    self._play_queue.put_nowait(audio)
                    self._emit_state(RealtimeState.SPEAKING)
            if part.get("text"):
                self._asst_parts_buf += part["text"]

        if content.get("interrupted"):
            self._asst_buf = ""
            self._asst_parts_buf = ""
            self._awaiting_model = False
            self._drain_play_queue()
            if self._speaker is not None:
                self._speaker.clear()
            self._emit_state(RealtimeState.LISTENING)

        if content.get("turnComplete"):
            if self._user_buf:
                self._emit_user_text(self._user_buf.strip())
                self._user_buf = ""
            # расшифровка речи приоритетнее, чтобы не дублировать ответ текстом
            text = (self._asst_buf or self._asst_parts_buf).strip()
            self._asst_buf = ""
            self._asst_parts_buf = ""
            if text:
                self._emit_assistant_text(text)
            self._emit_state(RealtimeState.LISTENING)

    async def _handle_tool_call(self, tool_call: dict[str, Any]) -> None:
        try:
            calls = tool_call.get("functionCalls") or tool_call.get("function_calls") or []
            responses: list[dict[str, Any]] = []
            for call in calls:
                name = call.get("name", "")
                args = call.get("args") or call.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                logger.info(f"Gemini Live tool call: {name}({args})")
                # в отдельном потоке, чтобы не блокировать аудио-цикл
                # (и ждать GUI-подтверждение)
                result = await asyncio.to_thread(self._execute_tool, name, args)
                logger.info(f"Gemini Live tool result: {name} -> {result.content[:300]}")
                responses.append(
                    {
                        "id": call.get("id", ""),
                        "name": name,
                        "response": {"result": result.to_model()},
                    }
                )
            if self._ws is not None and responses:
                await self._ws.send(
                    json.dumps({"toolResponse": {"functionResponses": responses}})
                )
                self._tool_reply_ts = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Gemini Live: ошибка обработки tool call")

    # ------------------------------------------------------------------ misc
    async def _send_text(self, text: str) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": text}]}],
                        "turnComplete": True,
                    }
                }
            )
        )

    async def _close(self) -> None:
        if self._mic is not None:
            self._mic.stop()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
