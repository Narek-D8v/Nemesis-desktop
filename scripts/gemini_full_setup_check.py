"""Проверка Gemini Live с настоящим setup приложения (промпт + все инструменты).

Ничего не выполняет: tool-вызовы только перехватывает и отвечает заглушкой.
Меряет: размер setup, задержку до первого звука, вызов инструмента и задержку
после tool-ответа.

Запуск:
    venv\\Scripts\\python.exe -X utf8 scripts\\gemini_full_setup_check.py --model gemini-3.8-live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.voice.realtime.gemini_live import _WS_URL  # noqa: E402


def build_session(model: str):
    from nemus.config import load_config
    from nemus.core.agent import build_system_prompt
    from nemus.core.factory import build_core
    from nemus.voice.realtime.base import RealtimeCallbacks
    from nemus.voice.realtime.gemini_live import GeminiLiveSession

    cfg = load_config()
    core = build_core(cfg)
    prompt = build_system_prompt(cfg, core.registry)
    session = GeminiLiveSession(
        api_key=cfg.llm.providers["gemini"].api_key,
        model=model,
        audio=True,
        voice=cfg.voice.realtime.gemini_voice,
        language="ru-RU",
        callbacks=RealtimeCallbacks(),
        registry=core.registry,
        permissions=core.permissions,
        system_prompt=prompt,
        barge_in=True,
        noise_reducer=None,
    )
    return cfg, core, session, prompt


async def run(key: str, session, prompt: str) -> int:
    import websockets

    setup = session._setup_message()
    payload = json.dumps({"setup": setup})
    tools = (setup.get("tools") or [{}])[0].get("functionDeclarations") or []
    print(f"setup: {len(payload) / 1024:.0f} КБ | промпт {len(prompt)} симв. | инструментов {len(tools)}")

    url = f"{_WS_URL}?key={key}"
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(payload)
        reply = json.loads(await ws.recv())
        if "setupComplete" not in reply and "setup" not in reply:
            print("SETUP FAILED:", json.dumps(reply, ensure_ascii=False)[:800])
            return 1
        print("setup принят")

        async def turn(text: str) -> dict:
            """Отправляет реплику и ждёт завершения хода, собирая аудио/текст/вызов."""
            sent = time.monotonic()
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
            result: dict = {"first_audio_ms": None, "tool_calls": [], "said": ""}
            said: list[str] = []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=40)
                msg = json.loads(raw)
                if "toolCall" in msg:
                    result["tool_calls"].extend(
                        msg["toolCall"].get("functionCalls") or []
                    )
                    result["said"] = " ".join(said).strip()[:200]
                    return result
                content = msg.get("serverContent") or {}
                transcription = content.get("outputTranscription") or {}
                if transcription.get("text"):
                    said.append(str(transcription["text"]))
                model_turn = content.get("modelTurn") or {}
                for part in model_turn.get("parts") or []:
                    if part.get("text"):
                        said.append(str(part["text"]))
                    inline = part.get("inlineData") or part.get("inline_data") or {}
                    if inline.get("data") and result["first_audio_ms"] is None:
                        result["first_audio_ms"] = round(
                            (time.monotonic() - sent) * 1000
                        )
                if content.get("turnComplete"):
                    result["said"] = " ".join(said).strip()[:200]
                    return result

        greeting = await turn("Скажи одно короткое слово: привет")
        print(
            f"текст -> первый звук: {greeting['first_audio_ms']} мс | "
            f"сказала: {greeting['said']!r}"
        )

        # просьба, под которую у приложения есть инструмент spotify
        answer = await turn("Включи плейлист «Чисто для души» в Spotify")
        print(f"ответ на просьбу: {answer['said']!r}")
        calls = answer["tool_calls"]
        if not calls:
            print("инструмент не вызван сразу — пробуем подтвердить")
            answer = await turn("Да, включай.")
            print(f"после подтверждения: {answer['said']!r}")
            calls = answer["tool_calls"]
        if not calls:
            print("МОДЕЛЬ НЕ ВЫЗВАЛА ИНСТРУМЕНТ spotify")
            return 1

        print("инструмент вызван:", [(c.get("name"), c.get("args")) for c in calls])
        print("raw calls:", json.dumps(calls, ensure_ascii=False)[:400])
        responses = [
            {
                "id": c.get("id", ""),
                "name": c.get("name", ""),
                "response": {"result": "OK: плейлист включён (проверка)"},
            }
            for c in calls
        ]
        sent = time.monotonic()
        await ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))
        said: list[str] = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=40)
            msg = json.loads(raw)
            print("  <-", json.dumps(msg, ensure_ascii=False)[:220])
            content = msg.get("serverContent") or {}
            transcription = content.get("outputTranscription") or {}
            if transcription.get("text"):
                said.append(str(transcription["text"]))
            model_turn = content.get("modelTurn") or {}
            for part in model_turn.get("parts") or []:
                if part.get("text"):
                    said.append(str(part["text"]))
                inline = part.get("inlineData") or part.get("inline_data") or {}
                if inline.get("data"):
                    print(
                        f"tool-ответ -> первый звук: "
                        f"{round((time.monotonic() - sent) * 1000)} мс"
                    )
                    print(f"сказала после инструмента: {' '.join(said).strip()[:200]!r}")
                    return 0
            if content.get("turnComplete"):
                print("tool-ответ: аудио не пришло, но ход завершён")
                return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-3.8-live")
    args = parser.parse_args()

    cfg, core, session, prompt = build_session(args.model)
    print(f"модель: {args.model}")
    try:
        return asyncio.run(
            run(cfg.llm.providers["gemini"].api_key, session, prompt)
        )
    finally:
        closer = getattr(core, "close", None)
        if callable(closer):
            closer()


if __name__ == "__main__":
    raise SystemExit(main())
