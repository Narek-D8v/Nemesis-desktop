"""Генерация и редактирование изображений: цепочка провайдеров.

Раньше инструмента генерации в проекте не было вовсе, и модель «рисовала»
кошку кружками в Pillow. Теперь есть настоящий PNG:

- **Pollinations** — бесплатно и без ключа, только текст→картинка;
- **Gemini** — ``gemini-3.1-flash-image``, умеет фото-референс (нужна квота);
- **OpenRouter** — те же image-модели через один ключ;
- **OpenAI** — ``gpt-image-1``, умеет и генерацию, и редактирование по фото.

Для фото-референса бесплатного варианта нет: Pollinations не сохраняет лицо.
Если кредитов нигде нет, инструмент честно скажет об этом, а не нарисует
кружки. Без референса бесплатный Pollinations работает сразу.
"""

from __future__ import annotations

import base64
import mimetypes
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from nemus.config import PROJECT_ROOT
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.utils.logger import logger

#: размеры, которые поддерживают платные API
SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
#: качество генерации
QUALITIES = ("auto", "low", "medium", "high")
#: сколько референсов принимаем
MAX_REFERENCES = 3
#: максимальный размер одного референса, МБ
MAX_REFERENCE_MB = 20
#: таймаут одного запроса к провайдеру, сек
IMAGE_TIMEOUT = 300.0
#: кэш переводов промптов для бесплатного Pollinations
_TRANSLATIONS: dict[str, str] = {}

#: подписи провайдеров для сообщений
PROVIDER_NAMES = {
    "pollinations": "Pollinations (бесплатно)",
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
}


