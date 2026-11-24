"""Telegram Mini App с настройками Немуси.

Mini App — обычная HTTPS-страница, которую Telegram открывает внутри чата.
Её нельзя подделать: каждый запрос приносит ``initData``, подписанный ботом.
Сервер проверяет подпись и ID пользователя, а затем читает/пишет тот же
``Config``, что и GUI. HTML-виджеты для настроек больше не используются.

Локальный HTTP-сервер поднимается внутри процесса бота. Наружу его отдаёт
HTTPS-туннель (Cloudflare Tunnel, ngrok, reverse proxy); публичный адрес
задаётся в ``telegram.web_app_url``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from nemus.config import PROJECT_ROOT, Config
from nemus.core import telegram_settings as settings_ui
from nemus.core.providers import PROVIDER_SPECS
from nemus.core.providers.gguf_split import (
    merged_filename,
    shard_filenames,
    split_part,
)
from nemus.core.providers.hf_catalog import (
    HFCatalogError,
    choose_hf_file,
    group_hf_files,
    is_split_gguf,
    list_hf_gguf_files,
    search_hf_models,
)
from nemus.core.providers.hf_download import (
    HFDownloadCancelled,
    HFSource,
    download_hf_model,
    hf_local_path,
    parse_hf_source,
)
from nemus.core.providers.models import fetch_models, list_models
from nemus.utils.logger import logger

#: initData живёт сутки; более старый запрос считаем повторным.
MAX_INIT_AGE = 86400
#: ограничение тела запроса, чтобы сервер не съел память
MAX_BODY = 128 * 1024


def validate_init_data(
    init_data: str,
    bot_token: str,
    allowed_users: list[int],
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Проверяет подпись Telegram WebApp initData.

    Формула из документации Telegram:
    secret = HMAC_SHA256("WebAppData", bot_token), затем HMAC от
    отсортированной строки ``key=value`` без ``hash``.
    """
    token = (bot_token or "").strip()
    if not token or not init_data:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
        data = dict(pairs)
        received = str(data.pop("hash", "") or "").lower()
        if not received:
            return None
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(
            b"WebAppData", token.encode("utf-8"), hashlib.sha256
        ).digest()
        expected = hmac.new(
            secret, check.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        auth_date = int(str(data.get("auth_date") or "0"))
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if auth_date <= 0 or current - auth_date > MAX_INIT_AGE:
        return None
    try:
        user = json.loads(str(data.get("user") or "{}"))
        user_id = int(user.get("id"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if user_id not in {int(item) for item in allowed_users}:
        return None
    data["_user"] = user
    data["_user_id"] = user_id
    return data


def _masked(value: Any) -> bool:
    return bool(value)


class WebAppSettings:
    """Потокобезопасные чтение и запись настроек для Mini App."""

    def __init__(self, config: Config, *, save_func: Any | None = None) -> None:
        self.config = config
        self._save = save_func or settings_ui.save
        self._lock = threading.RLock()
        self._autostart_at = 0.0
        self._autostart_value: bool | None = None
        self._skills_at = 0.0
        self._skills_value: list[tuple[str, bool]] = []
        self._skills_lock = threading.Lock()
        self._models_lock = threading.Lock()
        self._models_cache: dict[str, tuple[float, list[dict[str, Any]], bool]] = {}
        self._download_lock = threading.Lock()
        self._download_cancel: threading.Event | None = None
        self._download_thread: threading.Thread | None = None
        self._download: dict[str, Any] = {
            "state": "idle",
            "repo": "",
            "filename": "",
            "done": 0,
            "total": 0,
            "percent": None,
            "message": "",
            "path": "",
            "model": "",
        }

    @property
    def token(self) -> str:
        return (self.config.telegram.token or "").strip()

    def authorize(self, init_data: str) -> dict[str, Any] | None:
        return validate_init_data(
            init_data,
            self.token,
            list(self.config.telegram.allowed_users),
        )

    def _save_live(self, before: Config | None = None) -> tuple[bool, str]:
        """Сохраняет копию, не ломая абсолютные пути живого конфига."""
        disk = self.config.model_copy(deep=True)
        try:
            self._save(disk)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Web App: не удалось сохранить настройки")
            if before is not None:
                self.config.__dict__.clear()
                self.config.__dict__.update(before.__dict__)
            return False, f"Не удалось сохранить: {exc}"
        return True, "Сохранено."

    def _autostart(self) -> bool | None:
        now = time.monotonic()
        if now - self._autostart_at < 30.0:
            return self._autostart_value
        try:
            from nemus import telegram_autostart

            value = (
                bool(telegram_autostart.status()[0])
                if telegram_autostart.supported()
                else None
            )
        except Exception:  # noqa: BLE001
            value = None
        self._autostart_value = value
        self._autostart_at = now
        return value

    def _skills(self) -> list[tuple[str, bool]]:
        now = time.monotonic()
        if now - self._skills_at < 30.0:
            return self._skills_value
        # Скан скиллов читает все SKILL.md и их вспомогательные файлы.
        # Держим замок, чтобы два первых запроса не делали его параллельно.
        with self._skills_lock:
            now = time.monotonic()
            if now - self._skills_at >= 30.0:
                self._skills_value = settings_ui.skills_entries(self.config)
                self._skills_at = now
            return self._skills_value

    def _mcp_payload(self) -> list[dict[str, Any]]:
        result = []
        for index, server in enumerate(self.config.mcp.servers):
            result.append(
                {
                    "index": index,
                    "name": server.name or "",
                    "command": server.command,
                    "args": " ".join(server.args),
                    "cwd": server.cwd,
                    "env": ", ".join(
                        f"{key}={value}" for key, value in server.env.items()
                    ),
                    "enabled": bool(server.enabled),
                }
            )
        return result

    def snapshot(self) -> dict[str, Any]:
        """Всё, что нужно Mini App для отрисовки."""
        from nemus import __version__

        with self._lock:
            sections = []
            for item in settings_ui.SECTIONS:
                fields = []
                for field in item.fields:
                    value = settings_ui.field_value(self.config, field)
                    fields.append(
                        {
                            "id": field.id,
                            "label": field.label,
                            "kind": field.kind,
                            "choices": [str(choice) for choice in field.choices],
                            "minimum": field.minimum,
                            "maximum": field.maximum,
                            "step": field.step,
                            "unit": field.unit,
                            "secret": field.secret,
                                "hint": field.hint,
                                "has_value": _masked(value) if field.secret else True,
                            "bool": bool(value) if field.kind == "bool" else None,
                            "value": (
                                "" if field.secret else settings_ui.value_text(field, value)
                            ),
                        }
                    )
                note = item.note
                if item.id == "character":
                    try:
                        from nemus.core.emotions import EmotionStore, summary_line

                        state = EmotionStore(
                            self.config.character.state_file
                        ).snapshot()
                        note = summary_line(self.config, state)
                    except Exception:  # noqa: BLE001
                        pass
                sections.append(
                    {
                        "id": item.id,
                        "title": item.title,
                        "icon": item.icon,
                        "note": note,
                        "fields": fields,
                    }
                )
            autostart = self._autostart()
            return {
                "ok": True,
                "version": str(__version__),
                "assistant": self.config.assistant.name,
                "sections": sections,
                "mcp": {
                    "enabled": bool(self.config.mcp.enabled),
                    "refresh_seconds": self.config.mcp.refresh_seconds,
                    "servers": self._mcp_payload(),
                },
                "skills": {
                    "enabled": bool(self.config.skills.enabled),
                    "entries": [
                        {"name": name, "enabled": enabled}
                        for name, enabled in self._skills()
                    ],
                },
                "autostart": autostart,
                "models": {
                    "default_provider": self.config.llm.default_provider,
                    "default_model": self.config.llm.default_model,
                    "provider_labels": {
                        name: spec.label or name
                        for name, spec in PROVIDER_SPECS.items()
                    },
                    "local_models_dir": self.config.llm.llama_cpp.models_dir,
                },
            }

    # ----------------------------------------------------- каталог моделей
    def model_catalog(self, provider: str, *, refresh: bool = False) -> dict[str, Any]:
        """Живой список моделей провайдера с кэшем на две минуты."""
        name = (provider or "").strip()
        if name not in PROVIDER_SPECS:
            raise ValueError("Неизвестный провайдер.")
        now = time.monotonic()
        with self._models_lock:
            cached = self._models_cache.get(name)
            if cached and not refresh and now - cached[0] < 120.0:
                models, live = cached[1], cached[2]
            else:
                models = live = None
        if models is None:
            # Копия конфига: list_models только читает ключи и пути, но не
            # должен зависеть от параллельной записи настроек.
            with self._lock:
                snapshot = self.config.model_copy(deep=True)
            if name == "llama_cpp":
                # Для локального провайдера не показываем статическую
                # заглушку llama_cpp/local: только реальные .gguf.
                try:
                    pairs, live = fetch_models(name, snapshot), True
                except Exception:  # noqa: BLE001
                    pairs, live = [], False
            else:
                pairs, live = list_models(name, snapshot)
            models = [
                {"id": model, "free": bool(free)} for model, free in pairs
            ]
            with self._models_lock:
                self._models_cache[name] = (time.monotonic(), models, bool(live))
        with self._lock:
            current = (
                self.config.llm.default_model
                if self.config.llm.default_provider == name
                else ""
            )
        return {
            "provider": name,
            "models": models,
            "live": bool(live),
            "current": current,
        }

    def _models_invalidate(self) -> None:
        with self._models_lock:
            self._models_cache.clear()

    def hf_search(self, query: str) -> list[dict[str, Any]]:
        """Поиск GGUF-репозиториев на Hugging Face."""
        with self._lock:
            endpoint = self.config.llm.llama_cpp.hf_endpoint
        try:
            found = search_hf_models(query, endpoint=endpoint)
        except HFCatalogError as exc:
            raise ValueError(str(exc)) from exc
        return [
            {
                "repo_id": item.repo_id,
                "downloads": item.downloads,
                "likes": item.likes,
                "gated": item.gated,
            }
            for item in found
        ]

    def hf_files(self, repo: str) -> dict[str, Any]:
        """Список целых .gguf-файлов выбранного репозитория."""
        with self._lock:
            endpoint = self.config.llm.llama_cpp.hf_endpoint
        try:
            found = list_hf_gguf_files(repo, endpoint=endpoint)
        except HFCatalogError as exc:
            raise ValueError(str(exc)) from exc
        # Шарды одного разбитого GGUF показываем одной строкой: загрузчик
        # скачает все части и склеит их в целый файл.
        return {
            "files": [
                {
                    "filename": item.filename,
                    "size": item.size,
                    "split": item.split,
                    "parts": item.parts,
                }
                for item in group_hf_files(found)
            ]
        }

    def download_status(self) -> dict[str, Any]:
        with self._download_lock:
            return dict(self._download)

    @staticmethod
    def _part_path(target: Path) -> Path:
        return target.with_name(target.name + ".part")

    @classmethod
    def _sidecar_path(cls, target: Path) -> Path:
        part = cls._part_path(target)
        return part.with_name(part.name + ".json")

    @staticmethod
    def _model_id(path: Path) -> str:
        try:
            shown = path.resolve().relative_to(PROJECT_ROOT).as_posix()
        except (OSError, ValueError):
            shown = path.resolve().as_posix()
        return f"llama_cpp/{shown}"

    def _write_sidecar(
        self, target: Path, repo: str, filename: str, revision: str
    ) -> None:
        """Запоминает источник рядом с .part, чтобы докачка пережила рестарт."""
        sidecar = self._sidecar_path(target)
        payload = {
            "repo": repo,
            "filename": filename,
            "revision": revision or "main",
            "target": str(target),
        }
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            tmp = sidecar.with_name(sidecar.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(sidecar)
        except OSError as exc:
            logger.warning(f"Web App: не удалось сохранить источник докачки: {exc}")

    def _source_for_part(self, target: Path) -> tuple[str, str, str] | None:
        """Восстанавливает repo/file/revision для недокачанного файла."""
        sidecar = self._sidecar_path(target)
        candidates: list[tuple[str, str, str]] = []
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                candidates.append(
                    (
                        str(data.get("repo") or ""),
                        str(data.get("filename") or ""),
                        str(data.get("revision") or "main"),
                    )
                )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        with self._lock:
            cfg = self.config.model_copy(deep=True)
            candidates.append(
                (
                    cfg.llm.llama_cpp.hf_repo,
                    cfg.llm.llama_cpp.hf_file,
                    cfg.llm.llama_cpp.hf_revision or "main",
                )
            )
        for repo, filename, revision in candidates:
            if not repo or not filename:
                continue
            try:
                source = parse_hf_source(repo, filename, revision)
                expected = hf_local_path(cfg, source)
                if expected.resolve() == target.resolve():
                    return source.repo_id, source.filename, source.revision
            except Exception:  # noqa: BLE001
                continue
        return None

    def local_models(self) -> list[dict[str, Any]]:
        """Готовые и недокачанные GGUF в папке локальных моделей."""
        with self._lock:
            cfg = self.config.model_copy(deep=True)
        root = Path(cfg.llm.llama_cpp.models_dir).expanduser().resolve()
        active_path = ""
        if cfg.llm.default_provider == "llama_cpp" and cfg.llm.llama_cpp.model_path:
            try:
                active_path = str(Path(cfg.llm.llama_cpp.model_path).resolve())
            except OSError:
                active_path = cfg.llm.llama_cpp.model_path
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                lowered = path.name.lower()
                if lowered.endswith(".gguf.part"):
                    target = path.with_name(path.name[:-5])
                    if target.is_file():
                        continue
                    size = path.stat().st_size
                    source = self._source_for_part(target)
                    items.append(
                        {
                            "model": self._model_id(target),
                            "path": str(target.resolve()),
                            "filename": target.name,
                            "source": (
                                f"{source[0]}/{source[1]}" if source else ""
                            ),
                            "size": size,
                            "partial": True,
                            "can_resume": bool(source),
                            "is_active": False,
                        }
                    )
                elif lowered.endswith(".gguf"):
                    resolved = path.resolve()
                    model_id = self._model_id(resolved)
                    seen.add(str(resolved))
                    items.append(
                        {
                            "model": model_id,
                            "path": str(resolved),
                            "filename": resolved.name,
                            "source": "",
                            "size": path.stat().st_size,
                            "partial": False,
                            "can_resume": False,
                            "is_active": bool(
                                cfg.llm.default_provider == "llama_cpp"
                                and (
                                    cfg.llm.default_model == model_id
                                    or active_path == str(resolved)
                                )
                            ),
                            "external": False,
                        }
                    )
            except OSError:
                continue
        # Активная модель может лежать вне models_dir (путь задан вручную).
        # Показываем её, но не разрешаем удалять/докачивать чужой путь.
        if active_path and active_path not in seen:
            active = Path(active_path)
            try:
                if active.is_file() and active.suffix.lower() == ".gguf":
                    items.append(
                        {
                            "model": self._model_id(active),
                            "path": str(active.resolve()),
                            "filename": active.name,
                            "source": "вне папки моделей",
                            "size": active.stat().st_size,
                            "partial": False,
                            "can_resume": False,
                            "is_active": True,
                            "external": True,
                        }
                    )
            except OSError:
                pass
        items.sort(key=lambda item: (bool(item["partial"]), str(item["filename"]).lower()))
        return items

    def _resolve_local(self, model: str) -> tuple[Path, Path] | None:
        """Возвращает (target .gguf, .part) и проверяет путь внутри models_dir."""
        raw = (model or "").strip()
        if raw.startswith("llama_cpp/"):
            raw = raw[len("llama_cpp/") :]
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            resolved = path.resolve(strict=False)
            with self._lock:
                root = Path(self.config.llm.llama_cpp.models_dir).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        lowered = resolved.name.lower()
        if lowered.endswith(".gguf.part"):
            target = resolved.with_name(resolved.name[:-5])
        elif lowered.endswith(".gguf"):
            target = resolved
        else:
            return None
        return target, self._part_path(target)

    def delete_local_model(self, model: str) -> tuple[bool, str]:
        """Удаляет готовый .gguf или недокачанный .part вместе с sidecar."""
        resolved = self._resolve_local(model)
        if resolved is None:
            return False, "Файл не найден в папке локальных моделей."
        target, part = resolved
        if not target.is_file() and not part.is_file():
            return False, "Файл уже удалён."
        model_id = self._model_id(target)
        with self._lock:
            active = (
                self.config.llm.default_model == model_id
                or (
                    bool(self.config.llm.llama_cpp.model_path)
                    and Path(self.config.llm.llama_cpp.model_path).resolve()
                    == target.resolve()
                )
            )
        if active and target.is_file():
            return False, "Модель активна. Сначала выбери другую."
        removed = 0
        for path in (target, part, self._sidecar_path(target)):
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                return False, f"Не удалось удалить {path.name}: {exc}"
        self._models_invalidate()
        return True, f"Удалено файлов: {removed}."

    def resume_local_model(self, model: str) -> tuple[bool, str, dict[str, Any]]:
        """Продолжает .part с того же места через HF Range."""
        resolved = self._resolve_local(model)
        if resolved is None:
            return False, "Файл не найден.", self.download_status()
        target, part = resolved
        if not part.is_file():
            return False, "Нет недокачанного .part.", self.download_status()
        source_target = target
        part = split_part(target.name)
        if part is not None:
            source_target = target.with_name(shard_filenames(target.name)[0])
        source = self._source_for_part(source_target)
        if source is None:
            return (
                False,
                "Источник неизвестен. Выбери этот файл в Hugging Face и запусти скачивание — .part продолжит с того же места.",
                self.download_status(),
            )
        return self.start_download(source[0], source[1], source[2])

    def start_download(
        self,
        repo: str,
        filename: str,
        revision: str = "main",
        prefer: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        """Запускает скачивание GGUF в фоне и возвращает текущий статус.

        Пустой ``filename`` означает «выбери сам»: для строки поиска это
        избавляет от обязательного промежуточного экрана с файлами. Берём
        разумное квантование (Q4_K_M и дальше по списку) и склеиваем шарды.
        """
        with self._lock:
            cfg = self.config.model_copy(deep=True)
        selected = None
        if not (filename or "").strip():
            try:
                files = list_hf_gguf_files(
                    repo, endpoint=cfg.llm.llama_cpp.hf_endpoint
                )
            except HFCatalogError as exc:
                return False, str(exc), self.download_status()
            selected = choose_hf_file(files, prefer)
            if selected is None:
                return (
                    False,
                    f"В репозитории {repo} не найден подходящий .gguf-файл.",
                    self.download_status(),
                )
            filename = selected.filename
        try:
            source = parse_hf_source(repo, filename, revision or "main")
            target = hf_local_path(cfg, source)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), self.download_status()

        # Для split-модели пишем источник рядом с каждой частью: докачку
        # можно будет запустить с любой строки списка локальных файлов.
        sidecar_sources = [source]
        if is_split_gguf(source.filename):
            sidecar_sources = [
                HFSource(source.repo_id, name, source.revision)
                for name in shard_filenames(source.filename)
            ]
        for item in sidecar_sources:
            item_target = hf_local_path(cfg, item)
            self._write_sidecar(
                item_target, source.repo_id, item.filename, source.revision
            )

        output_target = target
        if is_split_gguf(source.filename):
            output_target = hf_local_path(
                cfg,
                HFSource(
                    source.repo_id,
                    merged_filename(source.filename),
                    source.revision,
                ),
            )
        with self._download_lock:
            if self._download.get("state") in ("starting", "running"):
                return False, "Загрузка уже идёт.", dict(self._download)
            cancel = threading.Event()
            self._download_cancel = cancel
            self._download = {
                "state": "starting",
                "repo": source.repo_id,
                "filename": source.filename,
                "done": 0,
                "total": 0,
                "percent": None,
                "message": "Подготовка…",
                "path": str(output_target),
                "model": "",
                "split": is_split_gguf(source.filename),
            }
            thread = threading.Thread(
                target=self._download_worker,
                args=(cfg, source.repo_id, source.filename, source.revision, cancel),
                name="telegram-webapp-hf-download",
                daemon=True,
            )
            self._download_thread = thread
        thread.start()
        if selected is not None and selected.parts > 1:
            message = (
                f"Скачиваю {selected.filename}: {selected.parts} части, "
                "после загрузки склею в целый GGUF."
            )
        elif selected is not None:
            message = f"Скачиваю {selected.filename}."
        else:
            message = "Загрузка началась."
        return True, message, self.download_status()

    def _download_worker(
        self,
        cfg: Config,
        repo: str,
        filename: str,
        revision: str,
        cancel: threading.Event,
    ) -> None:
        """Фоновое скачивание; прогресс читается через /api/hf/download."""
        def progress(done: int, total: int) -> None:
            percent = (
                max(0, min(100, int(done * 100 / total))) if total > 0 else None
            )
            with self._download_lock:
                if self._download.get("state") not in ("starting", "running"):
                    return
                self._download.update(
                    state="running",
                    done=int(done),
                    total=int(total),
                    percent=percent,
                )

        def status(message: str) -> None:
            with self._download_lock:
                if self._download.get("state") in ("starting", "running"):
                    self._download["message"] = message

        try:
            path = download_hf_model(
                cfg,
                repo=repo,
                filename=filename,
                revision=revision,
                progress=progress,
                status=status,
                cancel=cancel,
            )
        except HFDownloadCancelled:
            with self._download_lock:
                self._download.update(
                    state="cancelled", message="Загрузка отменена.", percent=None
                )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Web App: загрузка модели: {exc}")
            with self._download_lock:
                self._download.update(state="error", message=str(exc))
            return
        try:
            resolved = path.resolve()
            shown = resolved.relative_to(PROJECT_ROOT).as_posix()
        except (OSError, ValueError):
            resolved = path
            shown = path.as_posix()
        model = f"llama_cpp/{shown}"
        size = path.stat().st_size if path.is_file() else 0
        with self._download_lock:
            self._download.update(
                state="done",
                done=size,
                total=size,
                percent=100,
                path=str(resolved),
                model=model,
                message=(
                    "Части скачаны и склеены в один GGUF."
                    if self._download.get("split")
                    else "Модель скачана."
                ),
            )
        try:
            self._sidecar_path(path).unlink(missing_ok=True)
        except OSError:
            pass
        self._models_invalidate()

    def cancel_download(self) -> tuple[bool, str]:
        with self._download_lock:
            cancel = self._download_cancel
            if cancel is None or self._download.get("state") not in (
                "starting",
                "running",
            ):
                return False, "Загрузка не идёт."
            cancel.set()
            self._download["message"] = "Останавливаю…"
        return True, "Загрузка остановлена."

    def use_local_model(self, model: str) -> tuple[bool, str]:
        """Делает скачанный GGUF активной моделью llama.cpp."""
        raw = (model or "").strip()
        if raw.startswith("llama_cpp/"):
            raw = raw[len("llama_cpp/") :]
        if not raw:
            return False, "Не указана модель."
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False, "Файл модели не найден."
        with self._lock:
            models_dir = Path(self.config.llm.llama_cpp.models_dir).resolve()
            current_path = ""
            if self.config.llm.llama_cpp.model_path:
                try:
                    current_path = str(
                        Path(self.config.llm.llama_cpp.model_path).resolve()
                    )
                except OSError:
                    current_path = ""
        inside = True
        try:
            resolved.relative_to(models_dir)
        except ValueError:
            inside = False
        if not inside and str(resolved) != current_path:
            return False, "Модель должна лежать в папке локальных моделей."
        if resolved.suffix.lower() != ".gguf" or not resolved.is_file():
            return False, "Нужен файл .gguf."
        try:
            shown = resolved.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            shown = resolved.as_posix()
        model_id = f"llama_cpp/{shown}"
        with self._lock:
            before = self.config.model_copy(deep=True)
            self.config.llm.default_provider = "llama_cpp"
            self.config.llm.default_model = model_id
            self.config.llm.llama_cpp.model_path = str(resolved)
            ok, message = self._save_live(before)
            if ok:
                self._models_invalidate()
            return ok, message

    def update_field(
        self,
        section_id: str,
        field_id: str,
        raw: Any,
        *,
        actor_id: int | None = None,
    ) -> tuple[bool, str]:
        field = settings_ui.find_field(section_id, field_id)
        if field is None:
            return False, "Поле не найдено."
        text = str(raw if raw is not None else "")
        if field.secret and not text.strip():
            return True, "Секрет не изменён."
        with self._lock:
            before = self.config.model_copy(deep=True)
            ok, message = settings_ui.apply_text(self.config, field, text)
            if not ok:
                self.config.__dict__.clear()
                self.config.__dict__.update(before.__dict__)
                return False, message
            if (
                field.path == ("telegram", "allowed_users")
                and actor_id is not None
                and actor_id not in self.config.telegram.allowed_users
            ):
                self.config.__dict__.clear()
                self.config.__dict__.update(before.__dict__)
                return False, "Нельзя убрать свой ID — потеряешь доступ к боту."
            ok, save_message = self._save_live(before)
            if ok and field.path[:2] == ("llm", "providers"):
                self._models_invalidate()
            if ok and field.path in (
                ("llm", "default_provider"),
                ("llm", "default_model"),
                ("llm", "llama_cpp", "models_dir"),
            ):
                self._models_invalidate()
            return (ok, save_message if not ok else message)

    def update_skill(self, name: str, enabled: bool) -> tuple[bool, str]:
        with self._lock:
            entries = dict(self._skills())
            if name not in entries:
                return False, "Скилл не найден."
            if entries[name] == enabled:
                return True, "Без изменений."
            before = self.config.model_copy(deep=True)
            ok, message = settings_ui.toggle_skill(self.config, name)
            if not ok:
                return False, message
            ok, save_message = self._save_live(before)
            if ok:
                # Не перечитываем все SKILL.md: состояние уже известно.
                self._skills_value = [
                    (item, enabled if item == name else state)
                    for item, state in entries.items()
                ]
                self._skills_at = time.monotonic()
            else:
                self._skills_at = 0.0
            return ok, save_message if not ok else message

    def update_mcp(self, action: str, payload: dict[str, Any]) -> tuple[bool, str]:
        with self._lock:
            before = self.config.model_copy(deep=True)
            if action == "add":
                ok, message = settings_ui.add_mcp_server(
                    self.config, str(payload.get("command") or "")
                )
                fields = payload.get("fields")
                if ok and isinstance(fields, dict):
                    index = len(self.config.mcp.servers) - 1
                    for field_id, value in fields.items():
                        ok, message = settings_ui.mcp_apply_text(
                            self.config, index, str(field_id), str(value)
                        )
                        if not ok:
                            break
            elif action in ("update", "toggle", "delete"):
                try:
                    index = int(payload.get("index"))
                except (TypeError, ValueError):
                    return False, "Неверный сервер."
                if not 0 <= index < len(self.config.mcp.servers):
                    return False, "Сервер исчез — обнови список."
                if action == "delete":
                    self.config.mcp.servers.pop(index)
                    ok, message = True, "Сервер удалён."
                elif action == "toggle":
                    server = self.config.mcp.servers[index]
                    server.enabled = not server.enabled
                    ok, message = True, "Сервер переключён."
                else:
                    fields = payload.get("fields")
                    if fields is None:
                        ok, message = settings_ui.mcp_apply_text(
                            self.config,
                            index,
                            str(payload.get("field") or ""),
                            str(payload.get("value") or ""),
                        )
                    elif not isinstance(fields, dict):
                        return False, "Неверный набор полей."
                    else:
                        ok, message = True, "Сервер сохранён."
                        for field_id, value in fields.items():
                            ok, message = settings_ui.mcp_apply_text(
                                self.config, index, str(field_id), str(value)
                            )
                            if not ok:
                                break
            else:
                return False, "Неизвестное действие."
            if not ok:
                self.config.__dict__.clear()
                self.config.__dict__.update(before.__dict__)
                return False, message
            return self._save_live(before)

    def update_skills_enabled(self, enabled: bool) -> tuple[bool, str]:
        with self._lock:
            if self.config.skills.enabled == enabled:
                return True, "Без изменений."
            before = self.config.model_copy(deep=True)
            self.config.skills.enabled = enabled
            return self._save_live(before)

    def update_autostart(self, enabled: bool) -> tuple[bool, str]:
        try:
            from nemus import telegram_autostart

            if not telegram_autostart.supported():
                return False, "Автозапуск доступен только в Windows."
            ok, error = telegram_autostart.set_enabled(bool(enabled))
            return ok, error or ("Автозапуск включён." if enabled else "Автозапуск выключен.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Web App: автозапуск: {exc}")
            return False, str(exc)


class _WebAppHandler(BaseHTTPRequestHandler):
    """HTTP-обработчик Mini App; сервис передаётся через подкласс."""

    service: WebAppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            logger.debug("Web App: " + (fmt % args))
        except Exception:  # noqa: BLE001
            logger.debug(f"Web App: {fmt} {args}")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _init_data(self, body: dict[str, Any] | None = None) -> str:
        header = self.headers.get("X-Telegram-Init-Data") or ""
        if header:
            return header
        if body is not None:
            return str(body.get("initData") or "")
        query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
        return str(query.get("initData") or "")

    def _auth(self, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self.service.settings.authorize(self._init_data(body))

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            from nemus.core.webapp_ui import index_html

            try:
                page = index_html()
            except OSError as exc:
                logger.exception(f"Web App: не удалось прочитать index.html: {exc}")
                self._send_bytes(
                    500,
                    "Не удалось загрузить страницу Mini App.".encode("utf-8"),
                    "text/plain; charset=utf-8",
                )
                return
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(
                200,
                {
                    "ok": True,
                    "features": ["models", "hf", "local_models"],
                },
            )
            return
        if path == "/api/settings":
            if self._auth() is None:
                self._json(401, {"ok": False, "error": "Открой Mini App из Telegram."})
                return
            self._json(200, self.service.settings.snapshot())
            return
        if path in (
            "/api/models",
            "/api/models/local",
            "/api/hf/search",
            "/api/hf/files",
            "/api/hf/download",
        ):
            if self._auth() is None:
                self._json(401, {"ok": False, "error": "Открой Mini App из Telegram."})
                return
            query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            try:
                if path == "/api/models":
                    payload = self.service.settings.model_catalog(
                        str(query.get("provider") or ""),
                        refresh=bool(query.get("refresh")),
                    )
                elif path == "/api/models/local":
                    payload = {"models": self.service.settings.local_models()}
                elif path == "/api/hf/search":
                    payload = {
                        "models": self.service.settings.hf_search(
                            str(query.get("q") or "")
                        )
                    }
                elif path == "/api/hf/files":
                    repo = str(query.get("repo") or "")
                    payload = {
                        "repo": repo,
                        **self.service.settings.hf_files(repo),
                    }
                else:
                    payload = {"download": self.service.settings.download_status()}
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Web App: чтение моделей: {exc}")
                self._json(500, {"ok": False, "error": "Не удалось получить данные."})
                return
            self._json(200, {"ok": True, **payload})
            return
        self._json(404, {"ok": False, "error": "Не найдено."})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        body = self._read_json()
        if body is None:
            self._json(400, {"ok": False, "error": "Некорректный запрос."})
            return
        auth = self._auth(body)
        if auth is None:
            self._json(401, {"ok": False, "error": "Подпись Telegram неверна."})
            return
        if path == "/api/settings":
            ok, message = self.service.settings.update_field(
                str(body.get("section") or ""),
                str(body.get("field") or ""),
                body.get("value"),
                actor_id=int(auth.get("_user_id") or 0) or None,
            )
        elif path == "/api/skill":
            ok, message = self.service.settings.update_skill(
                str(body.get("name") or ""), bool(body.get("enabled"))
            )
        elif path == "/api/skills":
            ok, message = self.service.settings.update_skills_enabled(
                bool(body.get("enabled"))
            )
        elif path == "/api/mcp":
            ok, message = self.service.settings.update_mcp(
                str(body.get("action") or ""), body
            )
        elif path == "/api/autostart":
            ok, message = self.service.settings.update_autostart(bool(body.get("enabled")))
        elif path == "/api/hf/download":
            ok, message, download = self.service.settings.start_download(
                str(body.get("repo") or ""),
                str(body.get("file") or ""),
                str(body.get("revision") or "main"),
                str(body.get("prefer") or ""),
            )
            self._json(
                200 if ok else 400,
                {"ok": ok, "message": message, "download": download},
            )
            return
        elif path == "/api/hf/cancel":
            ok, message = self.service.settings.cancel_download()
            self._json(
                200 if ok else 400,
                {
                    "ok": ok,
                    "message": message,
                    "download": self.service.settings.download_status(),
                },
            )
            return
        elif path == "/api/models/delete":
            ok, message = self.service.settings.delete_local_model(
                str(body.get("model") or "")
            )
        elif path == "/api/models/resume":
            ok, message, download = self.service.settings.resume_local_model(
                str(body.get("model") or "")
            )
            self._json(
                200 if ok else 400,
                {
                    "ok": ok,
                    "message": message,
                    "download": download,
                    "models": self.service.settings.local_models(),
                },
            )
            return
        elif path == "/api/models/use":
            ok, message = self.service.settings.use_local_model(
                str(body.get("model") or "")
            )
        else:
            self._json(404, {"ok": False, "error": "Не найдено."})
            return
        payload: dict[str, Any] = {"ok": ok, "message": message}
        if ok:
            payload["settings"] = self.service.settings.snapshot()
        self._json(200 if ok else 400, payload)


class _WebAppHTTPServer(ThreadingHTTPServer):
    """HTTP-сервер без обратного DNS при старте.

    ``HTTPServer.server_bind`` вызывает ``socket.getfqdn(host)``. На Windows
    этот запрос иногда зависает на 10–15 секунд, и Mini App открывается
    позже, хотя сервер уже готов принимать соединения. Имя хоста нам не
    нужно: наружу его отдаёт туннель.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class WebAppServer:
    """Локальный HTTP-сервер Mini App внутри процесса бота."""

    def __init__(
        self,
        config: Config,
        *,
        host: str | None = None,
        port: int | None = None,
        save_func: Any | None = None,
    ) -> None:
        self.config = config
        self.settings = WebAppSettings(config, save_func=save_func)
        self.host = host or config.telegram.web_app_host or "127.0.0.1"
        self.port = int(port if port is not None else config.telegram.web_app_port)
        self._httpd: _WebAppHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def bound_port(self) -> int:
        if self._httpd is None:
            return 0
        return int(self._httpd.server_address[1])

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            handler = type(
                "NemusWebAppHandler",
                (_WebAppHandler,),
                {"service": self},
            )
            httpd = _WebAppHTTPServer((self.host, self.port), handler)
            self._httpd = httpd
        except OSError as exc:
            logger.warning(f"Web App: не удалось занять {self.host}:{self.port}: {exc}")
            self._httpd = None
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Web App: сервер не запущен: {exc}")
            self._httpd = None
            return False
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="telegram-webapp", daemon=True
        )
        self._thread.start()
        # Первый /api/settings сканирует скиллы и проверяет автозапуск.
        # Прогреваем кэш заранее, чтобы страница открывалась без паузы.
        threading.Thread(
            target=self._prewarm, name="telegram-webapp-prewarm", daemon=True
        ).start()
        logger.info(f"Web App: слушает http://{self.host}:{self.bound_port}")
        return True

    def _prewarm(self) -> None:
        try:
            self.settings.snapshot()
        except Exception:  # noqa: BLE001
            logger.debug("Web App: прогрев настроек не удался", exc_info=True)

    def stop(self) -> None:
        try:
            self.settings.cancel_download()
        except Exception:  # noqa: BLE001
            pass
        httpd = self._httpd
        self._httpd = None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
