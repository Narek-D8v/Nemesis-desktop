"""Веб-инструменты: поиск и получение содержимого URL."""

from __future__ import annotations

import ipaddress
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from nemus.core.tools.base import RiskLevel, Tool, ToolResult

_MAX_REDIRECTS = 5
#: таймаут HTTP-запроса при чтении страницы (секунды)
FETCH_TIMEOUT = 20
#: максимальный размер скачиваемой картинки, МБ
MAX_IMAGE_MB = 20
#: расширения по Content-Type — если URL не подсказал формат
_IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}


class UnsafeUrl(ValueError):
    """URL ведёт на локальный/внутренний адрес (SSRF)."""


def _is_blocked_ip(ip: str) -> bool:
    """Является ли IP внутренним/служебным (его нельзя запрашивать)."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or getattr(addr, "is_site_local", False)
    )


def _validate_url(url: str) -> str:
    """Проверяет схему и что хост не указывает на внутренний адрес."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl(f"Разрешены только http/https, получено: «{parts.scheme or '—'}».")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("В URL не указан хост.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeUrl(f"Не удалось разрешить хост «{host}»: {exc}") from exc
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            raise UnsafeUrl(
                f"Хост «{host}» указывает на внутренний адрес ({ip}) — запрос отклонён."
            )
    return url


class WebSearchTool(Tool):
    name = "web_search"
    description = "Выполняет веб-поиск с помощью DuckDuckGo (ddgs). Возвращает результаты с заголовками, ссылками и сниппетами."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос."},
            "max_results": {
                "type": "integer",
                "description": "Максимум результатов (1–10), по умолчанию 5.",
            },
        },
        "required": ["query"],
    }
    risk = RiskLevel.MEDIUM

    def execute(self, query: str = "", max_results: int = 5, **_: object) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Пустой запрос.")
        max_results = max(1, min(10, max_results))
        try:
            from ddgs import DDGS

            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):  # type: ignore[attr-defined]
                    results.append(
                        f"- {r.get('title','')}\n  {r.get('href','')}\n  {r.get('body','')}"
                    )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка поиска: {exc}")
        if not results:
            return ToolResult.success("Результатов не найдено.")
        return ToolResult.success("Результаты поиска:\n\n" + "\n\n".join(results))


