"""Быстрый видеоролик из содержательных сцен (Pillow + ffmpeg).

Пустое видео из одних заголовков — частая беда генеративных агентов. Этот
инструмент берёт готовый сториборд (заголовок, текст, картинка, длительность)
и собирает нормальный MP4 16:9/9:16: тёмный фон, акцентная полоса, читаемый
текст, вписанные изображения. Для сложной анимации остаётся hyperframes, а
этот инструмент закрывает быстрый и предсказуемый ролик.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

from nemus.config import PROJECT_ROOT
from nemus.core.skills import slugify
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: доступные форматы: 16:9, 9:16, 1:1
SIZES = {
    "1920x1080": (1920, 1080),
    "1080x1920": (1080, 1920),
    "1080x1080": (1080, 1080),
}
#: палитра в тон презентациям
BG = (12, 18, 32)
CARD = (24, 34, 54)
ACCENT = (245, 166, 35)
TEXT = (244, 246, 250)
MUTED = (158, 171, 194)


def _font(size: int) -> Any:
    """Шрифт с кириллицей; при отсутствии — встроенный Pillow."""
    from PIL import ImageFont

    for name in ("arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> str:
    return "\n".join(
        textwrap.wrap(text, width=width, break_long_words=False) or [""]
    )


def _fit_image(image: Any, box: tuple[int, int]) -> Any:
    from PIL import Image

    image = image.convert("RGB")
    image.thumbnail(box, Image.Resampling.LANCZOS)
    return image


def _draw_scene(
    title: str,
    body: str,
    image_path: Path | None,
    size: tuple[int, int],
) -> Any:
    """Рисует один кадр: заголовок, текст и (если есть) картинку."""
    from PIL import Image, ImageDraw

    width, height = size
    # вертикальный градиент: от тёмно-синего к почти чёрному
    frame = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(frame)
    for y in range(height):
        shade = int(8 + 18 * (1 - y / max(1, height - 1)))
        draw.line((0, y, width, y), fill=(shade, shade + 6, shade + 20))
    draw.rectangle((0, 0, max(10, width // 90), height), fill=ACCENT)

    margin = int(width * 0.06)
    text_width = int(width * 0.50) if image_path else width - 2 * margin
    title_size = max(28, int(width * 0.045))
    body_size = max(18, int(width * 0.022))

    title_box = _wrap(title, max(18, text_width // max(1, title_size // 2)))
    draw.multiline_text(
        (margin, int(height * 0.18)),
        title_box,
        font=_font(title_size),
        fill=TEXT,
        spacing=int(title_size * 0.18),
    )
    body_top = int(height * 0.18) + (title_box.count("\n") + 2) * title_size
    if body:
        draw.multiline_text(
            (margin, body_top),
            _wrap(body, max(24, text_width // max(1, body_size // 2))),
            font=_font(body_size),
            fill=MUTED,
            spacing=int(body_size * 0.45),
        )

    if image_path and image_path.is_file():
        from PIL import Image

        with Image.open(image_path) as raw:
            picture = _fit_image(raw, (int(width * 0.38), int(height * 0.62)))
        left = width - margin - picture.width
        top = (height - picture.height) // 2
        frame.paste(picture, (left, top))
        draw.rectangle(
            (left - 4, top - 4, left + picture.width + 4, top + picture.height + 4),
            outline=ACCENT,
            width=3,
        )

    draw.text(
        (margin, height - int(height * 0.075)),
        "Немуся",
        font=_font(max(14, int(width * 0.012))),
        fill=MUTED,
    )
    return frame


class CreateVideoTool(Tool):
    """Собирает MP4 из сториборда: сцены с текстом и картинками."""

    name = "create_video"
    description = (
        "Собирает настоящий MP4 из содержательных сцен: заголовок, текст, "
        "картинка и длительность. Используй для короткого ролика, слайд-шоу, "
        "объяснения или нарезки, когда нужен предсказуемый результат без "
        "сложного motion-пайплайна. Минимум 3 сцены; пустые сцены запрещены. "
        "Для сложной анимации/титров используй hyperframes. Готовый файл "
        "отправь через send_telegram."
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
                "description": "3–12 содержательных сцен.",
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
                            "description": "Путь к картинке (необязательно).",
                        },
                        "duration": {
                            "type": "number",
                            "description": "Длительность сцены, сек (по умолчанию 4).",
                        },
                    },
                    "required": ["title"],
                },
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
        "required": ["title", "scenes"],
    }
    risk = RiskLevel.MEDIUM

    workspace: str = "data/workspace"

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
            if not scene_title and not text and not image:
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
                    "duration": duration,
                }
            )

        target = self._target_path(kwargs.get("path"), title)
        target.parent.mkdir(parents=True, exist_ok=True)
        music = str(kwargs.get("music") or "").strip()
        music_path = Path(music).expanduser() if music else None
        if music_path is not None and not music_path.is_file():
            return ToolResult.failure(f"Музыка не найдена: {music_path}")

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
            f"сцен: {len(normalized) + 1}). Отправь файл через send_telegram.",
            data={"path": str(target), "scenes": len(normalized) + 1},
        )

    def _render(
        self,
        ffmpeg: str,
        title: str,
        scenes: list[dict[str, Any]],
        size: tuple[int, int],
        target: Path,
        music: Path | None,
    ) -> None:
        """Рендерит кадры и склеивает их в MP4 через ffmpeg."""
        with tempfile.TemporaryDirectory(prefix="nemus_video_") as tmp:
            folder = Path(tmp)
            items: list[tuple[Path, float]] = []
            title_frame = _draw_scene(title, "", None, size)
            title_png = folder / "title.png"
            title_frame.save(title_png, "PNG")
            items.append((title_png, 2.5))
            for index, scene in enumerate(scenes, start=1):
                image = Path(scene["image"]).expanduser() if scene["image"] else None
                frame = _draw_scene(
                    scene["title"] or title, scene["text"], image, size
                )
                path = folder / f"scene_{index:02d}.png"
                frame.save(path, "PNG")
                items.append((path, scene["duration"]))

            clips: list[Path] = []
            for index, (image, duration) in enumerate(items):
                clip = folder / f"clip_{index:02d}.mp4"
                cmd = [
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-loop",
                    "1",
                    "-i",
                    str(image),
                    "-t",
                    f"{duration:.2f}",
                    "-vf",
                    (
                        f"scale={size[0]}:{size[1]},"
                        "format=yuv420p,fps=30"
                    ),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    str(clip),
                ]
                process = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if process.returncode != 0:
                    tail = (process.stderr or "").strip().splitlines()
                    raise RuntimeError(tail[-1] if tail else "ffmpeg вернул ошибку")
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
            )
            if process.returncode != 0 or not target.is_file():
                tail = (process.stderr or "").strip().splitlines()
                raise RuntimeError(
                    tail[-1] if tail else "не удалось добавить музыку"
                )

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
