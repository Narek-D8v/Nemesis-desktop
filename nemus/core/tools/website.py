"""Автоматическая проверка сайта перед сдачей.

Инструмент открывает локальную сборку (``dist/index.html`` или обычный
``index.html``) либо готовый URL в headless-браузере, прокручивает страницу,
делает скриншот и проверяет:

- пустые и не загрузившиеся ``<img>``;
- якоря ``#section`` без соответствующего ``id``;
- пустые ссылки ``href=""`` и заглушки ``href="#"``;
- внутренние ссылки, которые отдают 404;
- низкий контраст текста по WCAG AA;
- переполнение карточек на мобильном экране;
- ошибки JavaScript и горизонтальный скролл на мобильном экране.

Скриншот сохраняется в ``data/workspace/screenshots`` и передаётся модели
как изображение, чтобы она действительно посмотрела результат, а не
догадывалась по коду.
"""

from __future__ import annotations

import base64
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from nemus.config import PROJECT_ROOT
from nemus.core.tools.base import RiskLevel, Tool, ToolResult
from nemus.core.tools.web import _validate_url
from nemus.utils.logger import logger

PAGE_LOAD_TIMEOUT_MS = 30_000
MOBILE_VIEWPORT = {"width": 390, "height": 844}
DESKTOP_VIEWPORT = {"width": 1440, "height": 1000}
#: сколько внешних ссылок проверять по HTTP (дорого и не всегда доступно)
MAX_EXTERNAL_CHECKS = 5
EXTERNAL_TIMEOUT_MS = 4_000