class ImageSearchTool(Tool):
    """Ищет реальные фотографии и возвращает прямые ссылки на файлы."""

    name = "search_images"
    description = (
        "Ищет фотографии в интернете (DuckDuckGo Images) и возвращает прямые "
        "URL на файлы, размеры и страницу-источник. Используй для сайта, "
        "презентации или обложки, когда нужны реальные фото: кофейня, интерьер, "
        "еда, товар, люди, природа. Найденную ссылку скачай через "
        "download_image и положи файл в проект — не вставляй чужие URL в HTML."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Что искать, например 'specialty coffee shop interior'.",
            },
            "max_results": {
                "type": "integer",
                "description": "Максимум результатов (1–12), по умолчанию 6.",
            },
        },
        "required": ["query"],
    }
    risk = RiskLevel.SAFE

    def execute(self, query: str = "", max_results: int = 6, **_: object) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("Пустой поисковый запрос.")
        max_results = max(1, min(12, int(max_results or 6)))
        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                rows = ddgs.images(
                    query, max_results=max_results, safesearch="moderate"
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка поиска картинок: {exc}")
        lines: list[str] = []
        for row in rows:
            image = str(row.get("image") or "").strip()
            if not image:
                continue
            title = str(row.get("title") or "Без названия").strip()
            source = str(row.get("url") or "").strip()
            size = ""
            if row.get("width") and row.get("height"):
                size = f"{row.get('width')}×{row.get('height')}"
            line = f"- {title}\n  image: {image}"
            if size:
                line += f"\n  размер: {size}"
            if source:
                line += f"\n  источник: {source}"
            lines.append(line)
        if not lines:
            return ToolResult.success("Картинки не найдены.")
        return ToolResult.success("Найденные картинки:\n\n" + "\n\n".join(lines))


class DownloadImageTool(Tool):
    """Скачивает картинку по прямой ссылке в рабочую папку."""

    name = "download_image"
    description = (
        "Скачивает изображение по прямой ссылке (из search_images или "
        "известного URL) в файл проекта. Проверяет, что это действительно "
        "картинка, ограничивает размер и возвращает путь. Для сайта скачивай "
        "фото в public/images и ссылайся на локальный файл, а не на чужой URL."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Прямая ссылка на файл картинки."},
            "path": {
                "type": "string",
                "description": (
                    "Куда сохранить. Относительный путь — от рабочей папки "
                    "(data/workspace). По умолчанию — "
                    "data/workspace/images/download-<время>.<ext>."
                ),
            },
        },
        "required": ["url"],
    }
    risk = RiskLevel.MEDIUM
    #: подставляется фабрикой из config.tools.workspace
    workspace: str = "data/workspace"
    guard: Any = None

    def _target(self, path: str, suffix: str) -> Path:
        if path.strip():
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = Path(self.workspace) / target
        else:
            target = Path(self.workspace) / "images" / f"download-{int(time.time())}{suffix}"
        if not target.suffix:
            target = target.with_suffix(suffix)
        if self.guard is not None:
            return self.guard.resolve_write(target, what="Картинка")
        return target

    @staticmethod
    def _fetch(url: str) -> tuple[bytes, str]:
        import httpx

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        current = _validate_url(url)
        limit = MAX_IMAGE_MB * 1024 * 1024
        with httpx.Client(follow_redirects=False, timeout=FETCH_TIMEOUT) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                with client.stream("GET", current, headers=headers) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        current = _validate_url(
                            urljoin(current, resp.headers["location"])
                        )
                        continue
                    resp.raise_for_status()
                    content_type = (
                        resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if content_type and not content_type.startswith("image/"):
                        raise ValueError(
                            f"по ссылке не картинка, а {content_type}"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise ValueError(
                                f"файл больше {MAX_IMAGE_MB} МБ"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), content_type
        raise UnsafeUrl(f"Слишком много редиректов (> {_MAX_REDIRECTS}).")

    def execute(
        self, url: str = "", path: str = "", **_: object
    ) -> ToolResult:
        if not url.strip():
            return ToolResult.failure("Пустой URL картинки.")
        try:
            data, content_type = self._fetch(url.strip())
        except UnsafeUrl as exc:
            return ToolResult.failure(f"Запрос отклонён: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось скачать картинку: {exc}")
        if not data:
            return ToolResult.failure("По ссылке пришёл пустой файл.")
        suffix = _IMAGE_SUFFIXES.get(content_type) or Path(
            urlsplit(url).path
        ).suffix.lower()
        if suffix == ".jpeg":
            suffix = ".jpg"
        allowed = {".jpg", ".png", ".webp", ".gif", ".svg", ".avif", ".bmp", ".tif", ".tiff"}
        if suffix not in allowed:
            suffix = ".jpg" if content_type == "image/jpeg" else ".png"
        try:
            target = self._target(path, suffix)
            target_suffix = target.suffix.lower()
            if target_suffix == ".jpeg":
                target_suffix = ".jpg"
            if suffix == ".svg" and target_suffix != ".svg":
                target = target.with_suffix(".svg")
                target_suffix = ".svg"
            if (
                target_suffix not in {".jpg", ".png"}
                and target_suffix != suffix
            ):
                # webp/gif/avif не перекодируем — просто даём файлу верное
                # расширение, чтобы сервер не отдавал неверный MIME
                target = target.with_suffix(suffix)
                target_suffix = suffix
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = "image/jpeg" if target_suffix == ".jpg" else "image/png"
            if target_suffix in {".jpg", ".png"} and content_type != expected:
                from io import BytesIO

                from PIL import Image

                with Image.open(BytesIO(data)) as image:
                    if target_suffix == ".jpg":
                        image.convert("RGB").save(target, "JPEG", quality=90)
                    else:
                        image.save(target, "PNG")
            else:
                target.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось сохранить картинку: {exc}")
        width = height = 0
        if suffix != ".svg":
            try:
                from io import BytesIO

                from PIL import Image

                with Image.open(BytesIO(data)) as image:
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    width, height = image.size
            except Exception as exc:  # noqa: BLE001
                target.unlink(missing_ok=True)
                return ToolResult.failure(
                    f"Файл не похож на изображение: {exc}"
                )
        elif not data.lstrip().startswith(b"<svg"):
            target.unlink(missing_ok=True)
            return ToolResult.failure("Файл не похож на SVG.")
        size_kb = target.stat().st_size / 1024
        size_text = f", {width}×{height}" if width and height else ""
        return ToolResult.success(
            f"Картинка скачана: {target} ({size_kb:.0f} КБ{size_text}). "
            "Используй локальный путь в проекте.",
            data={"path": str(target), "width": width, "height": height},
        )


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Читает страницу по URL: заголовок, описание, основной текст и при "
        "необходимости ссылки. Локальные и внутренние адреса запрещены."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Полный URL, включая https://."},
            "max_chars": {
                "type": "integer",
                "description": "Максимум символов ответа (по умолчанию 12_000).",
            },
            "include_links": {
                "type": "boolean",
                "description": "Добавить список ссылок со страницы.",
            },
        },
        "required": ["url"],
    }
    risk = RiskLevel.SAFE

    @staticmethod
    def _get_following_redirects(client: object, url: str, headers: dict[str, str]) -> object:
        """GET с ручной обработкой редиректов — каждый хост проверяется на SSRF."""
        current = _validate_url(url)
        for _ in range(_MAX_REDIRECTS + 1):
            resp = client.get(current, headers=headers)  # type: ignore[attr-defined]
            if resp.is_redirect and resp.headers.get("location"):
                current = _validate_url(urljoin(current, resp.headers["location"]))
                continue
            resp.raise_for_status()
            return resp
        raise UnsafeUrl(f"Слишком много редиректов (> {_MAX_REDIRECTS}).")

    def execute(
        self,
        url: str = "",
        max_chars: int = 12_000,
        include_links: bool = False,
        **_: object,
    ) -> ToolResult:
        if not url.strip():
            return ToolResult.failure("Пустой URL.")
        max_chars = max(1000, min(50000, max_chars))
        try:
            import httpx
            from bs4 import BeautifulSoup
            from markdownify import markdownify as md

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            with httpx.Client(follow_redirects=False, timeout=FETCH_TIMEOUT) as client:
                resp = self._get_following_redirects(client, url.strip(), headers)
            soup = BeautifulSoup(resp.text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            description = ""
            meta = soup.find("meta", attrs={"name": "description"})
            if meta is not None:
                description = str(meta.get("content") or "").strip()
            links: list[str] = []
            if include_links:
                seen: set[str] = set()
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "").strip()
                    if not href or href.startswith(("#", "javascript:", "mailto:")):
                        continue
                    absolute = urljoin(str(resp.url), href)
                    label = anchor.get_text(" ", strip=True)[:80]
                    if absolute in seen:
                        continue
                    seen.add(absolute)
                    links.append(f"- [{label or absolute}]({absolute})")
                    if len(links) >= 30:
                        break
            for tag in soup(
                ["script", "style", "nav", "footer", "noscript", "header", "aside", "form"]
            ):
                tag.decompose()
            main = (
                soup.find("article")
                or soup.find("main")
                or soup.find(attrs={"role": "main"})
                or soup.body
                or soup
            )
            headings = [
                f"{tag.name.upper()}: {tag.get_text(' ', strip=True)[:160]}"
                for tag in main.find_all(["h1", "h2", "h3"])
                if tag.get_text(" ", strip=True)
            ][:15]
            text = main.get_text("\n", strip=True)
            if len(text) < 2000:
                try:
                    text = md(str(main))
                except Exception:  # noqa: BLE001
                    pass
            if not text.strip():
                return ToolResult.failure(
                    "На странице не найден текст: возможно, она собирается "
                    "JavaScript или требует входа."
                )
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n...[обрезано]"
            parts = [f"Источник: {resp.url}"]
            if title:
                parts.append(f"Заголовок: {title}")
            if description:
                parts.append(f"Описание: {description}")
            if headings:
                parts.append("Структура:\n" + "\n".join(headings))
            parts.append("Текст:\n" + text)
            if include_links:
                parts.append(
                    "Ссылки:\n" + ("\n".join(links) if links else "(не найдены)")
                )
            return ToolResult.success("\n\n".join(parts))
        except UnsafeUrl as exc:
            return ToolResult.failure(f"Запрос отклонён: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка загрузки: {exc}")


def web_tools() -> list[Tool]:
    return [
        WebSearchTool(),
        ImageSearchTool(),
        DownloadImageTool(),
        FetchUrlTool(),
    ]
