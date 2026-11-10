"""Магазин скиллов: каталог на GitHub, поиск и установка в один клик.

Источник — GitHub. Репозитории со скиллами находятся поиском, дерево
репозитория читается через API, сами файлы качаются с
``raw.githubusercontent.com``. Git и токен не нужны (с токеном в
``GITHUB_TOKEN`` просто выше лимит запросов).

Установленный скилл — обычная папка ``.agents/skills/<имя>``, поэтому он
работает без перезапуска: системный промпт собирается заново на каждый
запрос, а значит модель увидит новый скилл со следующего сообщения.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import time
import zipfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Sequence, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

from nemus.config import PROJECT_ROOT
from nemus.core.skills import (
    SKILL_FILE,
    Skill,
    get_skill,
    load_skills,
    parse_skill,
    skills_root,
    slugify,
)
from nemus.utils.logger import logger

#: GitHub API и сырые файлы
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

#: таймаут одного сетевого запроса, секунды
TIMEOUT = 15.0

#: сколько скиллов показывать из одного репозитория
MAX_SKILLS_PER_REPO = 40

#: сколько файлов максимум тянем для одного скилла
MAX_FILES = 300

#: предел для плагина: в пакете лежат скиллы, команды и шаблоны сразу
MAX_PLUGIN_FILES = 1200

#: предел на файл (гигабайтный архив в скилле нам не нужен)
MAX_FILE_BYTES = 4_000_000

#: репозитории, которые показываем в «Рекомендуемых» всегда
RECOMMENDED_REPOS: tuple[str, ...] = (
    "anthropics/skills",
    "obra/superpowers",
)

#: темы GitHub, по которым ищутся репозитории со скиллами
RECOMMENDED_TOPICS: tuple[str, ...] = (
    "claude-skills",
    "agent-skills",
    "claude-code-skills",
)

#: сколько сетевых запросов держим одновременно
WORKERS = 8

#: кэш каталога на диске: без него открытие магазина тратит лимит GitHub
DEFAULT_CACHE = PROJECT_ROOT / "data" / "skill_store_cache.json"

#: сколько живёт разобранный репозиторий и результаты поиска
CACHE_TTL = 12 * 3600
SEARCH_TTL = 30 * 60

#: манифест плагина Claude Code: ``<плагин>/.claude-plugin/plugin.json``
PLUGIN_DIR = ".claude-plugin"
PLUGIN_FILE = "plugin.json"

#: сколько плагинов показывать из одного репозитория
MAX_PLUGINS_PER_REPO = 60

#: репозитории-маркетплейсы в «Рекомендуемых» магазина плагинов
PLUGIN_RECOMMENDED_REPOS: tuple[str, ...] = (
    "anthropics/claude-code",
    "wshobson/agents",
    "obra/superpowers",
)

#: темы GitHub, по которым ищутся репозитории с плагинами
PLUGIN_RECOMMENDED_TOPICS: tuple[str, ...] = (
    "claude-code-plugin",
    "claude-plugins",
)

#: откуда установлены скиллы: без этого не понять, где искать обновление
ORIGIN_FILE = ".nemus-store.json"

#: что не копируем из локального плагина: служебные и чужие каталоги
LOCAL_SKIP = (
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".DS_Store",
    "__MACOSX",
)

T = TypeVar("T")
R = TypeVar("R")

#: текст ошибки не должен разрастаться на весь экран
_MAX_ERROR = 300


class StoreError(RuntimeError):
    """Ошибка магазина: сеть, лимит GitHub, битый или слишком большой скилл."""


@dataclass(frozen=True)
class StoreSkill:
    """Скилл, найденный в репозитории GitHub (ещё не установленный)."""

    name: str
    description: str
    #: репозиторий в виде ``owner/name``
    repo: str
    #: папка скилла внутри репозитория (пусто — скилл в корне)
    path: str
    #: ветка, с которой качаем
    ref: str
    #: файлы скилла относительно его папки (первым идёт SKILL.md)
    files: tuple[str, ...] = ()
    stars: int = 0
    repo_description: str = ""
    #: когда репозиторий последний раз обновляли (ISO, ``pushed_at``)
    updated: str = ""
    #: плагин (набор), внутри которого лежит скилл; пусто — обычный скилл
    plugin: str = ""

    @property
    def slug(self) -> str:
        """Имя папки, в которую встанет скилл."""
        try:
            return slugify(self.name)
        except ValueError:
            return slugify(self.path.split("/")[-1] or self.repo.split("/")[-1])

    @property
    def key(self) -> str:
        """Уникальный ключ карточки (репозиторий + папка)."""
        return f"{self.repo}:{self.path}"

    @property
    def url(self) -> str:
        """Ссылка на скилл на GitHub."""
        if self.path:
            return f"https://github.com/{self.repo}/tree/{self.ref}/{self.path}"
        return f"https://github.com/{self.repo}"

    @property
    def source(self) -> str:
        """Строка-источник для карточки: ``owner/repo · ★ 120``."""
        return f"{self.repo} · ★ {self.stars}" if self.stars else self.repo


def _skill_to_cache(skill: StoreSkill) -> dict[str, Any]:
    """Карточка скилла в виде, пригодном для JSON-кэша."""
    return {
        "name": skill.name,
        "description": skill.description,
        "repo": skill.repo,
        "path": skill.path,
        "ref": skill.ref,
        "files": list(skill.files),
        "stars": skill.stars,
        "repo_description": skill.repo_description,
        "updated": skill.updated,
        "plugin": skill.plugin,
    }


def _skill_from_cache(data: Any) -> StoreSkill | None:
    """Восстанавливает карточку из кэша (битое — None)."""
    if not isinstance(data, dict):
        return None
    try:
        return StoreSkill(
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            repo=str(data["repo"]),
            path=str(data.get("path") or ""),
            ref=str(data.get("ref") or "main"),
            files=tuple(str(name) for name in data.get("files") or ()),
            stars=int(data.get("stars") or 0),
            repo_description=str(data.get("repo_description") or ""),
            updated=str(data.get("updated") or ""),
            plugin=str(data.get("plugin") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class StorePlugin:
    """Плагин Claude Code из репозитория (ещё не установленный).

    Плагин — это пакет: скиллы, команды, агенты и хуки лежат вместе и
    ставятся одной папкой, поэтому Немуся получает всё, что задумал автор.
    """

    name: str
    description: str
    #: репозиторий в виде ``owner/name``
    repo: str
    #: папка плагина внутри репозитория (пусто — плагин в корне)
    path: str
    #: ветка, с которой качаем
    ref: str
    #: все файлы плагина относительно его папки
    files: tuple[str, ...] = ()
    stars: int = 0
    repo_description: str = ""
    updated: str = ""
    version: str = ""
    #: состав пакета: сколько в нём скиллов, команд и агентов
    skills: int = 0
    commands: int = 0
    agents: int = 0

    @property
    def slug(self) -> str:
        """Имя папки, в которую встанет плагин."""
        try:
            return slugify(self.name)
        except ValueError:
            return slugify(self.path.split("/")[-1] or self.repo.split("/")[-1])

    @property
    def key(self) -> str:
        """Уникальный ключ карточки (репозиторий + папка)."""
        return f"{self.repo}:{self.path}"

    @property
    def url(self) -> str:
        """Ссылка на плагин на GitHub."""
        if self.path:
            return f"https://github.com/{self.repo}/tree/{self.ref}/{self.path}"
        return f"https://github.com/{self.repo}"

    @property
    def source(self) -> str:
        """Строка-источник для карточки: ``owner/repo · ★ 120``."""
        return f"{self.repo} · ★ {self.stars}" if self.stars else self.repo


def _plugin_to_cache(plugin: StorePlugin) -> dict[str, Any]:
    """Карточка плагина в виде, пригодном для JSON-кэша."""
    return {
        "name": plugin.name,
        "description": plugin.description,
        "repo": plugin.repo,
        "path": plugin.path,
        "ref": plugin.ref,
        "files": list(plugin.files),
        "stars": plugin.stars,
        "repo_description": plugin.repo_description,
        "updated": plugin.updated,
        "version": plugin.version,
        "skills": plugin.skills,
        "commands": plugin.commands,
        "agents": plugin.agents,
    }


def _plugin_from_cache(data: Any) -> StorePlugin | None:
    """Восстанавливает плагин из кэша (битое — None)."""
    if not isinstance(data, dict):
        return None
    try:
        return StorePlugin(
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            repo=str(data["repo"]),
            path=str(data.get("path") or ""),
            ref=str(data.get("ref") or "main"),
            files=tuple(str(name) for name in data.get("files") or ()),
            stars=int(data.get("stars") or 0),
            repo_description=str(data.get("repo_description") or ""),
            updated=str(data.get("updated") or ""),
            version=str(data.get("version") or ""),
            skills=int(data.get("skills") or 0),
            commands=int(data.get("commands") or 0),
            agents=int(data.get("agents") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _short_error(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _MAX_ERROR else text[: _MAX_ERROR - 1] + "…"


class GitHubClient:
    """Тонкий клиент GitHub API с кэшем метаданных и деревьев.

    Тесты подменяют методы ``repo_info``, ``tree``, ``read_bytes`` и
    ``search_repos`` — сеть им не нужна.
    """

    def __init__(
        self,
        token: str = "",
        timeout: float = TIMEOUT,
        cache_path: str | Path | None = DEFAULT_CACHE,
    ) -> None:
        self.token = (
            token
            or os.environ.get("GITHUB_TOKEN", "")
            or os.environ.get("GH_TOKEN", "")
        )
        self.timeout = timeout
        self.cache_path = Path(cache_path) if cache_path else None
        self._repos: dict[str, dict[str, Any]] = {}
        self._trees: dict[tuple[str, str], list[dict[str, Any]]] = {}
        #: разобранные скиллы репозитория: повторное открытие витрины — мгновенно
        self._skills: dict[tuple[str, str], tuple[int, list[StoreSkill]]] = {}
        #: разобранные плагины репозитория (кэш тот же, ключ другой)
        self._plugins: dict[tuple[str, str], tuple[int, list[StorePlugin]]] = {}
        self._cache: dict[str, Any] | None = None

    # ---------------------------------------------------------------- транспорт
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "Nemus-SkillStore/1.0",
            "Accept": "application/vnd.github+json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _fetch(self, url: str) -> bytes:
        """Скачивает URL; сетевые ошибки превращаются в понятный StoreError."""
        try:
            with urlopen(  # noqa: S310 — только https-адреса GitHub
                Request(url, headers=self._headers()), timeout=self.timeout
            ) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise StoreError("Скилл или репозиторий не найден на GitHub.") from None
            if exc.code in (403, 429):
                raise StoreError(
                    "GitHub ограничил число запросов. Подождите минуту или "
                    "задайте токен в GITHUB_TOKEN."
                ) from None
            raise StoreError(f"GitHub ответил ошибкой {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise StoreError("Нет связи с GitHub — проверьте интернет.") from None

    def _get_json(self, url: str) -> Any:
        try:
            return json.loads(self._fetch(url).decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise StoreError("GitHub вернул неожиданный ответ.") from None

    # ---------------------------------------------------------------- метаданные
    def repo_info(self, repo: str) -> dict[str, Any]:
        """Ветка по умолчанию, звёзды и описание репозитория."""
        repo = repo.strip().strip("/")
        if repo in self._repos:
            return self._repos[repo]
        # метаданные тоже кэшируем на диске: они нужны и для дат новизны
        stored = self.cache_get("repo", repo, CACHE_TTL)
        if isinstance(stored, dict) and "updated" in stored:
            self._repos[repo] = stored
            return stored
        data = self._get_json(f"{API}/repos/{repo}")
        if not isinstance(data, dict):
            raise StoreError("GitHub вернул неожиданный ответ.")
        info = {
            "ref": str(data.get("default_branch") or "main"),
            "stars": int(data.get("stargazers_count") or 0),
            "description": str(data.get("description") or ""),
            "updated": str(data.get("pushed_at") or data.get("updated_at") or ""),
        }
        self._repos[repo] = info
        self.cache_put("repo", repo, info)
        return info

    def tree(self, repo: str, ref: str) -> list[dict[str, Any]]:
        """Все файлы ветки (``recursive``), только блобы."""
        key = (repo, ref)
        if key in self._trees:
            return self._trees[key]
        data = self._get_json(f"{API}/repos/{repo}/git/trees/{ref}?recursive=1")
        entries = [
            entry
            for entry in (data.get("tree") if isinstance(data, dict) else None) or []
            if isinstance(entry, dict) and entry.get("type") == "blob"
        ]
        if isinstance(data, dict) and data.get("truncated"):
            logger.warning(f"Дерево {repo} обрезано GitHub — видны не все скиллы")
        self._trees[key] = entries
        return entries

    def read_bytes(self, repo: str, ref: str, path: str) -> bytes:
        """Содержимое файла скилла (raw, без лимитов API)."""
        url = f"{RAW}/{repo}/{ref}/{quote(path)}"
        return self._fetch(url)

    def read_text(self, repo: str, ref: str, path: str) -> str:
        return self.read_bytes(repo, ref, path).decode("utf-8", "replace")

    def cached_skills(
        self, repo: str, ref: str, limit: int = MAX_SKILLS_PER_REPO
    ) -> list[StoreSkill] | None:
        """Уже разобранные скиллы этого репозитория (из памяти или кэша).

        Кэш помнит, с каким лимитом его сохранили: более щедрый запрос
        (догрузка поиска) перечитывает репозиторий, а не видит обрезок.
        """
        entry = self._skills.get((repo, ref))
        if entry is not None:
            kept, skills = entry
            return skills[:limit] if kept >= limit else None
        stored = self.cache_get("skills", repo, CACHE_TTL)
        if isinstance(stored, dict):
            kept = int(stored.get("limit") or 0)
            items = stored.get("skills")
        elif isinstance(stored, list):  # старый формат без лимита
            kept, items = len(stored), stored
        else:
            return None
        if not isinstance(items, list) or kept < limit:
            return None
        skills = [_skill_from_cache(item) for item in items]
        loaded = [skill for skill in skills if skill is not None]
        self._skills[(repo, ref)] = (kept, loaded)
        return loaded[:limit]

    def remember_skills(
        self,
        repo: str,
        ref: str,
        skills: Sequence[StoreSkill],
        limit: int = MAX_SKILLS_PER_REPO,
    ) -> None:
        self._skills[(repo, ref)] = (limit, list(skills))
        payload = {"limit": limit, "skills": [_skill_to_cache(skill) for skill in skills]}
        self.cache_put("skills", repo, payload)

    def forget_skills(self, repo: str) -> None:
        """Сбрасывает кэш репозитория (кнопка «Обновить» в магазине)."""
        for key in [key for key in self._skills if key[0] == repo]:
            del self._skills[key]
        for key in [key for key in self._plugins if key[0] == repo]:
            del self._plugins[key]
        self.cache_drop("skills", repo)
        self.cache_drop("plugins", repo)

    def cached_plugins(
        self, repo: str, ref: str, limit: int = MAX_PLUGINS_PER_REPO
    ) -> list[StorePlugin] | None:
        """Уже разобранные плагины этого репозитория (память или кэш на диске)."""
        entry = self._plugins.get((repo, ref))
        if entry is not None:
            kept, plugins = entry
            return plugins[:limit] if kept >= limit else None
        stored = self.cache_get("plugins", repo, CACHE_TTL)
        if not isinstance(stored, dict):
            return None
        kept = int(stored.get("limit") or 0)
        items = stored.get("plugins")
        if not isinstance(items, list) or kept < limit:
            return None
        loaded = [
            plugin for plugin in (_plugin_from_cache(item) for item in items)
            if plugin is not None
        ]
        self._plugins[(repo, ref)] = (kept, loaded)
        return loaded[:limit]

    def remember_plugins(
        self,
        repo: str,
        ref: str,
        plugins: Sequence[StorePlugin],
        limit: int = MAX_PLUGINS_PER_REPO,
    ) -> None:
        self._plugins[(repo, ref)] = (limit, list(plugins))
        payload = {
            "limit": limit,
            "plugins": [_plugin_to_cache(plugin) for plugin in plugins],
        }
        self.cache_put("plugins", repo, payload)

    # -------------------------------------------------------------- кэш на диске
    def _load_cache(self) -> dict[str, Any]:
        if self._cache is None:
            data: Any = {}
            if self.cache_path is not None and self.cache_path.is_file():
                try:
                    data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
            self._cache = data if isinstance(data, dict) else {}
        return self._cache

    def _save_cache(self) -> None:
        if self.cache_path is None or self._cache is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass

    def cache_get(self, kind: str, key: str, ttl: float) -> Any:
        entry = self._load_cache().get(f"{kind}:{key}")
        if not isinstance(entry, dict):
            return None
        stamp = entry.get("ts")
        if not isinstance(stamp, (int, float)) or time.time() - stamp > ttl:
            return None
        return entry.get("value")

    def cache_put(self, kind: str, key: str, value: Any) -> None:
        self._load_cache()[f"{kind}:{key}"] = {"ts": time.time(), "value": value}
        self._save_cache()

    def cache_drop(self, kind: str, key: str) -> None:
        if self._load_cache().pop(f"{kind}:{key}", None) is not None:
            self._save_cache()

    def search_repos(self, query: str, limit: int = 6) -> list[str]:
        """Имена репозиториев по запросу (``owner/name``), по звёздам."""
        query = (query or "").strip()
        if not query:
            return []
        url = (
            f"{API}/search/repositories?q={quote(query)}"
            f"&sort=stars&order=desc&per_page={max(1, min(int(limit), 30))}"
        )
        data = self._get_json(url)
        items = data.get("items") if isinstance(data, dict) else None
        names: list[str] = []
        for item in items or []:
            full = str(item.get("full_name") or "") if isinstance(item, dict) else ""
            if full and full not in names:
                names.append(full)
        return names[:limit]


# ---------------------------------------------------------------- разбор репозитория
def _is_ignored(path: str) -> bool:
    """Скрытые папки и node_modules скиллами не считаем."""
    return any(part.startswith(".") or part == "node_modules" for part in path.split("/"))


def _skill_files(entries: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Папки со ``SKILL.md`` → файлы этих папок (пути относительно папки)."""
    blobs: dict[str, list[str]] = {}
    for entry in entries:
        path = str(entry.get("path") or "")
        if not path or _is_ignored(path):
            continue
        blobs.setdefault(path, [])
    found: dict[str, list[str]] = {}
    for path in blobs:
        if path == SKILL_FILE:
            found[""] = [SKILL_FILE]
            continue
        if not path.endswith("/" + SKILL_FILE):
            continue
        folder = path[: -len(SKILL_FILE) - 1]
        prefix = folder + "/"
        files = [
            other[len(prefix):]
            for other in blobs
            if other.startswith(prefix) and not _is_ignored(other[len(prefix):])
        ]
        # SKILL.md первым: по нему сразу видно, что папка — скилл
        found[folder] = sorted(files, key=lambda name: (name != SKILL_FILE, name))
    return found