class _QuietHandler(SimpleHTTPRequestHandler):
    """Статический сервер без логов и без выхода за пределы каталога."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def _as_project_path(path: str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target.resolve()


def _site_root(path: str) -> tuple[Path, str]:
    """Возвращает каталог для раздачи и стартовый файл внутри него."""
    target = _as_project_path(path)
    if target.is_file():
        return target.parent, target.name
    if not target.is_dir():
        raise FileNotFoundError(f"Путь не найден: {target}")
    dist = target / "dist"
    if (dist / "index.html").is_file():
        return dist, "index.html"
    if (target / "index.html").is_file():
        return target, "index.html"
    raise FileNotFoundError(
        f"В {target} нет index.html (искал также dist/index.html). "
        "Сначала соберите сайт."
    )


def _serve(root: Path, entry: str) -> tuple[ThreadingHTTPServer, str]:
    """Поднимает временный локальный сервер и возвращает URL стартовой страницы."""
    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/{quote(entry)}"
    return server, url


def _normalise_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise ValueError("Пустой URL.")
    if not value.startswith(("http://", "https://")):
        raise ValueError("URL должен начинаться с http:// или https://.")
    parts = urlsplit(value)
    if parts.hostname in {"127.0.0.1", "localhost", "::1"}:
        return value
    return _validate_url(value)


def _scroll(page: Any) -> None:
    """Прокручивает страницу, чтобы сработали lazy-load и reveal-анимации."""
    height = page.evaluate("document.body ? document.body.scrollHeight : 0")
    for y in range(0, int(height), 600):
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(60)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(350)


_CONTRAST_JS = r"""
() => {
  const alphaValue = (value) => {
    if (!value) return 1;
    const text = value.trim();
    const parsed = text.endsWith('%') ? parseFloat(text) / 100 : parseFloat(text);
    return Number.isNaN(parsed) ? 1 : Math.max(0, Math.min(1, parsed));
  };
  const angleValue = (value) => {
    const text = value.trim().toLowerCase();
    if (text.endsWith('turn')) return parseFloat(text) * 360;
    if (text.endsWith('rad')) return (parseFloat(text) * 180) / Math.PI;
    if (text.endsWith('grad')) return parseFloat(text) * 0.9;
    return parseFloat(text);
  };
  const linearToSrgb = (value) => {
    const channel = value <= 0.0031308
      ? 12.92 * value
      : 1.055 * Math.pow(value, 1 / 2.4) - 0.055;
    return Math.max(0, Math.min(255, Math.round(channel * 255)));
  };
  const oklabToRgb = (lightness, a, b, alpha) => {
    const l = Math.pow(lightness + 0.3963377774 * a + 0.2158037573 * b, 3);
    const m = Math.pow(lightness - 0.1055613458 * a - 0.0638541728 * b, 3);
    const s = Math.pow(lightness - 0.0894841775 * a - 1.2914855480 * b, 3);
    return {
      r: linearToSrgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
      g: linearToSrgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
      b: linearToSrgb(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s),
      a: alpha,
    };
  };
  const parseOklch = (body) => {
    const [main, alphaPart] = body.split('/');
    const parts = main.trim().split(/\s+/).filter(Boolean);
    if (parts.length < 3) return null;
    const lightness = parts[0].endsWith('%') ? parseFloat(parts[0]) / 100 : parseFloat(parts[0]);
    const chroma = parts[1].endsWith('%') ? parseFloat(parts[1]) * 0.004 : parseFloat(parts[1]);
    const hue = angleValue(parts[2]);
    if ([lightness, chroma, hue].some(Number.isNaN)) return null;
    const radians = (hue * Math.PI) / 180;
    return oklabToRgb(
      lightness,
      chroma * Math.cos(radians),
      chroma * Math.sin(radians),
      alphaValue(alphaPart)
    );
  };
  const parseOklab = (body) => {
    const [main, alphaPart] = body.split('/');
    const parts = main.trim().split(/\s+/).filter(Boolean);
    if (parts.length < 3) return null;
    const lightness = parts[0].endsWith('%') ? parseFloat(parts[0]) / 100 : parseFloat(parts[0]);
    const a = parts[1].endsWith('%') ? parseFloat(parts[1]) * 0.004 : parseFloat(parts[1]);
    const b = parts[2].endsWith('%') ? parseFloat(parts[2]) * 0.004 : parseFloat(parts[2]);
    if ([lightness, a, b].some(Number.isNaN)) return null;
    return oklabToRgb(lightness, a, b, alphaValue(alphaPart));
  };
  const parseColor = (value) => {
    const raw = String(value || '').trim();
    if (!raw) return null;
    if (raw === 'transparent') return { r: 0, g: 0, b: 0, a: 0 };
    let match = raw.match(/^rgba?\((.+)\)$/i);
    if (match) {
      const parts = match[1].replace(/\//g, ' ').split(/[\s,]+/).filter(Boolean);
      if (parts.length < 3) return null;
      const channel = (part) => {
        const number = parseFloat(part);
        return part.endsWith('%') ? number * 2.55 : number;
      };
      const nums = parts.map(channel);
      if (nums.slice(0, 3).some((n) => Number.isNaN(n))) return null;
      return {
        r: nums[0],
        g: nums[1],
        b: nums[2],
        a: alphaValue(parts[3]),
      };
    }
    match = raw.match(/^oklch\((.+)\)$/i);
    if (match) return parseOklch(match[1]);
    match = raw.match(/^oklab\((.+)\)$/i);
    if (match) return parseOklab(match[1]);
    match = raw.match(/^#([0-9a-f]{3,8})$/i);
    if (match) {
      let hex = match[1];
      if (hex.length === 3 || hex.length === 4) {
        hex = hex.split('').map((part) => part + part).join('');
      }
      if (hex.length !== 6 && hex.length !== 8) return null;
      const number = parseInt(hex.slice(0, 6), 16);
      return {
        r: (number >> 16) & 255,
        g: (number >> 8) & 255,
        b: number & 255,
        a: hex.length === 8 ? parseInt(hex.slice(6), 16) / 255 : 1,
      };
    }
    return null;
  };
  const blend = (top, bottom) => {
    const a = Math.max(0, Math.min(1, top.a));
    return {
      r: top.r * a + bottom.r * (1 - a),
      g: top.g * a + bottom.g * (1 - a),
      b: top.b * a + bottom.b * (1 - a),
      a: 1,
    };
  };
  const luminance = (color) => {
    const channel = (value) => {
      const v = value / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
  };
  const contrast = (first, second) => {
    const a = luminance(first);
    const b = luminance(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const label = (element) => {
    const tag = element.tagName.toLowerCase();
    const classes = Array.from(element.classList || []).slice(0, 2);
    return tag + classes.map((name) => '.' + name).join('');
  };
  const effectiveOpacity = (element) => {
    let opacity = 1;
    let node = element;
    while (node && node.nodeType === 1) {
      opacity *= parseFloat(getComputedStyle(node).opacity || '1');
      node = node.parentElement;
    }
    return opacity;
  };
  const backgroundFor = (element) => {
    const layers = [];
    let node = element;
    while (node && node.nodeType === 1) {
      const cs = getComputedStyle(node);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      if (cs.backdropFilter && cs.backdropFilter !== 'none') return null;
      const color = parseColor(cs.backgroundColor);
      if (color && color.a > 0.001) layers.push(color);
      if (color && color.a >= 0.999) break;
      node = node.parentElement;
    }
    let result = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i -= 1) result = blend(layers[i], result);
    return result;
  };
  const failures = [];
  const seen = new Set();
  for (const element of document.querySelectorAll('body *')) {
    const text = Array.from(element.childNodes)
      .filter((node) => node.nodeType === 3)
      .map((node) => node.textContent.replace(/\s+/g, ' ').trim())
      .filter(Boolean)
      .join(' ');
    if (!text) continue;
    if (element.closest('svg, canvas, video, script, style, noscript, iframe, object')) continue;
    const cs = getComputedStyle(element);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (cs.mixBlendMode && cs.mixBlendMode !== 'normal') continue;
    if (cs.textShadow && cs.textShadow !== 'none') continue;
    if (cs.backgroundClip === 'text' || cs.webkitTextFillColor === 'transparent') continue;
    if (effectiveOpacity(element) < 0.5) continue;
    const rect = element.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) continue;
    const foreground = parseColor(cs.color);
    if (!foreground) continue;
    const background = backgroundFor(element);
    if (!background) continue;
    const textColor = foreground.a < 1 ? blend(foreground, background) : foreground;
    const ratio = contrast(textColor, background);
    const size = parseFloat(cs.fontSize) || 16;
    const weight = cs.fontWeight === 'bold' ? 700 : cs.fontWeight === 'normal' ? 400 : parseInt(cs.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const required = large ? 3 : 4.5;
    if (ratio + 0.05 < required) {
      const key = label(element) + '|' + text.slice(0, 80) + '|' + ratio.toFixed(2);
      if (seen.has(key)) continue;
      seen.add(key);
      failures.push({
        selector: label(element),
        text: text.slice(0, 90),
        ratio: Math.round(ratio * 100) / 100,
        required,
        fontSize: Math.round(size * 10) / 10,
        color: cs.color,
        background: 'rgb(' + Math.round(background.r) + ', ' + Math.round(background.g) + ', ' + Math.round(background.b) + ')',
      });
    }
  }
  failures.sort((a, b) => a.ratio - b.ratio);
  return failures.slice(0, 12);
}
"""

_CARD_OVERFLOW_JS = r"""
() => {
  const viewport = document.documentElement.clientWidth;
  const selector = [
    'article',
    '[class*="card" i]',
    '[class*="tile" i]',
    '[class*="panel" i]',
    '[class*="service" i]',
    '[class*="feature" i]',
    '[class*="review" i]',
    '[class*="testimonial" i]',
    '[class*="product" i]',
    '[class*="offer" i]',
    '[class*="rounded" i]',
  ].join(', ');
  const label = (element) => {
    const tag = element.tagName.toLowerCase();
    const classes = Array.from(element.classList || []).slice(0, 2);
    return tag + classes.map((name) => '.' + name).join('');
  };
  const results = [];
  for (const element of Array.from(document.querySelectorAll(selector)).slice(0, 800)) {
    if (element.matches('button, input, select, textarea, option')) continue;
    const cs = getComputedStyle(element);
    if (cs.display === 'contents' || cs.position === 'fixed') continue;
    if (cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
    const rect = element.getBoundingClientRect();
    if (rect.width < 140 || rect.height < 50) continue;
    if (rect.right <= 0 || rect.left >= viewport) continue;
    const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
    if (text.length < 20) continue;
    const classes = (element.getAttribute('class') || '').toLowerCase();
    const radius = parseFloat(cs.borderTopLeftRadius) || 0;
    const looksLikeCard = element.tagName === 'ARTICLE'
      || radius >= 8
      || /card|tile|panel|service|feature|review|testimonial|product|offer/.test(classes);
    if (!looksLikeCard) continue;

    let reason = '';
    let culprit = '';
    if (rect.width > viewport + 2 || rect.left < -2 || rect.right > viewport + 2) {
      reason = 'карточка шире экрана';
    }
    if (!reason) {
      for (const child of element.querySelectorAll('*')) {
        if (child.closest('script, style, svg')) continue;
        const childStyle = getComputedStyle(child);
        if (childStyle.display === 'none' || childStyle.visibility === 'hidden') continue;
        if (childStyle.position === 'absolute' || childStyle.position === 'fixed') continue;
        if (parseFloat(childStyle.opacity) < 0.05) continue;
        const childRect = child.getBoundingClientRect();
        if (childRect.width < 1 || childRect.height < 1) continue;
        if (childRect.right > rect.right + 2 || childRect.left < rect.left - 2) {
          reason = 'элемент вылезает за карточку';
          culprit = label(child);
          break;
        }
      }
    }
    if (!reason) {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        if (node.textContent.trim() && !node.parentElement.closest('script, style, svg, select, option, datalist')) {
          const parentStyle = getComputedStyle(node.parentElement);
          if (parentStyle.textOverflow !== 'ellipsis') {
            let ancestor = node.parentElement;
            let hidden = false;
            while (ancestor && ancestor !== element.parentElement) {
              const ancestorStyle = getComputedStyle(ancestor);
              if (ancestorStyle.position === 'absolute' || ancestorStyle.position === 'fixed') {
                hidden = true;
                break;
              }
              if (ancestor === element) break;
              ancestor = ancestor.parentElement;
            }
            if (!hidden) {
              const range = document.createRange();
              range.selectNodeContents(node);
              const textRect = range.getBoundingClientRect();
              if ((textRect.width >= 1 || textRect.height >= 1)
                && (textRect.right > rect.right + 2 || textRect.left < rect.left - 2)) {
                reason = 'текст вылезает за карточку';
                break;
              }
            }
          }
        }
        node = walker.nextNode();
      }
    }
    if (reason) {
      results.push({
        element,
        selector: label(element),
        text: text.slice(0, 90),
        reason: reason + (culprit ? ' (' + culprit + ')' : ''),
      });
    }
  }
  return results
    .filter((item, index) => !results.some((other, otherIndex) => otherIndex !== index && other.element.contains(item.element)))
    .slice(0, 10)
    .map(({ element, ...item }) => item);
}
"""

_SCAN_JS = r"""
() => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const images = Array.from(document.images).map((img) => ({
    src: img.currentSrc || img.src || '',
    alt: clean(img.alt),
    width: img.naturalWidth || 0,
    height: img.naturalHeight || 0,
    complete: !!img.complete,
    visible: !!(img.offsetWidth || img.offsetHeight),
  }));
  const ids = new Set(Array.from(document.querySelectorAll('[id]')).map((el) => el.id));
  const names = new Set(
    Array.from(document.querySelectorAll('a[name]')).map((el) => el.getAttribute('name'))
  );
  const anchors = Array.from(document.querySelectorAll('a[href]')).map((a) => ({
    href: a.getAttribute('href') || '',
    text: clean(a.textContent).slice(0, 90),
  }));
  const emptyLinks = [];
  const brokenAnchors = [];
  const internalLinks = [];
  const externalLinks = [];
  for (const anchor of anchors) {
    const raw = (anchor.href || '').trim();
    if (!raw || raw === '#') {
      emptyLinks.push(anchor);
      continue;
    }
    if (raw.startsWith('#')) {
      const target = decodeURIComponent(raw.slice(1));
      if (target && !ids.has(target) && !names.has(target)) {
        brokenAnchors.push(anchor);
      }
      continue;
    }
    let parsed;
    try {
      parsed = new URL(anchor.href, location.href);
    } catch (_) {
      continue;
    }
    if (parsed.origin === location.origin) {
      if (parsed.hash && parsed.pathname === location.pathname) {
        const target = decodeURIComponent(parsed.hash.slice(1));
        if (target && !ids.has(target) && !names.has(target)) {
          brokenAnchors.push(anchor);
          continue;
        }
      }
      internalLinks.push({ href: parsed.href, text: anchor.text });
    } else if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      externalLinks.push({ href: parsed.href, text: anchor.text });
    }
  }
  const unique = (items) => {
    const seen = new Set();
    return items.filter((item) => {
      if (seen.has(item.href)) return false;
      seen.add(item.href);
      return true;
    });
  };
  return {
    title: clean(document.title),
    h1: Array.from(document.querySelectorAll('h1')).map((el) => clean(el.textContent)),
    sections: document.querySelectorAll('section').length,
    bodyText: clean(document.body ? document.body.innerText : ''),
    images,
    emptyLinks,
    brokenAnchors,
    internalLinks: unique(internalLinks),
    externalLinks: unique(externalLinks),
  };
}
"""


def _image_data_url(data: bytes, *, max_width: int = 1200) -> str:
    """Уменьшает скриншот и отдаёт data URL для vision-контекста модели."""
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            if image.width > max_width:
                ratio = max_width / image.width
                image = image.resize(
                    (max_width, max(1, int(image.height * ratio)))
                )
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = BytesIO()
            image.save(buffer, "JPEG", quality=82, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(
                buffer.getvalue()
            ).decode()
    except Exception:  # noqa: BLE001
        return "data:image/png;base64," + base64.b64encode(data).decode()


class VerifyWebsiteTool(Tool):
    """Проверяет собранный сайт в браузере и отдаёт скриншот модели."""

    name = "verify_website"
    description = (
        "Проверяет сайт перед сдачей в настоящем браузере (Playwright): "
        "открывает локальную сборку или URL, прокручивает страницу, делает "
        "скриншот, проверяет пустые/битые картинки, мёртвые якоря, пустые "
        "ссылки, 404 внутренних страниц, низкий контраст текста, "
        "переполнение карточек и мобильный скролл. Возвращает "
        "скриншот, чтобы модель увидела результат. ОБЯЗАТЕЛЬНО вызывай после "
        "сборки и ДО отправки сайта пользователю. Если проверка не прошла — "
        "исправь и вызови снова."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Папка проекта, папка dist или index.html. Для проекта "
                    "с dist сначала собирает именно dist."
                ),
            },
            "url": {
                "type": "string",
                "description": "Готовый URL (например, локальный preview).",
            },
            "check_external": {
                "type": "boolean",
                "description": "Проверять внешние ссылки по HTTP (по умолчанию true).",
            },
        },
    }
    #: проверка только читает сайт; скриншоты пишутся в workspace
    risk = RiskLevel.SAFE
    #: подставляется фабрикой из config.tools.workspace
    workspace: str = "data/workspace"

    def _screenshot_path(self, suffix: str) -> Path:
        workspace = Path(self.workspace).expanduser()
        if not workspace.is_absolute():
            workspace = PROJECT_ROOT / workspace
        folder = workspace / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return folder / f"website-{stamp}{suffix}.png"

    @staticmethod
    def _launch(playwright: Any) -> Any:
        """Запускает Chromium, при отсутствии сборки — установленный Chrome."""
        try:
            return playwright.chromium.launch(headless=True)
        except Exception as first:  # noqa: BLE001
            try:
                return playwright.chromium.launch(headless=True, channel="chrome")
            except Exception as second:  # noqa: BLE001
                raise RuntimeError(
                    "Playwright не смог запустить браузер. "
                    "Установите его: `playwright install chromium`. "
                    f"Chromium: {first}; Chrome: {second}"
                ) from second

    def _open(self, context: Any, url: str) -> Any:
        page = context.new_page()
        page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)
        console_errors: list[str] = []
        page_errors: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.goto(
                url,
                wait_until="networkidle",
                timeout=PAGE_LOAD_TIMEOUT_MS,
            )
        except Exception:
            # Внешние шрифты/аналитика могут не дать networkidle — страница
            # уже может быть готова, поэтому пробуем domcontentloaded.
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT_MS,
            )
        _scroll(page)
        return page, console_errors, page_errors

    @staticmethod
    def _check_page(page: Any, *, mobile: bool) -> dict[str, Any]:
        data = page.evaluate(_SCAN_JS)
        data["console_errors"] = []
        data["page_errors"] = []
        data["mobile"] = mobile
        data["contrast_failures"] = []
        data["card_overflows"] = []
        if mobile:
            data["card_overflows"] = page.evaluate(_CARD_OVERFLOW_JS)
        else:
            data["contrast_failures"] = page.evaluate(_CONTRAST_JS)
        data["overflow"] = (
            page.evaluate(
                "Math.max(0, document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth)"
            )
            if mobile
            else 0
        )
        return data

    def _check_internal_links(
        self, context: Any, links: list[dict[str, str]]
    ) -> tuple[list[str], list[str]]:
        broken: list[str] = []
        checked: list[str] = []
        for link in links[:15]:
            href = link["href"]
            try:
                response = context.request.get(href, timeout=5_000)
                checked.append(href)
                if response.status in (404, 410):
                    broken.append(f"{href} → HTTP {response.status}")
            except Exception:  # noqa: BLE001
                # внутренняя ссылка может зависеть от SPA-роутера; не считаем
                # сетевой сбой доказательством битой ссылки
                continue
        return broken, checked

    def _check_external_links(
        self, context: Any, links: list[dict[str, str]]
    ) -> tuple[list[str], list[str]]:
        broken: list[str] = []
        unchecked: list[str] = []
        for link in links[:MAX_EXTERNAL_CHECKS]:
            href = link["href"]
            try:
                response = context.request.head(
                    href, timeout=EXTERNAL_TIMEOUT_MS, max_redirects=3
                )
                if response.status in (404, 410):
                    broken.append(f"{href} → HTTP {response.status}")
            except Exception as exc:  # noqa: BLE001
                unchecked.append(f"{href} ({type(exc).__name__})")
        return broken, unchecked

    def execute(
        self,
        path: str = "",
        url: str = "",
        check_external: bool = True,
        **_: object,
    ) -> ToolResult:
        server: ThreadingHTTPServer | None = None
        try:
            if url.strip():
                target_url = _normalise_url(url)
                source = target_url
            elif path.strip():
                root, entry = _site_root(path)
                server, target_url = _serve(root, entry)
                source = f"{root} ({target_url})"
            else:
                return ToolResult.failure(
                    "Укажите path к папке проекта/dist/index.html или url."
                )

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                try:
                    context = browser.new_context(
                        viewport=DESKTOP_VIEWPORT,
                        device_scale_factor=1,
                    )
                    desktop_page, desktop_console, desktop_errors = self._open(
                        context, target_url
                    )
                    desktop = self._check_page(desktop_page, mobile=False)
                    desktop["console_errors"] = desktop_console
                    desktop["page_errors"] = desktop_errors
                    desktop_png = desktop_page.screenshot(
                        full_page=True, type="png"
                    )
                    desktop_path = self._screenshot_path("")
                    desktop_path.write_bytes(desktop_png)

                    mobile_context = browser.new_context(
                        viewport=MOBILE_VIEWPORT,
                        device_scale_factor=1,
                        is_mobile=True,
                        has_touch=True,
                    )
                    mobile_page, mobile_console, mobile_errors = self._open(
                        mobile_context, target_url
                    )
                    mobile = self._check_page(mobile_page, mobile=True)
                    mobile["console_errors"] = mobile_console
                    mobile["page_errors"] = mobile_errors
                    mobile_png = mobile_page.screenshot(
                        full_page=True, type="png"
                    )
                    mobile_path = self._screenshot_path("-mobile")
                    mobile_path.write_bytes(mobile_png)

                    internal_broken, _ = self._check_internal_links(
                        context, desktop["internalLinks"]
                    )
                    external_broken: list[str] = []
                    external_unchecked: list[str] = []
                    if check_external:
                        external_broken, external_unchecked = (
                            self._check_external_links(
                                context, desktop["externalLinks"]
                            )
                        )
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"verify_website: {exc}")
            return ToolResult.failure(f"Проверка сайта не удалась: {exc}")
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()

        empty_images = [
            image
            for image in desktop["images"]
            if not image["src"]
            or not image["complete"]
            or image["width"] <= 0
            or image["height"] <= 0
        ]
        empty_links = desktop["emptyLinks"]
        broken_anchors = desktop["brokenAnchors"]
        page_errors = desktop["page_errors"]
        console_errors = desktop["console_errors"]
        mobile_overflow = mobile["overflow"]
        contrast_failures = desktop["contrast_failures"]
        card_overflows = mobile["card_overflows"]
        empty_page = len(desktop["bodyText"]) < 120 and desktop["sections"] < 2
        blocking = bool(
            empty_images
            or empty_links
            or broken_anchors
            or internal_broken
            or external_broken
            or page_errors
            or empty_page
            or mobile_overflow
            or contrast_failures
            or card_overflows
        )

        lines = [
            f"{'❌' if blocking else '✅'} Проверка сайта: "
            f"{'есть проблемы' if blocking else 'OK'}",
            f"Источник: {source}",
            f"Title: {desktop['title'] or '(пусто)'}",
            f"Секций: {desktop['sections']}, H1: {len(desktop['h1'])}, "
            f"изображений: {len(desktop['images'])}",
            f"Пустые картинки: {len(empty_images)}",
            f"Пустые ссылки: {len(empty_links)}",
            f"Битые якоря: {len(broken_anchors)}",
            f"Внутренние 404: {len(internal_broken)}",
            f"Мобильный скролл: {mobile_overflow}px",
            f"Низкий контраст: {len(contrast_failures)}",
            f"Переполнение карточек: {len(card_overflows)}",
        ]
        if console_errors:
            lines.append(f"Ошибки консоли: {len(console_errors)}")
        if external_broken:
            lines.append(f"Внешние 404: {len(external_broken)}")
        if external_unchecked:
            lines.append(f"Внешние не проверены: {len(external_unchecked)}")
        if empty_images:
            lines.append("Пустые картинки:")
            lines += [
                f"- {image['src'] or '(нет src)'} (alt: {image['alt'] or '—'})"
                for image in empty_images[:8]
            ]
        if empty_links:
            lines.append("Пустые ссылки:")
            lines += [
                f"- «{link['text'] or '(без текста)'}» href={link['href']!r}"
                for link in empty_links[:8]
            ]
        if broken_anchors:
            lines.append("Битые якоря:")
            lines += [
                f"- «{link['text'] or '(без текста)'}» → {link['href']}"
                for link in broken_anchors[:8]
            ]
        if internal_broken:
            lines.append("Внутренние 404:")
            lines += [f"- {item}" for item in internal_broken[:8]]
        if external_broken:
            lines.append("Внешние 404:")
            lines += [f"- {item}" for item in external_broken[:8]]
        if page_errors:
            lines.append("Ошибки JavaScript:")
            lines += [f"- {item[:300]}" for item in page_errors[:5]]
        if console_errors:
            lines.append("Консоль:")
            lines += [f"- {item[:300]}" for item in console_errors[:5]]
        if empty_page:
            lines.append("Страница выглядит пустой: мало текста и секций.")
        if mobile_overflow:
            lines.append("На мобильном есть горизонтальный скролл.")
        if contrast_failures:
            lines.append("Низкий контраст текста:")
            lines += [
                f"- {item['selector']} «{item['text']}»: "
                f"{item['ratio']}:1 (нужно {item['required']}:1, "
                f"{item['fontSize']}px; {item['color']} на {item['background']})"
                for item in contrast_failures[:8]
            ]
        if card_overflows:
            lines.append("Переполнение карточек на мобильном:")
            lines += [
                f"- {item['selector']} «{item['text']}»: {item['reason']}"
                for item in card_overflows[:8]
            ]
        lines.append(f"Скриншот: {desktop_path}")
        lines.append(f"Мобильный скриншот: {mobile_path}")
        summary = "\n".join(lines)

        images = [
            _image_data_url(desktop_png),
            _image_data_url(mobile_png),
        ]
        if blocking:
            return ToolResult.failure(
                summary,
                data={"screenshots": [str(desktop_path), str(mobile_path)]},
                images=images,
            )
        return ToolResult.success(
            summary + "\nМожно сдавать сайт.",
            data={"screenshots": [str(desktop_path), str(mobile_path)]},
            images=images,
        )


def website_tools() -> list[Tool]:
    return [VerifyWebsiteTool()]