def _as_paths(value: Any) -> list[Path]:
    """Приводит ``reference`` к списку путей: строка, список или ничего."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, Path)):
        raw = [str(value)]
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value if str(item or "").strip()]
    else:
        raw = [str(value)]
    return [Path(item).expanduser() for item in raw if item.strip()]


def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/png"


def _data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{_mime(path)};base64,{encoded}"


def _save_bytes(data: bytes, target: Path) -> Path:
    """Сохраняет картинку, приводя формат к расширению файла."""
    if not data:
        raise RuntimeError("провайдер вернул пустой файл")
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            image.convert("RGB").save(target, "JPEG", quality=92)
        return target
    if suffix == ".png":
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            image.save(target, "PNG")
        return target
    target.write_bytes(data)
    return target


class GenerateImageTool(Tool):
    """Создаёт PNG по текстовому описанию или по загруженному фото."""

    name = "generate_image"
    description = (
        "Создаёт или изменяет изображение настоящей моделью генерации картинок "
        "и сохраняет файл. ЭТО ЕДИНСТВЕННЫЙ ПРАВИЛЬНЫЙ способ нарисовать "
        "картинку: НИКОГДА не рисуй кружками, линиями или фигурами через "
        "Pillow/Python — это считается невыполненной задачей. "
        "Если пользователь прислал фото и просит чиби-версию, стилизацию, "
        "портрет или «сделай такое же» — передай путь к фото в reference. "
        "Без кредитов работает бесплатный Pollinations: без референса — "
        "текст→картинка, с референсом — приближённая чиби/стилизация по "
        "описанию фото. Точное лицо дают Gemini/OpenRouter/OpenAI при наличии "
        "кредитов. Готовый файл отправь через send_telegram."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Подробное описание изображения: кто/что, поза, одежда, "
                    "фон, свет, стиль. Для стилизации по фото опиши желаемый "
                    "результат, а не только исходник."
                ),
            },
            "reference": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Пути к фото-референсам (1–3). С ними используется "
                    "редактирование: сохраняется лицо/композиция исходника."
                ),
            },
            "size": {
                "type": "string",
                "enum": list(SIZES),
                "description": "Размер: квадрат, горизонтальный или вертикальный.",
            },
            "quality": {
                "type": "string",
                "enum": list(QUALITIES),
                "description": "Качество: low быстрее и дешевле, high детальнее.",
            },
            "background": {
                "type": "string",
                "enum": ["auto", "transparent", "opaque"],
                "description": "Прозрачный фон — только если он действительно нужен.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Куда сохранить файл. По умолчанию — "
                    "data/workspace/images/image-<дата>.png."
                ),
            },
        },
        "required": ["prompt"],
    }
    risk = RiskLevel.MEDIUM

    #: инъекция из конфига в factory._inject_settings
    provider: str = "auto"
    openai_api_key: str = ""
    openai_api_base: str = ""
    openai_model: str = "gpt-image-1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-image"
    openrouter_api_key: str = ""
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "google/gemini-3.1-flash-image"
    pollinations_model: str = "flux"
    workspace: str = "data/workspace"

    def execute(self, **kwargs: Any) -> ToolResult:
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt:
            return ToolResult.failure("Не указано описание изображения (prompt).")
        size = str(kwargs.get("size") or "auto").strip()
        if size not in SIZES:
            size = "auto"
        quality = str(kwargs.get("quality") or "high").strip()
        if quality not in QUALITIES:
            quality = "high"
        background = str(kwargs.get("background") or "auto").strip()
        if background not in ("auto", "transparent", "opaque"):
            background = "auto"

        refs = _as_paths(kwargs.get("reference"))
        if len(refs) > MAX_REFERENCES:
            return ToolResult.failure(
                f"Слишком много референсов: {len(refs)}. Максимум "
                f"{MAX_REFERENCES}."
            )
        for ref in refs:
            if not ref.is_file():
                return ToolResult.failure(f"Референс не найден: {ref}")
            size_mb = ref.stat().st_size / (1024 * 1024)
            if size_mb > MAX_REFERENCE_MB:
                return ToolResult.failure(
                    f"Референс {ref.name} весит {size_mb:.1f} МБ — максимум "
                    f"{MAX_REFERENCE_MB} МБ."
                )

        target = self._target_path(kwargs.get("path"))
        errors: list[str] = []
        for name in self._provider_order(refs):
            if name == "openai" and not self.openai_api_key:
                continue
            if name == "gemini" and not self.gemini_api_key:
                continue
            if name == "openrouter" and not self.openrouter_api_key:
                continue
            try:
                data = getattr(self, f"_via_{name}")(
                    prompt, refs, size, quality, background
                )
                _save_bytes(data, target)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{PROVIDER_NAMES.get(name, name)}: {exc}")
                logger.warning(f"generate_image/{name}: {exc}")
                continue
            approximate = bool(refs and name == "pollinations")
            kind = "по фото (приближённо, по описанию)" if approximate else (
                "по референсу" if refs else "с нуля"
            )
            note = (
                " Бесплатный провайдер сохранил сходство приблизительно; для "
                "точного лица нужны кредиты Gemini/OpenRouter/OpenAI."
                if approximate
                else ""
            )
            return ToolResult.success(
                f"Изображение создано {kind} и сохранено: {target} "
                f"({target.stat().st_size / 1024:.0f} КБ), провайдер: "
                f"{PROVIDER_NAMES.get(name, name)}.{note} "
                "Отправь его пользователю через send_telegram.",
                data={"path": str(target), "provider": name},
            )

        detail = " | ".join(errors[-3:]) or "все провайдеры недоступны"
        return ToolResult.failure(
            "Не удалось создать изображение. Проверьте ключи и баланс: "
            f"{detail}"
        )

    def _provider_order(self, refs: list[Path]) -> list[str]:
        explicit = (self.provider or "auto").strip().lower()
        if explicit != "auto":
            if explicit not in PROVIDER_NAMES:
                return []
            if refs and explicit == "pollinations":
                return []
            return [explicit]
        if refs:
            # платные умеют точный image-to-image; Pollinations — последний
            # резерв по текстовому описанию фото (без сохранения лица)
            return ["gemini", "openrouter", "openai", "pollinations"]
        # без референса сначала бесплатный, чтобы картинка работала без кредитов
        return ["pollinations", "gemini", "openrouter", "openai"]

    # ------------------------------------------------------------ providers
    def _describe_reference(self, path: Path) -> str:
        """Краткое описание фото для бесплатной генерации по описанию.

        Это не заменяет image-to-image: лицо не сохраняется, но персонаж,
        одежда, поза и фон остаются узнаваемыми, а не превращаются в кружки.
        """
        if not self.gemini_api_key:
            return ""
        try:
            response = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash:generateContent",
                params={"key": self.gemini_api_key},
                json={
                    "contents": [
                        {
                            "parts": [
                                {
                                    "text": (
                                        "Describe this person/character for an "
                                        "image generator in one short English "
                                        "sentence: age range, hair, face, "
                                        "clothing, pose, background. Do not "
                                        "identify the person by name."
                                    )
                                },
                                {
                                    "inline_data": {
                                        "mime_type": _mime(path),
                                        "data": base64.b64encode(
                                            path.read_bytes()
                                        ).decode(),
                                    }
                                },
                            ]
                        }
                    ],
                    "generationConfig": {"temperature": 0},
                },
                timeout=60,
            )
            if response.status_code >= 400:
                return ""
            data = response.json()
            parts = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            text = " ".join(
                str(part.get("text") or "")
                for part in parts
                if part.get("text")
            ).strip()
            return text[:600]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"generate_image: описание референса не удалось: {exc}")
            return ""

    def _english_prompt(self, prompt: str) -> str:
        """Переводит русский промпт на английский для Pollinations.

        Flux на бесплатном тарифе плохо понимает кириллицу и может выдать
        случайное животное вместо описанной сцены. Gemini-перевод (если ключ
        есть) заметно повышает попадание; без ключа отправляем как есть.
        """
        if not any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in prompt):
            return prompt
        if not self.gemini_api_key:
            return prompt
        cached = _TRANSLATIONS.get(prompt)
        if cached:
            return cached
        try:
            response = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash:generateContent",
                params={"key": self.gemini_api_key},
                json={
                    "contents": [
                        {
                            "parts": [
                                {
                                    "text": (
                                        "Translate this image prompt to English. "
                                        "Return only the translation, no quotes "
                                        "or explanations:\n" + prompt
                                    )
                                }
                            ]
                        }
                    ],
                    "generationConfig": {"temperature": 0},
                },
                timeout=30,
            )
            if response.status_code >= 400:
                return prompt
            data = response.json()
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            translated = str(text).strip().strip('"').strip()
            if translated:
                _TRANSLATIONS[prompt] = translated
                return translated
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"generate_image: перевод промпта не удался: {exc}")
        return prompt

    def _via_pollinations(
        self, prompt: str, refs: list[Path], size: str, quality: str, background: str
    ) -> bytes:
        if refs:
            described = self._describe_reference(refs[0])
            if described:
                prompt = (
                    f"{prompt}. Character from the photo: {described}. "
                    "Keep the hairstyle, clothing and overall vibe, but draw "
                    "it in a cute chibi / stylized illustration."
                )
        prompt = self._english_prompt(prompt)
        width, height = {
            "auto": (768, 768),
            "1024x1024": (768, 768),
            "1536x1024": (768, 512),
            "1024x1536": (512, 768),
        }.get(size, (768, 768))
        url = f"https://image.pollinations.ai/prompt/{quote(prompt)}"
        response = httpx.get(
            url,
            params={
                "width": width,
                "height": height,
                "model": self.pollinations_model or "flux",
                "nologo": "true",
                "seed": random.randint(0, 2**31 - 1),
            },
            timeout=IMAGE_TIMEOUT,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code}: {response.text.strip()[:300]}"
            )
        if not (response.headers.get("content-type") or "").startswith("image/"):
            raise RuntimeError(
                "вместо картинки пришёл текст: "
                f"{response.text.strip()[:300]}"
            )
        return response.content

    def _via_gemini(
        self, prompt: str, refs: list[Path], size: str, quality: str, background: str
    ) -> bytes:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for ref in refs:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": _mime(ref),
                        "data": base64.b64encode(ref.read_bytes()).decode(),
                    }
                }
            )
        aspect = {
            "1536x1024": "3:2",
            "1024x1536": "2:3",
        }.get(size, "1:1")
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": aspect},
            },
        }
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model or 'gemini-3.1-flash-image'}:generateContent",
            params={"key": self.gemini_api_key},
            json=payload,
            timeout=IMAGE_TIMEOUT,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code}: {response.text.strip()[:400]}"
            )
        text_parts: list[str] = []
        for candidate in (response.json() or {}).get("candidates", []) or []:
            for part in candidate.get("content", {}).get("parts", []) or []:
                if part.get("text"):
                    text_parts.append(str(part["text"]))
                inline = part.get("inlineData") or part.get("inline_data") or {}
                if inline.get("data"):
                    return base64.b64decode(inline["data"])
        reason = " ".join(text_parts)[:300] or "нет inline-картинки в ответе"
        raise RuntimeError(f"Gemini не вернул изображение: {reason}")

    def _via_openrouter(
        self, prompt: str, refs: list[Path], size: str, quality: str, background: str
    ) -> bytes:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for ref in refs:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(ref)}}
            )
        response = httpx.post(
            f"{self.openrouter_api_base.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "HTTP-Referer": "https://github.com/nemus",
                "X-Title": "Nemus",
            },
            json={
                "model": self.openrouter_model,
                "messages": [{"role": "user", "content": content}],
                "modalities": ["image", "text"],
            },
            timeout=IMAGE_TIMEOUT,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code}: {response.text.strip()[:400]}"
            )
        choices = (response.json() or {}).get("choices", []) or []
        if not choices:
            raise RuntimeError("OpenRouter вернул пустой ответ")
        message = choices[0].get("message") or {}
        for image in message.get("images") or []:
            url = ""
            if isinstance(image, dict):
                url = str(
                    (image.get("image_url") or {}).get("url")
                    or image.get("url")
                    or ""
                )
                if image.get("b64_json"):
                    return base64.b64decode(image["b64_json"])
            elif isinstance(image, str):
                url = image
            if url.startswith("data:"):
                return base64.b64decode(url.split(",", 1)[1])
            if url.startswith("http"):
                download = httpx.get(url, timeout=120, follow_redirects=True)
                download.raise_for_status()
                return download.content
        raise RuntimeError("OpenRouter не вернул изображение")

    def _via_openai(
        self, prompt: str, refs: list[Path], size: str, quality: str, background: str
    ) -> bytes:
        try:
            from openai import OpenAI
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"пакет openai не установлен: {exc}") from exc

        client = OpenAI(
            api_key=self.openai_api_key,
            base_url=self.openai_api_base.strip() or None,
            timeout=IMAGE_TIMEOUT,
        )
        options: dict[str, Any] = {
            "model": self.openai_model or "gpt-image-1",
            "prompt": prompt,
            "quality": quality if quality != "auto" else "high",
        }
        if size != "auto":
            options["size"] = size
        if background != "auto":
            options["background"] = background
        handles: list[Any] = []
        try:
            if refs:
                handles = [ref.open("rb") for ref in refs]
                options["input_fidelity"] = "high"
                response = client.images.edit(image=handles, **options)
            else:
                response = client.images.generate(**options)
        finally:
            for handle in handles:
                handle.close()

        data = getattr(response, "data", None) or []
        if not data:
            raise RuntimeError("OpenAI вернул пустой ответ")
        encoded = getattr(data[0], "b64_json", None)
        if encoded:
            return base64.b64decode(encoded)
        url = getattr(data[0], "url", None)
        if url:
            download = httpx.get(url, timeout=120, follow_redirects=True)
            download.raise_for_status()
            return download.content
        raise RuntimeError("OpenAI вернул изображение без данных и без ссылки")

    def _target_path(self, raw: Any) -> Path:
        """Путь сохранения: явный, иначе images/image-<время>.png."""
        text = str(raw or "").strip()
        if text:
            target = Path(text).expanduser()
            if not target.is_absolute():
                # явный путь — от корня проекта, как у остальных файловых
                # инструментов; рабочая папка используется только по умолчанию
                target = PROJECT_ROOT / target
            if not target.suffix:
                target = target.with_suffix(".png")
            return target
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Path(self.workspace) / "images" / f"image-{stamp}.png"


def image_tools() -> list[Tool]:
    """Инструменты изображений для реестра."""
    return [GenerateImageTool()]


__all__ = ["GenerateImageTool", "image_tools", "SIZES", "QUALITIES"]