def _parallel(func: Callable[[T], R], items: Sequence[T], workers: int = WORKERS) -> list[R]:
    """Прогоняет сетевые вызовы параллельно (порядок результатов сохраняется)."""
    if len(items) <= 1:
        return [func(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(func, items))


def _skill_from_text(
    text: str,
    repo: str,
    ref: str,
    folder: str,
    files: list[str],
    stars: int = 0,
    repo_description: str = "",
    updated: str = "",
    plugin: str = "",
) -> StoreSkill | None:
    """Собирает карточку скилла из текста SKILL.md."""
    if not files:
        return None
    if len(files) > MAX_FILES:
        logger.warning(f"Скилл {repo}:{folder} пропущен — файлов больше {MAX_FILES}")
        return None
    skill_path = f"{folder}/{SKILL_FILE}" if folder else SKILL_FILE
    try:
        meta, body = parse_skill(text)
    except ValueError:
        logger.debug(f"Скилл {repo}:{skill_path} пропущен — нет frontmatter")
        return None
    name = str(meta.get("name") or Path(folder).name or repo.split("/")[-1]).strip()
    description = str(meta.get("description") or "").strip()
    if not description:
        description = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return StoreSkill(
        name=name or "skill",
        description=description[:300],
        repo=repo,
        path=folder,
        ref=ref,
        files=tuple(files),
        stars=stars,
        repo_description=repo_description,
        updated=updated,
        plugin=plugin,
    )


def _plugin_folders(entries: Iterable[dict[str, Any]]) -> list[str]:
    """Папки плагинов в дереве — там, где есть ``.claude-plugin/plugin.json``."""
    suffix = f"/{PLUGIN_DIR}/{PLUGIN_FILE}"
    root_path = suffix.lstrip("/")
    folders: list[str] = []
    for entry in entries:
        path = str(entry.get("path") or "")
        if path == root_path:
            folder = ""
        elif path.endswith(suffix):
            folder = path[: -len(suffix)]
        else:
            continue
        if folder not in folders:
            folders.append(folder)
    return folders


def _plugin_source(raw: Any) -> str:
    """Путь плагина из ``source`` маркетплейса (строка или объект с ``path``)."""
    value = raw.get("path") if isinstance(raw, dict) else raw
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _plugin_meta(
    client: GitHubClient, repo: str, ref: str, entries: Iterable[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Папка плагина → его данные (имя, описание, версия).

    Источники — ``marketplace.json`` и ``plugin.json`` самого плагина (второй
    уточняет первый). Манифесты читаются с raw — лимит GitHub API не тратится.
    Папки без имени в манифестах остаются с пустым именем: на карточке имя
    возьмётся из папки (``plugins/<имя>/…``).
    """
    entries = list(entries)
    folders = _plugin_folders(entries)
    if not folders:
        return {}
    meta: dict[str, dict[str, str]] = {folder: {} for folder in folders}
    market_path = f"{PLUGIN_DIR}/marketplace.json"
    if any(str(entry.get("path") or "") == market_path for entry in entries):
        try:
            data = json.loads(client.read_text(repo, ref, market_path))
        except (StoreError, ValueError):
            data = None
        listed = data.get("plugins") if isinstance(data, dict) else None
        for item in listed or []:
            if not isinstance(item, dict):
                continue
            folder = _plugin_source(item.get("source"))
            fields = meta.get(folder)
            if fields is None:  # плагин живёт не в этом дереве (внешний source)
                continue
            for key in ("name", "description", "version"):
                value = str(item.get(key) or "").strip()
                if value and not fields.get(key):
                    fields[key] = value

    def read(folder: str) -> dict[str, Any]:
        path = (
            f"{folder}/{PLUGIN_DIR}/{PLUGIN_FILE}"
            if folder
            else f"{PLUGIN_DIR}/{PLUGIN_FILE}"
        )
        try:
            data = json.loads(client.read_text(repo, ref, path))
        except (StoreError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # манифест самого плагина читаем только там, где маркетплейс не дал полных данных
    need = [
        folder
        for folder in folders
        if not (meta[folder].get("name") and meta[folder].get("version"))
    ]
    for folder, data in zip(need, _parallel(read, need), strict=True):
        for key in ("name", "description", "version"):
            value = str(data.get(key) or "").strip()
            if value and not meta[folder].get(key):
                meta[folder][key] = value
    return meta


def _plugin_names(
    client: GitHubClient, repo: str, ref: str, entries: Iterable[dict[str, Any]]
) -> dict[str, str]:
    """Папка плагина → его имя (для пометок ``плагин`` на карточках скиллов)."""
    return {
        folder: fields["name"]
        for folder, fields in _plugin_meta(client, repo, ref, entries).items()
        if fields.get("name")
    }


def _default_plugin_name(folder: str, repo: str) -> str:
    """Имя плагина без манифеста: хвост папки, для корня — имя репозитория."""
    tail = folder.split("/")[-1] if folder else ""
    return tail or repo.split("/")[-1] or "plugin"


def _plugin_of(path: str, names: Mapping[str, str]) -> str:
    """Имя плагина, внутри которого лежит скилл (пусто — обычный скилл)."""
    best, best_name = "", ""
    for folder, name in names.items():
        if not name:
            continue
        if folder and path != folder and not path.startswith(folder + "/"):
            continue
        if len(folder) >= len(best):
            best, best_name = folder, name
    if best_name:
        return best_name
    parts = path.split("/")
    if "plugins" in parts:
        index = parts.index("plugins")
        if index < len(parts) - 1 and parts[index + 1]:
            return parts[index + 1]
    return ""


def _spread(
    folders: list[tuple[str, list[str]]],
    plugins: Mapping[str, str],
    limit: int,
) -> list[tuple[str, list[str]]]:
    """Чередует плагины набора: иначе лимит забивает один первый плагин."""
    groups: dict[str, list[tuple[str, list[str]]]] = {}
    order: list[str] = []
    for folder, files in folders:
        key = _plugin_of(folder, plugins) or f"~{folder}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((folder, files))
    picked: list[tuple[str, list[str]]] = []
    index = 0
    while len(picked) < limit:
        added = False
        for key in order:
            if index < len(groups[key]):
                picked.append(groups[key][index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1
    return picked


def repo_skills(
    client: GitHubClient,
    repo: str,
    ref: str = "",
    limit: int = MAX_SKILLS_PER_REPO,
) -> list[StoreSkill]:
    """Все скиллы одного репозитория (по дереву и SKILL.md)."""
    # кэш проверяем до сетевых запросов: вкладка магазина должна открываться без API
    cached = client.cached_skills(repo, ref, limit)
    if cached is not None:
        return cached
    info: dict[str, Any] = {"ref": ref, "stars": 0, "description": ""}
    if not ref:
        info = client.repo_info(repo)
    branch = ref or str(info.get("ref") or "main")

    entries = client.tree(repo, branch)
    found = _skill_files(entries)
    if not any(files for files in found.values()):
        client.remember_skills(repo, branch, [], limit)
        return []
    plugins = _plugin_names(client, repo, branch, entries)
    folders = _spread(
        [(folder, files) for folder, files in sorted(found.items()) if files],
        plugins,
        limit,
    )

    def read(item: tuple[str, list[str]]) -> str:
        folder = item[0]
        path = f"{folder}/{SKILL_FILE}" if folder else SKILL_FILE
        try:
            return client.read_text(repo, branch, path)
        except StoreError:
            return ""

    texts = _parallel(read, folders)
    skills: list[StoreSkill] = []
    for (folder, files), text in zip(folders, texts, strict=True):
        if not text:
            continue
        skill = _skill_from_text(
            text,
            repo,
            branch,
            folder,
            files,
            stars=int(info.get("stars") or 0),
            repo_description=str(info.get("description") or ""),
            updated=str(info.get("updated") or ""),
            plugin=_plugin_of(folder, plugins),
        )
        if skill is not None:
            skills.append(skill)
    client.remember_skills(repo, branch, skills, limit)
    return skills


def repo_plugins(
    client: GitHubClient,
    repo: str,
    ref: str = "",
    limit: int = MAX_PLUGINS_PER_REPO,
) -> list[StorePlugin]:
    """Все плагины одного репозитория (каждая папка с манифестом).

    У плагина файлы лежат как есть: ``skills/``, ``commands/``, ``agents/``,
    ``hooks/`` — поэтому установка забирает пакет целиком, а не выбирает
    один скилл из него.
    """
    cached = client.cached_plugins(repo, ref, limit)
    if cached is not None:
        return cached
    info: dict[str, Any] = {"ref": ref, "stars": 0, "description": ""}
    if not ref:
        info = client.repo_info(repo)
    branch = ref or str(info.get("ref") or "main")

    entries = client.tree(repo, branch)
    meta = _plugin_meta(client, repo, branch, entries)
    blobs = [
        str(entry.get("path") or "")
        for entry in entries
        if str(entry.get("type") or "blob") != "tree"
    ]
    blobs = [path for path in blobs if path]
    manifest = f"{PLUGIN_DIR}/{PLUGIN_FILE}"
    plugins: list[StorePlugin] = []
    for folder, fields in sorted(meta.items()):
        prefix = f"{folder}/" if folder else ""
        files: list[str] = []
        for path in blobs:
            if prefix and not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if not rel or _is_ignored(rel):
                continue
            files.append(rel)
        if manifest in files:
            files.remove(manifest)
        files.insert(0, manifest)
        if len(files) > MAX_PLUGIN_FILES:
            logger.warning(
                f"Плагин {repo}:{folder} пропущен — файлов больше {MAX_PLUGIN_FILES}"
            )
            continue
        plugins.append(
            StorePlugin(
                name=fields.get("name") or _default_plugin_name(folder, repo),
                description=fields.get("description") or "",
                repo=repo,
                path=folder,
                ref=branch,
                files=tuple(files),
                stars=int(info.get("stars") or 0),
                repo_description=str(info.get("description") or ""),
                updated=str(info.get("updated") or ""),
                version=fields.get("version") or "",
                skills=sum(
                    1
                    for name in files
                    if name == SKILL_FILE or name.endswith("/" + SKILL_FILE)
                ),
                commands=sum(
                    1
                    for name in files
                    if name.startswith("commands/") and name.endswith(".md")
                ),
                agents=sum(
                    1
                    for name in files
                    if name.startswith("agents/") and name.endswith(".md")
                ),
            )
        )
    plugins.sort(key=lambda plugin: plugin.name.lower())
    plugins = plugins[:limit]
    client.remember_plugins(repo, branch, plugins, limit)
    return plugins


def _scan_repos(
    client: GitHubClient, repos: Sequence[str], per_repo: int
) -> list[StoreSkill]:
    """Скиллы нескольких репозиториев сразу (по сети — параллельно)."""

    def scan(repo: str) -> list[StoreSkill]:
        try:
            return repo_skills(client, repo, limit=per_repo)
        except StoreError as exc:
            logger.debug(f"Магазин: {repo} пропущен — {exc}")
            return []

    found: list[StoreSkill] = []
    for chunk in _parallel(scan, list(repos)):
        found.extend(chunk)
    return found


def _by_stars(skills: Iterable[StoreSkill]) -> list[StoreSkill]:
    """Сортировка витрины: сначала скиллы из самых звёздных репозиториев."""
    return sorted(skills, key=lambda s: (-s.stars, s.name.lower()))


def _by_freshness(skills: Iterable[StoreSkill]) -> list[StoreSkill]:
    """Сортировка по новизне: свежие пуши репозитория — наверху.

    Даты ISO сравниваются как строки; одинаковые даты разводятся звёздами
    (два устойчивых прохода, чтобы не терять порядок при равенстве).
    """
    ordered = _by_stars(skills)
    return sorted(ordered, key=lambda s: s.updated or "", reverse=True)


def _with_freshness(
    client: GitHubClient, skills: Iterable[StoreSkill]
) -> list[StoreSkill]:
    """Дополняет даты пуша у скиллов из кэша, записанного до их появления."""
    skills = list(skills)
    missing = {skill.repo for skill in skills if not skill.updated}
    if not missing:
        return skills
    dates: dict[str, str] = {}
    for repo in missing:
        try:
            dates[repo] = str(client.repo_info(repo).get("updated") or "")
        except StoreError as exc:
            logger.debug(f"Магазин: {repo} без даты обновления — {exc}")
            dates[repo] = ""
    return [
        skill if skill.updated else replace(skill, updated=dates.get(skill.repo, ""))
        for skill in skills
    ]


def sort_skills(skills: Iterable[StoreSkill], by: str = "stars") -> list[StoreSkill]:
    """Порядок витрины: ``stars`` — по звёздам, ``new`` — по свежести."""
    return _by_freshness(skills) if by == "new" else _by_stars(skills)


def _dedupe(skills: Iterable[StoreSkill]) -> list[StoreSkill]:
    """Убирает копии одного скилла, но не разные плагины с одинаковым именем.

    Одинаковые имена из разных плагинов — разные скиллы набора: если их
    схлопывать, из пака пропадала бы половина состава.
    """
    unique: list[StoreSkill] = []
    seen: set[tuple[str, str]] = set()
    for skill in skills:
        key = (skill.name.lower(), skill.plugin.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(skill)
    return unique


def _search_queries(query: str) -> tuple[str, ...]:
    """Запросы к GitHub: сначала репозитории с темой скиллов, потом свободный."""
    return (
        f"{query} topic:claude-skills",
        f"{query} topic:agent-skills",
        f"{query} SKILL.md in:readme",
    )


def _topic_repos(client: GitHubClient, topic: str, limit: int = 8) -> list[str]:
    """Репозитории темы (с кэшем: темы — самые «дорогие» запросы к GitHub)."""
    key = f"topic:{topic}"
    cached = client.cache_get("search", key, CACHE_TTL)
    if isinstance(cached, list):
        return [str(repo) for repo in cached][:limit]
    repos = client.search_repos(key, limit=limit)
    client.cache_put("search", key, repos)
    return repos


def search(
    client: GitHubClient,
    query: str,
    limit_repos: int = 5,
    per_repo: int = 8,
) -> list[StoreSkill]:
    """Поиск скиллов: репозитории по запросу → их скиллы."""
    query = (query or "").strip()
    if not query:
        return []
    repos: list[str] = []
    # лимит входит в ключ: расширение охвата не должно натыкаться на узкий кэш
    cache_key = f"{query.lower()}|{limit_repos}"
    cached = client.cache_get("search", cache_key, SEARCH_TTL)
    if isinstance(cached, list):
        repos = [str(repo) for repo in cached]
    else:
        for github_query in _search_queries(query):
            try:
                found = client.search_repos(github_query, limit=limit_repos)
            except StoreError as exc:
                logger.debug(f"Магазин: поиск «{github_query}» — {exc}")
                found = []
            for repo in found:
                if repo not in repos:
                    repos.append(repo)
            if len(repos) >= limit_repos:
                break
        if repos:
            client.cache_put("search", cache_key, repos)
    return _by_stars(_dedupe(_scan_repos(client, repos[:limit_repos], per_repo)))


def recommended_pool(
    client: GitHubClient,
    limit: int = 24,
    repos_limit: int = 8,
    refresh: bool = False,
) -> list[StoreSkill]:
    """Весь каталог витрины: скиллы популярных репозиториев, по звёздам.

    ``repos_limit`` расширяет охват: магазин доливает список при прокрутке
    (8 → 14 → 20 → 26 репозиториев), ``refresh`` сбрасывает кэш репозиториев.
    """
    repos: list[str] = list(RECOMMENDED_REPOS)
    for topic in RECOMMENDED_TOPICS:
        try:
            repos.extend(_topic_repos(client, topic))
        except StoreError as exc:
            logger.debug(f"Магазин: тема {topic} не найдена — {exc}")
    unique: list[str] = []
    for repo in repos:
        if repo not in unique:
            unique.append(repo)
    if refresh:
        for repo in unique[:repos_limit]:
            client.forget_skills(repo)

    pool = _dedupe(_scan_repos(client, unique[:repos_limit], limit))
    if not pool:
        raise StoreError(
            "Не удалось загрузить каталог: проверьте интернет или задайте GITHUB_TOKEN."
        )
    return _by_stars(_with_freshness(client, pool))


def recommended(
    client: GitHubClient,
    limit: int = 24,
    sample: int = 40,
    repos_limit: int = 8,
    refresh: bool = False,
) -> list[StoreSkill]:
    """Рекомендуемые: случайная выборка скиллов из популярных репозиториев.

    Выборка каждый раз новая (витрина не должна выглядеть одинаково), но
    порядок всегда по звёздам: наверху — самые популярные репозитории.
    """
    pool = recommended_pool(
        client, limit=limit, repos_limit=repos_limit, refresh=refresh
    )
    rng = random.SystemRandom()
    picked = rng.sample(pool, k=min(sample, len(pool)))
    return _by_stars(picked)


# ---------------------------------------------------------------- плагины
def _scan_plugin_repos(
    client: GitHubClient, repos: Sequence[str], per_repo: int
) -> list[StorePlugin]:
    """Плагины нескольких репозиториев сразу (по сети — параллельно)."""

    def scan(repo: str) -> list[StorePlugin]:
        try:
            return repo_plugins(client, repo, limit=per_repo)
        except StoreError as exc:
            logger.debug(f"Магазин: плагины {repo} пропущены — {exc}")
            return []

    found: list[StorePlugin] = []
    for chunk in _parallel(scan, list(repos)):
        found.extend(chunk)
    return found


def _dedupe_plugins(plugins: Iterable[StorePlugin]) -> list[StorePlugin]:
    """Убирает повтор одной и той же папки (одно имя из разных репо — разное)."""
    unique: list[StorePlugin] = []
    seen: set[tuple[str, str, str]] = set()
    for plugin in plugins:
        key = (plugin.name.lower(), plugin.repo.lower(), plugin.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(plugin)
    return unique


def _by_stars_plugins(plugins: Iterable[StorePlugin]) -> list[StorePlugin]:
    """Сортировка плагинов: сначала репозитории с большим числом звёзд."""
    return sorted(plugins, key=lambda p: (-p.stars, p.name.lower()))


def _spread_plugins(plugins: Sequence[StorePlugin], limit: int) -> list[StorePlugin]:
    """Чередует репозитории: витрина из одного огромного маркетплейса скучна."""
    groups: dict[str, list[StorePlugin]] = {}
    order: list[str] = []
    for plugin in plugins:
        if plugin.repo not in groups:
            groups[plugin.repo] = []
            order.append(plugin.repo)
        groups[plugin.repo].append(plugin)
    order.sort(key=lambda repo: -max(p.stars for p in groups[repo]))
    picked: list[StorePlugin] = []
    index = 0
    while len(picked) < limit:
        added = False
        for repo in order:
            if index < len(groups[repo]):
                picked.append(groups[repo][index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1
    return picked


def _plugin_search_queries(query: str) -> tuple[str, ...]:
    """Запросы к GitHub: сначала темы плагинов, потом упоминания маркетплейса."""
    return (
        f"{query} topic:claude-code-plugin",
        f"{query} topic:claude-plugins",
        f"{query} claude plugin marketplace in:readme",
    )


def search_plugins(
    client: GitHubClient,
    query: str,
    limit_repos: int = 5,
    per_repo: int = 8,
) -> list[StorePlugin]:
    """Поиск плагинов: репозитории по запросу → их плагины, по звёздам."""
    query = (query or "").strip()
    if not query:
        return []
    repos: list[str] = []
    cache_key = f"plugins|{query.lower()}|{limit_repos}"
    cached = client.cache_get("search", cache_key, SEARCH_TTL)
    if isinstance(cached, list):
        repos = [str(repo) for repo in cached]
    else:
        for github_query in _plugin_search_queries(query):
            try:
                found = client.search_repos(github_query, limit=limit_repos)
            except StoreError as exc:
                logger.debug(f"Магазин: поиск плагинов «{github_query}» — {exc}")
                found = []
            for repo in found:
                if repo not in repos:
                    repos.append(repo)
            if len(repos) >= limit_repos:
                break
        if repos:
            client.cache_put("search", cache_key, repos)
    found_plugins = _scan_plugin_repos(client, repos[:limit_repos], per_repo)
    return _by_stars_plugins(_dedupe_plugins(found_plugins))


def recommended_plugins(
    client: GitHubClient,
    limit: int = 24,
    repos_limit: int = 8,
    refresh: bool = False,
) -> list[StorePlugin]:
    """Рекомендуемые плагины: маркетплейсы и темы, поровну от каждого репо."""
    repos: list[str] = list(PLUGIN_RECOMMENDED_REPOS)
    for topic in PLUGIN_RECOMMENDED_TOPICS:
        try:
            repos.extend(_topic_repos(client, topic))
        except StoreError as exc:
            logger.debug(f"Магазин: тема {topic} не найдена — {exc}")
    unique: list[str] = []
    for repo in repos:
        if repo not in unique:
            unique.append(repo)
    if refresh:
        for repo in unique[:repos_limit]:
            client.forget_skills(repo)
    pool = _dedupe_plugins(
        _scan_plugin_repos(client, unique[:repos_limit], max(limit, 8))
    )
    if not pool:
        raise StoreError(
            "Не удалось загрузить плагины: проверьте интернет или задайте GITHUB_TOKEN."
        )
    return _spread_plugins(_by_stars_plugins(pool), limit)


# ---------------------------------------------------------------- установка
def installed_names(directory: str | Path | None = None) -> set[str]:
    """Имена уже установленных скиллов (без встроенных)."""
    return {skill.name.lower() for skill in load_skills(directory, builtin=False)}


def is_installed(skill: StoreSkill, directory: str | Path | None = None) -> bool:
    """Есть ли уже этот скилл в папке скиллов (по источнику, а не по имени)."""
    root = skills_root(directory)
    origins = _read_origins(directory)
    for slug, entry in origins.items():
        if (
            isinstance(entry, dict)
            and entry.get("repo") == skill.repo
            and entry.get("path") == skill.path
            and entry.get("kind", "skill") != "plugin"
            and (root / slug).is_dir()
        ):
            return True
    base = skill.slug
    # папку с этим именем занял другой скилл из магазина — это не «наш» скилл
    return (root / base).is_dir() and base not in origins


def _slug_candidates(skill: StoreSkill) -> list[str]:
    """Имя папки и запасные варианты, если имя занято другим скиллом."""
    options = [skill.slug]
    if skill.plugin:
        options.append(slugify(f"{skill.plugin}-{skill.slug}"))
    tail = skill.repo.split("/")[-1]
    if tail:
        options.append(slugify(f"{tail}-{skill.slug}"))
    unique: list[str] = []
    for slug in options:
        if slug not in unique:
            unique.append(slug)
    return unique


def install_slug(skill: StoreSkill, directory: str | Path | None = None) -> str:
    """Папка, в которую встанет скилл: имя, а при занятости — с плагином.

    Одинаковые имена из разных плагинов не должны затирать друг друга: второй
    скилл получает папку ``<плагин>-<имя>``, а если занята и она —
    ``<репозиторий>-<имя>``. Уже установленный отсюда скилл остаётся на месте.
    """
    root = skills_root(directory)
    origins = _read_origins(directory)
    candidates = _slug_candidates(skill)
    for slug in candidates:
        entry = origins.get(slug)
        if isinstance(entry, dict):
            if (
                entry.get("repo") == skill.repo
                and entry.get("path") == skill.path
                and entry.get("kind", "skill") != "plugin"
            ):
                return slug
            continue
        if not (root / slug).exists():
            return slug
    return candidates[-1]


def _safe_rel(path: str) -> Path:
    """Относительный путь внутри папки скилла (без ``..`` и абсолютных путей)."""
    rel = Path(str(path).replace("\\", "/"))
    if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
        raise StoreError("В скилле недопустимый путь файла.")
    return rel


_INVALID_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def install(
    skill: StoreSkill,
    directory: str | Path | None = None,
    client: GitHubClient | None = None,
) -> Skill:
    """Скачивает скилл в папку скиллов и возвращает его с диска.

    Скачивание идёт во временную папку и только потом переименовывается —
    недокачанный скилл не попадёт в список. Существующий скилл с тем же
    именем не перезаписывается: свои правки важнее.
    """
    client = client or GitHubClient()
    root = skills_root(directory)
    slug = install_slug(skill, directory)
    target = root / slug
    if target.exists():
        raise StoreError(f"Скилл «{slug}» уже установлен: {target.as_posix()}")
    if not skill.files:
        raise StoreError("В скилле нет файлов для скачивания.")

    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{slug}.installing"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _download(skill, tmp, client, slug)
        if target.exists():
            raise StoreError(f"Скилл «{slug}» уже установлен: {target.as_posix()}")
        tmp.replace(target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    remember_origin(directory, slug, skill)
    logger.info(f"Магазин: скилл «{slug}» установлен из {skill.repo} → {target}")
    installed = get_skill(slug, directory)
    if installed is not None:
        return installed
    return Skill(name=slug, description=skill.description, path=target)


def _origins_path(directory: str | Path | None = None) -> Path:
    return skills_root(directory) / ORIGIN_FILE


def _read_origins(directory: str | Path | None = None) -> dict[str, Any]:
    """Карта «имя папки → источник» (битый или чужой файл — пустая карта)."""
    path = _origins_path(directory)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_origins(directory: str | Path | None, data: dict[str, Any]) -> None:
    """Сохраняет карту источников атомарно (сбой записи не ломает установку)."""
    path = _origins_path(directory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug(f"Магазин: не удалось запомнить источники: {exc}")


def remember_origin(
    directory: str | Path | None,
    slug: str,
    source: StoreSkill | StorePlugin,
    kind: str = "skill",
) -> None:
    """Запоминает, откуда скачан скилл или плагин — чтобы обновлять его потом."""
    data = _read_origins(directory)
    data[slug] = {
        "repo": source.repo,
        "path": source.path,
        "name": source.name,
        "kind": kind,
    }
    _write_origins(directory, data)


def forget_origin(directory: str | Path | None, slug: str) -> None:
    """Забывает источник удалённого скилла или плагина."""
    data = _read_origins(directory)
    if data.pop(slug, None) is not None:
        _write_origins(directory, data)


def remember_local_origin(
    directory: str | Path | None,
    slug: str,
    source: str | Path,
    name: str,
    kind: str = "plugin",
) -> None:
    """Запоминает локальный источник: из него же потом берётся обновление."""
    data = _read_origins(directory)
    data[slug] = {
        "repo": "",
        "path": "",
        "name": name,
        "kind": kind,
        "local": str(Path(source).resolve()),
    }
    _write_origins(directory, data)


def origin_of(directory: str | Path | None, slug: str) -> dict[str, str] | None:
    """Источник установленного скилла или плагина (``None`` — поставлен вручную).

    У плагина из файла вместо ``repo`` заполнено ``local`` — путь к архиву
    или папке, откуда он поставлен.
    """
    entry = _read_origins(directory).get(slug)
    if not isinstance(entry, dict) or not (entry.get("repo") or entry.get("local")):
        return None
    origin = {key: str(entry.get(key) or "") for key in ("repo", "path", "name")}
    origin["local"] = str(entry.get("local") or "")
    origin["kind"] = str(entry.get("kind") or "skill")
    return origin


def _folder_digest(folder: Path) -> str:
    """Отпечаток содержимого папки: имена файлов и их байты."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(folder).as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
        digest.update(b"\0")
    return digest.hexdigest()


def _rename_skill(folder: Path, slug: str) -> None:
    """Ставит скиллу имя папки: два «review» из разных плагинов не слипнутся."""
    path = folder / SKILL_FILE
    meta, body = parse_skill(path.read_text(encoding="utf-8"))
    meta["name"] = slug
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")


def _download_files(
    client: GitHubClient,
    repo: str,
    ref: str,
    folder: str,
    files: Iterable[str],
    tmp: Path,
) -> None:
    """Скачивает файлы папки во временную директорию, проверяя пути и размер."""
    tmp.mkdir(parents=True)
    for rel in files:
        name = _safe_rel(rel)
        remote = f"{folder}/{name.as_posix()}" if folder else name.as_posix()
        data = client.read_bytes(repo, ref, remote)
        if len(data) > MAX_FILE_BYTES:
            raise StoreError(f"Файл {name.as_posix()} слишком большой.")
        dest = tmp / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _download(
    skill: StoreSkill, tmp: Path, client: GitHubClient, slug: str = ""
) -> None:
    """Скачивает все файлы скилла во временную папку и проверяет SKILL.md."""
    _download_files(client, skill.repo, skill.ref, skill.path, skill.files, tmp)
    skill_file = tmp / SKILL_FILE
    if not skill_file.is_file():
        raise StoreError("В скилле нет SKILL.md.")
    parse_skill(skill_file.read_text(encoding="utf-8"))
    if slug and slug != skill.slug:
        _rename_skill(tmp, slug)


def _download_plugin(
    plugin: StorePlugin, tmp: Path, client: GitHubClient
) -> None:
    """Скачивает плагин целиком и проверяет, что манифест на месте."""
    _download_files(client, plugin.repo, plugin.ref, plugin.path, plugin.files, tmp)
    manifest = tmp / PLUGIN_DIR / PLUGIN_FILE
    if not manifest.is_file():
        raise StoreError("В плагине нет .claude-plugin/plugin.json.")


def update(
    skill: StoreSkill,
    directory: str | Path | None = None,
    client: GitHubClient | None = None,
) -> tuple[Skill, bool]:
    """Обновляет установленный скилл с GitHub (возвращает скилл и «изменился ли»)."""
    client = client or GitHubClient()
    root = skills_root(directory)
    slug = install_slug(skill, directory)
    target = root / slug
    if not target.is_dir():
        raise StoreError(f"Скилл «{slug}» не установлен.")
    if not skill.files:
        raise StoreError("В скилле нет файлов для скачивания.")

    tmp = root / f".{slug}.updating"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _download(skill, tmp, client, slug)
        if _folder_digest(tmp) == _folder_digest(target):
            shutil.rmtree(tmp, ignore_errors=True)
            fresh = get_skill(slug, directory)
            return (
                fresh
                or Skill(name=slug, description=skill.description, path=target),
                False,
            )
        backup = root / f".{slug}.backup"
        shutil.rmtree(backup, ignore_errors=True)
        target.replace(backup)
        try:
            tmp.replace(target)
        except OSError:
            backup.replace(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    remember_origin(directory, slug, skill)
    logger.info(f"Магазин: скилл «{slug}» обновлён из {skill.repo}")
    fresh = get_skill(slug, directory)
    return (
        fresh or Skill(name=slug, description=skill.description, path=target),
        True,
    )


def update_installed(
    name: str,
    directory: str | Path | None = None,
    client: GitHubClient | None = None,
) -> tuple[Skill, bool]:
    """Обновляет установленный скилл, находя свежую версию по запомненному источнику."""
    client = client or GitHubClient()
    slug = slugify(name)
    origin = origin_of(directory, slug)
    if origin is None:
        # скилл из набора мог встать в папку с именем плагина
        for entry in _read_origins(directory).values():
            if not isinstance(entry, dict) or not (
                entry.get("repo") or entry.get("local")
            ):
                continue
            if slugify(str(entry.get("name") or "")) == slug:
                origin = {
                    key: str(entry.get(key) or "") for key in ("repo", "path", "name")
                }
                origin["local"] = str(entry.get("local") or "")
                break
    if origin is None:
        raise StoreError(
            "Неизвестно, откуда скачан этот скилл — обновить его с GitHub нельзя."
        )
    if not origin.get("repo"):
        # скилл из файла: обновляем из того же архива или папки
        if not origin.get("local"):
            raise StoreError("У скилла не записан источник — обновить его нельзя.")
        local = Path(origin["local"])
        if not local.exists():
            raise StoreError(f"Локальный источник пропал: {local.as_posix()}")
        return install_skill_local(local, directory, replace=True, slug=slug)
    fresh: StoreSkill | None = None
    found = repo_skills(client, origin["repo"])
    for skill in found:
        if origin["path"] and skill.path == origin["path"]:
            fresh = skill
            break
    for skill in found:
        if fresh is not None:
            break
        if skill.name.lower() == name.lower() or skill.slug == slug:
            fresh = skill
    if fresh is None:
        raise StoreError("Скилл больше не найден в репозитории на GitHub.")
    return update(fresh, directory, client)


@dataclass(frozen=True)
class InstalledPlugin:
    """Плагин, который уже лежит в папке скиллов."""

    #: имя папки плагина относительно корня скиллов
    folder: str
    name: str
    description: str
    version: str
    path: Path
    skills: int = 0
    commands: int = 0
    agents: int = 0
    #: известен источник — значит, плагин можно обновить с GitHub
    tracked: bool = False
    #: путь локального архива или папки (пусто — поставлен не из файла)
    local: str = ""


def plugin_slug(
    plugin: StorePlugin, directory: str | Path | None = None
) -> str:
    """Папка, в которую встанет плагин: имя, а при занятости — с репозиторием."""
    root = skills_root(directory)
    origins = _read_origins(directory)
    options = [plugin.slug]
    tail = plugin.repo.split("/")[-1]
    if tail:
        options.append(slugify(f"{tail}-{plugin.slug}"))
    unique: list[str] = []
    for slug in options:
        if slug not in unique:
            unique.append(slug)
    for slug in unique:
        entry = origins.get(slug)
        if isinstance(entry, dict):
            if (
                entry.get("repo") == plugin.repo
                and entry.get("path") == plugin.path
                and entry.get("kind") == "plugin"
            ):
                return slug
            continue
        if not (root / slug).exists():
            return slug
    return unique[-1]


def is_plugin_installed(
    plugin: StorePlugin, directory: str | Path | None = None
) -> bool:
    """Есть ли уже этот плагин в папке скиллов."""
    root = skills_root(directory)
    for slug, entry in _read_origins(directory).items():
        if (
            isinstance(entry, dict)
            and entry.get("kind") == "plugin"
            and entry.get("repo") == plugin.repo
            and entry.get("path") == plugin.path
            and (root / slug).is_dir()
        ):
            return True
    for slug, entry in _read_origins(directory).items():
        if not isinstance(entry, dict) or entry.get("kind") != "plugin":
            continue
        if not entry.get("local") or not (root / slug).is_dir():
            continue
        same_name = str(entry.get("name") or "").strip().lower() == plugin.name.lower()
        if slug == plugin.slug or same_name:
            return True
    target = root / plugin.slug
    return (
        (target / PLUGIN_DIR / PLUGIN_FILE).is_file()
        and plugin.slug not in _read_origins(directory)
    )


def install_plugin(
    plugin: StorePlugin,
    directory: str | Path | None = None,
    client: GitHubClient | None = None,
) -> str:
    """Ставит плагин целиком в отдельную папку и возвращает её имя.

    Папка плагина атомарна: недокачанный пакет не появится в списке скиллов.
    Команды и агенты внутри неё Немуся подхватывает сама (см.
    ``nemus.core.skills``).
    """
    client = client or GitHubClient()
    root = skills_root(directory)
    slug = plugin_slug(plugin, directory)
    target = root / slug
    if target.exists():
        raise StoreError(f"Плагин «{slug}» уже установлен: {target.as_posix()}")
    if not plugin.files:
        raise StoreError("В плагине нет файлов для скачивания.")

    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{slug}.installing"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _download_plugin(plugin, tmp, client)
        if target.exists():
            raise StoreError(f"Плагин «{slug}» уже установлен: {target.as_posix()}")
        tmp.replace(target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    remember_origin(directory, slug, plugin, kind="plugin")
    logger.info(f"Магазин: плагин «{slug}» установлен из {plugin.repo} → {target}")
    return slug


def _zip_roots(archive: zipfile.ZipFile) -> list[str]:
    """Папки-корни плагинов в архиве (пустая строка — плагин лежит в корне)."""
    suffix = f"{PLUGIN_DIR}/{PLUGIN_FILE}"
    roots: list[str] = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if "__MACOSX" in name or name.endswith("/"):
            continue
        if name == suffix:
            root = ""
        elif name.endswith("/" + suffix):
            root = name[: -len(suffix) - 1]
        else:
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _archive_manifest(archive: zipfile.ZipFile, root: str) -> dict[str, Any]:
    """Манифест плагина из архива (``root`` — папка плагина внутри)."""
    path = f"{root}/{PLUGIN_DIR}/{PLUGIN_FILE}" if root else f"{PLUGIN_DIR}/{PLUGIN_FILE}"
    try:
        data = json.loads(archive.read(path).decode("utf-8", "replace"))
    except (KeyError, ValueError) as exc:
        raise StoreError("У плагина битый .claude-plugin/plugin.json.") from exc
    return data if isinstance(data, dict) else {}


def _folder_copy(
    source: Path, tmp: Path, limit: int = MAX_PLUGIN_FILES, noun: str = "плагин"
) -> None:
    """Копирует папку во временную, не пуская служебные каталоги."""
    count = 0
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(part in LOCAL_SKIP for part in rel.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue  # ссылки наружу не тащим
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            raise StoreError(f"Файл {rel.as_posix()} слишком большой.")
        count += 1
        if count > limit:
            raise StoreError(
                f"В папке больше {limit} файлов — это не один {noun}."
            )
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def _archive_copy(
    archive: zipfile.ZipFile,
    root: str,
    tmp: Path,
    limit: int = MAX_PLUGIN_FILES,
    noun: str = "плагин",
) -> None:
    """Распаковывает одну папку из архива, отбрасывая чужие файлы."""
    prefix = f"{root}/" if root else ""
    count = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix) :]
        parts = PurePosixPath(rel).parts
        if not parts or rel.startswith("/") or ".." in parts:
            raise StoreError(f"В архиве недопустимый путь: {rel}")
        if any(part in LOCAL_SKIP for part in parts):
            continue
        if info.file_size > MAX_FILE_BYTES:
            raise StoreError(f"Файл {rel} слишком большой.")
        count += 1
        if count > limit:
            raise StoreError(
                f"В папке больше {limit} файлов — это не один {noun}."
            )
        dest = tmp.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)


def install_plugin_local(
    source: str | Path,
    directory: str | Path | None = None,
    *,
    replace: bool = False,
    slug: str = "",
) -> tuple[str, bool]:
    """Ставит плагин из папки или ``.zip`` и запоминает локальный источник.

    В папке или архиве должен быть ровно один плагин — папка с
    ``.claude-plugin/plugin.json`` (если в архиве плагин обёрнут в папку,
    обёртка отбрасывается). Возвращает имя папки и «изменилось ли».
    """
    src = Path(os.path.expanduser(str(source))).resolve()
    if not src.exists():
        raise StoreError(f"Файл или папка не найдены: {src.as_posix()}")

    archive: zipfile.ZipFile | None = None
    root = ""
    if src.is_dir():
        if not (src / PLUGIN_DIR / PLUGIN_FILE).is_file():
            raise StoreError(
                "В папке нет плагина: нужен .claude-plugin/plugin.json."
            )
        try:
            manifest = json.loads(
                (src / PLUGIN_DIR / PLUGIN_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise StoreError("У плагина битый .claude-plugin/plugin.json.") from exc
        manifest = manifest if isinstance(manifest, dict) else {}
    elif src.suffix.lower() == ".zip":
        try:
            archive = zipfile.ZipFile(src)
        except (OSError, zipfile.BadZipFile) as exc:
            raise StoreError(f"Не удалось открыть архив: {exc}") from exc
        roots = _zip_roots(archive)
        if not roots:
            archive.close()
            raise StoreError(
                "В архиве нет плагина: нужен .claude-plugin/plugin.json."
            )
        if len(roots) > 1:
            archive.close()
            raise StoreError(
                "В архиве несколько плагинов — распакуйте нужный отдельно."
            )
        root = roots[0]
        manifest = _archive_manifest(archive, root)
    else:
        raise StoreError("Нужен .zip или папка с .claude-plugin/plugin.json.")

    fallback = src.stem if src.is_file() else src.name
    fallback = re.sub(r"[-_](main|master)$", "", fallback, flags=re.IGNORECASE)
    name = str(manifest.get("name") or fallback).strip() or fallback
    folder = slug or (slugify(name) if name else "") or "plugin"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", folder):
        raise StoreError(f"Неподходящее имя папки плагина: {folder}")

    base = skills_root(directory)
    target = base / folder
    if target.exists() and not replace:
        raise StoreError(f"Плагин «{folder}» уже установлен: {target.as_posix()}")
    if not target.exists() and replace:
        raise StoreError(f"Плагин «{folder}» не установлен.")

    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".{folder}.installing"
    shutil.rmtree(tmp, ignore_errors=True)
    changed = True
    try:
        if archive is not None:
            try:
                _archive_copy(archive, root, tmp)
            finally:
                archive.close()
        else:
            _folder_copy(src, tmp)
        if not (tmp / PLUGIN_DIR / PLUGIN_FILE).is_file():
            raise StoreError("В плагине нет .claude-plugin/plugin.json.")
        if replace:
            if _folder_digest(tmp) == _folder_digest(target):
                shutil.rmtree(tmp, ignore_errors=True)
                changed = False
            else:
                backup = base / f".{folder}.backup"
                shutil.rmtree(backup, ignore_errors=True)
                target.replace(backup)
                try:
                    tmp.replace(target)
                except OSError:
                    backup.replace(target)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
        else:
            if target.exists():
                raise StoreError(
                    f"Плагин «{folder}» уже установлен: {target.as_posix()}"
                )
            tmp.replace(target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    remember_local_origin(directory, folder, src, name, kind="plugin")
    logger.info(f"Магазин: плагин «{folder}» установлен из {src.as_posix()}")
    return folder, changed


def _zip_skill_roots(archive: zipfile.ZipFile) -> list[str]:
    """Папки скиллов в архиве (там, где лежит SKILL.md)."""
    roots: list[str] = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or "__MACOSX" in name:
            continue
        parts = name.split("/")
        if parts[-1] != SKILL_FILE:
            continue
        if any(part in LOCAL_SKIP for part in parts):
            continue
        root = "/".join(parts[:-1])
        if root not in roots:
            roots.append(root)
    return roots


def install_skill_local(
    source: str | Path,
    directory: str | Path | None = None,
    *,
    replace: bool = False,
    slug: str = "",
) -> tuple[Skill, bool]:
    """Ставит скилл из папки или ``.zip`` и запоминает локальный источник.

    В папке должен быть ``SKILL.md``, в архиве — ровно один скилл (обёртка
    вокруг его папки отбрасывается). Имя скилла берётся из frontmatter.
    """
    src = Path(os.path.expanduser(str(source))).resolve()
    if not src.exists():
        raise StoreError(f"Файл или папка не найдены: {src.as_posix()}")

    archive: zipfile.ZipFile | None = None
    root = ""
    if src.is_dir():
        skill_file = src / SKILL_FILE
        if not skill_file.is_file():
            raise StoreError("В папке нет SKILL.md.")
        try:
            text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError("Не удалось прочитать SKILL.md.") from exc
    elif src.suffix.lower() == ".zip":
        try:
            archive = zipfile.ZipFile(src)
        except (OSError, zipfile.BadZipFile) as exc:
            raise StoreError(f"Не удалось открыть архив: {exc}") from exc
        roots = _zip_skill_roots(archive)
        if not roots:
            archive.close()
            raise StoreError("В архиве нет SKILL.md.")
        if len(roots) > 1:
            archive.close()
            raise StoreError(
                "В архиве несколько скиллов — распакуйте нужный отдельно."
            )
        root = roots[0]
        path = f"{root}/{SKILL_FILE}" if root else SKILL_FILE
        try:
            text = archive.read(path).decode("utf-8")
        except (KeyError, UnicodeDecodeError) as exc:
            archive.close()
            raise StoreError("Не удалось прочитать SKILL.md в архиве.") from exc
    else:
        raise StoreError("Нужен .zip или папка с SKILL.md.")

    try:
        meta, _ = parse_skill(text)
    except ValueError as exc:
        if archive is not None:
            archive.close()
        raise StoreError(f"Битый SKILL.md: {exc}") from exc

    fallback = root.split("/")[-1] if root else (src.stem if src.is_file() else src.name)
    fallback = re.sub(r"[-_](main|master)$", "", fallback, flags=re.IGNORECASE)
    name = str(meta.get("name") or fallback).strip() or fallback
    try:
        folder = slug or slugify(name) or slugify(fallback)
    except ValueError as exc:
        if archive is not None:
            archive.close()
        raise StoreError(f"Неподходящее имя скилла: {name}") from exc

    base = skills_root(directory)
    target = base / folder
    if target.exists() and not replace:
        raise StoreError(f"Скилл «{folder}» уже установлен: {target.as_posix()}")
    if not target.exists() and replace:
        raise StoreError(f"Скилл «{folder}» не установлен.")

    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".{folder}.installing"
    shutil.rmtree(tmp, ignore_errors=True)
    changed = True
    try:
        if archive is not None:
            try:
                _archive_copy(archive, root, tmp, MAX_FILES, "скилл")
            finally:
                archive.close()
        else:
            _folder_copy(src, tmp, MAX_FILES, "скилл")
        installed_file = tmp / SKILL_FILE
        if not installed_file.is_file():
            raise StoreError("В скилле нет SKILL.md.")
        parse_skill(installed_file.read_text(encoding="utf-8"))
        if replace:
            if _folder_digest(tmp) == _folder_digest(target):
                shutil.rmtree(tmp, ignore_errors=True)
                changed = False
            else:
                backup = base / f".{folder}.backup"
                shutil.rmtree(backup, ignore_errors=True)
                target.replace(backup)
                try:
                    tmp.replace(target)
                except OSError:
                    backup.replace(target)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
        else:
            if target.exists():
                raise StoreError(
                    f"Скилл «{folder}» уже установлен: {target.as_posix()}"
                )
            tmp.replace(target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    remember_local_origin(directory, folder, src, name, kind="skill")
    logger.info(f"Магазин: скилл «{folder}» установлен из {src.as_posix()}")
    skills = [
        skill
        for skill in load_skills(directory, builtin=False)
        if skill.path.resolve() == target
    ]
    skill = (
        skills[0]
        if skills
        else Skill(name=name, description=str(meta.get("description") or ""), path=target)
    )
    return skill, changed


def installed_plugins(
    directory: str | Path | None = None,
) -> list[InstalledPlugin]:
    """Плагины в папке скиллов (только целые папки верхнего уровня)."""
    root = skills_root(directory)
    if not root.is_dir():
        return []
    origins = _read_origins(directory)
    found: list[InstalledPlugin] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError as exc:
        logger.debug(f"Магазин: папка скиллов недоступна — {exc}")
        return []
    for folder in children:
        manifest = folder / PLUGIN_DIR / PLUGIN_FILE
        if folder.name.startswith(".") or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        name = str(data.get("name") or folder.name).strip() or folder.name
        entry = origins.get(folder.name)
        files = [path for path in folder.rglob("*") if path.is_file()]
        rel = [path.relative_to(folder) for path in files]
        found.append(
            InstalledPlugin(
                folder=folder.name,
                name=name,
                description=str(data.get("description") or "").strip(),
                version=str(data.get("version") or "").strip(),
                path=folder,
                skills=sum(1 for path in rel if path.name == SKILL_FILE),
                commands=sum(
                    1
                    for path in rel
                    if path.parts[0] == "commands" and path.suffix == ".md"
                ),
                agents=sum(
                    1
                    for path in rel
                    if path.parts[0] == "agents" and path.suffix == ".md"
                ),
                tracked=(
                    isinstance(entry, dict)
                    and entry.get("kind") == "plugin"
                    and bool(entry.get("repo"))
                ),
                local=(
                    str(entry.get("local") or "")
                    if isinstance(entry, dict) and entry.get("kind") == "plugin"
                    else ""
                ),
            )
        )
    return found


def update_plugin(
    folder: str,
    directory: str | Path | None = None,
    client: GitHubClient | None = None,
) -> tuple[str, bool]:
    """Обновляет установленный плагин по запомненному источнику."""
    client = client or GitHubClient()
    slug = Path(str(folder or "")).name
    if not slug or slug.startswith("."):
        raise StoreError("Некорректное имя папки плагина.")
    origin = origin_of(directory, slug)
    if origin is None or origin.get("kind") != "plugin":
        raise StoreError(
            "Неизвестно, откуда скачан этот плагин — обновить его с GitHub нельзя."
        )
    if not origin.get("repo"):
        # плагин из файла: обновляем из того же архива или папки
        local = Path(origin.get("local") or "")
        if not origin.get("local"):
            raise StoreError("У плагина не записан источник — обновить его нельзя.")
        if not local.exists():
            raise StoreError(f"Локальный источник пропал: {local.as_posix()}")
        return install_plugin_local(local, directory, replace=True, slug=slug)
    root = skills_root(directory)
    target = root / slug
    if not target.is_dir():
        raise StoreError(f"Плагин «{slug}» не установлен.")
    fresh = next(
        (
            plugin
            for plugin in repo_plugins(client, origin["repo"])
            if plugin.path == origin["path"]
        ),
        None,
    )
    if fresh is None:
        raise StoreError("Плагин больше не найден в репозитории на GitHub.")

    tmp = root / f".{slug}.updating"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _download_plugin(fresh, tmp, client)
        if _folder_digest(tmp) == _folder_digest(target):
            shutil.rmtree(tmp, ignore_errors=True)
            return slug, False
        backup = root / f".{slug}.backup"
        shutil.rmtree(backup, ignore_errors=True)
        target.replace(backup)
        try:
            tmp.replace(target)
        except OSError:
            backup.replace(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    remember_origin(directory, slug, fresh, kind="plugin")
    logger.info(f"Магазин: плагин «{slug}» обновлён из {fresh.repo}")
    return slug, True


def remove_plugin(folder: str, directory: str | Path | None = None) -> bool:
    """Удаляет папку установленного плагина целиком (вместе с командами)."""
    name = Path(str(folder or "")).name
    if not name or name.startswith("."):
        return False
    root = skills_root(directory).resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_dir():
        return False
    if not (target / PLUGIN_DIR / PLUGIN_FILE).is_file():
        return False
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        return False
    forget_origin(directory, name)
    logger.info(f"Магазин: плагин «{name}» удалён из {root}")
    return True


__all__ = [
    "GitHubClient",
    "RECOMMENDED_REPOS",
    "RECOMMENDED_TOPICS",
    "InstalledPlugin",
    "PLUGIN_RECOMMENDED_REPOS",
    "StoreError",
    "StorePlugin",
    "StoreSkill",
    "install",
    "forget_origin",
    "install_plugin",
    "install_plugin_local",
    "install_skill_local",
    "install_slug",
    "installed_names",
    "installed_plugins",
    "is_installed",
    "is_plugin_installed",
    "origin_of",
    "plugin_slug",
    "recommended",
    "recommended_plugins",
    "remember_local_origin",
    "remember_origin",
    "remove_plugin",
    "repo_plugins",
    "repo_skills",
    "search",
    "search_plugins",
    "update",
    "update_installed",
    "update_plugin",
]
