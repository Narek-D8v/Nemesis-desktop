"""Видеоролик из содержательных сцен с настоящим визуалом (Pillow + ffmpeg).

Главная беда генеративных агентов — «видео» из текста на статичном фоне.
Этот инструмент так не умеет: для каждой сцены нужен образ (image или visual),
недостающие картинки он генерирует сам, а рендер делает движение — медленный
наезд камеры, панораму, плавные переходы и появление подписи. Текст здесь
только подпись поверх кадра, а не сам кадр.

Для сложной motion-графики (титры, инфографика, 3D) остаются HyperFrames и
Remotion; этот инструмент закрывает предсказуемый ролик с фотографиями.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from nemus.config import PROJECT_ROOT
from nemus.core.skills import slugify
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: не показывать консольное окно ffmpeg поверх рабочего стола
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: доступные форматы: 16:9, 9:16, 1:1
SIZES = {
    "1920x1080": (1920, 1080),
    "1080x1920": (1080, 1920),
    "1080x1080": (1080, 1080),
}
#: палитра в тон презентациям
BG = (12, 18, 32)
CARD = (18, 26, 42)
ACCENT = (245, 166, 35)
TEXT = (247, 249, 252)
MUTED = (190, 202, 220)
#: титульная сцена
TITLE_SECONDS = 2.5
#: частота кадров
FPS = 30


def _font(size: int) -> Any:
    """Шрифт с кириллицей; при отсутствии — встроенный Pillow."""
    from PIL import ImageFont

    for name in ("arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_px(text: str, font: Any, width: int, draw: Any) -> str:
    """Перенос текста по реальной ширине шрифта, а не по числу символов."""
    words = str(text).split()
    if not words:
        return ""
    lines: list[str] = []
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        if not line or draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return "\n".join(lines)


def _cover(image: Any, size: tuple[int, int], blur: int = 0) -> Any:
    """Заполняет кадр картинкой без чёрных полей (cover-crop)."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    filled = ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )
    if blur:
        filled = filled.filter(ImageFilter.GaussianBlur(blur))
    filled = ImageEnhance.Brightness(filled).enhance(0.72 if blur else 0.92)
    filled = ImageEnhance.Color(filled).enhance(0.95)
    return filled


