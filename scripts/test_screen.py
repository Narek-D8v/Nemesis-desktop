"""Офлайн-проверки инструментов экрана и инъекции изображения в контекст агента.

Запуск:  venv\\Scripts\\python.exe scripts\\test_screen.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import Agent  # noqa: E402
from nemus.core.llm import StreamEvent  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, ToolCall  # noqa: E402
from nemus.core.tools import screen  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402


def test_see_screen() -> None:
    cfg = get_config()
    tool = screen.SeeScreenTool()
    tool.settings = cfg.screen
    tool.llm_config = cfg.llm
    res = tool.execute()
    assert res.ok, res.content
    assert res.images and res.images[0].startswith("data:image/png;base64,"), res.images
    assert screen._last_image_size[0] > 0
    # снимок 1:1 с экраном → координаты картинки == координаты экрана
    assert screen._last_scale == 1.0, screen._last_scale
    assert screen._last_image_size == screen._last_screen_size
    assert screen._last_elements, "нет списка элементов"
    print(f"OK see_screen: снимок 1:1, элементов {len(screen._last_elements)}")


def test_vision_detection() -> None:
    from nemus.core.tools.screen import model_supports_vision

    assert model_supports_vision("gemini-2.0-flash")
    assert model_supports_vision("gpt-4o")
    assert not model_supports_vision("gpt-3.5-turbo")
    assert not model_supports_vision("llama-3.1-70b-instruct")
    assert model_supports_vision("gpt-3.5-turbo", "image")
    assert not model_supports_vision("gemini-2.0-flash", "ocr")
    print("OK vision detection: эвристика и режимы override")


def test_elements_and_coords() -> None:
    assert screen._last_elements
    for element in screen._last_elements[:20]:
        assert isinstance(element["id"], int) and element["id"] >= 1
        assert 0 <= element["cx"] < screen._last_screen_size[0]
        assert 0 <= element["cy"] < screen._last_screen_size[1]
    # поиск по тексту
    text_el = next(e for e in screen._last_elements if e["kind"] == "text")
    assert screen._find_element(text_el["text"]) is not None
    assert screen._find_element("заведомо-нет-такого-текста-xyz") is None
    # клик по несуществующему id не должен падать с ошибкой
    tool = screen.ControlScreenTool()
    tool.settings = get_config().screen
    assert not tool.execute(action="click_element", id=10**9).ok
    assert not tool.execute(action="click_text", text="нет-такого-текста-xyz").ok
    print("OK элементы: id/координаты, поиск по тексту, обработка ошибок")


def test_see_screen_ocr_fallback() -> None:
    cfg = get_config()
    tool = screen.SeeScreenTool()
    tool.settings = cfg.screen.model_copy(update={"vision_mode": "ocr"})
    tool.llm_config = cfg.llm
    res = tool.execute()
    assert res.ok, res.content
    assert not res.images, "текстовая модель не должна получать картинку"
    assert "Распознанный текст" in res.content, res.content[:300]
    assert "Элементы:" in res.content
    assert "пиксели экрана" in res.content
    assert res.data.get("mode") == "ocr"
    assert res.data.get("width", 0) > 0
    assert res.data.get("elements")
    print("OK see_screen (ocr): текст + координаты, без изображения")


def test_see_screen_scaled() -> None:
    cfg = get_config()
    tool = screen.SeeScreenTool()
    tool.settings = cfg.screen.model_copy(
        update={"max_width": 960, "ocr_max_width": 960}
    )
    tool.llm_config = cfg.llm
    res = tool.execute()
    assert res.ok, res.content
    assert screen._last_scale > 1.0, screen._last_scale
    # координаты элементов всё равно в пикселях экрана
    w, h = screen._last_screen_size
    for element in res.data["elements"][:20]:
        assert 0 <= element["cx"] < w and 0 <= element["cy"] < h
    print(f"OK see_screen (scale={screen._last_scale:g}): координаты в пикселях экрана")


def test_see_screen_ocr_regions() -> None:
    from nemus.core.tools.screen import _detect_regions

    img = screen._capture(2200)[0]
    regions = _detect_regions(img)
    assert isinstance(regions, list)
    print(f"OK cv2 regions: найдено областей {len(regions)}")


def test_control_validation() -> None:
    cfg = get_config()
    tool = screen.ControlScreenTool()
    tool.settings = cfg.screen
    assert not tool.execute(action="click").ok  # нет координат
    assert not tool.execute(action="scroll", amount=0).ok
    assert not tool.execute(action="nope").ok
    assert not tool.execute(action="").ok
    print("OK control_screen: валидация аргументов")


def test_control_disabled() -> None:
    tool = screen.ControlScreenTool()
    tool.settings = screen.ScreenSettings()
    tool.settings.enabled = False
    assert not tool.execute(action="move", x=1, y=1).ok
    print("OK control_screen: отключено настройками")


class FakeVisionLLM:
    """Первый шаг — вызвать see_screen, второй — финал."""

    def __init__(self) -> None:
        self.step = 0
        self.saw_image = False

    def stream(self, messages, tools=None, **overrides):
        self.step += 1
        if self.step == 1:
            call = ToolCall(id="c1", name="see_screen", arguments={})
            yield StreamEvent(
                kind="final", response=LLMResponse(content="", tool_calls=[call])
            )
        else:
            for m in messages:
                content = m.content
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "image_url":
                            self.saw_image = True
            yield StreamEvent(kind="final", response=LLMResponse(content="Вижу экран."))


def test_agent_injects_image() -> None:
    cfg = get_config()
    registry = ToolRegistry()
    registry.register_many(screen.screen_tools())
    for name in ("see_screen", "control_screen"):
        registry.get(name).settings = cfg.screen

    # авто-разрешение see_screen в smart-режиме не обязательно: она SAFE
    agent = Agent(
        config=cfg,
        llm=FakeVisionLLM(),  # type: ignore[arg-type]
        registry=registry,
        permissions=PermissionManager(cfg),
    )
    kinds = [e.kind for e in agent.run("Посмотри, что у меня на экране")]
    assert "tool_result" in kinds and "final" in kinds, kinds
    assert agent.llm.saw_image, "модель не получила изображение"  # type: ignore[attr-defined]
    print("OK agent: изображение экрана попало в контекст модели")


def test_clipboard_unicode() -> None:
    """Кириллица в буфере обмена: WinAPI, без COM/Qt и без потери символов."""
    from nemus.core.tools.clipboard import get_clipboard, set_clipboard

    if sys.platform != "win32":
        print("SKIP clipboard: проверка только для Windows")
        return
    previous = get_clipboard()
    text = "Рецепт блинчиков: 2 яйца, 500 мл молока — щепотка соли."
    try:
        assert set_clipboard(text), "не удалось положить Unicode-текст в буфер"
        assert get_clipboard() == text, "буфер обмена исказил кириллицу"
    finally:
        if previous:
            set_clipboard(previous)
    print("OK clipboard: кириллица без COM/Qt")


class FakeWriter:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.hotkeys: list[tuple[str, ...]] = []

    def write(self, text, interval=0.0):  # noqa: ANN001
        self.typed.append(text)

    def hotkey(self, *keys):  # noqa: ANN002
        self.hotkeys.append(tuple(keys))


def test_control_type_unicode() -> None:
    """Нелатинский текст — только через буфер обмена; результат не обрезается."""
    tool = screen.ControlScreenTool()
    tool.settings = get_config().screen
    fake = FakeWriter()
    text = "Рецепт блинчиков:\n1. Ингредиенты: 2 яйца, 500 мл молока."
    calls: list[str] = []
    original = screen.paste_text

    try:
        screen.paste_text = lambda pg, value: calls.append(value) or True
        res = tool._type(fake, {"text": text})
        assert res.ok and calls == [text], (res.content, calls)
        assert f"{len(text)} символов" in res.content, res.content
        assert not fake.typed, "кириллица не должна набираться typewrite"
    finally:
        screen.paste_text = original

    try:
        screen.paste_text = lambda pg, value: False
        res = tool._type(fake, {"text": "Привет"})
        assert not res.ok, res.content
        assert "НЕ введён" in res.content, res.content
        assert not fake.typed, fake.typed
    finally:
        screen.paste_text = original
    print("OK control_screen: Unicode через буфер обмена, без тихой потери")


def main() -> int:
    test_vision_detection()
    test_see_screen()
    test_elements_and_coords()
    test_control_validation()
    test_control_disabled()
    test_clipboard_unicode()
    test_control_type_unicode()
    test_see_screen_ocr_fallback()
    test_see_screen_scaled()
    test_see_screen_ocr_regions()
    test_agent_injects_image()
    print("\nSCREEN TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
