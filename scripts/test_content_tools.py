"""Тесты контентных возможностей: STT, изображения, презентации, скиллы.

Запуск:  venv\\Scripts\\python.exe scripts\\test_content_tools.py
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nemus.voice.stt as stt_module  # noqa: E402
from nemus.config import get_config  # noqa: E402
from nemus.core.skills import explicit_skill, load_skills, match_skills  # noqa: E402
from nemus.core.tools.base import ToolResult  # noqa: E402
from nemus.core.tools.images import GenerateImageTool  # noqa: E402
from nemus.core.tools.presentations import CreatePresentationTool  # noqa: E402
from nemus.core.tools.video import CreateVideoTool  # noqa: E402
from nemus.voice.stt import GeminiTranscriber, OpenAITranscriber  # noqa: E402


def test_skill_router() -> None:
    cfg = get_config(reload=True)
    skills = load_skills(cfg.skills.dir)
    names = {skill.name for skill in skills}
    assert "pptx-design-styles" in names, "скилл презентаций должен находиться"
    assert match_skills("создай презентацию о пшенице", skills)[0].name == (
        "pptx-design-styles"
    )
    assert match_skills("нарисуй чиби-версию по фото", skills)[0].name == (
        "superimage-generator"
    )
    assert match_skills("сделай видео про кота", skills)[0].name == "hyperframes"
    command = explicit_skill("/pptx-design-styles сделай колоду", skills)
    assert command is not None and command.name == "pptx-design-styles"
    assert explicit_skill("обычный текст", skills) is None
    print("ok  скиллы: авто-выбор по теме и команда /имя")


def test_cloud_stt() -> None:
    seen: dict[str, object] = {}
    attempts: list[str] = []

    class Response:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"text": "  привет, немуся  "}

    def fake_post(url: str, **kwargs: object) -> Response:
        seen["url"] = url
        seen.update(kwargs)
        data = kwargs.get("data") or {}
        attempts.append(str(data.get("model")))  # type: ignore[union-attr]
        return Response()

    original = stt_module.httpx.post
    stt_module.httpx.post = fake_post  # type: ignore[assignment]
    try:
        audio = Path(tempfile.mkdtemp()) / "voice.ogg"
        audio.write_bytes(b"OggS")
        text = OpenAITranscriber("key", language="ru", prompt="Немуся").transcribe(audio)
    finally:
        stt_module.httpx.post = original  # type: ignore[assignment]
    assert text == "привет, немуся"
    assert str(seen["url"]).endswith("/audio/transcriptions")
    assert attempts == ["gpt-4o-mini-transcribe"]
    print("ok  облачное STT: OGG уходит напрямую, текст очищается")


def test_gemini_stt() -> None:
    seen: dict[str, object] = {}

    class Response:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"audioTranscription": {"text": "не муся, привет"}}
                            ]
                        }
                    }
                ]
            }

    def fake_post(url: str, **kwargs: object) -> Response:
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    original = stt_module.httpx.post
    stt_module.httpx.post = fake_post  # type: ignore[assignment]
    try:
        audio = Path(tempfile.mkdtemp()) / "voice.ogg"
        audio.write_bytes(b"OggS")
        text = GeminiTranscriber("key", language="ru").transcribe(audio)
    finally:
        stt_module.httpx.post = original  # type: ignore[assignment]
    assert text == "не муся, привет"
    assert "gemini-3.5-transcribe" in str(seen["url"])
    payload = seen["json"]
    assert isinstance(payload, dict)
    print("ok  Gemini STT: OGG как inline-данные, разбор audioTranscription")


def test_telegram_skill_route() -> None:
    from nemus.core.telegram_bot import TelegramBot

    bot = object.__new__(TelegramBot)
    bot.config = get_config(reload=True)
    bot._skill_names = None
    assert bot._is_skill_command("/i-have-adhd"), "команда скилла должна доходить"
    assert bot._is_skill_command("/pptx-design-styles сделай колоду")
    assert not bot._is_skill_command("/start")
    assert not bot._is_skill_command("обычный текст")
    print("ok  Telegram: /имя скилла маршрутизируется в агента")


def test_image_tool() -> None:
    calls: dict[str, object] = {}
    #: валидный PNG — инструмент приводит формат через Pillow
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 80, 20)).save(buffer, "PNG")
    png = buffer.getvalue()

    class FakeImages:
        def generate(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return types.SimpleNamespace(
                data=[
                    types.SimpleNamespace(
                        b64_json=base64.b64encode(png).decode(), url=None
                    )
                ]
            )

        def edit(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return types.SimpleNamespace(
                data=[
                    types.SimpleNamespace(
                        b64_json=base64.b64encode(png).decode(), url=None
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **_: object) -> None:
            self.images = FakeImages()

    import openai

    original = openai.OpenAI
    openai.OpenAI = FakeClient  # type: ignore[assignment]
    try:
        tmp = Path(tempfile.mkdtemp())
        photo = tmp / "photo.png"
        photo.write_bytes(b"photo")
        tool = GenerateImageTool()
        tool.provider = "openai"
        tool.openai_api_key = "key"
        tool.workspace = str(tmp)
        result = tool.execute(
            prompt="чиби-портрет",
            reference=[str(photo)],
            size="1024x1024",
            quality="low",
            path=str(tmp / "out.png"),
        )
    finally:
        openai.OpenAI = original  # type: ignore[assignment]
    assert result.ok, result.content
    with Image.open(tmp / "out.png") as saved:
        assert saved.size == (2, 2)
    assert calls["model"] == "gpt-image-1"
    assert calls["input_fidelity"] == "high"

    no_key = GenerateImageTool()
    no_key.provider = "openai"
    missing = no_key.execute(prompt="кот")
    assert not missing.ok and "Не удалось создать изображение" in missing.content
    print("ok  generate_image: референс, сохранение PNG, понятная ошибка без ключа")


def test_presentation_tool() -> None:
    tmp = Path(tempfile.mkdtemp())
    tool = CreatePresentationTool()
    tool.workspace = str(tmp)
    result = tool.execute(
        topic="Пшеница: от поля до стола",
        subtitle="Школьный доклад",
        path=str(tmp / "deck.pptx"),
        slides=[
            {
                "title": "Пшеница кормит 4,5 млрд человек",
                "points": ["Главная зерновая культура", "20% калорий в рационе"],
                "stat": "20%",
                "stat_label": "калорий человечества",
            },
            {
                "title": "Урожай зависит от трёх факторов",
                "points": ["Сорт и семена", "Влага и сроки", "Защита от болезней"],
            },
            {
                "title": "Производство растёт",
                "points": ["Лидеры — Китай, Индия, Россия"],
                "chart": {
                    "labels": ["2021", "2022", "2023"],
                    "values": [778, 781, 785],
                    "title": "Млн тонн",
                },
            },
        ],
    )
    assert result.ok, result.content
    deck = tmp / "deck.pptx"
    assert deck.is_file() and deck.stat().st_size > 20_000

    from pptx import Presentation

    prs = Presentation(str(deck))
    assert len(prs.slides) == 4
    text = "\n".join(
        shape.text
        for slide in prs.slides
        for shape in slide.shapes
        if hasattr(shape, "text")
    )
    assert "Пшеница" in text and "20%" in text and "Урожай" in text

    empty = tool.execute(topic="Пусто", slides=[{"title": "Только заголовок"}] * 3)
    assert not empty.ok, "пустые слайды должны отклоняться"
    print("ok  create_presentation: 4 слайда, тексты и цифры на месте")


def test_video_tool() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("skip create_video: ffmpeg/ffprobe не установлены")
        return
    tmp = Path(tempfile.mkdtemp())
    tool = CreateVideoTool()
    tool.workspace = str(tmp)
    result = tool.execute(
        title="Пшеница: от поля до стола",
        size="1920x1080",
        path=str(tmp / "video.mp4"),
        scenes=[
            {"title": "Пшеница кормит миллиарды", "text": "Основа хлеба."},
            {"title": "Урожай зависит от трёх факторов", "text": "Сорт, влага, защита."},
            {"title": "Будущее за устойчивыми сортами", "text": "Селекция."},
        ],
    )
    assert result.ok, result.content
    video = tmp / "video.mp4"
    assert video.is_file() and video.stat().st_size > 10_000
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert float(probe.stdout.strip()) > 5, probe.stdout
    print("ok  create_video: 4 сцены, MP4 длиннее 5 секунд")


def main() -> int:
    test_skill_router()
    test_cloud_stt()
    test_gemini_stt()
    test_telegram_skill_route()
    test_image_tool()
    test_presentation_tool()
    test_video_tool()
    assert isinstance(ToolResult.success("x"), ToolResult)
    print("\nВсе тесты контентных инструментов пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
