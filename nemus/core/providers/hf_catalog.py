"""Поиск GGUF-моделей и их файлов через API Hugging Face.

Используется окном настроек: пользователь вводит запрос, видит репозитории,
выбирает модель и конкретный `.gguf`. Скачивание выполняет hf_download.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from nemus.core.providers.gguf_split import is_split_gguf, split_part
from nemus.utils.logger import logger

HF_TIMEOUT = 12.0


class HFCatalogError(RuntimeError):
    """Hugging Face API недоступен или вернул неожиданный ответ."""


@dataclass(frozen=True)
class HFModel:
    repo_id: str
    downloads: int = 0
    likes: int = 0
    gated: bool = False


@dataclass(frozen=True)
class HFFile:
    filename: str
    size: int = 0
    #: сколько файлов скачать; >1 — это шарды одного GGUF
    parts: int = 1
    split: bool = False


def _token() -> str:
    return (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or ""
    ).strip()


def _api_base(endpoint: str) -> str:
    base = (endpoint or "https://huggingface.co").rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    return f"{base}/api/models"


def _get_json(url: str) -> Any:
    headers = {"User-Agent": "nemus-hf-catalog", "Accept": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HF_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise HFCatalogError(
                "Hugging Face закрыл доступ. Для приватной модели задайте HF_TOKEN."
            ) from exc
        raise HFCatalogError(
            f"Hugging Face ответил HTTP {exc.code}: {exc.reason}"
        ) from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise HFCatalogError(f"Не удалось связаться с Hugging Face: {exc}") from exc


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_hf_search(data: Any, limit: int = 25) -> list[HFModel]:
    """Разбирает ответ ``/api/models?search=...&filter=gguf``."""
    items = data if isinstance(data, list) else []
    out: list[HFModel] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        repo = str(item.get("id") or item.get("modelId") or "").strip()
        if not repo or "/" not in repo or repo in seen:
            continue
        tags = [str(tag).lower() for tag in item.get("tags") or []]
        if tags and "gguf" not in tags and "gguf" not in repo.lower():
            # filter=gguf уже отсекает лишнее, но API мог проигнорировать его;
            # пустые tags не считаем признаком не-GGUF (API их не всегда отдаёт)
            continue
        siblings = item.get("siblings")
        if isinstance(siblings, list):
            names = [str(s.get("rfilename") or "") for s in siblings if isinstance(s, dict)]
            usable = [
                name
                for name in names
                if name.lower().endswith(".gguf")
                and "mmproj" not in name.lower()
            ]
            if not usable:
                # В репозитории вообще нет GGUF — показывать нечего.
                continue
        seen.add(repo)
        out.append(
            HFModel(
                repo_id=repo,
                downloads=_count(item.get("downloads")),
                likes=_count(item.get("likes")),
                gated=bool(item.get("gated")),
            )
        )
        if len(out) >= limit:
            break
    return out


def parse_hf_files(data: Any) -> list[HFFile]:
    """Достаёт .gguf-файлы из ``/api/models/{repo}?blobs=true``."""
    if not isinstance(data, dict):
        return []
    siblings = data.get("siblings")
    if not isinstance(siblings, list):
        return []
    out: list[HFFile] = []
    seen: set[str] = set()
    for item in siblings:
        if not isinstance(item, dict):
            continue
        name = str(item.get("rfilename") or "").strip()
        lowered = name.lower()
        if not lowered.endswith(".gguf") or name in seen:
            continue
        # проектор для vision-моделей — не самостоятельная чат-модель
        if "mmproj" in lowered:
            continue
        seen.add(name)
        out.append(HFFile(filename=name, size=_count(item.get("size"))))
    # крупные (обычно более качественные) — выше; при равном размере по имени
    return sorted(out, key=lambda f: (-f.size, f.filename.lower()))


def search_hf_models(
    query: str,
    *,
    endpoint: str = "https://huggingface.co",
    limit: int = 25,
) -> list[HFModel]:
    """Ищет публичные GGUF-репозитории по строке."""
    params = {
        "search": (query or "").strip(),
        "filter": "gguf",
        "sort": "downloads",
        "direction": "-1",
        "limit": str(max(1, min(100, limit))),
        # full=true отдаёт siblings: так мы отсеиваем репозитории, где нет
        # ни одного .gguf (включая разбитые на шарды), ещё до списка файлов.
        "full": "true",
    }
    url = f"{_api_base(endpoint)}?{urllib.parse.urlencode(params)}"
    logger.debug(f"Hugging Face: поиск GGUF «{query}»")
    return parse_hf_search(_get_json(url), limit=limit)


def list_hf_gguf_files(
    repo_id: str,
    *,
    endpoint: str = "https://huggingface.co",
) -> list[HFFile]:
    """Список GGUF-файлов выбранного репозитория."""
    repo = (repo_id or "").strip().strip("/")
    if "/" not in repo:
        raise HFCatalogError("Репозиторий должен быть в виде owner/model.")
    url = f"{_api_base(endpoint)}/{urllib.parse.quote(repo, safe='/')}?blobs=true"
    logger.debug(f"Hugging Face: список файлов {repo}")
    files = parse_hf_files(_get_json(url))
    if not files:
        raise HFCatalogError(
            f"В репозитории {repo} не найден ни один .gguf-файл. "
            "Проверьте, что это GGUF-репозиторий."
        )
    return files


#: порядок предпочтения квантований для кнопки «Скачать»: чаще всего это
#: разумный баланс качества и размера, а не самый большой файл в репозитории
_QUANT_ORDER = (
    "q4_k_m",
    "q5_k_m",
    "q4_k_s",
    "iq4_xs",
    "iq4_nl",
    "q4_0",
    "q5_k_s",
    "q6_k",
    "q8_0",
    "q3_k_m",
    "iq3_m",
    "iq3_s",
    "iq3_xs",
    "iq3_xxs",
    "q3_k_s",
    "q2_k",
    "iq2_m",
    "iq2_s",
    "iq2_xs",
    "iq2_xxs",
    "q2_0",
    "iq1_m",
    "iq1_s",
    "fp16",
    "f16",
    "bf16",
)


def _quant_rank(filename: str) -> int:
    """Место квантования в порядке предпочтения (больше — менее желанно)."""
    lowered = filename.lower()
    for index, quant in enumerate(_QUANT_ORDER):
        if quant in lowered:
            return index
    return len(_QUANT_ORDER) + 1


def choose_hf_file(files: list[HFFile], prefer: str = "") -> HFFile | None:
    """Выбирает один GGUF для автоматической загрузки.

    ``prefer`` — исходный запрос пользователя: если в нём явно назван
    квант (q8, q5_k_m, …), он важнее общего порядка. Шарды одного GGUF
    сначала схлопываются, поэтому вернётся первая часть и загрузчик
    скачает/склеит все остальные.
    """
    grouped = group_hf_files(files)
    if not grouped:
        return None
    wanted = (prefer or "").lower().replace("-", "_")
    requested = next(
        (
            quant
            for quant in _QUANT_ORDER
            if quant in wanted or quant.split("_", 1)[0] in wanted
        ),
        "",
    )

    def score(item: HFFile) -> tuple[int, int, int, str]:
        lowered = item.filename.lower()
        explicit = 0 if requested and requested in lowered else 1
        return (explicit, _quant_rank(item.filename), -item.size, lowered)

    return min(grouped, key=score)


def group_hf_files(files: list[HFFile]) -> list[HFFile]:
    """Схлопывает шарды одного GGUF в одну строку для выбора."""
    output: list[HFFile] = []
    groups: dict[tuple[str, int], list[tuple[int, HFFile]]] = {}
    order: list[tuple[str, int]] = []
    for item in files:
        part = split_part(item.filename)
        if part is None:
            output.append(
                HFFile(
                    filename=item.filename,
                    size=item.size,
                    parts=1,
                    split=False,
                )
            )
            continue
        key = (part.prefix, part.count)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((part.index, item))
    for key in order:
        members = sorted(groups[key], key=lambda pair: pair[0])
        first = members[0][1]
        output.append(
            HFFile(
                filename=first.filename,
                size=sum(item.size for _, item in members),
                parts=key[1],
                split=True,
            )
        )
    return output


__all__ = [
    "choose_hf_file",
    "group_hf_files",
    "HFCatalogError",
    "HFFile",
    "HFModel",
    "is_split_gguf",
    "list_hf_gguf_files",
    "parse_hf_files",
    "parse_hf_search",
    "search_hf_models",
]
