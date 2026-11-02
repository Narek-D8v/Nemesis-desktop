"""Инструменты работы с экраном: просмотр и управление мышью/клавиатурой.

``see_screen`` — «показывает» экран модели. Если модель поддерживает
изображения (vision) — отправляется снимок. Если модель текстовая — включается
локальный cv2/OCR-конвейер: снимок обрабатывается OpenCV, текст распознаётся
средствами Windows (winrt-Windows.Media.Ocr), а элементы ищутся контурами cv2.

Все координаты в системе — **пиксели экрана**. Снимок по умолчанию делается 1:1
с экраном, поэтому координаты на картинке совпадают с координатами экрана и не
требуют пересчёта. Дополнительно ``see_screen`` всегда отдаёт нумерованный список
элементов с точными координатами, а ``control_screen`` умеет кликать по элементу
(``click_element``) или по тексту (``click_text``) — так модель не угадывает пиксели.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import threading
from typing import Any

from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.clipboard import paste_text

#: размер последнего снимка (ширина, высота) в пикселях изображения.
_last_image_size: tuple[int, int] = (0, 0)

#: Шаг координатной сетки в пикселях **экрана** (при уменьшении снимка).
GRID_STEP_SCREEN_PX = 100
#: Минимальный шаг сетки в пикселях изображения, чтобы подписи не налезали.
_GRID_MIN_STEP_PX = 24
#: размер экрана (ширина, высота).
_last_screen_size: tuple[int, int] = (0, 0)
#: во сколько раз картинка уменьшена относительно экрана (>= 1.0).
_last_scale: float = 1.0
#: пронумерованные элементы последнего снимка (координаты — пиксели экрана).
_last_elements: list[dict[str, Any]] = []

#: защищает согласованность состояния последнего снимка между потоками
#: (текстовый чат и realtime-сессия выполняются в разных потоках).
_state_lock = threading.Lock()

#: подстроки имён моделей, которые обычно понимают изображения
_KNOWN_VISION = (
    "gemini",
    "gpt-4o",
    "gpt-4.1",
    "gpt-4-turbo",
    "gpt-4-vision",
    "chatgpt-4o",
    "o1",
    "o3",
    "o4",
    "claude-3",
    "claude-4",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
    "vision",
    "llava",
    "pixtral",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "gemma-3",
    "internvl",
    "minicpm-v",
    "moondream",
    "phi-3.5-vision",
    "phi-4",
    "grok-vision",
    "grok-2-vision",
    "sonnet",
    "multimodal",
)


def model_supports_vision(model: str, mode: str = "auto") -> bool:
    """Определяет, понимает ли модель изображения.

    ``mode``: ``image`` — всегда да, ``ocr`` — всегда нет, ``auto`` — по имени модели.
    """
    if mode == "image":
        return True
    if mode == "ocr":
        return False
    name = (model or "").lower()
    return any(key in name for key in _KNOWN_VISION)


class ScreenSettings:
    """Настройки инструментов экрана (подставляются из конфига фабрикой)."""

    enabled: bool = True
    #: максимальная ширина снимка, отдаваемого vision-модели (0 — 1:1 с экраном)
    max_width: int = 0
    #: auto | image | ocr — как «показывать» экран текстовой/vision-модели
    vision_mode: str = "auto"
    #: максимальная ширина снимка для локального OCR
    ocr_max_width: int = 2200
    #: язык распознавания
    ocr_lang: str = "ru-RU"
    #: искать ли области/элементы через cv2
    detect_regions: bool = True
    #: ограничение числа строк текста в ответе
    max_lines: int = 80
    #: рисовать ли координатную сетку при уменьшении картинки
    draw_grid: bool = True


# ------------------------------------------------------------------ capture
def _screen_size() -> tuple[int, int]:
    import pyautogui

    size = pyautogui.size()
    return int(size[0]), int(size[1])


def _resize(image: Any, max_width: int) -> tuple[Any, float]:
    """Уменьшает изображение до ``max_width`` через cv2 (если доступно)."""
    if not max_width or image.width <= max_width:
        return image, 1.0
    ratio = max_width / image.width
    new_size = (max_width, max(1, int(round(image.height * ratio))))
    try:
        import cv2
        import numpy as np

        arr = np.array(image.convert("RGB"))
        small = cv2.resize(arr, new_size, interpolation=cv2.INTER_AREA)
        from PIL import Image

        return Image.fromarray(small), image.width / new_size[0]
    except Exception:  # noqa: BLE001
        return image.resize(new_size), image.width / new_size[0]


def _capture(max_width: int) -> tuple[Any, float]:
    import pyautogui

    img = pyautogui.screenshot()
    return _resize(img, max_width)


def _encode_png(image: Any) -> tuple[str, bytes]:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    raw = buf.getvalue()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii"), raw


def _store_state(
    screen: tuple[int, int],
    image_size: tuple[int, int],
    scale: float,
    elements: list[dict[str, Any]],
) -> None:
    """Атомарно сохраняет состояние последнего снимка экрана."""
    global _last_image_size, _last_screen_size, _last_scale, _last_elements

    with _state_lock:
        _last_screen_size = screen
        _last_image_size = image_size
        _last_scale = scale
        _last_elements = list(elements)


def _get_screen_size() -> tuple[int, int]:
    with _state_lock:
        return _last_screen_size


def _get_elements() -> list[dict[str, Any]]:
    with _state_lock:
        return list(_last_elements)


def _to_screen(x: Any, y: Any) -> tuple[int, int]:
    """Приводит координаты к пикселям экрана и ограничивает их границами."""
    try:
        sx = int(round(float(x)))
        sy = int(round(float(y)))
    except (TypeError, ValueError):
        raise ValueError("координаты должны быть числами") from None
    screen = _get_screen_size()
    if screen[0]:
        sx = max(0, min(sx, screen[0] - 1))
    if screen[1]:
        sy = max(0, min(sy, screen[1] - 1))
    return sx, sy


def _to_screen_xy(x: float, y: float, scale: float) -> tuple[int, int]:
    """Пересчитывает координаты картинки (при уменьшении) в пиксели экрана."""
    return _to_screen(float(x) * scale, float(y) * scale)


def _overlay_grid(image: Any, screen: tuple[int, int], scale: float) -> Any:
    """Рисует сетку с подписями в координатах экрана (при уменьшении снимка)."""
    if scale <= 1.001:
        return image
    try:
        import cv2
        import numpy as np
        from PIL import Image

        arr = np.array(image.convert("RGB"))
        step = max(_GRID_MIN_STEP_PX, int(GRID_STEP_SCREEN_PX / scale))
        color = (255, 0, 0)
        for x in range(step, image.width, step):
            cv2.line(arr, (x, 0), (x, image.height), color, 1)
            cv2.putText(
                arr,
                str(int(x * scale)),
                (x + 2, 14),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                color,
                1,
                cv2.LINE_AA,
            )
        for y in range(step, image.height, step):
            cv2.line(arr, (0, y), (image.width, y), color, 1)
            cv2.putText(
                arr,
                str(int(y * scale)),
                (2, y - 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                color,
                1,
                cv2.LINE_AA,
            )
        return Image.fromarray(arr)
    except Exception:  # noqa: BLE001
        return image


# ------------------------------------------------------------- local cv2/OCR
def _run_coro(coro: Any) -> Any:
    """Запускает корутину даже если в текущем потоке уже есть активный цикл."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ocr_png(png_bytes: bytes, lang: str) -> Any:
    """Распознаёт текст на PNG через Windows OCR (winrt-Windows.Media.Ocr)."""
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = None
    for tag in (lang, lang.split("-")[0]):
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
        except Exception:  # noqa: BLE001
            engine = None
        if engine is not None:
            break
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR недоступен (нет языкового пакета).")

    async def _recognize() -> Any:
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png_bytes)
        await writer.store_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        return await engine.recognize_async(bitmap)

    return _run_coro(_recognize())


