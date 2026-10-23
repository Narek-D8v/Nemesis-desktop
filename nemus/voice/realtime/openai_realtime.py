"""OpenAI Realtime API: непрерывный голосовой диалог по WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import websockets

from nemus.config import DEFAULT_OPENAI_VOICE
from nemus.core.language import (
    REPLY_AUTO,
    anti_reasoning_directive,
    detect_language,
    reply_language_directive,
)
from nemus.utils.logger import logger
from nemus.voice.params import SAMPLE_RATE
from nemus.voice.realtime.audio_io import MicStreamer, SpeakerPlayer, resample_pcm16
from nemus.voice.realtime.base import BaseRealtimeSession, RealtimeState

_WS_URL = "wss://api.openai.com/v1/realtime"

#: Модель транскрипции входного аудио в Realtime API.
TRANSCRIPTION_MODEL = "whisper-1"


class OpenAIRealtimeSession(BaseRealtimeSession):
    """Реализация классического Realtime-протокола OpenAI (input_audio_buffer)."""

    provider = "openai"
    send_rate = 24000
    receive_rate = 24000
    mic_rate = SAMPLE_RATE

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str = DEFAULT_OPENAI_VOICE,
        audio: bool = True,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("Не задан API-ключ OpenAI (llm.providers.openai.api_key).")
        self._key = api_key
        self._model = model
        self._voice = voice
        self._audio = audio
        self._temperature = temperature

        self._ws: Any = None
        self._mic: MicStreamer | None = None
        self._speaker: SpeakerPlayer | None = None
        self._mic_queue: asyncio.Queue[bytes] | None = None
        self._play_queue: asyncio.Queue[bytes] | None = None
    @property
    def _detected_lang(self) -> str:
        return getattr(self, "_detected_lang_cache", "")

    @_detected_lang.setter
    def _detected_lang(self, value: str) -> None:
        self._detected_lang_cache = value

    # ------------------------------------------------------------------ run
    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._emit_state(RealtimeState.CONNECTING)
        self._detected_lang_cache = ""
        if not self._running:
            return
        self._mic_queue = asyncio.Queue()
        self._play_queue = asyncio.Queue()

        uri = f"{_WS_URL}?model={self._model}"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "OpenAI-Beta": "realtime=v1",
        }
        try:
            async with websockets.connect(
                uri, additional_headers=headers, max_size=16 * 1024 * 1024
            ) as ws:
                self._ws = ws
                await ws.send(json.dumps(self._session_update()))

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

                tasks = [asyncio.create_task(self._recv_loop())]
                if self._audio and self._mic is not None:
                    tasks.append(asyncio.create_task(self._mic_loop()))
                    tasks.append(asyncio.create_task(self._play_loop()))
                while self._running:
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED, timeout=0.5
                    )
                    if done:
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
                logger.exception("OpenAI Realtime: ошибка сессии")
                self._emit_error(str(exc))
        finally:
            if self._mic is not None:
                self._mic.stop()
            if self._speaker is not None:
                self._speaker.stop()

    def _session_update(self) -> dict[str, Any]:
        instructions = self.system_prompt
        if not instructions.strip():
            instructions = "Ты — голосовой ассистент Немуся."
        instructions += f"\n\n{self.language_instruction()}"
        instructions += f"\n{anti_reasoning_directive()}"
        session: dict[str, Any] = {
            "type": "realtime",
            "model": self._model,
            "modalities": ["audio", "text"] if self._audio else ["text"],
            "instructions": instructions.strip(),
            "voice": self._voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": TRANSCRIPTION_MODEL},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.6,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 600,
            },
            "temperature": self._temperature,
        }
        tools = self.registry.schemas()
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return {"type": "session.update", "session": session}

    # ----------------------------------------------------------- audio hooks
    def _on_mic_chunk(self, chunk: bytes) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed() and self._running:
            loop.call_soon_threadsafe(self._mic_queue.put_nowait, chunk)

    async def _mic_loop(self) -> None:
        while self._running:
            chunk = await self._mic_queue.get()
            # полудуплекс: при выключенном barge-in не слушаем, пока говорим сами
            if not self.barge_in and self._speaker is not None and self._speaker.is_playing:
                continue
            if self._ws is None:
                continue
            # микрофон 16 кГц -> формат сессии 24 кГц
            chunk = resample_pcm16(chunk, self.mic_rate, self.send_rate)
            await self._ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                )
            )

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
            await self._handle_event(json.loads(raw))

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype == "error":
            err = event.get("error", {})
            self._emit_error(err.get("message", "неизвестная ошибка OpenAI Realtime"))
            return

        if etype == "session.created":
            self._emit_state(RealtimeState.READY)
        elif etype == "input_audio_buffer.speech_started":
            self._emit_state(RealtimeState.LISTENING)
            if self._responding:
                self._drain_play_queue()
                if self._speaker is not None:
                    self._speaker.clear()
                await self._send({"type": "response.cancel"})
        elif etype == "input_audio_buffer.speech_stopped":
            # пользователь закончил говорить — модель ещё думает
            self._emit_state(RealtimeState.THINKING)
        elif etype == "conversation.item.input_audio_transcription.delta":
            self._user_buf += event.get("delta", "")
        elif etype == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "") or self._user_buf
            text = text.strip()
            if text and getattr(self, "reply_language", REPLY_AUTO) == REPLY_AUTO:
                detected = detect_language(text)
                if detected and getattr(self, "_detected_lang_cache", "") != detected:
                    self._detected_lang_cache = detected
                    try:
                        await self._send(
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": f"[Языковая настройка] {reply_language_directive(REPLY_AUTO, detected)}",
                                        }
                                    ],
                                },
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass
            self._emit_user_text(text)
            self._user_buf = ""
        elif etype == "response.audio_transcript.delta":
            self._asst_buf += event.get("delta", "")
            self._emit_state(RealtimeState.SPEAKING)
        elif etype == "response.audio.delta":
            delta = event.get("delta")
            if delta and self._play_queue is not None:
                self._play_queue.put_nowait(base64.b64decode(delta))
                self._emit_state(RealtimeState.SPEAKING)
        elif etype == "response.audio_transcript.done":
            text = event.get("transcript", "") or self._asst_buf
            self._emit_assistant_text(text.strip())
            self._asst_buf = ""
        elif etype == "response.output_text.delta":
            self._asst_buf += event.get("delta", "")
        elif etype == "response.output_text.done":
            text = event.get("text", "") or self._asst_buf
            if text:
                self._emit_assistant_text(text.strip())
            self._asst_buf = ""
        elif etype in ("response.done", "response.cancelled"):
            self._responding = False
            # если инструмент ещё выполняется — рано показывать «Слушаю…»
            if self._pending_tools:
                self._emit_state(RealtimeState.THINKING)
            else:
                self._emit_state(RealtimeState.LISTENING)
        elif etype == "response.function_call_arguments.done":
            # Инструменты ходят в сеть секундами — не блокируем приём аудио.
            self._pending_tools += 1
            self._emit_state(RealtimeState.THINKING)
            task = asyncio.create_task(self._handle_tool_call(event))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        elif etype == "response.created":
            self._responding = True
            self._emit_state(RealtimeState.THINKING)

    async def _handle_tool_call(self, event: dict[str, Any]) -> None:
        try:
            name = event.get("name", "")
            call_id = event.get("call_id", "")
            raw_args = event.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            logger.info(f"OpenAI Realtime tool call: {name}({args})")
            # в отдельном потоке, чтобы не блокировать аудио-цикл
            # (и ждать GUI-подтверждение)
            result = await asyncio.to_thread(self._execute_tool, name, args)
            logger.info(f"OpenAI Realtime tool result: {name} -> {result.content[:300]}")
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result.to_model(),
                    },
                }
            )
            await self._send({"type": "response.create"})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("OpenAI Realtime: ошибка обработки tool call")
        finally:
            self._pending_tools = max(0, self._pending_tools - 1)

    # ------------------------------------------------------------------ misc
    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(message))

    async def _send_text(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def _close(self) -> None:
        if self._mic is not None:
            self._mic.stop()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
