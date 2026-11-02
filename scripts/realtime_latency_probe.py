"""Замер реальной задержки Gemini Live: «конец речи → первый звук ответа».

Синтезирует русскую фразу (edge-tts), стримит её в Live API как микрофон
(16 кГц PCM, блоки 60 мс), затем шлёт тишину, чтобы сработал серверный VAD.
Меряет время от последнего блока речи до первого аудио-чанка модели, а также
сетевой RTT (websocket ping).

Запуск:
    venv\\Scripts\\python.exe -X utf8 scripts\\realtime_latency_probe.py
    venv\\Scripts\\python.exe -X utf8 scripts\\realtime_latency_probe.py --thinking 0
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
PHRASE = "Привет, Немуся! Расскажи коротко, что ты умеешь."
VOICE = "ru-RU-SvetlanaNeural"
SAMPLE_RATE = 16000
CHUNK_MS = 60


async def synth_speech(text: str) -> bytes:
    """Озвучивает текст и возвращает PCM16 mono 16 кГц."""
    import av
    import edge_tts

    communicate = edge_tts.Communicate(text, VOICE)
    data = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            data.extend(chunk["data"])
    mp3 = bytes(data)
    if not mp3:
        raise RuntimeError("edge-tts вернул пустой звук")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "phrase.mp3"
        path.write_bytes(mp3)
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            parts: list[bytes] = []
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    parts.append(out.to_ndarray().tobytes())
            for out in resampler.resample(None):
                parts.append(out.to_ndarray().tobytes())
    return b"".join(parts)


def make_setup(*, silence_ms: int, thinking: int | None, model: str) -> dict:
    setup: dict = {
        "model": model if model.startswith("models/") else f"models/{model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "languageCode": "ru-RU",
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}},
            },
        },
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                "prefixPaddingMs": 20,
                "silenceDurationMs": silence_ms,
            }
        },
        "systemInstruction": {
            "parts": [{"text": "Ты голосовой ассистент. Отвечай коротко, по-русски."}]
        },
    }
    if thinking is not None:
        setup["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking}
    return setup


async def one_run(
    key: str,
    pcm: bytes,
    *,
    silence_ms: int,
    thinking: int | None,
    model: str,
    label: str,
    text: str = "",
) -> dict:
    import websockets

    result: dict = {"label": label}
    chunk = int(SAMPLE_RATE * CHUNK_MS / 1000) * 2  # байт
    url = f"{_WS_URL}?key={key}"

    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"setup": make_setup(
            silence_ms=silence_ms, thinking=thinking, model=model
        )}))
        reply = json.loads(await ws.recv())
        if "setupComplete" not in reply and "setup" not in reply:
            result["error"] = f"setup: {reply}"
            return result

        rtts: list[float] = []
        for _ in range(3):
            started = time.perf_counter()
            pong = await ws.ping()
            await asyncio.wait_for(pong, timeout=10)
            rtts.append((time.perf_counter() - started) * 1000)
        result["rtt_ms"] = round(sum(rtts) / len(rtts), 1)

        first_audio: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        heard: list[str] = []
        done = asyncio.Event()

        async def receiver() -> None:
            nonlocal heard
            try:
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    content = msg.get("serverContent") or {}
                    transcription = content.get("inputTranscription") or {}
                    if transcription.get("text"):
                        heard.append(str(transcription["text"]))
                    turn = content.get("modelTurn") or {}
                    for part in turn.get("parts") or []:
                        inline = part.get("inlineData") or part.get("inline_data") or {}
                        if inline.get("data"):
                            if not first_audio.done():
                                first_audio.set_result(time.monotonic())
                            done.set()
                            break
                    if content.get("turnComplete"):
                        done.set()
            except Exception as exc:  # noqa: BLE001
                result["recv_error"] = repr(exc)

        receiver_task = asyncio.create_task(receiver())

        if text:
            sent_at = time.monotonic()
            await ws.send(
                json.dumps(
                    {
                        "clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": text}]}],
                            "turnComplete": True,
                        }
                    }
                )
            )
            try:
                await asyncio.wait_for(first_audio, timeout=20)
                result["first_audio_ms"] = round(
                    (first_audio.result() - sent_at) * 1000, 1
                )
            except asyncio.TimeoutError:
                result["first_audio_ms"] = None
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            return result

        async def send_pcm(data: bytes) -> None:
            await ws.send(
                json.dumps(
                    {
                        "realtimeInput": {
                            "audio": {
                                "data": base64.b64encode(data).decode("ascii"),
                                "mimeType": f"audio/pcm;rate={SAMPLE_RATE}",
                            }
                        }
                    }
                )
            )

        for offset in range(0, len(pcm) - chunk, chunk):
            await send_pcm(pcm[offset : offset + chunk])
            await asyncio.sleep(CHUNK_MS / 1000)

        speech_end = time.monotonic()
        silence = b"\x00" * chunk
        # шлём тишину (как настоящий микрофон), пока модель не ответит
        for _ in range(int(4000 / CHUNK_MS)):
            if done.is_set():
                break
            await send_pcm(silence)
            await asyncio.sleep(CHUNK_MS / 1000)

        try:
            await asyncio.wait_for(first_audio, timeout=15)
            result["first_audio_ms"] = round((first_audio.result() - speech_end) * 1000, 1)
        except asyncio.TimeoutError:
            result["first_audio_ms"] = None
        try:
            await asyncio.wait_for(done.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        result["heard"] = " ".join(heard).strip()[:80]
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass
    return result


async def main_async(args: argparse.Namespace) -> int:
    from nemus.config import load_config

    key = load_config().llm.providers.get("gemini").api_key
    if not key:
        print("Нет ключа Gemini в config.yaml")
        return 1

    print("Синтез фразы…")
    phrase = args.phrase
    pcm = await synth_speech(phrase)
    if args.text:
        print(f"Текстовый режим (без VAD/ASR): {phrase!r}")
    else:
        print(f"Фраза: {len(pcm) / 2 / SAMPLE_RATE:.2f} с аудио: {phrase!r}")
    print()

    if args.text:
        variants = [("текст", 400, args.thinking)]
    else:
        variants = [
            ("текущие настройки (silence=400)", 400, None),
            ("silence=250", 250, None),
        ]
        if args.thinking is not None:
            variants.append((f"thinking={args.thinking}", 400, args.thinking))
    if args.extra:
        variants.append((f"extra: {args.extra}", 400, args.thinking))

    failures = 0
    for label, silence, thinking in variants:
        for attempt in range(1, args.runs + 1):
            try:
                data = await one_run(
                    key,
                    pcm,
                    silence_ms=silence,
                    thinking=thinking,
                    model=args.model,
                    label=label,
                    text=phrase if args.text else "",                )
            except Exception as exc:  # noqa: BLE001
                print(f"{label} (прогон {attempt}): ОШИБКА {exc!r}")
                failures += 1
                continue
            if data.get("error"):
                print(f"{label} (прогон {attempt}): {data['error'][:400]}")
                failures += 1
                continue
            print(
                f"{label} (прогон {attempt}): "
                f"RTT {data.get('rtt_ms')} мс | "
                f"конец речи -> первый звук {data.get('first_audio_ms')} мс | "
                f"услышала: {data.get('heard')!r}"
            )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--thinking", type=int, default=None)
    parser.add_argument("--extra", type=str, default="")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--phrase", type=str, default=PHRASE)
    parser.add_argument("--model", type=str, default="gemini-2.5-flash-native-audio-latest")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