def _ocr_items(png_bytes: bytes, lang: str) -> list[dict[str, Any]]:
    """Возвращает распознанные строки с рамками в пикселях изображения."""
    result = _ocr_png(png_bytes, lang)
    items: list[dict[str, Any]] = []
    for line in result.lines:
        words = line.words
        if not words:
            continue
        xs = [w.bounding_rect.x for w in words]
        ys = [w.bounding_rect.y for w in words]
        x2 = [w.bounding_rect.x + w.bounding_rect.width for w in words]
        y2 = [w.bounding_rect.y + w.bounding_rect.height for w in words]
        items.append(
            {
                "text": line.text,
                "x": int(min(xs)),
                "y": int(min(ys)),
                "w": int(max(x2) - min(xs)),
                "h": int(max(y2) - min(ys)),
            }
        )
    return items


def _detect_regions(image: Any, max_n: int = 12) -> list[tuple[int, int, int, int]]:
    """Ищет прямоугольные области (кнопки/окна) контурами cv2."""
    try:
        import cv2
        import numpy as np

        arr = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(
            edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
    except Exception:  # noqa: BLE001
        return []

    height, width = gray.shape[:2]
    min_area = width * height * 0.0006
    boxes: list[tuple[int, int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 24 or h < 16 or w * h < min_area:
            continue
        if w > width * 0.99 and h > height * 0.99:
            continue
        boxes.append((w * h, x, y, w, h))
    boxes.sort(reverse=True)
    return [(x, y, w, h) for _, x, y, w, h in boxes[:max_n]]


def _build_elements(
    image: Any, png_bytes: bytes, scale: float, settings: ScreenSettings
) -> list[dict[str, Any]]:
    """Строит нумерованный список элементов в пикселях экрана."""
    elements: list[dict[str, Any]] = []
    try:
        items = _ocr_items(png_bytes, settings.ocr_lang)
    except Exception:  # noqa: BLE001
        items = []
    max_lines = max(1, int(getattr(settings, "max_lines", 80)))
    for item in items[:max_lines]:
        cx, cy = _to_screen_xy(item["x"] + item["w"] / 2, item["y"] + item["h"] / 2, scale)
        elements.append(
            {
                "kind": "text",
                "text": item["text"],
                "cx": cx,
                "cy": cy,
                "screen": (cx, cy),
            }
        )
    if getattr(settings, "detect_regions", True):
        base = len(elements)
        regions = _detect_regions(image)
        for i, (x, y, w, h) in enumerate(regions):
            cx, cy = _to_screen_xy(x + w / 2, y + h / 2, scale)
            elements.append(
                {
                    "kind": "region",
                    "text": "",
                    "label": f"область {base + i}",
                    "cx": cx,
                    "cy": cy,
                    "screen": (cx, cy),
                }
            )
    for i, element in enumerate(elements, start=1):
        element["id"] = i
    return elements


def _format_elements(elements: list[dict[str, Any]]) -> str:
    lines = []
    for element in elements:
        name = element.get("text") or element.get("label") or ""
        name = name.replace('"', "'")
        lines.append(
            f'[{element["id"]}] {element["kind"]} "{name}" '
            f'→ пиксели экрана ({element["cx"]}, {element["cy"]})'
        )
    return "\n".join(lines)


# ------------------------------------------------------------------- tools
class SeeScreenTool(Tool):
    name = "see_screen"
    description = (
        "Смотрит, что сейчас на экране пользователя. Возвращает снимок экрана "
        "(если модель понимает изображения) и/или распознанный текст. Все координаты — "
        "в пикселях ЭКРАНА, снимок 1:1 с экраном. Возвращает нумерованный список "
        "элементов с точными координатами. Чтобы нажать — вызывай control_screen: "
        "проще всего click_element(id=…) или click_text(text=…) — тогда не нужно "
        "угадывать координаты."
    )
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE

    #: подставляются фабрикой
    settings: ScreenSettings | None = None
    llm: Any = None
    llm_config: Any = None

    def _current_model(self) -> str:
        if self.llm is not None:
            return getattr(self.llm, "model", "") or ""
        if self.llm_config is not None:
            return getattr(self.llm_config, "default_model", "") or ""
        return ""

    def _use_image(self) -> bool:
        settings = self.settings or ScreenSettings()
        mode = getattr(settings, "vision_mode", "auto")
        if mode == "image":
            return True
        if mode == "ocr":
            return False
        model = self._current_model()
        if not model:
            return True
        return model_supports_vision(model, mode)

    def execute(self, **_: object) -> ToolResult:
        settings = self.settings or ScreenSettings()
        if not settings.enabled:
            return ToolResult.failure("Просмотр экрана отключён в настройках.")

        screen = _screen_size()
        use_image = self._use_image()
        limit = settings.max_width if use_image else settings.ocr_max_width
        try:
            image, scale = _capture(limit)
            _, raw = _encode_png(image)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось сделать снимок экрана: {exc}")

        elements = _build_elements(image, raw, scale, settings)
        _store_state(screen, (image.width, image.height), scale, elements)
        listing = _format_elements(elements)
        hint = (
            "Чтобы кликнуть по элементу, вызови "
            "control_screen(action='click_element', id=N) или "
            "control_screen(action='click_text', text='…')."
        )

        if use_image:
            send = _overlay_grid(image, screen, scale) if settings.draw_grid else image
            data_url, _ = _encode_png(send)
            text = (
                f"Снимок экрана. Экран: {screen[0]}x{screen[1]} px; изображение "
                f"{image.width}x{image.height} px (масштаб {scale:g}). Координаты — "
                f"в пикселях ЭКРАНА.\nЭлементы:\n{listing}\n{hint}"
            )
            return ToolResult.success(
                text,
                data={
                    "width": image.width,
                    "height": image.height,
                    "scale": scale,
                    "mode": "image",
                    "elements": elements,
                },
                images=[data_url],
            )

        return self._ocr_result(image, screen, elements, listing, hint, scale)

    def _ocr_result(
        self,
        image: Any,
        screen: tuple[int, int],
        elements: list[dict[str, Any]],
        listing: str,
        hint: str,
        scale: float,
    ) -> ToolResult:
        if not elements:
            return ToolResult.failure(
                "Модель не поддерживает изображения, а локальный OCR ничего не "
                "распознал или недоступен. Установите: pip install "
                "winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
                "winrt-Windows.Storage.Streams winrt-Windows.Globalization."
            )
        head = (
            f"Распознанный текст с экрана (OCR, {image.width}x{image.height} px; "
            f"экран {screen[0]}x{screen[1]} px). Координаты — в пикселях ЭКРАНА.\n"
            f"Элементы:\n{listing}\n{hint}"
        )
        full_text = "\n".join(
            e["text"] for e in elements if e["kind"] == "text" and e["text"]
        )
        return ToolResult.success(
            head + "\n\nПолный распознанный текст:\n" + full_text,
            data={
                "width": image.width,
                "height": image.height,
                "scale": scale,
                "mode": "ocr",
                "elements": elements,
            },
        )


class ControlScreenTool(Tool):
    name = "control_screen"
    description = (
        "Управляет мышью и клавиатурой на экране. Координаты — в пикселях ЭКРАНА "
        "(как в see_screen). Действия (action): "
        "'click_element' (id — надёжнее всего: клик по элементу из see_screen), "
        "'click_text' (text — клик по видимому тексту, сам найдёт место), "
        "'move' (x, y), 'click' (x, y; button=left|right|middle, clicks), "
        "'double_click' (x, y), 'right_click' (x, y), "
        "'drag' (x, y, x2, y2), 'scroll' (amount: >0 вверх, <0 вниз; x, y), "
        "'type' (text), 'key' (keys — список клавиш, напр. ['ctrl','s']). "
        "Перед кликом сначала вызови see_screen."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "click_element | click_text | move | click | double_click | "
                    "right_click | drag | scroll | type | key"
                ),
            },
            "id": {
                "type": "integer",
                "description": "Номер элемента из see_screen для click_element.",
            },
            "text": {
                "type": "string",
                "description": "Текст для click_text (действие) или ввода.",
            },
            "x": {"type": "integer", "description": "Координата X в пикселях экрана."},
            "y": {"type": "integer", "description": "Координата Y в пикселях экрана."},
            "x2": {"type": "integer", "description": "Конечная X для drag."},
            "y2": {"type": "integer", "description": "Конечная Y для drag."},
            "button": {
                "type": "string",
                "description": "Кнопка мыши: left | right | middle (по умолчанию left).",
            },
            "clicks": {"type": "integer", "description": "Число кликов (по умолчанию 1)."},
            "amount": {
                "type": "integer",
                "description": "Величина прокрутки: >0 вверх, <0 вниз.",
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Клавиши для action='key', напр. ['ctrl', 's'].",
            },
        },
        "required": ["action"],
    }
    risk = RiskLevel.DANGEROUS

    settings: ScreenSettings | None = None

    def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        action = (action or "").strip().lower()
        if not action:
            return ToolResult.failure("Не указано действие (action).")
        settings = self.settings or ScreenSettings()
        if not settings.enabled:
            return ToolResult.failure("Управление экраном отключено в настройках.")

        try:
            import pyautogui
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"pyautogui недоступен: {exc}")

        needs_view = (
            "click",
            "double_click",
            "right_click",
            "click_element",
            "click_text",
            "drag",
        )
        if action in needs_view and _get_screen_size()[0] == 0:
            return ToolResult.failure(
                "Сначала вызови see_screen, чтобы понять, где находится нужный элемент."
            )

        try:
            if action == "click_element":
                return self._click_element(pyautogui, kwargs, settings)
            if action == "click_text":
                return self._click_text(pyautogui, kwargs, settings)
            if action == "move":
                return self._move(pyautogui, kwargs)
            if action in ("click", "double_click", "right_click"):
                return self._click(pyautogui, action, kwargs)
            if action == "drag":
                return self._drag(pyautogui, kwargs)
            if action == "scroll":
                return self._scroll(pyautogui, kwargs)
            if action == "type":
                return self._type(pyautogui, kwargs)
            if action == "key":
                return self._key(pyautogui, kwargs)
            return ToolResult.failure(f"Неизвестное действие: {action}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка управления экраном: {exc}")

    # ------------------------------------------------------------- actions
    def _click_element(self, pg: Any, kw: dict[str, Any], settings: ScreenSettings) -> ToolResult:
        elements = _get_elements()
        if not elements:
            return ToolResult.failure("Список элементов пуст — вызови see_screen.")
        try:
            element_id = int(kw.get("id"))
        except (TypeError, ValueError):
            return ToolResult.failure("Для click_element нужен числовой id.")
        for element in elements:
            if element["id"] == element_id:
                x, y = _to_screen(element["cx"], element["cy"])
                pg.click(x=x, y=y)
                name = element.get("text") or element.get("label") or ""
                return ToolResult.success(
                    f"Клик по элементу [{element_id}] «{name}» в ({x}, {y})."
                )
        return ToolResult.failure(f"Элемент id={element_id} не найден.")

    def _click_text(self, pg: Any, kw: dict[str, Any], settings: ScreenSettings) -> ToolResult:
        query = str(kw.get("text") or "").strip()
        if not query:
            return ToolResult.failure("Для click_text нужен text.")
        match = _find_element(query)
        if match is None and not _get_elements():
            _refresh_elements(settings)
            match = _find_element(query)
        if match is None:
            return ToolResult.failure(f"Текст «{query}» не найден на экране.")
        x, y = _to_screen(match["cx"], match["cy"])
        pg.click(x=x, y=y)
        return ToolResult.success(f"Клик по тексту «{match['text']}» в ({x}, {y}).")

    def _move(self, pg: Any, kw: dict[str, Any]) -> ToolResult:
        x, y = self._coords(kw)
        pg.moveTo(x, y, duration=0.2)
        return ToolResult.success(f"Курсор перемещён в ({x}, {y}).")

    def _click(self, pg: Any, action: str, kw: dict[str, Any]) -> ToolResult:
        x, y = self._coords(kw)
        button = str(kw.get("button") or ("right" if action == "right_click" else "left"))
        if action == "double_click":
            clicks = 2
        else:
            try:
                clicks = max(1, int(kw.get("clicks") or 1))
            except (TypeError, ValueError):
                clicks = 1
        pg.click(x=x, y=y, clicks=clicks, interval=0.12, button=button)
        return ToolResult.success(f"Клик ({button}) x{clicks} в ({x}, {y}).")

    def _drag(self, pg: Any, kw: dict[str, Any]) -> ToolResult:
        if kw.get("x2") is None or kw.get("y2") is None:
            return ToolResult.failure("Для drag нужны x, y, x2, y2.")
        x, y = self._coords(kw)
        x2, y2 = _to_screen(kw["x2"], kw["y2"])
        button = str(kw.get("button") or "left")
        pg.moveTo(x, y, duration=0.2)
        pg.dragTo(x2, y2, duration=0.4, button=button)
        return ToolResult.success(f"Перетаскивание ({x}, {y}) → ({x2}, {y2}).")

    def _scroll(self, pg: Any, kw: dict[str, Any]) -> ToolResult:
        try:
            amount = int(kw.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount == 0:
            return ToolResult.failure("Для scroll нужен ненулевой amount.")
        if kw.get("x") is not None and kw.get("y") is not None:
            x, y = self._coords(kw)
            pg.moveTo(x, y, duration=0.1)
        pg.scroll(amount)
        return ToolResult.success(f"Прокрутка на {amount}.")

    def _type(self, pg: Any, kw: dict[str, Any]) -> ToolResult:
        text = str(kw.get("text") or "")
        if not text:
            return ToolResult.failure("Для type нужен text.")
        # Длину сообщаем целиком: обрезанный фрагмент модель принимает за
        # неполный ввод и повторяет печать (дубли текста в окне).
        if text.isascii():
            pg.write(text, interval=0.02)
            return ToolResult.success(f"Набран текст ({len(text)} символов).")
        if paste_text(pg, text):
            return ToolResult.success(
                f"Вставлен текст ({len(text)} символов) из буфера обмена."
            )
        return ToolResult.failure(
            "Не удалось вставить нелатинский текст: буфер обмена недоступен. "
            "Текст НЕ введён — не повторяй ввод, сообщи об ошибке."
        )

    def _key(self, pg: Any, kw: dict[str, Any]) -> ToolResult:
        keys = kw.get("keys")
        if isinstance(keys, str):
            keys = [k for k in keys.replace("+", " ").split() if k]
        if not keys:
            key = str(kw.get("key") or "")
            if not key:
                return ToolResult.failure("Для key нужны keys (список) или key (строка).")
            pg.press(key)
            return ToolResult.success(f"Нажата клавиша «{key}».")
        combo = [str(k).lower() for k in keys]
        if len(combo) == 1:
            pg.press(combo[0])
        else:
            pg.hotkey(*combo)
        return ToolResult.success(f"Нажато: {'+'.join(combo)}.")

    # ------------------------------------------------------------- helpers
    def _coords(self, kw: dict[str, Any]) -> tuple[int, int]:
        if kw.get("x") is None or kw.get("y") is None:
            raise ValueError("нужны координаты x и y")
        return _to_screen(kw["x"], kw["y"])


def _find_element(query: str) -> dict[str, Any] | None:
    needle = query.strip().lower()
    if not needle:
        return None
    best: dict[str, Any] | None = None
    for element in _get_elements():
        if element["kind"] != "text":
            continue
        text = (element.get("text") or "").lower()
        if needle == text:
            return element
        if needle in text and (best is None or len(text) < len(best.get("text", ""))):
            best = element
    return best


def _refresh_elements(settings: ScreenSettings) -> None:
    screen = _screen_size()
    image, scale = _capture(settings.ocr_max_width)
    _, raw = _encode_png(image)
    elements = _build_elements(image, raw, scale, settings)
    _store_state(screen, (image.width, image.height), scale, elements)


def screen_tools() -> list[Tool]:
    return [SeeScreenTool(), ControlScreenTool()]
