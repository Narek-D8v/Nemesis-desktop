"""Веб-инструменты: поиск и получение содержимого URL."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from nemus.core.tools.base import RiskLevel, Tool, ToolResult

_MAX_REDIRECTS = 5


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


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Получает содержимое страницы по URL. Пытается извлечь основной текст и "
        "конвертировать в markdown. Локальные и внутренние адреса запрещены."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Полный URL, включая https://."},
            "max_chars": {
                "type": "integer",
                "description": "Максимум символов ответа (по умолчанию 12_000).",
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

    def execute(self, url: str = "", max_chars: int = 12_000, **_: object) -> ToolResult:
        if not url.strip():
            return ToolResult.failure("Пустой URL.")
        max_chars = max(1000, min(50000, max_chars))
        try:
            import httpx
            from bs4 import BeautifulSoup
            from markdownify import markdownify as md

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            with httpx.Client(follow_redirects=False, timeout=20) as client:
                resp = self._get_following_redirects(client, url.strip(), headers)
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "noscript"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
            if len(text) < 2000:
                try:
                    text = md(str(soup))
                except Exception:  # noqa: BLE001
                    pass
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n...[обрезано]"
            return ToolResult.success(f"Содержимое {url}:\n\n{text}")
        except UnsafeUrl as exc:
            return ToolResult.failure(f"Запрос отклонён: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Ошибка загрузки: {exc}")


def web_tools() -> list[Tool]:
    return [WebSearchTool(), FetchUrlTool()]
