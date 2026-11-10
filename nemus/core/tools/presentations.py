"""Сборка презентаций PPTX из готового плана.

Модель часто отдавала «презентацию» из трёх пустых слайдов или вовсе один
заголовок. Этот инструмент принимает структуру (заголовки, тезисы, цифры,
графики, картинки) и собирает оформленный файл 16:9: тёмная тема, карточки,
крупные метрики, номера слайдов. Модели остаётся придумать содержание, а не
бороться с геометрией python-pptx.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from nemus.config import PROJECT_ROOT
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: палитра тёмной темы: фон, карточки, акцент, текст, вторичный текст
DARK = {
    "bg": (12, 18, 32),
    "card": (24, 34, 54),
    "accent": (245, 166, 35),
    "text": (244, 246, 250),
    "muted": (158, 171, 194),
}


def _rgb(triple: tuple[int, int, int]) -> Any:
    from pptx.dml.color import RGBColor

    return RGBColor(*triple)


def _add_text(
    slide: Any,
    left: float,
    top: float,
    width: float,
    height: float,
    text: str,
    *,
    size: int = 18,
    bold: bool = False,
    color: tuple[int, int, int] = DARK["text"],
    align: Any = None,
    anchor: Any = None,
    spacing: float = 1.0,
) -> Any:
    """Текстовое поле без автоподбора: размеры и перенос задаём сами."""
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    if anchor is not None:
        frame.vertical_anchor = anchor
    paragraph = frame.paragraphs[0]
    paragraph.text = text
    paragraph.line_spacing = spacing
    if align is not None:
        paragraph.alignment = align
    for run in paragraph.runs:
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = "Aptos"
        run.font.color.rgb = _rgb(color)
    return box


def _add_card(
    slide: Any,
    left: float,
    top: float,
    width: float,
    height: float,
    fill: tuple[int, int, int] = DARK["card"],
) -> Any:
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(left),
        Inches(top),
        Inches(width),
        Inches(height),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill)
    shape.line.fill.background()
    shape.adjustments[0] = 0.06
    return shape


def _paint_background(slide: Any) -> None:
    from pptx.dml.color import RGBColor

    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor(*DARK["bg"])


def _title_slide(prs: Any, topic: str, subtitle: str) -> None:
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _paint_background(slide)
    # акцентная полоса слева и тонкая линия под заголовком
    bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(0.22), prs.slide_height
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(DARK["accent"])
    bar.line.fill.background()
    _add_text(
        slide, 0.85, 1.55, 11.4, 1.0,
        "НЕМУСЯ · ПРЕЗЕНТАЦИЯ", size=13, bold=True, color=DARK["accent"],
    )
    _add_text(
        slide, 0.85, 2.15, 11.5, 2.3, topic,
        size=46, bold=True, spacing=0.95,
    )
    if subtitle:
        _add_text(
            slide, 0.85, 4.65, 10.6, 1.2, subtitle,
            size=20, color=DARK["muted"], spacing=1.15,
        )
    _add_text(
        slide, 0.85, 6.65, 8.0, 0.4,
        time.strftime("%d.%m.%Y"), size=12, color=DARK["muted"],
    )


def _metric(
    slide: Any, value: str, label: str, left: float, top: float, width: float
) -> None:
    _add_card(slide, left, top, width, 1.9)
    _add_text(
        slide, left + 0.25, top + 0.18, width - 0.5, 0.95, value,
        size=40, bold=True, color=DARK["accent"],
    )
    _add_text(
        slide, left + 0.25, top + 1.12, width - 0.5, 0.55, label,
        size=12, color=DARK["muted"], spacing=1.0,
    )


def _points(
    slide: Any, points: list[str], left: float, top: float, width: float
) -> None:
    """Карточки-тезисы: каждый пункт в своей плашке, а не голый список."""
    if not points:
        return
    gap = 0.14
    height = min(1.02, (6.3 - top - gap * (len(points) - 1)) / len(points))
    size = 17 if len(points) <= 3 else 15
    for index, point in enumerate(points[:6], start=1):
        y = top + (index - 1) * (height + gap)
        _add_card(slide, left, y, width, height)
        _add_text(
            slide, left + 0.22, y + 0.1, 0.55, height - 0.2,
            f"{index:02d}", size=14, bold=True, color=DARK["accent"],
        )
        _add_text(
            slide, left + 0.82, y + 0.09, width - 1.0, height - 0.18,
            point, size=size, spacing=1.05,
        )


def _chart(slide: Any, spec: dict[str, Any], left: float, top: float,
           width: float, height: float) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches, Pt

    labels = [str(item) for item in (spec.get("labels") or [])]
    raw_values = list(spec.get("values") or [])
    if not labels or len(labels) != len(raw_values):
        raise ValueError("chart: labels и values должны быть одной длины")
    values: list[float] = []
    for value in raw_values:
        try:
            values.append(float(str(value).replace(",", ".").replace("%", "")))
        except ValueError:
            raise ValueError(f"chart: значение «{value}» не число") from None
    data = CategoryChartData()
    data.categories = labels
    data.add_series(str(spec.get("series") or "Значение"), values)
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(left), Inches(top), Inches(width), Inches(height),
        data,
    ).chart
    chart.has_legend = False
    chart.has_title = bool(spec.get("title"))
    if spec.get("title"):
        chart.chart_title.text_frame.text = str(spec["title"])
        for run in chart.chart_title.text_frame.paragraphs[0].runs:
            run.font.size = Pt(14)
            run.font.bold = True
            run.font.color.rgb = _rgb(DARK["text"])
    chart.font.size = Pt(11)
    chart.font.color.rgb = _rgb(DARK["text"])
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.font.size = Pt(10)
    plot.data_labels.font.color.rgb = _rgb(DARK["text"])
    series = plot.series[0]
    series.format.fill.solid()
    series.format.fill.fore_color.rgb = _rgb(DARK["accent"])
    try:
        chart.value_axis.format.line.color.rgb = _rgb(DARK["muted"])
        chart.category_axis.format.line.color.rgb = _rgb(DARK["muted"])
    except Exception:  # noqa: BLE001
        pass


def _image(slide: Any, path: Path, left: float, top: float,
           width: float, height: float) -> None:
    """Вписывает картинку в область с сохранением пропорций."""
    from PIL import Image
    from pptx.util import Inches

    with Image.open(path) as img:
        w, h = img.size
    scale = min(width / w, height / h)
    draw_w, draw_h = w * scale, h * scale
    slide.shapes.add_picture(
        str(path),
        Inches(left + (width - draw_w) / 2),
        Inches(top + (height - draw_h) / 2),
        width=Inches(draw_w),
        height=Inches(draw_h),
    )


def _content_slide(prs: Any, index: int, spec: dict[str, Any]) -> None:
    from pptx.enum.text import PP_ALIGN

    title = str(spec.get("title") or "").strip()
    points = [str(item).strip() for item in (spec.get("points") or []) if str(item).strip()]
    stat = str(spec.get("stat") or "").strip()
    stat_label = str(spec.get("stat_label") or "").strip()
    chart_spec = spec.get("chart") if isinstance(spec.get("chart"), dict) else None
    image_path = str(spec.get("image") or "").strip()
    if not title:
        raise ValueError(f"слайд {index}: нет заголовка (title)")
    has_visual = bool(chart_spec or image_path or stat)
    if not points and not has_visual:
        raise ValueError(f"слайд {index}: пустой — нужны points, stat или chart")

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _paint_background(slide)
    _add_text(
        slide, 0.7, 0.42, 2.0, 0.35,
        f"{index:02d}", size=12, bold=True, color=DARK["accent"],
    )
    _add_text(
        slide, 0.7, 0.78, 11.9, 1.05, title,
        size=28, bold=True, spacing=0.98,
    )
    _add_text(
        slide, 0.7, 6.95, 4.0, 0.3, "Немуся", size=10, color=DARK["muted"]
    )
    _add_text(
        slide, 11.6, 6.95, 1.0, 0.3, str(index), size=10,
        color=DARK["muted"], align=PP_ALIGN.RIGHT,
    )

    body_top = 2.05
    if has_visual and points:
        _points(slide, points, 0.7, body_top, 5.85)
        visual_left, visual_width = 6.85, 5.75
    elif has_visual:
        visual_left, visual_width = 0.7, 11.9
    else:
        _points(slide, points, 0.7, body_top, 11.9)
        return

    if stat:
        _metric(slide, stat, stat_label or "ключевой показатель",
                visual_left, body_top, visual_width)
        if points and chart_spec:
            _chart(slide, chart_spec, visual_left, body_top + 2.15,
                   visual_width, 2.8)
    elif chart_spec:
        _chart(slide, chart_spec, visual_left, body_top, visual_width, 4.6)
    elif image_path:
        image = Path(image_path).expanduser()
        if not image.is_file():
            raise ValueError(f"слайд {index}: картинка не найдена: {image}")
        _image(slide, image, visual_left, body_top, visual_width, 4.6)


class CreatePresentationTool(Tool):
    """Создаёт оформленный PPTX из плана слайдов."""

    name = "create_presentation"
    description = (
        "Собирает настоящий PPTX (16:9, тёмная тема, карточки, крупные цифры, "
        "графики) из готового плана. ИСПОЛЬЗУЙ ЭТОТ ИНСТРУМЕНТ вместо ручного "
        "кода python-pptx, когда просят презентацию, слайды или доклад. "
        "Сначала собери содержание: 8–12 слайдов, у каждого —Action Title "
        "(заголовок-вывод), 2–4 тезиса, хотя бы на нескольких — цифры или "
        "график. Не создавай пустые слайды-заглушки. Затем отправь файл через "
        "send_telegram."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Тема презентации — заголовок титульного слайда.",
            },
            "subtitle": {
                "type": "string",
                "description": "Подзаголовок: аудитория, цель или дата.",
            },
            "slides": {
                "type": "array",
                "description": "8–12 содержательных слайдов.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Action Title — законченный вывод, не тема.",
                        },
                        "points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "2–4 конкретных тезиса с фактами.",
                        },
                        "stat": {
                            "type": "string",
                            "description": "Крупная цифра, например «17%».",
                        },
                        "stat_label": {
                            "type": "string",
                            "description": "Что означает цифра.",
                        },
                        "chart": {
                            "type": "object",
                            "properties": {
                                "labels": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "values": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                                "title": {"type": "string"},
                                "series": {"type": "string"},
                            },
                            "required": ["labels", "values"],
                        },
                        "image": {
                            "type": "string",
                            "description": "Путь к картинке для слайда.",
                        },
                    },
                    "required": ["title"],
                },
            },
            "path": {
                "type": "string",
                "description": "Куда сохранить .pptx (по умолчанию — рабочая папка).",
            },
        },
        "required": ["topic", "slides"],
    }
    risk = RiskLevel.MEDIUM

    workspace: str = "data/workspace"

    def execute(self, **kwargs: Any) -> ToolResult:
        topic = str(kwargs.get("topic") or "").strip()
        slides = kwargs.get("slides") or []
        if not topic:
            return ToolResult.failure("Не указана тема презентации (topic).")
        if not isinstance(slides, list) or not slides:
            return ToolResult.failure(
                "Нет слайдов (slides). Собери 8–12 слайдов с заголовками и тезисами."
            )
        if len(slides) < 3:
            return ToolResult.failure(
                f"Слайдов {len(slides)} — это пустая заготовка. Нужно 8–12 "
                "содержательных слайдов: история, механика, цифры, применение, "
                "риски, будущее."
            )
        try:
            from pptx import Presentation
            from pptx.util import Inches
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                f"python-pptx не установлен ({exc}). Установи пакет "
                "python-pptx и повтори."
            )

        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        try:
            _title_slide(prs, topic, str(kwargs.get("subtitle") or "").strip())
            for index, spec in enumerate(slides, start=1):
                if not isinstance(spec, dict):
                    raise ValueError(f"слайд {index}: ожидался объект")
                _content_slide(prs, index, spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"create_presentation: {exc}")
            return ToolResult.failure(f"Не удалось собрать презентацию: {exc}")

        target = self._target_path(kwargs.get("path"), topic)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            prs.save(str(target))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось сохранить PPTX: {exc}")

        return ToolResult.success(
            f"Презентация «{topic}» готова: {target} "
            f"(слайдов: {len(slides) + 1}, {target.stat().st_size / 1024:.0f} КБ). "
            "Отправь файл пользователю через send_telegram.",
            data={"path": str(target), "slides": len(slides) + 1},
        )

    def _target_path(self, raw: Any, topic: str) -> Path:
        text = str(raw or "").strip()
        if text:
            target = Path(text).expanduser()
            if not target.is_absolute():
                # явный путь — от корня проекта, как у файловых инструментов
                target = PROJECT_ROOT / target
            if target.suffix.lower() != ".pptx":
                target = target.with_suffix(".pptx")
            return target
        from nemus.core.skills import slugify

        try:
            slug = slugify(topic)[:40].strip("-") or "presentation"
        except ValueError:
            slug = "presentation"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Path(self.workspace) / "presentations" / f"{slug}-{stamp}.pptx"


def presentation_tools() -> list[Tool]:
    """Инструменты презентаций для реестра."""
    return [CreatePresentationTool()]


__all__ = ["CreatePresentationTool", "presentation_tools"]