def _background(
    image_path: Path | None, size: tuple[int, int], *, blur: bool = False
) -> Any:
    """Фон кадра: полноэкранный визуал с затемнением под текст."""
    from PIL import Image, ImageDraw

    width, height = size
    if image_path and image_path.is_file():
        with Image.open(image_path) as raw:
            frame = _cover(raw, size, blur=22 if blur else 0)
    else:
        # резерв: глубокий градиент с мягкими световыми пятнами, без текста
        frame = Image.new("RGB", size, BG)
        draw = ImageDraw.Draw(frame)
        for y in range(height):
            shade = int(8 + 26 * (1 - y / max(1, height - 1)))
            draw.line((0, y, width, y), fill=(shade, shade + 7, shade + 22))
        for radius, point, color in (
            (int(height * 0.55), (int(width * 0.82), int(height * 0.20)), (54, 76, 118)),
            (int(height * 0.42), (int(width * 0.18), int(height * 0.78)), (92, 58, 34)),
        ):
            glow = Image.new("RGBA", size, (0, 0, 0, 0))
            gdraw = ImageDraw.Draw(glow)
            gdraw.ellipse(
                (
                    point[0] - radius,
                    point[1] - radius,
                    point[0] + radius,
                    point[1] + radius,
                ),
                fill=(*color, 70),
            )
            from PIL import ImageFilter

            glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
            frame = Image.alpha_composite(frame.convert("RGBA"), glow).convert("RGB")

    # Затемнение слева и снизу: подпись читается на любом фото. Два слоя
    # складываем отдельно: иначе линия с alpha=0 затирает предыдущий градиент
    # и на кадре появляется резкая горизонтальная полоса.
    left = Image.new("RGBA", size, (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left)
    for x in range(width):
        alpha = int(196 * (1 - x / max(1, width - 1)) ** 1.25)
        if alpha > 0:
            left_draw.line((x, 0, x, height), fill=(3, 7, 16, alpha))
    bottom = Image.new("RGBA", size, (0, 0, 0, 0))
    bottom_draw = ImageDraw.Draw(bottom)
    for y in range(height):
        if y > height * 0.42:
            alpha = int(174 * ((y - height * 0.42) / (height * 0.58)) ** 1.15)
            if alpha > 0:
                bottom_draw.line((0, y, width, y), fill=(3, 7, 16, alpha))
    shade = Image.alpha_composite(left, bottom)
    return Image.alpha_composite(frame.convert("RGBA"), shade).convert("RGB")


def _text_overlay(
    title: str,
    body: str,
    size: tuple[int, int],
    *,
    index: int = 0,
    total: int = 0,
) -> Any:
    """Прозрачный слой с подписью, шагом и полосой прогресса."""
    from PIL import Image, ImageDraw

    width, height = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    margin = max(24, int(width * 0.065))
    max_width = int(width * (0.78 if height > width else 0.58))
    title_font = _font(max(30, int(width * (0.048 if height > width else 0.052))))
    body_font = _font(max(18, int(width * 0.021)))
    small_font = _font(max(14, int(width * 0.015)))

    title_text = _wrap_px(title, title_font, max_width, draw)
    body_text = _wrap_px(body, body_font, max_width, draw) if body else ""
    title_box = draw.multiline_textbbox(
        (0, 0), title_text, font=title_font, spacing=int(title_font.size * 0.18)
    )
    body_box = (
        draw.multiline_textbbox(
            (0, 0), body_text, font=body_font, spacing=int(body_font.size * 0.40)
        )
        if body_text
        else (0, 0, 0, 0)
    )
    gap = int(title_font.size * 0.34) if body_text else 0
    total_height = (title_box[3] - title_box[1]) + gap + (body_box[3] - body_box[1])
    top = height - margin - total_height - int(height * 0.035)
    top = max(int(height * 0.18), top)

    # акцентная вертикальная линия
    draw.rounded_rectangle(
        (margin - int(width * 0.022), top, margin - int(width * 0.014), top + total_height),
        radius=max(2, int(width * 0.004)),
        fill=ACCENT,
    )
    draw.multiline_text(
        (margin, top),
        title_text,
        font=title_font,
        fill=TEXT,
        spacing=int(title_font.size * 0.18),
        stroke_width=max(1, int(width * 0.0012)),
        stroke_fill=(4, 8, 16),
    )
    if body_text:
        draw.multiline_text(
            (margin, top + (title_box[3] - title_box[1]) + gap),
            body_text,
            font=body_font,
            fill=MUTED,
            spacing=int(body_font.size * 0.40),
            stroke_width=max(1, int(width * 0.0008)),
            stroke_fill=(4, 8, 16),
        )

    # номер сцены и полоса прогресса — ощущение движения вперёд
    if total:
        label = f"{index:02d} / {total:02d}"
        draw.text(
            (margin, top - int(height * 0.065)),
            label,
            font=small_font,
            fill=ACCENT,
            stroke_width=1,
            stroke_fill=(4, 8, 16),
        )
        bar_y = height - int(height * 0.045)
        bar_width = width - 2 * margin
        draw.rounded_rectangle(
            (margin, bar_y, margin + bar_width, bar_y + max(3, int(height * 0.004))),
            radius=2,
            fill=(255, 255, 255, 48),
        )
        draw.rounded_rectangle(
            (
                margin,
                bar_y,
                margin + bar_width * min(1.0, index / max(1, total)),
                bar_y + max(3, int(height * 0.004)),
            ),
            radius=2,
            fill=ACCENT,
        )
    return layer


def _title_overlay(title: str, subtitle: str, size: tuple[int, int]) -> Any:
    """Титульный слой: крупное название по центру."""
    from PIL import Image, ImageDraw

    width, height = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    margin = int(width * 0.08)
    title_font = _font(max(42, int(width * 0.072)))
    sub_font = _font(max(20, int(width * 0.021)))
    title_text = _wrap_px(title, title_font, width - 2 * margin, draw)
    sub_text = _wrap_px(subtitle, sub_font, int(width * 0.72), draw) if subtitle else ""
    title_box = draw.multiline_textbbox(
        (0, 0), title_text, font=title_font, spacing=int(title_font.size * 0.12)
    )
    sub_box = (
        draw.multiline_textbbox(
            (0, 0), sub_text, font=sub_font, spacing=int(sub_font.size * 0.34)
        )
        if sub_text
        else (0, 0, 0, 0)
    )
    gap = int(title_font.size * 0.35) if sub_text else 0
    total = (title_box[3] - title_box[1]) + gap + (sub_box[3] - sub_box[1])
    top = (height - total) // 2
    draw.text(
        (margin, top - int(height * 0.075)),
        "НЕМУСЯ ПРЕДСТАВЛЯЕТ",
        font=_font(max(14, int(width * 0.014))),
        fill=ACCENT,
        stroke_width=1,
        stroke_fill=(4, 8, 16),
    )
    draw.multiline_text(
        (margin, top),
        title_text,
        font=title_font,
        fill=TEXT,
        spacing=int(title_font.size * 0.12),
        stroke_width=max(1, int(width * 0.0014)),
        stroke_fill=(4, 8, 16),
    )
    if sub_text:
        draw.multiline_text(
            (margin, top + (title_box[3] - title_box[1]) + gap),
            sub_text,
            font=sub_font,
            fill=MUTED,
            spacing=int(sub_font.size * 0.34),
            stroke_width=1,
            stroke_fill=(4, 8, 16),
        )
    draw.rounded_rectangle(
        (margin, top + total + int(height * 0.035), margin + int(width * 0.16), top + total + int(height * 0.043)),
        radius=3,
        fill=ACCENT,
    )
    return layer


class CreateVideoTool(Tool):
    """Собирает MP4 из сцен: визуал, подпись, движение камеры, переходы."""

    name = "create_video"
    description = (
        "Собирает готовый MP4 из сториборда: каждая сцена — отдельный визуал "
        "(фото/картинка) с подписью, медленным наездом камеры, панорамой и "
        "плавными переходами. Это НЕ статичное слайд-шоу из текста. Для каждой "
        "сцены передай image (путь к готовой картинке) или visual (что показать "
        "словами) — недостающие картинки инструмент сгенерирует сам. Минимум 3 "
        "содержательные сцены; сцена без визуала и без visual отклоняется. "
        "Для сложной motion-графики, титров и инфографики используй HyperFrames "
        "или Remotion, а этот инструмент — для предсказуемого ролика с кадрами. "
        "Готовый файл отправь через send_telegram."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Название ролика (титульная сцена).",
            },
            "scenes": {
                "type": "array",
                "description": "3–12 содержательных сцен, у каждой свой визуал.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Заголовок сцены — короткий вывод.",
                        },
                        "text": {
                            "type": "string",
                            "description": "1–3 предложения содержания.",
                        },
                        "image": {
                            "type": "string",
                            "description": (
                                "Путь к готовой картинке для этой сцены. Если её "
                                "нет — заполни visual, и инструмент сгенерирует кадр."
                            ),
                        },
                        "visual": {
                            "type": "string",
                            "description": (
                                "Что должно быть в кадре: объект, действие, место, "
                                "свет, ракурс. Без текста и надписей на картинке. "
                                "Например: «руки наливают тесто на горячую сковороду, "
                                "крупный план, тёплый утренний свет»."
                            ),
                        },
                        "duration": {
                            "type": "number",
                            "description": "Длительность сцены, сек (по умолчанию 4).",
                        },
                    },
                    "required": ["title"],
                },
            },
            "duration": {
                "type": "number",
                "description": (
                    "Целевая длительность всего ролика, сек. Обязательна: "
                    "если пользователь сказал 2 минуты — передай 120, и сцены "
                    "должны дать примерно столько же. Короткий ролик вместо "
                    "запрошенного будет отклонён."
                ),
            },
            "style": {
                "type": "string",
                "description": (
                    "Единый визуальный стиль для генерируемых кадров: например "
                    "«тёплая кухня, плёночная фотография, мягкий свет». "
                    "Необязательно."
                ),
            },
            "size": {
                "type": "string",
                "enum": list(SIZES),
                "description": "Формат: 16:9, 9:16 или квадрат.",
            },
            "music": {
                "type": "string",
                "description": "Путь к аудио для фоновой музыки (необязательно).",
            },
            "path": {
                "type": "string",
                "description": "Куда сохранить MP4 (по умолчанию — рабочая папка).",
            },
        },
        "required": ["title", "scenes", "duration"],
    }
    risk = RiskLevel.MEDIUM

    workspace: str = "data/workspace"
    #: настроенный generate_image из фабрики; если нет — создаём свой
    image_tool: Any = None

    def execute(self, **kwargs: Any) -> ToolResult:
        title = str(kwargs.get("title") or "").strip()
        scenes = kwargs.get("scenes") or []
        if not title:
            return ToolResult.failure("Не указано название ролика (title).")
        if not isinstance(scenes, list) or len(scenes) < 3:
            return ToolResult.failure(
                "Нужно минимум 3 содержательные сцены (scenes), а не пустой "
                "ролик из одного кадра."
            )
        try:
            target_duration = float(kwargs.get("duration") or 0)
        except (TypeError, ValueError):
            target_duration = 0
        if target_duration <= 0:
            return ToolResult.failure(
                "Не указана целевая длительность duration (сек). Для 2-минутного "
                "ролика передай 120: иначе можно случайно сдать 15-секундную "
                "нарезку вместо полного видео."
            )
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return ToolResult.failure(
                "ffmpeg не найден в PATH — без него собрать MP4 нельзя."
            )
        size_name = str(kwargs.get("size") or "1920x1080").strip()
        size = SIZES.get(size_name)
        if size is None:
            return ToolResult.failure(
                f"Неизвестный формат {size_name}. Доступно: {', '.join(SIZES)}."
            )

        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(scenes, start=1):
            if not isinstance(raw, dict):
                return ToolResult.failure(f"Сцена {index}: ожидался объект.")
            scene_title = str(raw.get("title") or "").strip()
            text = str(raw.get("text") or "").strip()
            image = str(raw.get("image") or "").strip()
            visual = str(raw.get("visual") or "").strip()
            if not scene_title and not text and not image and not visual:
                return ToolResult.failure(f"Сцена {index} пустая — добавь содержание.")
            try:
                duration = float(raw.get("duration") or 4.0)
            except (TypeError, ValueError):
                duration = 4.0
            duration = min(12.0, max(1.5, duration))
            normalized.append(
                {
                    "title": scene_title,
                    "text": text,
                    "image": image,
                    "visual": visual,
                    "duration": duration,
                }
            )

        total_duration = sum(scene["duration"] for scene in normalized)
        if total_duration < target_duration * 0.8:
            return ToolResult.failure(
                f"Сцены дают {total_duration:.0f} с, а нужен ролик на "
                f"{target_duration:.0f} с. Добавь все шаги/сцены до нужной "
                f"длительности (не хватает ~{target_duration - total_duration:.0f} с); "
                "для motion-видео используй HyperFrames/Remotion, а не обрезанный "
                "текстовый ролик."
            )
        if total_duration > target_duration * 1.2:
            scale = target_duration / total_duration
            for scene in normalized:
                scene["duration"] = max(1.0, scene["duration"] * scale)

        target = self._target_path(kwargs.get("path"), title)
        target.parent.mkdir(parents=True, exist_ok=True)
        music = str(kwargs.get("music") or "").strip()
        music_path = Path(music).expanduser() if music else None
        if music_path is not None and not music_path.is_file():
            return ToolResult.failure(f"Музыка не найдена: {music_path}")

        style = str(kwargs.get("style") or "").strip()
        try:
            generated = self._ensure_visuals(normalized, title, style, size_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"create_video: визуал не готов: {exc}")
            return ToolResult.failure(
                "Не удалось подготовить визуал для ролика: "
                f"{exc}. Ролик из одного текста на фоне не собираю: "
                "передай image/visual для каждой сцены или проверь generate_image."
            )

        try:
            self._render(ffmpeg, title, normalized, size, target, music_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"create_video: {exc}")
            return ToolResult.failure(f"Не удалось собрать видео: {exc}")
        if not target.is_file() or target.stat().st_size < 1024:
            return ToolResult.failure("ffmpeg не создал файл видео.")
        return ToolResult.success(
            f"Видео «{title}» готово: {target} "
            f"({target.stat().st_size / 1024 / 1024:.1f} МБ, "
            f"сцен: {len(normalized) + 1}, визуалов: "
            f"{len(generated) or len(normalized)}). Отправь файл через send_telegram.",
            data={
                "path": str(target),
                "scenes": len(normalized) + 1,
                "visuals": len(generated),
            },
        )

    # ------------------------------------------------------------- visuals
    def _ensure_visuals(
        self,
        scenes: list[dict[str, Any]],
        title: str,
        style: str,
        size_name: str,
    ) -> list[str]:
        """Проверяет картинки и генерирует недостающие кадры.

        Текстовые сцены без визуала запрещены: именно они превращали ролик в
        «текст на фоне». Если картинки нет, но есть visual — генерируем её
        через тот же generate_image, что и остальные инструменты.
        """
        generated: list[str] = []
        missing: list[tuple[int, str]] = []
        for index, scene in enumerate(scenes, start=1):
            image = str(scene.get("image") or "").strip()
            if image:
                path = Path(image).expanduser()
                if path.is_file():
                    scene["image"] = str(path)
                    continue
                raise RuntimeError(
                    f"сцена {index}: картинка не найдена — {path}; "
                    "передай существующий файл или заполни visual"
                )
            prompt = str(scene.get("visual") or "").strip()
            if not prompt:
                raise RuntimeError(
                    f"сцена {index}: нет ни image, ни visual — не из чего "
                    "сделать кадр, а текст на фоне не собираю"
                )
            missing.append((index, prompt))

        if not missing:
            return generated

        image_tool = self.image_tool
        if image_tool is None:
            from nemus.core.tools.images import GenerateImageTool

            image_tool = GenerateImageTool()
            image_tool.workspace = self.workspace

        aspect_size = {
            "1920x1080": "1536x1024",
            "1080x1920": "1024x1536",
            "1080x1080": "1024x1024",
        }.get(size_name, "auto")
        slug = self._slug(title)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        assets = Path(self.workspace) / "videos" / "assets"
        assets.mkdir(parents=True, exist_ok=True)

        def make(item: tuple[int, str]) -> tuple[int, str]:
            index, prompt = item
            full = (
                f"{prompt}. Визуальный стиль: {style or 'кинематографично, естественный свет, фотореалистично'}. "
                "Кадр для видео, без текста, без надписей, без букв, без логотипов и водяных знаков."
            )
            target = assets / f"{slug}-{stamp}-{index:02d}.png"
            last = ""
            for attempt in range(1, 4):
                result = image_tool.execute(
                    prompt=full,
                    path=str(target),
                    size=aspect_size,
                    quality="high",
                )
                if result.ok:
                    data = result.data or {}
                    path = str(data.get("path") or target)
                    if not Path(path).is_file():
                        raise RuntimeError(f"сцена {index}: файл не создан")
                    return index, path
                last = result.content
                errors = []
                if isinstance(result.data, dict):
                    errors = [str(item) for item in result.data.get("errors") or []]
                delay = self._retry_after(errors or [last])
                if delay is None or attempt == 3:
                    break
                logger.info(
                    f"create_video: лимит генерации кадров, сцена {index}, "
                    f"жду {delay} с (попытка {attempt + 1}/3)"
                )
                time.sleep(delay)
            raise RuntimeError(f"сцена {index}: {last}")

        # Pollinations на бесплатном тарифе даёт 1 картинку в 45 секунд.
        # Поэтому генерируем последовательно и пережидаем лимит, а не роняем
        # весь ролик из-за параллельных запросов.
        for index, prompt in missing:
            try:
                scene_index, path = make((index, prompt))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"сцена {index}: {exc}") from exc
            scenes[scene_index - 1]["image"] = path
            generated.append(path)
        return generated

    def _retry_after(self, messages: list[str]) -> float | None:
        """Сколько ждать, если бесплатный провайдер упёрся в лимит."""
        text = " ".join(str(message) for message in messages)
        match = re.search(r"retry after\s+(\d+)\s*s", text, re.IGNORECASE)
        if match:
            return min(90.0, max(5.0, float(match.group(1)) + 2))
        if "FREE_LIMIT" in text or "Free limit reached" in text:
            return 47.0
        return None

    def _slug(self, title: str) -> str:
        try:
            return slugify(title)[:40].strip("-") or "video"
        except ValueError:
            return "video"

    # --------------------------------------------------------------- render
    def _render(
        self,
        ffmpeg: str,
        title: str,
        scenes: list[dict[str, Any]],
        size: tuple[int, int],
        target: Path,
        music: Path | None,
    ) -> None:
        """Рисует слои и склеивает клипы с движением и переходами."""
        with tempfile.TemporaryDirectory(prefix="nemus_video_") as tmp:
            folder = Path(tmp)
            items: list[tuple[Path, Path, float]] = []
            first_image = Path(scenes[0]["image"]) if scenes else None
            title_bg = _background(first_image, size, blur=True)
            title_layer = _title_overlay(title, "", size)
            title_bg_path = folder / "title_bg.png"
            title_layer_path = folder / "title_layer.png"
            title_bg.save(title_bg_path, "PNG")
            title_layer.save(title_layer_path, "PNG")
            items.append((title_bg_path, title_layer_path, TITLE_SECONDS))

            for index, scene in enumerate(scenes, start=1):
                image = Path(scene["image"])
                background = _background(image, size)
                overlay = _text_overlay(
                    scene["title"] or title,
                    scene["text"],
                    size,
                    index=index,
                    total=len(scenes),
                )
                bg_path = folder / f"scene_{index:02d}_bg.png"
                layer_path = folder / f"scene_{index:02d}_text.png"
                background.save(bg_path, "PNG")
                overlay.save(layer_path, "PNG")
                items.append((bg_path, layer_path, scene["duration"]))

            clips: list[Path] = []
            for index, (image, layer, duration) in enumerate(items):
                clip = folder / f"clip_{index:02d}.mp4"
                self._render_clip(
                    ffmpeg, image, layer, duration, size, clip, index
                )
                clips.append(clip)

            concat = folder / "list.txt"
            concat.write_text(
                "\n".join(f"file '{clip.as_posix()}'" for clip in clips),
                encoding="utf-8",
            )
            joined = folder / "joined.mp4"
            process = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat),
                    "-c",
                    "copy",
                    str(joined),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
            if process.returncode != 0 or not joined.is_file():
                tail = (process.stderr or "").strip().splitlines()
                raise RuntimeError(
                    tail[-1] if tail else "не удалось склеить сцены"
                )

            if music is None:
                shutil.copyfile(joined, target)
                return
            process = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(joined),
                    "-i",
                    str(music),
                    "-shortest",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
            if process.returncode != 0 or not target.is_file():
                tail = (process.stderr or "").strip().splitlines()
                raise RuntimeError(
                    tail[-1] if tail else "не удалось добавить музыку"
                )

    def _render_clip(
        self,
        ffmpeg: str,
        background: Path,
        overlay: Path,
        duration: float,
        size: tuple[int, int],
        target: Path,
        index: int,
    ) -> None:
        """Один клип: наезд/панорама фона, выезд подписи, fade-переход."""
        width, height = size
        frames = max(1, int(round(duration * FPS)))
        # чередуем наезд и панораму, чтобы ролик не выглядел однообразно
        if index % 2:
            zoom = f"1+0.075*on/{frames}"
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih/2-(ih/zoom/2)"
        else:
            zoom = f"1.075-0.075*on/{frames}"
            x_expr = "(iw-iw/zoom)*on/" + str(frames)
            y_expr = "ih/2-(ih/zoom/2)"
        fade_out = max(0.1, duration - 0.32)
        filter_complex = (
            f"[0:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan=z='{zoom}':x='{x_expr}':y='{y_expr}':d=1:"
            f"s={width}x{height}:fps={FPS}[bg];"
            f"[bg][1:v]overlay=0:'(1-min(t/0.55,1))*22':format=auto,"
            f"fade=t=in:st=0:d=0.28,fade=t=out:st={fade_out:.2f}:d=0.32,"
            "format=yuv420p[v]"
        )
        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-loop",
            "1",
            "-i",
            str(background),
            "-loop",
            "1",
            "-i",
            str(overlay),
            "-t",
            f"{duration:.2f}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ]
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=CREATE_NO_WINDOW,
        )
        if process.returncode != 0:
            tail = (process.stderr or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else "ffmpeg вернул ошибку")

    def _target_path(self, raw: Any, title: str) -> Path:
        text = str(raw or "").strip()
        if text:
            target = Path(text).expanduser()
            if not target.is_absolute():
                target = PROJECT_ROOT / target
            if target.suffix.lower() != ".mp4":
                target = target.with_suffix(".mp4")
            return target
        try:
            slug = slugify(title)[:40].strip("-") or "video"
        except ValueError:
            slug = "video"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Path(self.workspace) / "videos" / f"{slug}-{stamp}.mp4"


def video_tools() -> list[Tool]:
    """Инструменты видео для реестра."""
    return [CreateVideoTool()]


__all__ = ["CreateVideoTool", "video_tools"]
