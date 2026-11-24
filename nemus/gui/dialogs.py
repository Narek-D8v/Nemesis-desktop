"""Диалоги: настройки (в т.ч. вход в Spotify) и подтверждение вызова."""

from __future__ import annotations

import html
import json
import re
import shlex
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMouseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from nemus import __version__, telegram_autostart
from nemus.config import (
    PROJECT_ROOT,
    MCPServerConfig,
    ProviderConfig,
    _resolve_path,
    get_config,
    load_config,
    save_config,
)
from nemus.core.cleanup import DEFAULT_CLEANUP_DAYS, cleanup_storage, human_bytes
from nemus.core.language import REPLY_AUTO, REPLY_CHOICES
from nemus.core.language import normalize as normalize_lang
from nemus.core.mcp import MCP_PRESETS, parse_claude_config, parse_mcp_command
from nemus.core.providers import PROVIDER_SPECS
from nemus.core.providers.hf_catalog import (
    HFCatalogError,
    group_hf_files,
    list_hf_gguf_files,
    search_hf_models,
)
from nemus.core.providers.hf_download import (
    HFDownloadCancelled,
    download_hf_model,
    format_bytes,
)
from nemus.core.providers.llama_cpp import (
    LlamaCppInstallCancelled,
    install_llama_cpp,
    package_status,
)
from nemus.core.providers.models import list_models, static_models
from nemus.core.skills import load_skills, skill_highlights, skills_root
from nemus.core.tools.base import RiskLevel, Tool
from nemus.gui import i18n
from nemus.gui.theme import (
    ACCENT_DANGER,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    TEXT,
    TEXT_DIM,
    qss,
)
from nemus.gui.widgets import ROLE_FREE, ElidedLabel, ModelComboBox, ToggleSwitch
from nemus.utils.secrets import set_secret
from nemus.voice.tts import OPENAI_VOICES, TTSEngine, list_engine_voices, resolve_openai_key

PROVIDER_OPTIONS = list(PROVIDER_SPECS.keys())

_HINT = f"color:{TEXT_DIM};font-size:12px;"

#: дискретные периоды обновления инструментов MCP (секунды) — их ходит ползунок
_REFRESH_STEPS: tuple[int, ...] = (5, 15, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400)


def _parse_user_ids(text: str) -> list[int]:
    """ID пользователей из строки «123, 456; 789» (мусор пропускаем)."""
    ids: list[int] = []
    for chunk in re.split(r"[,;\s]+", text or ""):
        value = chunk.strip()
        if not value:
            continue
        try:
            uid = int(value)
        except ValueError:
            continue
        if uid > 0:  # ID пользователя всегда положительный, группы — нет
            ids.append(uid)
    return ids


def _fmt_refresh(seconds: int) -> str:
    """5 → «5 с», 900 → «15 мин», 3600 → «1 ч», 86400 → «1 д»."""
    if seconds % 86400 == 0:
        return i18n.tr("mcp.unit_day", n=seconds // 86400)
    if seconds % 3600 == 0:
        return i18n.tr("mcp.unit_h", n=seconds // 3600)
    if seconds % 60 == 0:
        return i18n.tr("mcp.unit_min", n=seconds // 60)
    return i18n.tr("mcp.unit_s", n=seconds)

#: подписи риска берутся через i18n.tr на момент построения диалога
_RISK_KEYS = {
    RiskLevel.SAFE: ("risk.safe", ACCENT_SUCCESS),
    RiskLevel.MEDIUM: ("risk.medium", ACCENT_WARNING),
    RiskLevel.DANGEROUS: ("risk.dangerous", ACCENT_DANGER),
}


class _SpotifyAuthWorker(QThread):
    """Выполняет OAuth-вход Spotify в фоне, чтобы не блокировать GUI."""

    done = Signal(bool, str)

    def __init__(self, settings: Any) -> None:
        super().__init__()
        self._settings = settings

    def run(self) -> None:  # noqa: D102
        try:
            from nemus.core.tools.spotify import authorize

            path = authorize(self._settings, open_browser=True)
            self.done.emit(True, path)
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


def _redirect_uri_warning(uri: str) -> str:
    """Проверяет Redirect URI на типичные ошибки, из-за которых Spotify отказывает."""
    from nemus.core.tools.spotify import redirect_uri_problem

    return redirect_uri_problem(uri)


# ------------------------------------------------------------------- voices
#: запасной набор голосов edge-tts — показывается до загрузки полного списка
_FALLBACK_VOICES: list[dict[str, str]] = [
    {"ShortName": "ru-RU-DmitryNeural", "Gender": "Male"},
    {"ShortName": "ru-RU-SvetlanaNeural", "Gender": "Female"},
    {"ShortName": "uk-UA-OstapNeural", "Gender": "Male"},
    {"ShortName": "uk-UA-PolinaNeural", "Gender": "Female"},
    {"ShortName": "kk-KZ-DauletNeural", "Gender": "Male"},
    {"ShortName": "kk-KZ-AigulNeural", "Gender": "Female"},
    {"ShortName": "en-US-GuyNeural", "Gender": "Male"},
    {"ShortName": "en-US-JennyNeural", "Gender": "Female"},
    {"ShortName": "en-US-AriaNeural", "Gender": "Female"},
    {"ShortName": "en-GB-RyanNeural", "Gender": "Male"},
    {"ShortName": "en-GB-SoniaNeural", "Gender": "Female"},
    {"ShortName": "de-DE-ConradNeural", "Gender": "Male"},
    {"ShortName": "de-DE-KatjaNeural", "Gender": "Female"},
    {"ShortName": "fr-FR-HenriNeural", "Gender": "Male"},
    {"ShortName": "fr-FR-DeniseNeural", "Gender": "Female"},
    {"ShortName": "es-ES-AlvaroNeural", "Gender": "Male"},
    {"ShortName": "es-ES-ElviraNeural", "Gender": "Female"},
    {"ShortName": "it-IT-DiegoNeural", "Gender": "Male"},
    {"ShortName": "it-IT-ElsaNeural", "Gender": "Female"},
    {"ShortName": "pt-BR-AntonioNeural", "Gender": "Male"},
    {"ShortName": "pt-BR-FranciscaNeural", "Gender": "Female"},
    {"ShortName": "pl-PL-MarekNeural", "Gender": "Male"},
    {"ShortName": "pl-PL-ZofiaNeural", "Gender": "Female"},
    {"ShortName": "tr-TR-AhmetNeural", "Gender": "Male"},
    {"ShortName": "tr-TR-EmelNeural", "Gender": "Female"},
    {"ShortName": "ja-JP-KeitaNeural", "Gender": "Male"},
    {"ShortName": "ja-JP-NanamiNeural", "Gender": "Female"},
    {"ShortName": "zh-CN-YunxiNeural", "Gender": "Male"},
    {"ShortName": "zh-CN-XiaoxiaoNeural", "Gender": "Female"},
]


def _voice_parts(voice: Any) -> tuple[str, str, str]:
    """Нормализует запись ``list_voices`` → (имя, локаль, пол).

    У голосов без локали (OpenAI, системные Windows) локаль — пустая строка,
    такие голоса не фильтруются по языку и всегда показываются в списке.
    """
    if isinstance(voice, str):
        short = voice
        gender = ""
    else:
        short = str(voice.get("ShortName") or voice.get("short_name") or "")
        gender = str(voice.get("Gender") or voice.get("gender") or "")
    parts = short.split("-")
    locale = "-".join(parts[:2]) if len(parts) >= 2 else ""
    return short, locale, gender


def _pct(value: int) -> str:
    """Проценты в формате edge-tts: «+0%», «-15%»."""
    return f"{value:+d}%"


def _pct_value(raw: Any) -> int:
    """Обратное преобразование: «+10%» → 10 (нечисла → 0)."""
    try:
        return int(str(raw).strip().rstrip("%").replace("+", ""))
    except (TypeError, ValueError):
        return 0


def _split_args(text: str) -> list[str]:
    """Кавычки учитывает, обратный слэш в путях Windows — нет."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _soft_wrap(text: str, limit: int = 28) -> str:
    """Вставляет невидимые разрывы в токены без пробелов (пути, base64).

    QLabel с переносом по словам не режет длинный токен: минимум по ширине
    равен его длине, и текст уезжает за край диалога. Невидимый пробел
    (U+200B) даёт место переноса, внешний вид текста не меняется.
    """
    out: list[str] = []
    for token in re.split(r"(\s+)", str(text)):
        if len(token) <= limit or token.isspace():
            out.append(token)
            continue
        out.append(
            "".join(
                token[i : i + limit] + "\u200b" for i in range(0, len(token), limit)
            )
        )
    return "".join(out)


def _short_text(text: str, limit: int = 180) -> str:
    """Сжимает длинное описание скилла в одну читаемую фразу.

    Описания Agent Skills бывают по несколько абзацев: в списке они показаны
    кратко (полный текст — в подсказке), иначе строка распирает всю вкладку.
    """
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{cut}…"


def _parse_env(text: str) -> dict[str, str]:
    """«KEY=значение;KEY2=значение2» → словарь (пустые пары пропускаются)."""
    env: dict[str, str] = {}
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        if key.strip():
            env[key.strip()] = value.strip()
    return env


# ------------------------------------------------------------------ workers
#: воркеры, которые не успели завершиться к закрытию диалога: держим в живых,
#: чтобы Python не удалил работающий QThread (это роняет Qt)
_DETACHED: set[QThread] = set()


class _VoicesWorker(QThread):
    """Загружает список голосов выбранного движка в фоне."""

    done = Signal(object)  # list | None (None — не удалось загрузить)

    def __init__(self, engine: str = "edge-tts", api_key: str = "") -> None:
        super().__init__()
        self._engine = engine
        self._api_key = api_key

    def run(self) -> None:  # noqa: D102
        try:
            voices = list_engine_voices(self._engine, self._api_key)
            self.done.emit(list(voices) if voices else None)
        except Exception:  # noqa: BLE001
            self.done.emit(None)


class _TTSPreviewWorker(QThread):
    """Озвучивает короткую фразу выбранным голосом (фон, не блокирует GUI)."""

    done = Signal(str)  # "" — ок, иначе текст ошибки

    def __init__(
        self,
        voice: str,
        rate: str,
        volume: str,
        text: str,
        engine: str = "edge-tts",
        model: str = "",
        api_key: str = "",
    ) -> None:
        super().__init__()
        self._engine = TTSEngine(
            voice=voice, rate=rate, volume=volume, engine=engine, model=model, api_key=api_key
        )
        self._text = text

    def run(self) -> None:  # noqa: D102
        try:
            self._engine.speak(self._text, block=True)
            self.done.emit("")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(str(exc))

    def cancel(self) -> None:
        """Останавливает воспроизведение (вызывается из главного потока)."""
        try:
            self._engine.stop()
        except Exception:  # noqa: BLE001
            pass


class _MCPProbeWorker(QThread):
    """Проверяет MCP-сервер: подключается, читает инструменты, отключается."""

    done = Signal(bool, str)

    def __init__(self, server: MCPServerConfig) -> None:
        super().__init__()
        self._server = server

    def run(self) -> None:  # noqa: D102
        try:
            from nemus.core.mcp import probe_server

            ok, message = probe_server(self._server)
            self.done.emit(ok, message)
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


class _MCPRefreshWorker(QThread):
    """Принудительно перечитывает инструменты живого MCP-менеджера в фоне."""

    done = Signal(bool, str)

    def __init__(self, manager: Any) -> None:
        super().__init__()
        self._manager = manager

    def run(self) -> None:  # noqa: D102
        try:
            self._manager.refresh()
            names = ", ".join(self._manager.tool_names)
            self.done.emit(True, names or i18n.tr("mcp.no_tools"))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


class _HFSearchWorker(QThread):
    """Ищет GGUF-репозитории через API Hugging Face."""

    done = Signal(bool, object, str)

    def __init__(self, query: str, endpoint: str) -> None:
        super().__init__()
        self._query = query
        self._endpoint = endpoint

    def run(self) -> None:  # noqa: D102
        try:
            self.done.emit(True, search_hf_models(self._query, endpoint=self._endpoint), "")
        except HFCatalogError as exc:
            self.done.emit(False, [], str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, [], str(exc))


class _HFFilesWorker(QThread):
    """Загружает список GGUF-файлов выбранного репозитория."""

    done = Signal(bool, object, str, str)  # ok, files, error, repo

    def __init__(self, repo: str, endpoint: str) -> None:
        super().__init__()
        self._repo = repo
        self._endpoint = endpoint

    def run(self) -> None:  # noqa: D102
        try:
            self.done.emit(
                True,
                list_hf_gguf_files(self._repo, endpoint=self._endpoint),
                "",
                self._repo,
            )
        except HFCatalogError as exc:
            self.done.emit(False, [], str(exc), self._repo)
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, [], str(exc), self._repo)


class _HFDownloadWorker(QThread):
    """Качает GGUF с Hugging Face в фоне, не блокируя окно настроек."""

    progress = Signal(int, int)
    done = Signal(bool, str, str)

    def __init__(
        self,
        cfg: Any,
        repo: str,
        filename: str,
        revision: str,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._repo = repo
        self._filename = filename
        self._revision = revision
        self._cancel = threading.Event()

    def cancel_download(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D102
        try:
            path = download_hf_model(
                self._cfg,
                repo=self._repo,
                filename=self._filename,
                revision=self._revision,
                progress=lambda done, total: self.progress.emit(done, total),
                cancel=self._cancel,
            )
            self.done.emit(True, "", str(path))
        except HFDownloadCancelled:
            self.done.emit(False, i18n.tr("llm.hf_cancelled"), "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc), "")


class _LlamaInstallWorker(QThread):
    """Ставит llama-cpp-python в venv, не блокируя окно настроек."""

    stage = Signal(str)
    progress = Signal(int, int)  # получено байт, всего байт (0 — неизвестно)
    done = Signal(bool, str, bool)  # ok, error, cancelled

    def __init__(self, force: bool = False) -> None:
        super().__init__()
        self._cancel = threading.Event()
        self._force = force

    def cancel_install(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D102
        try:
            install_llama_cpp(
                on_stage=self.stage.emit,
                cancel=self._cancel,
                on_progress=self.progress.emit,
                force=self._force,
            )
            self.done.emit(True, "", False)
        except LlamaCppInstallCancelled:
            self.done.emit(False, "", True)
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc), False)


class _ModelsWorker(QThread):
    """Тянет живой список моделей провайдера в фоне, не блокируя GUI."""

    done = Signal(str, list)

    def __init__(self, provider: str, cfg: Any) -> None:
        super().__init__()
        self._provider = provider
        self._cfg = cfg

    def run(self) -> None:  # noqa: D102
        try:
            models, _ = list_models(self._provider, self._cfg)
        except Exception:  # noqa: BLE001
            models = []
        self.done.emit(self._provider, models)


class _TelegramCheckWorker(QThread):
    """Проверяет токен бота через getMe, не блокируя GUI."""

    done = Signal(bool, str)

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def run(self) -> None:  # noqa: D102
        import urllib.request

        try:
            url = f"https://api.telegram.org/bot{self._token}/getMe"
            with urllib.request.urlopen(url, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("ok"):
                self.done.emit(True, str(data["result"].get("username") or ""))
            else:
                self.done.emit(False, str(data.get("description") or ""))
        except Exception as exc:  # noqa: BLE001
            # В тексте HTTPError может оказаться URL с токеном — маскируем
            self.done.emit(False, str(exc).replace(self._token, "***"))


class _MCPServerCard(QFrame):
    """Карточка одного MCP-сервера в списке: шапка с тумблером и статусом + форма.

    Живёт рядом с моделью ``server``: шапка (название, команда, счётчик
    инструментов, точка состояния) перерисовывается из неё, а форма пишет
    правки прямо в ``server`` — отдельного «толкания» не требуется.
    """

    probe_requested = Signal(object)  # просит диалог проверить этот сервер
    delete_requested = Signal(object)  # просит диалог удалить этот сервер

    def __init__(self, server: MCPServerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MCPCard")
        self.server = server
        #: состояние последней проверки: "" | "ok" | "fail"
        self.state = ""
        #: текст последней проверки (инструменты или ошибка)
        self.message = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # --- шапка: тумблер, название/команда, статус, кнопки
        head = QHBoxLayout()
        head.setSpacing(10)
        self.sw_enable = ToggleSwitch()
        self.sw_enable.setAccessibleName(i18n.tr("mcp.server_enable"))
        self.sw_enable.setToolTip(i18n.tr("mcp.server_enable_tip"))
        head.addWidget(self.sw_enable, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.lbl_name = ElidedLabel()
        self.lbl_name.setStyleSheet("font-weight:600;font-size:13px;")
        self.lbl_cmd = ElidedLabel()
        self.lbl_cmd.setStyleSheet(_HINT)
        titles.addWidget(self.lbl_name)
        titles.addWidget(self.lbl_cmd)
        head.addLayout(titles, 1)

        self.lbl_tools = QLabel("")
        self.lbl_tools.setStyleSheet(_HINT)
        head.addWidget(self.lbl_tools, 0, Qt.AlignmentFlag.AlignTop)
        self.lbl_dot = QLabel("●")
        self.lbl_dot.setObjectName("DotLabel")
        self.lbl_dot.setStyleSheet(f"color:{TEXT_DIM};font-size:13px;")
        head.addWidget(self.lbl_dot, 0, Qt.AlignmentFlag.AlignTop)

        self.btn_probe = QPushButton(i18n.tr("mcp.probe"))
        self.btn_probe.setObjectName("Ghost")
        self.btn_probe.setToolTip(i18n.tr("mcp.probe_tip"))
        head.addWidget(self.btn_probe, 0, Qt.AlignmentFlag.AlignTop)
        self.btn_delete = QPushButton(i18n.tr("mcp.delete"))
        self.btn_delete.setObjectName("Ghost")
        self.btn_delete.setToolTip(i18n.tr("mcp.delete_tip"))
        head.addWidget(self.btn_delete, 0, Qt.AlignmentFlag.AlignTop)
        self.btn_expand = QPushButton("▾")
        self.btn_expand.setObjectName("Ghost")
        self.btn_expand.setCheckable(True)
        self.btn_expand.setFixedWidth(34)
        self.btn_expand.setToolTip(i18n.tr("mcp.expand_tip"))
        head.addWidget(self.btn_expand, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        # --- раскрываемая форма настроек
        form_wrap = QWidget()
        form_wrap.setObjectName("MCPCardForm")
        form = QFormLayout(form_wrap)
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(10)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText(i18n.tr("mcp.name_hint"))
        self.edit_cmd = QLineEdit()
        self.edit_cmd.setPlaceholderText(i18n.tr("mcp.cmd_ph"))
        self.edit_cmd.setToolTip(i18n.tr("mcp.cmd_tip"))
        self.edit_args = QLineEdit()
        self.edit_args.setPlaceholderText(i18n.tr("mcp.args_ph"))
        self.edit_cwd = QLineEdit()
        self.edit_cwd.setPlaceholderText(".")
        self.edit_env = QPlainTextEdit()
        self.edit_env.setPlaceholderText(i18n.tr("mcp.env_hint"))
        self.edit_env.setFixedHeight(56)
        form.addRow(i18n.tr("mcp.name"), self.edit_name)
        form.addRow(i18n.tr("mcp.cmd"), self.edit_cmd)
        form.addRow(i18n.tr("mcp.args"), self.edit_args)
        form.addRow(i18n.tr("mcp.cwd"), self.edit_cwd)
        form.addRow(i18n.tr("mcp.env"), self.edit_env)
        outer.addWidget(form_wrap)
        self._form_wrap = form_wrap
        form_wrap.hide()

        # --- модель ↔ вид
        self.sw_enable.toggled.connect(self._on_edited)
        self.edit_name.textChanged.connect(self._on_edited)
        self.edit_cmd.textChanged.connect(self._on_edited)
        self.edit_args.textChanged.connect(self._on_edited)
        self.edit_cwd.textChanged.connect(self._on_edited)
        self.edit_env.textChanged.connect(self._on_edited)
        self.btn_expand.toggled.connect(self._on_expand)
        self.btn_probe.clicked.connect(lambda: self.probe_requested.emit(self))
        self.btn_delete.clicked.connect(lambda: self.delete_requested.emit(self))

        self.pull()

    # -- модель ↔ вид
    def pull(self) -> None:
        """Заполняет виджеты из модели (без сигналов, чтобы не зациклиться)."""
        server = self.server
        for wdg in (
            self.sw_enable,
            self.edit_name,
            self.edit_cmd,
            self.edit_args,
            self.edit_cwd,
            self.edit_env,
        ):
            wdg.blockSignals(True)
        self.sw_enable.setChecked(server.enabled)
        self.edit_name.setText(server.name)
        self.edit_cmd.setText(server.command)
        self.edit_args.setText(" ".join(server.args))
        self.edit_cwd.setText(server.cwd)
        self.edit_env.setPlainText("\n".join(f"{k}={v}" for k, v in server.env.items()))
        for wdg in (
            self.sw_enable,
            self.edit_name,
            self.edit_cmd,
            self.edit_args,
            self.edit_cwd,
            self.edit_env,
        ):
            wdg.blockSignals(False)
        self.repaint_header()

    def _on_edited(self, *_args: Any) -> None:
        """Правки формы и тумблер сразу попадают в модель."""
        server = self.server
        server.name = self.edit_name.text().strip()
        server.command = self.edit_cmd.text().strip()
        server.args = _split_args(self.edit_args.text())
        server.cwd = self.edit_cwd.text().strip() or "."
        server.env = _parse_env(self.edit_env.toPlainText())
        server.enabled = self.sw_enable.isChecked()
        self.repaint_header()

    def _on_expand(self, checked: bool) -> None:
        self._form_wrap.setVisible(checked)
        self.btn_expand.setText("▴" if checked else "▾")

    def set_expanded(self, expanded: bool) -> None:
        """Раскрывает/сворачивает форму (гасит сигналы кнопки)."""
        self.btn_expand.blockSignals(True)
        self.btn_expand.setChecked(expanded)
        self.btn_expand.blockSignals(False)
        self._form_wrap.setVisible(expanded)
        self.btn_expand.setText("▴" if expanded else "▾")

    # -- вид
    def repaint_header(self) -> None:
        """Перерисовывает шапку по модели: название, команда, точка состояния."""
        server = self.server
        self.lbl_name.setText(server.name.strip() or i18n.tr("mcp.nameless"))
        args = " ".join(server.args)
        self.lbl_cmd.setText(f"{server.command} {args}".strip() or i18n.tr("mcp.cmd_empty"))
        color = {"ok": ACCENT_SUCCESS, "fail": ACCENT_DANGER}.get(self.state, TEXT_DIM)
        self.lbl_dot.setStyleSheet(f"color:{color};font-size:13px;")
        # рамка карточки тонируется по состоянию (#MCPCard[probe=...])
        self.setProperty("probe", self.state)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        if self.message:
            key = "mcp.tools_tip" if self.state == "ok" else "mcp.fail_tip"
            tip = i18n.tr(key, tools=self.message, msg=self.message)
            self.lbl_dot.setToolTip(tip)
            self.lbl_tools.setToolTip(tip)
        else:
            self.lbl_dot.setToolTip("")
            self.lbl_tools.setToolTip("")

    def set_tools(self, count: int | None) -> None:
        """Показывает/прячет счётчик инструментов в шапке."""
        self.lbl_tools.setText(i18n.tr("mcp.tools_count", n=count) if count is not None else "")


class _SkillRow(QFrame):
    """Строка списка скиллов: галочка, название и краткое описание в переносе.

    Описания скиллов — абзацы текста, поэтому строка двухстрочная: одна строка
    с длинным текстом распирала вкладку и обрезалась по краю окна. Клик по
    краткому описанию разворачивает подробности прямо под ним — в ширину строки,
    а не отдельным окном на всю ширину экрана.
    """

    def __init__(
        self,
        name: str,
        description: str,
        path: str,
        checked: bool,
        skill: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SkillRow")
        self.name = name
        self._description = str(description or "")
        self._path = path
        self._highlights = skill_highlights(getattr(skill, "body", "") or "")
        self._tools = list(getattr(skill, "allowed_tools", None) or [])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        tooltip = f"{name}\n{path}"
        if self._description:
            tooltip += f"\n\n{self._description}"
        self.setToolTip(tooltip)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        self.box = QCheckBox()
        self.box.setChecked(checked)
        self.box.setAccessibleName(name)
        self.box.setToolTip(i18n.tr("skills.toggle_tip", name=name))
        row.addWidget(self.box, 0, Qt.AlignmentFlag.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet("font-weight:600;font-size:13px;")
        self.lbl_desc = QLabel()
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.lbl_desc.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        # описание — кнопка «подробнее»: клик не переключает скилл
        self.lbl_desc.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_desc.setToolTip(i18n.tr("skills.more"))
        self.lbl_desc.installEventFilter(self)
        texts.addWidget(self.lbl_name)
        texts.addWidget(self.lbl_desc)
        self.detail = self._build_detail()
        #: подробности открыты (isVisible() не годится: вкладка может быть скрыта)
        self._details_open = False
        self.detail.setVisible(False)
        texts.addWidget(self.detail)
        row.addLayout(texts, 1)
        self._refresh_desc()

        self.box.toggled.connect(self._on_toggled)
        self._paint_state()

    # ------------------------------------------------------------ подробности
    def _build_detail(self) -> QFrame:
        """Панель подробностей: полное описание, разделы инструкции, инструменты.

        Всё в переносе и с невидимыми разрывами в длинных токенах, поэтому
        панель не шире строки и не растягивает вкладку.
        """
        box = QFrame()
        box.setObjectName("SkillDetail")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        def add(text: str, dim: bool = False) -> QLabel:
            lbl = QLabel(_soft_wrap(text))
            lbl.setWordWrap(True)
            # текст скилла — не разметка: показываем ровно то, что в файле
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setStyleSheet(f"font-size:12px;color:{TEXT_DIM};" if dim else "font-size:12px;")
            v.addWidget(lbl)
            return lbl

        if self._description:
            add(self._description)
        if self._highlights:
            bullets = "\n".join(f"• {h}" for h in self._highlights)
            add(f"{i18n.tr('skills.in_instruction')}\n{bullets}")
        if self._tools:
            add(i18n.tr("skills.tools") + " " + ", ".join(self._tools), dim=True)
        add(i18n.tr("skills.path") + " " + self._path, dim=True)
        return box

    def _refresh_desc(self) -> None:
        """Краткое описание со стрелкой: ▸ свёрнуто, ▾ подробности открыты."""
        mark = "▾" if self._details_open else "▸"
        short = _short_text(self._description) or i18n.tr("skills.no_desc")
        self.lbl_desc.setText(f"{mark} {short}")

    def _toggle_details(self) -> None:
        """Разворачивает/сворачивает подробности по клику на описание."""
        self._details_open = not self._details_open
        self.detail.setVisible(self._details_open)
        self._refresh_desc()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.lbl_desc and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._toggle_details()
                return True
        return super().eventFilter(obj, event)

    def _on_toggled(self, _checked: bool) -> None:
        self._paint_state()

    def _paint_state(self) -> None:
        """Выключенный скилл виден сразу: строка тускнеет и теряет фон."""
        on = self.box.isChecked()
        self.setProperty("state", "on" if on else "off")
        color = TEXT if on else TEXT_DIM
        self.lbl_name.setStyleSheet(f"font-weight:600;font-size:13px;color:{color};")
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Клик по строке (не только по квадратику) переключает скилл."""
        if event.button() == Qt.MouseButton.LeftButton and self.box.isEnabled():
            self.box.toggle()
            event.accept()
            return
        super().mousePressEvent(event)


class SettingsDialog(QDialog):
    """Диалог настроек: провайдеры, ключи API, голос, инструменты."""

    def __init__(self, parent: QWidget | None = None, core: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("set.window"))
        self.setStyleSheet(qss())
        self.resize(940, 640)
        self.setMinimumSize(820, 560)
        self.config = load_config()
        #: живое ядро приложения (для статуса/обновления MCP) — может быть None
        self.core = core
        #: последний живой список моделей по провайдерам (кэш для фильтров)
        self._models_cache: dict[str, list[tuple[str, bool]]] = {}
        #: все живые воркеры списков: быстрые переключения не должны убивать QThread
        self._models_workers: set[_ModelsWorker] = set()
        self._build_ui()
        self._load()
        # подключаем после _load: начальное заполнение списка моделей —
        # синхронное (статический фолбэк), живая подгрузка — только по действию
        # пользователя, чтобы не держать фоновый поток при закрытии диалога
        self.cb_provider.currentIndexChanged.connect(self._on_provider_changed)

    # --------------------------------------------------------------- build ui
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(18)
        layout.setContentsMargins(28, 24, 28, 24)

        header = QVBoxLayout()
        header.setSpacing(2)
        title = QLabel(i18n.tr("set.title"))
        title.setObjectName("HeaderLabel")
        sub = QLabel(i18n.tr("set.sub"))
        sub.setObjectName("SubLabel")
        header.addWidget(title)
        header.addWidget(sub)
        layout.addLayout(header)

        tabs = QTabWidget()
        self._tts_page = self._scrollable(self._build_tts_tab())
        self._mcp_page = self._scrollable(self._build_mcp_tab())
        tabs.addTab(self._scrollable(self._build_llm_tab()), i18n.tr("tab.llm"))
        tabs.addTab(self._scrollable(self._build_voice_tab()), i18n.tr("tab.voice"))
        tabs.addTab(
            self._scrollable(self._build_character_tab()), i18n.tr("tab.character")
        )
        tabs.addTab(self._tts_page, i18n.tr("tab.tts"))
        tabs.addTab(
            self._scrollable(self._build_spotify_tab()), i18n.tr("tab.spotify")
        )
        tabs.addTab(
            self._scrollable(self._build_telegram_tab()), i18n.tr("tab.telegram")
        )
        tabs.addTab(self._scrollable(self._build_tools_tab()), i18n.tr("tab.tools"))
        tabs.addTab(self._mcp_page, i18n.tr("tab.mcp"))
        self._skills_page = self._scrollable(self._build_skills_tab())
        tabs.addTab(self._skills_page, i18n.tr("tab.skills"))
        tabs.addTab(self._scrollable(self._build_about_tab()), i18n.tr("tab.about"))
        tabs.tabBar().setExpanding(False)
        tabs.setDocumentMode(False)
        tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs = tabs
        layout.addWidget(tabs)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        cancel_btn = QPushButton(i18n.tr("btn.cancel"))
        cancel_btn.setObjectName("Ghost")
        save_btn = QPushButton(i18n.tr("btn.save"))
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save)
        cancel_btn.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        layout.addLayout(buttons)

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        """Оборачивает содержимое вкладки в прокручиваемую область."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        # если содержимое не влезает по ширине — показываем полосу, а не прячем
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setWidget(widget)
        return area

    # ------------------------------------------------------------- ХАРАКТЕР
    def _build_character_tab(self) -> QWidget:
        """Базовый и расширенный характер, чувства и инициатива."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(12)

        intro = QLabel(i18n.tr("character.intro"))
        intro.setWordWrap(True)
        intro.setStyleSheet(_HINT)
        v.addWidget(intro)

        self.chk_character_enabled = QCheckBox(i18n.tr("character.enabled"))
        v.addWidget(self.chk_character_enabled)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(QLabel(i18n.tr("character.mode")))
        self.cb_character_mode = QComboBox()
        self.cb_character_mode.addItem(
            i18n.tr("character.mode.basic"), "basic"
        )
        self.cb_character_mode.addItem(
            i18n.tr("character.mode.advanced"), "advanced"
        )
        mode_row.addWidget(self.cb_character_mode, 1)
        v.addLayout(mode_row)

        self._character_stack = QStackedWidget()

        basic = QWidget()
        bv = QVBoxLayout(basic)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)

        traits = QGroupBox(i18n.tr("character.traits"))
        tform = QFormLayout(traits)
        tform.setSpacing(8)
        self._character_spins: dict[str, QSpinBox] = {}
        for attr, key in (
            ("playfulness", "character.playfulness"),
            ("sensitivity", "character.sensitivity"),
            ("attachment", "character.attachment"),
            ("independence", "character.independence"),
            ("curiosity", "character.curiosity"),
        ):
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setSingleStep(5)
            spin.setSuffix(" / 100")
            self._character_spins[attr] = spin
            tform.addRow(i18n.tr(key), spin)
        bv.addWidget(traits)

        feelings = QGroupBox(i18n.tr("character.feelings"))
        fform = QFormLayout(feelings)
        fform.setSpacing(8)
        self._character_feelings: dict[str, QSpinBox] = {}
        for attr, key, low, high in (
            ("base_mood", "character.mood", -100, 100),
            ("base_energy", "character.energy", 0, 100),
            ("base_trust", "character.trust", 0, 100),
            ("base_hurt", "character.hurt", 0, 100),
        ):
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(5)
            self._character_feelings[attr] = spin
            fform.addRow(i18n.tr(key), spin)
        bv.addWidget(feelings)

        proactive = QGroupBox(i18n.tr("character.proactive"))
        pform = QFormLayout(proactive)
        pform.setSpacing(8)
        self.chk_character_proactive = QCheckBox(
            i18n.tr("character.proactive_enabled")
        )
        pform.addRow(self.chk_character_proactive)
        rhythm = QLabel(i18n.tr("character.rhythm"))
        rhythm.setWordWrap(True)
        rhythm.setStyleSheet(_HINT)
        pform.addRow(rhythm)
        quiet_row = QHBoxLayout()
        quiet_row.setSpacing(8)
        self.edit_character_quiet_start = QLineEdit()
        self.edit_character_quiet_start.setPlaceholderText("23:00")
        self.edit_character_quiet_end = QLineEdit()
        self.edit_character_quiet_end.setPlaceholderText("08:00")
        quiet_row.addWidget(QLabel(i18n.tr("character.quiet")))
        quiet_row.addWidget(self.edit_character_quiet_start, 1)
        quiet_row.addWidget(QLabel("—"))
        quiet_row.addWidget(self.edit_character_quiet_end, 1)
        pform.addRow(quiet_row)
        bv.addWidget(proactive)

        basic_hint = QLabel(i18n.tr("character.hint"))
        basic_hint.setWordWrap(True)
        basic_hint.setStyleSheet(_HINT)
        bv.addWidget(basic_hint)
        bv.addStretch(1)
        self._character_stack.addWidget(basic)

        advanced_page = QWidget()
        av = QVBoxLayout(advanced_page)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(10)
        advanced_intro = QLabel(i18n.tr("character.advanced_intro"))
        advanced_intro.setWordWrap(True)
        advanced_intro.setStyleSheet(_HINT)
        av.addWidget(advanced_intro)
        self.edit_character_prompt = QPlainTextEdit()
        self.edit_character_prompt.setPlaceholderText(
            i18n.tr("character.advanced_placeholder")
        )
        self.edit_character_prompt.setMinimumHeight(220)
        av.addWidget(self.edit_character_prompt, 1)
        advanced_hint = QLabel(i18n.tr("character.advanced_hint"))
        advanced_hint.setWordWrap(True)
        advanced_hint.setStyleSheet(_HINT)
        av.addWidget(advanced_hint)
        self._character_stack.addWidget(advanced_page)

        self.cb_character_mode.currentIndexChanged.connect(
            self._character_stack.setCurrentIndex
        )
        v.addWidget(self._character_stack, 1)

        state_box = QGroupBox(i18n.tr("character.state"))
        sv = QVBoxLayout(state_box)
        sv.setSpacing(8)
        self.lbl_character_state = QLabel("")
        self.lbl_character_state.setWordWrap(True)
        self.lbl_character_state.setStyleSheet(_HINT)
        sv.addWidget(self.lbl_character_state)
        state_buttons = QHBoxLayout()
        state_buttons.setSpacing(8)
        refresh = QPushButton(i18n.tr("character.refresh"))
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self._refresh_character_state)
        reset = QPushButton(i18n.tr("character.reset"))
        reset.setObjectName("Ghost")
        reset.clicked.connect(self._reset_character_state)
        state_buttons.addWidget(refresh)
        state_buttons.addWidget(reset)
        state_buttons.addStretch(1)
        sv.addLayout(state_buttons)
        v.addWidget(state_box)
        return w

    def _character_store(self):
        from nemus.core.emotions import EmotionStore

        return EmotionStore(self.config.character.state_file)

    def _refresh_character_state(self) -> None:
        """Показывает текущее настроение из общего файла состояния."""
        try:
            from nemus.core.emotions import summary_text

            state = self._character_store().snapshot()
            self.lbl_character_state.setText(summary_text(self.config, state))
        except Exception as exc:  # noqa: BLE001
            self.lbl_character_state.setText(str(exc))

    def _reset_character_state(self) -> None:
        if (
            QMessageBox.question(
                self,
                i18n.tr("character.reset"),
                i18n.tr("character.reset_confirm"),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self._character_store().reset(self.config.character)
            self._refresh_character_state()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, i18n.tr("character.reset"), str(exc))

    # ------------------------------------------------------------------ LLM
    def _build_llm_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Немуся")

        self._llm_form = form
        self.cb_provider = QComboBox()
        self.cb_provider.addItems(PROVIDER_OPTIONS)
        # комбо со своим попапом: поиск по подстроке, фильтры
        # «Все / Бесплатные / Платные», счётчик (456 моделей OpenRouter)
        self.cb_model = ModelComboBox()
        self.cb_model.setToolTip(i18n.tr("llm.model_tip"))
        self.edit_temp = QLineEdit()
        self.edit_max_tokens = QSpinBox()
        self.edit_max_tokens.setRange(256, 200000)
        self.edit_max_tokens.setSingleStep(256)

        form.addRow(i18n.tr("llm.name"), self.name_input)
        form.addRow(i18n.tr("llm.provider"), self.cb_provider)
        form.addRow(i18n.tr("llm.model"), self.cb_model)
        form.addRow(i18n.tr("llm.temp"), self.edit_temp)
        form.addRow(i18n.tr("llm.max_tokens"), self.edit_max_tokens)

        os_group = QGroupBox(i18n.tr("llm.os_group"))
        oform = QFormLayout(os_group)
        oform.setSpacing(10)
        self.cb_os_family = QComboBox()
        for code in ("auto", "windows", "linux", "macos"):
            self.cb_os_family.addItem(i18n.tr(f"os.opt.{code}"), code)
        self.edit_os_details = QLineEdit()
        self.edit_os_details.setPlaceholderText(
            "Ubuntu 24.04 · Windows 11 Pro · Fedora 40 · macOS Sonoma"
        )
        oform.addRow(i18n.tr("os.family"), self.cb_os_family)
        oform.addRow(i18n.tr("os.details"), self.edit_os_details)
        os_hint = QLabel(i18n.tr("os.tip"))
        os_hint.setWordWrap(True)
        os_hint.setStyleSheet(_HINT)
        oform.addRow("", os_hint)
        form.addRow(os_group)

        local = self._local_group = QGroupBox(i18n.tr("llm.local_group"))
        lform = QFormLayout(local)
        lform.setSpacing(10)
        self.edit_lc_path = QLineEdit()
        self.edit_lc_path.setPlaceholderText("data/models/model.gguf")
        self.btn_lc_browse = QPushButton(i18n.tr("llm.local_browse"))
        self.btn_lc_browse.clicked.connect(self._browse_gguf)
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_row.addWidget(self.edit_lc_path, 1)
        path_row.addWidget(self.btn_lc_browse)
        self.edit_lc_host = QLineEdit()
        self.edit_lc_host.setPlaceholderText("127.0.0.1")
        self.spin_lc_port = QSpinBox()
        self.spin_lc_port.setRange(1, 65535)
        self.spin_lc_ctx = QSpinBox()
        self.spin_lc_ctx.setRange(512, 262144)
        self.spin_lc_ctx.setSingleStep(1024)
        self.spin_lc_gpu = QSpinBox()
        self.spin_lc_gpu.setRange(-1, 512)
        self.spin_lc_gpu.setToolTip(i18n.tr("llm.local_gpu_tip"))
        self.spin_lc_threads = QSpinBox()
        self.spin_lc_threads.setRange(0, 256)
        self.chk_lc_autostart = QCheckBox(i18n.tr("llm.local_autostart"))
        lform.addRow(i18n.tr("llm.local_path"), path_row)
        lform.addRow("", self.chk_lc_autostart)

        self.lbl_lc_pkg = QLabel("")
        self.lbl_lc_pkg.setWordWrap(True)
        self.btn_lc_install = QPushButton(i18n.tr("llm.local_install"))
        self.btn_lc_install.clicked.connect(self._install_or_cancel_lc)
        pkg_row = QHBoxLayout()
        pkg_row.setSpacing(8)
        pkg_row.addWidget(self.lbl_lc_pkg, 1)
        pkg_row.addWidget(self.btn_lc_install)
        self.lc_install_progress = QProgressBar()
        self.lc_install_progress.setRange(0, 0)
        self.lc_install_progress.setTextVisible(False)
        self.lc_install_progress.setVisible(False)
        lform.addRow(i18n.tr("llm.local_pkg"), pkg_row)
        lform.addRow("", self.lc_install_progress)

        self.btn_lc_advanced = QPushButton("▸ " + i18n.tr("llm.local_advanced"))
        self.btn_lc_advanced.setObjectName("Ghost")
        self.btn_lc_advanced.setCheckable(True)
        self.btn_lc_advanced.toggled.connect(self._toggle_lc_advanced)
        self._lc_advanced = QWidget()
        aform = QFormLayout(self._lc_advanced)
        aform.setContentsMargins(0, 0, 0, 0)
        aform.setSpacing(10)
        aform.addRow(i18n.tr("llm.local_host"), self.edit_lc_host)
        aform.addRow(i18n.tr("llm.local_port"), self.spin_lc_port)
        aform.addRow(i18n.tr("llm.local_ctx"), self.spin_lc_ctx)
        aform.addRow(i18n.tr("llm.local_gpu"), self.spin_lc_gpu)
        aform.addRow(i18n.tr("llm.local_threads"), self.spin_lc_threads)
        self._lc_advanced.setVisible(False)
        lform.addRow("", self.btn_lc_advanced)
        lform.addRow("", self._lc_advanced)

        local_hint = QLabel(i18n.tr("llm.local_tip"))
        local_hint.setWordWrap(True)
        local_hint.setStyleSheet(_HINT)
        lform.addRow("", local_hint)
        form.addRow(local)

        hf = self._hf_group = QGroupBox(i18n.tr("llm.hf_group"))
        hbox = QVBoxLayout(hf)
        hbox.setSpacing(8)

        self.edit_hf_query = QLineEdit()
        self.edit_hf_query.setPlaceholderText(i18n.tr("llm.hf_search_ph"))
        self.edit_hf_query.setClearButtonEnabled(True)
        self.edit_hf_query.returnPressed.connect(self._search_hf)
        self.btn_hf_search = QPushButton(i18n.tr("llm.hf_search"))
        self.btn_hf_search.setMinimumWidth(88)
        self.btn_hf_search.clicked.connect(self._search_hf)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self.edit_hf_query, 1)
        search_row.addWidget(self.btn_hf_search)

        self.lbl_hf_search = QLabel(i18n.tr("llm.hf_search_hint"))
        self.lbl_hf_search.setWordWrap(True)
        self.lbl_hf_search.setStyleSheet(_HINT)

        self.cb_hf_results = QListWidget()
        self.cb_hf_results.setObjectName("HFList")
        self.cb_hf_results.setMinimumHeight(150)
        self.cb_hf_results.setMaximumHeight(220)
        self.cb_hf_results.setToolTip(i18n.tr("llm.hf_results_ph"))
        self.cb_hf_results.currentItemChanged.connect(self._on_hf_result_selected)
        self.cb_hf_files = QListWidget()
        self.cb_hf_files.setObjectName("HFList")
        self.cb_hf_files.setMinimumHeight(96)
        self.cb_hf_files.setMaximumHeight(150)
        self.cb_hf_files.setToolTip(i18n.tr("llm.hf_files_ph"))
        self.cb_hf_files.currentItemChanged.connect(self._on_hf_file_selected)

        results_caption = QLabel(i18n.tr("llm.hf_results"))
        results_caption.setStyleSheet("font-weight:600;font-size:12px;")
        files_caption = QLabel(i18n.tr("llm.hf_files"))
        files_caption.setStyleSheet("font-weight:600;font-size:12px;")

        self.edit_hf_repo = QLineEdit()
        self.edit_hf_repo.setPlaceholderText("Qwen/Qwen2.5-7B-Instruct-GGUF")
        self.edit_hf_file = QLineEdit()
        self.edit_hf_file.setPlaceholderText("qwen2.5-7b-instruct-q4_k_m.gguf")
        self.edit_hf_revision = QLineEdit()
        self.edit_hf_revision.setPlaceholderText("main")
        self.chk_hf_auto = QCheckBox(i18n.tr("llm.hf_auto"))

        self.btn_hf_manual = QPushButton("▸ " + i18n.tr("llm.hf_manual"))
        self.btn_hf_manual.setObjectName("Ghost")
        self.btn_hf_manual.setCheckable(True)
        self.btn_hf_manual.toggled.connect(self._toggle_hf_manual)
        self._hf_manual = QWidget()
        mform = QFormLayout(self._hf_manual)
        mform.setContentsMargins(0, 0, 0, 0)
        mform.setSpacing(8)
        mform.addRow(i18n.tr("llm.hf_repo"), self.edit_hf_repo)
        mform.addRow(i18n.tr("llm.hf_file"), self.edit_hf_file)
        mform.addRow(i18n.tr("llm.hf_revision"), self.edit_hf_revision)
        self._hf_manual.setVisible(False)
        self.edit_hf_repo.textChanged.connect(self._update_hf_download_state)
        self.edit_hf_file.textChanged.connect(self._update_hf_download_state)

        self.btn_hf_download = QPushButton(i18n.tr("llm.hf_download"))
        self.btn_hf_download.setObjectName("Primary")
        self.btn_hf_download.setEnabled(False)
        self.btn_hf_download.clicked.connect(self._download_hf_model)
        self.hf_progress = QProgressBar()
        self.hf_progress.setRange(0, 100)
        self.hf_progress.setValue(0)
        self.hf_progress.setVisible(False)
        self.lbl_hf_status = QLabel("")
        self.lbl_hf_status.setWordWrap(True)
        self.lbl_hf_status.setStyleSheet(_HINT)
        download_row = QHBoxLayout()
        download_row.setSpacing(8)
        download_row.addWidget(self.btn_hf_download)
        download_row.addWidget(self.lbl_hf_status, 1)

        hbox.addLayout(search_row)
        hbox.addWidget(self.lbl_hf_search)
        hbox.addWidget(results_caption)
        hbox.addWidget(self.cb_hf_results)
        hbox.addWidget(files_caption)
        hbox.addWidget(self.cb_hf_files)
        hbox.addLayout(download_row)
        hbox.addWidget(self.hf_progress)
        hbox.addWidget(self.btn_hf_manual)
        hbox.addWidget(self._hf_manual)
        hbox.addWidget(self.chk_hf_auto)
        hf_hint = QLabel(i18n.tr("llm.hf_tip"))
        hf_hint.setWordWrap(True)
        hf_hint.setStyleSheet(_HINT)
        hbox.addWidget(hf_hint)
        form.addRow(hf)
        self._hf_worker: _HFDownloadWorker | None = None
        self._hf_search_worker: _HFSearchWorker | None = None
        self._hf_files_worker: _HFFilesWorker | None = None
        self._hf_pending_repo = ""
        self._lc_install_worker: _LlamaInstallWorker | None = None
        self._lc_install_running = False
        self._lc_package_ok = False
        self._lc_force_reinstall = False
        self._refresh_lc_package_state()

        keys = self._keys_group = QGroupBox(i18n.tr("llm.keys"))
        kform = QFormLayout(keys)
        kform.setSpacing(10)
        self.key_edits: dict[str, QLineEdit] = {}
        for name in PROVIDER_OPTIONS:
            le = QLineEdit()
            le.setEchoMode(QLineEdit.EchoMode.Password)
            le.setPlaceholderText(i18n.tr("llm.key_for", name=name))
            kform.addRow(name, le)
            self.key_edits[name] = le
        form.addRow(keys)

        langs = QGroupBox(i18n.tr("lang.group"))
        lform = QFormLayout(langs)
        lform.setSpacing(10)
        self.cb_reply_lang = QComboBox()
        for code, label in REPLY_CHOICES:
            self.cb_reply_lang.addItem(i18n.tr(f"lang.opt.{code}") or label, code)
        self.cb_ui_lang = QComboBox()
        for code, label in i18n.available_languages():
            self.cb_ui_lang.addItem(label, code)
        lform.addRow(i18n.tr("lang.reply"), self.cb_reply_lang)
        lform.addRow(i18n.tr("lang.ui"), self.cb_ui_lang)
        lang_hint = QLabel(
            i18n.tr("lang.reply_tip") + "\n" + i18n.tr("lang.ui_tip")
        )
        lang_hint.setWordWrap(True)
        lang_hint.setStyleSheet(_HINT)
        lform.addRow("", lang_hint)
        form.addRow(langs)

        return w

    # ------------------------------------------------------- список моделей
    def _fill_models(
        self,
        provider: str,
        keep: str = "",
        models: list[tuple[str, bool]] | None = None,
    ) -> None:
        """Заполняет combo моделей: бесплатные сверху, у каждого — бейдж.

        С учётом фильтра «только бесплатные»: список сужается, но если
        выбранная/введённая модель не проходит фильтр — она остаётся в строке.
        """
        if models is None:
            models = self._models_cache.get(provider) or static_models(provider)
        current = keep if keep is not None else ""
        self.cb_model.blockSignals(True)
        self.cb_model.clear()
        for model, free in models:
            badge = i18n.tr("llm.free") if free else i18n.tr("llm.paid")
            self.cb_model.addItem(f"{model} — {badge}", model)
            self.cb_model.setItemData(self.cb_model.count() - 1, free, ROLE_FREE)
        index = self.cb_model.findData(current)
        if index >= 0:
            self.cb_model.setCurrentIndex(index)
        else:
            self.cb_model.setEditText(current)
        self.cb_model.blockSignals(False)

    def _browse_gguf(self) -> None:
        """Выбор локального GGUF-файла для llama.cpp."""
        start = self.edit_lc_path.text().strip() or "data/models"
        path, _ = QFileDialog.getOpenFileName(
            self,
            i18n.tr("llm.local_group"),
            start,
            "GGUF (*.gguf);;All files (*)",
        )
        if path:
            self.edit_lc_path.setText(path)

    def _hf_endpoint(self) -> str:
        return self.config.llm.llama_cpp.hf_endpoint or "https://huggingface.co"

    def _search_hf(self) -> None:
        """Ищет GGUF-модели на Hugging Face в фоне."""
        worker = self._hf_search_worker
        if worker is not None and worker.isRunning():
            return
        self.btn_hf_search.setEnabled(False)
        self.lbl_hf_search.setText(i18n.tr("llm.hf_searching"))
        worker = _HFSearchWorker(
            self.edit_hf_query.text().strip(), self._hf_endpoint()
        )
        worker.done.connect(self._hf_search_done)
        self._hf_search_worker = worker
        worker.start()

    def _hf_search_done(self, ok: bool, models: Any, error: str) -> None:
        self.btn_hf_search.setEnabled(True)
        if not ok:
            self.lbl_hf_search.setText(i18n.tr("llm.hf_search_failed", msg=error))
            return
        self.cb_hf_results.clear()
        self.cb_hf_files.clear()
        self.edit_hf_file.clear()
        self._update_hf_download_state()
        items = list(models or [])
        for model in items:
            downloads = f"{model.downloads:,}".replace(",", " ")
            label = f"{model.repo_id}\n⬇ {downloads} · ♥ {model.likes}"
            if model.gated:
                label += " · 🔒"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, model.repo_id)
            item.setToolTip(model.repo_id)
            self.cb_hf_results.addItem(item)
        if items:
            self.lbl_hf_search.setText(i18n.tr("llm.hf_found", n=len(items)))
            self.cb_hf_results.setCurrentRow(0)
        else:
            self.lbl_hf_search.setText(i18n.tr("llm.hf_not_found"))

    def _on_hf_result_selected(
        self, current: QListWidgetItem | None, _previous: Any = None
    ) -> None:
        if current is None:
            return
        repo = str(current.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not repo:
            return
        self.edit_hf_repo.setText(repo)
        self._load_hf_files(repo)

    def _load_hf_files(self, repo: str) -> None:
        """Подтягивает .gguf-файлы выбранного репозитория."""
        worker = self._hf_files_worker
        if worker is not None and worker.isRunning():
            self._hf_pending_repo = repo
            QTimer.singleShot(100, self._load_pending_hf_files)
            return
        self.cb_hf_files.clear()
        self.edit_hf_file.clear()
        self._update_hf_download_state()
        self.lbl_hf_search.setText(i18n.tr("llm.hf_files_loading"))
        worker = _HFFilesWorker(repo, self._hf_endpoint())
        worker.done.connect(self._hf_files_done)
        self._hf_files_worker = worker
        worker.start()

    def _hf_files_done(
        self, ok: bool, files: Any, error: str, repo: str
    ) -> None:
        pending = self._hf_pending_repo
        self._hf_pending_repo = ""
        if repo != self.edit_hf_repo.text().strip():
            if pending:
                QTimer.singleShot(0, self._load_pending_hf_files)
            return  # пользователь уже выбрал другой репозиторий
        if not ok:
            self.lbl_hf_search.setText(i18n.tr("llm.hf_files_failed", msg=error))
            return
        items = group_hf_files(list(files or []))
        self.cb_hf_files.clear()
        self.edit_hf_file.clear()
        self._update_hf_download_state()
        for item in items:
            size = format_bytes(item.size) if item.size else "?"
            suffix = (
                f" · {item.parts} части, будут склеены" if item.split else ""
            )
            entry = QListWidgetItem(f"{item.filename}\n{size}{suffix}")
            entry.setData(Qt.ItemDataRole.UserRole, item.filename)
            entry.setToolTip(item.filename)
            self.cb_hf_files.addItem(entry)
        self.lbl_hf_search.setText(i18n.tr("llm.hf_files_found", n=len(items)))
        if items:
            self.cb_hf_files.setCurrentRow(0)
        if pending:
            QTimer.singleShot(0, self._load_pending_hf_files)

    def _load_pending_hf_files(self) -> None:
        repo = self._hf_pending_repo
        if not repo:
            return
        worker = self._hf_files_worker
        if worker is not None and worker.isRunning():
            QTimer.singleShot(100, self._load_pending_hf_files)
            return
        self._hf_pending_repo = ""
        self._load_hf_files(repo)

    def _on_hf_file_selected(
        self, current: QListWidgetItem | None, _previous: Any = None
    ) -> None:
        filename = ""
        if current is not None:
            filename = str(current.data(Qt.ItemDataRole.UserRole) or "").strip()
        if filename:
            self.edit_hf_file.setText(filename)
        self._update_hf_download_state()

    def _update_hf_download_state(self, *_args: Any) -> None:
        """Кнопка скачивания активна, когда есть репозиторий и файл."""
        worker = self._hf_worker
        if worker is not None and worker.isRunning():
            self.btn_hf_download.setEnabled(False)
            return
        repo = self.edit_hf_repo.text().strip()
        filename = self.edit_hf_file.text().strip()
        direct = "huggingface.co" in repo or "hf.co" in repo
        self.btn_hf_download.setEnabled(bool(repo and (filename or direct)))

    def _toggle_hf_manual(self, checked: bool) -> None:
        """Прячет ручные поля: при обычном поиске они только мешают."""
        self._hf_manual.setVisible(checked)
        arrow = "▾" if checked else "▸"
        self.btn_hf_manual.setText(f"{arrow} {i18n.tr('llm.hf_manual')}")

    def _toggle_lc_advanced(self, checked: bool) -> None:
        """Параметры сервера нужны редко — держим их свёрнутыми."""
        self._lc_advanced.setVisible(checked)
        arrow = "▾" if checked else "▸"
        self.btn_lc_advanced.setText(f"{arrow} {i18n.tr('llm.local_advanced')}")

    def _refresh_lc_package_state(self) -> None:
        """Показывает, работает ли llama-cpp-python, и нужную кнопку."""
        if self._lc_install_running:
            return
        status = package_status()
        self.lbl_lc_pkg.setToolTip("")
        self._lc_package_ok = status.available
        self._lc_force_reinstall = status.broken
        if status.available:
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_pkg_ok"))
            self.lbl_lc_pkg.setStyleSheet(
                f"color:{ACCENT_SUCCESS};font-weight:600;"
            )
            self.btn_lc_install.setVisible(False)
            self.lc_install_progress.setVisible(False)
            return

        if status.broken:
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_pkg_broken"))
            self.lbl_lc_pkg.setToolTip(status.detail)
            self.lbl_lc_pkg.setStyleSheet(
                f"color:{ACCENT_DANGER};font-weight:600;"
            )
        else:
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_pkg_missing"))
            self.lbl_lc_pkg.setStyleSheet(
                f"color:{ACCENT_WARNING};font-weight:600;"
            )
        if getattr(sys, "frozen", False):
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_pkg_manual"))
            self.btn_lc_install.setVisible(False)
        elif status.broken:
            self.btn_lc_install.setText(i18n.tr("llm.local_reinstall"))
            self.btn_lc_install.setEnabled(True)
            self.btn_lc_install.setVisible(True)
        else:
            self.btn_lc_install.setText(i18n.tr("llm.local_install"))
            self.btn_lc_install.setEnabled(True)
            self.btn_lc_install.setVisible(True)

    def _install_or_cancel_lc(self) -> None:
        """Запускает установку пакета или отменяет уже идущую."""
        worker = self._lc_install_worker
        if self._lc_install_running and worker is not None:
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_install_cancelling"))
            self.btn_lc_install.setEnabled(False)
            worker.cancel_install()
            return
        if getattr(sys, "frozen", False):
            return

        worker = _LlamaInstallWorker(force=self._lc_force_reinstall)
        worker.stage.connect(self._lc_install_stage)
        worker.progress.connect(self._lc_install_progress)
        worker.done.connect(self._lc_install_done)
        self._lc_install_worker = worker
        self._lc_install_running = True
        self.btn_lc_install.setText(i18n.tr("llm.local_install_cancel"))
        self.btn_lc_install.setEnabled(True)
        self.lc_install_progress.setRange(0, 0)
        self.lc_install_progress.setVisible(True)
        self.lbl_lc_pkg.setToolTip("")
        self.lbl_lc_pkg.setStyleSheet(f"color:{TEXT_DIM};")
        self.lbl_lc_pkg.setText(i18n.tr("llm.local_install_starting"))
        worker.start()

    def _lc_install_stage(self, stage: str) -> None:
        """Показывает текущий этап установки в строке статуса."""
        key = {
            "resolve": "llm.local_install_resolve",
            "download": "llm.local_install_download",
            "install": "llm.local_install_installing",
            "done": "llm.local_install_finishing",
        }.get(stage)
        if key:
            self.lbl_lc_pkg.setText(i18n.tr(key))

    def _lc_install_progress(self, done: int, total: int) -> None:
        """Показывает точный процент скачивания пакета и зависимостей."""
        if total > 0:
            percent = min(100, int(done * 100 / total))
            self.lc_install_progress.setRange(0, 100)
            self.lc_install_progress.setValue(percent)
            self.lbl_lc_pkg.setText(
                i18n.tr(
                    "llm.local_install_progress",
                    percent=percent,
                    done=format_bytes(done),
                    total=format_bytes(total),
                )
            )
        else:
            self.lc_install_progress.setRange(0, 0)
            self.lbl_lc_pkg.setText(
                i18n.tr("llm.local_install_downloaded", done=format_bytes(done))
            )

    def _lc_install_done(self, ok: bool, error: str, cancelled: bool) -> None:
        """Завершает установку и обновляет статус пакета."""
        self._lc_install_running = False
        self.btn_lc_install.setEnabled(True)
        if cancelled:
            self.lc_install_progress.setVisible(False)
            self._refresh_lc_package_state()
            if self._lc_package_ok:
                return
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_install_cancelled"))
            self.lbl_lc_pkg.setStyleSheet(f"color:{TEXT_DIM};")
            return
        if not ok:
            self.lc_install_progress.setVisible(False)
            # после частичной установки повтор должен быть принудительным
            self._lc_force_reinstall = True
            self.lbl_lc_pkg.setText(
                i18n.tr(
                    "llm.local_install_failed",
                    msg=error.splitlines()[0] if error else "?",
                )
            )
            self.lbl_lc_pkg.setToolTip(error)
            self.lbl_lc_pkg.setStyleSheet(
                f"color:{ACCENT_DANGER};font-weight:600;"
            )
            self.btn_lc_install.setText(i18n.tr("llm.local_install_retry"))
            self.btn_lc_install.setEnabled(True)
            return

        self.lc_install_progress.setRange(0, 100)
        self.lc_install_progress.setValue(100)
        self.lc_install_progress.setVisible(True)
        self._lc_package_ok = True
        self._lc_force_reinstall = False
        self.lbl_lc_pkg.setText(i18n.tr("llm.local_install_done"))
        self.lbl_lc_pkg.setStyleSheet(f"color:{ACCENT_SUCCESS};font-weight:600;")
        self.btn_lc_install.setVisible(False)

    def _cancel_lc_install(self) -> None:
        worker = self._lc_install_worker
        if self._lc_install_running and worker is not None:
            worker.cancel_install()

    def _download_hf_model(self) -> None:
        """Запускает скачивание выбранного GGUF в фоне."""
        if self._hf_worker is not None and self._hf_worker.isRunning():
            return
        repo = self.edit_hf_repo.text().strip()
        filename = self.edit_hf_file.text().strip()
        revision = self.edit_hf_revision.text().strip() or "main"
        if not repo or (
            not filename
            and "huggingface.co" not in repo
            and "hf.co" not in repo
        ):
            self.lbl_hf_status.setText(i18n.tr("llm.hf_need_fields"))
            return

        self.lbl_hf_status.setText(i18n.tr("llm.hf_starting"))
        self.hf_progress.setRange(0, 0)
        self.hf_progress.setVisible(True)
        self.btn_hf_download.setEnabled(False)
        worker = _HFDownloadWorker(self.config, repo, filename, revision)
        worker.progress.connect(self._hf_progress)
        worker.done.connect(self._hf_done)
        self._hf_worker = worker
        worker.start()

    def _hf_progress(self, done: int, total: int) -> None:
        """Обновляет полосу загрузки (вызывается из потока через сигнал)."""
        if total > 0:
            # QProgressBar(int) не переживает размеры в десятки гигабайт,
            # поэтому шкала — тысячные доли, а точный размер показываем текстом.
            self.hf_progress.setRange(0, 1000)
            self.hf_progress.setValue(min(1000, int(done * 1000 / total)))
            self.lbl_hf_status.setText(
                f"{format_bytes(done)} / {format_bytes(total)}"
            )
        else:
            self.hf_progress.setRange(0, 0)
            self.lbl_hf_status.setText(format_bytes(done))

    def _hf_done(self, ok: bool, message: str, path: str) -> None:
        """Завершает загрузку: выбирает файл и обновляет каталог моделей."""
        self.btn_hf_download.setEnabled(True)
        if not ok:
            self.hf_progress.setVisible(False)
            self.lbl_hf_status.setText(i18n.tr("llm.hf_failed", msg=message))
            return

        target = Path(path)
        self.edit_lc_path.setText(str(target))
        try:
            shown = target.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            shown = target.resolve().as_posix()
        model = f"llama_cpp/{shown}"
        self.config.llm.llama_cpp.model_path = str(target)
        try:
            models, _ = list_models("llama_cpp", self.config)
        except Exception:  # noqa: BLE001
            models = []
        if models:
            self._models_cache["llama_cpp"] = list(models)
            if self.cb_provider.currentText().strip() == "llama_cpp":
                self._fill_models("llama_cpp", keep=model, models=models)
        self.hf_progress.setRange(0, 100)
        self.hf_progress.setValue(100)
        self.hf_progress.setVisible(True)
        self.lbl_hf_status.setText(i18n.tr("llm.hf_done", name=target.name))

    def _cancel_hf_download(self) -> None:
        worker = self._hf_worker
        if worker is not None and worker.isRunning():
            worker.cancel_download()
            worker.wait(2000)

    def reject(self) -> None:
        """Отмена настроек не оставляет фоновую загрузку без присмотра."""
        self._cancel_hf_download()
        self._cancel_lc_install()
        super().reject()

    def _set_llm_group_visible(self, group: QGroupBox, visible: bool) -> None:
        """Скрывает не только группу, но и строку формы — без пустого зазора."""
        group.setVisible(visible)
        try:
            self._llm_form.setRowVisible(group, visible)
        except (AttributeError, TypeError):
            pass

    def _update_provider_visibility(self) -> None:
        """llama.cpp и ключи API взаимоисключающие: меньше лишних полей."""
        local = self.cb_provider.currentText().strip() == "llama_cpp"
        self._set_llm_group_visible(self._local_group, local)
        self._set_llm_group_visible(self._hf_group, local)
        self._set_llm_group_visible(self._keys_group, not local)

    def _on_provider_changed(self) -> None:
        """Сменили провайдера — сразу показать статический список, потом живой."""
        provider = self.cb_provider.currentText().strip()
        self._update_provider_visibility()
        keep = self._current_model()
        self._fill_models(provider, keep=keep)
        if provider == "llama_cpp":
            # локальный список уже собран из конфига; сеть и лишний QThread не нужны
            return
        worker = _ModelsWorker(provider, self.config)
        worker.done.connect(self._models_ready)
        worker.finished.connect(lambda w=worker: self._models_workers.discard(w))
        self._models_worker = worker
        self._models_workers.add(worker)
        worker.start()

    def _models_ready(self, provider: str, models: list) -> None:
        """Живой список пришёл — перерисовать combo, сохранив выбор."""
        if provider != self.cb_provider.currentText().strip() or not models:
            return
        self._models_cache[provider] = list(models)
        self._fill_models(provider, keep=self._current_model(), models=models)

    def _current_model(self) -> str:
        """Выбранная модель без бейджа; произвольный ввод — как есть."""
        index = self.cb_model.currentIndex()
        if index >= 0:
            data = self.cb_model.currentData()
            # пункт выбран и строка совпадает с ним — это выбор из списка
            same_text = (
                self.cb_model.currentText().strip()
                == self.cb_model.itemText(index).strip()
            )
            if isinstance(data, str) and data and same_text:
                return data
        raw = self.cb_model.currentText().strip()
        for badge in (i18n.tr("llm.free"), i18n.tr("llm.paid")):
            marker = f" — {badge}"
            if raw.endswith(marker):
                raw = raw[: -len(marker)].strip()
        return raw

    # ----------------------------------------------------------------- Voice
    def _build_voice_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        self.cb_wake_engine = QComboBox()
        self.cb_wake_engine.addItems(["vosk", "porcupine", "off"])
        self.edit_wake_words = QLineEdit()
        self.chk_wake = QCheckBox(i18n.tr("voice.wake_enable"))
        form.addRow(i18n.tr("voice.wake"), self.chk_wake)
        form.addRow(i18n.tr("voice.engine"), self.cb_wake_engine)
        self.edit_wake_words.setToolTip(i18n.tr("voice.words_tip"))
        form.addRow(i18n.tr("voice.words"), self.edit_wake_words)

        rt = QGroupBox(i18n.tr("voice.rt_group"))
        rt.setToolTip(i18n.tr("voice.rt_group_tip"))
        rform = QFormLayout(rt)
        rform.setSpacing(10)
        self.chk_realtime = QCheckBox(i18n.tr("voice.rt_enable"))
        self.cb_rt_provider = QComboBox()
        self.cb_rt_provider.addItems(["gemini", "openai", "local"])
        self.edit_rt_gemini = QLineEdit()
        self.edit_rt_openai = QLineEdit()
        self.edit_rt_voice = QLineEdit()
        self.chk_rt_audio = QCheckBox(i18n.tr("voice.rt_audio"))
        self.chk_rt_aec = QCheckBox(i18n.tr("voice.rt_aec"))
        self.chk_rt_aec.setToolTip(i18n.tr("voice.rt_aec_tip"))
        self.chk_rt_wake = QCheckBox(i18n.tr("voice.rt_wake"))
        self.chk_rt_fallback = QCheckBox(i18n.tr("voice.rt_fallback"))
        self.chk_barge = QCheckBox(i18n.tr("voice.barge_in"))
        self.chk_barge.setToolTip(i18n.tr("voice.barge_in_tip"))
        rform.addRow(self.chk_realtime)
        rform.addRow(i18n.tr("voice.rt_provider"), self.cb_rt_provider)
        rform.addRow(i18n.tr("voice.rt_gemini"), self.edit_rt_gemini)
        rform.addRow(i18n.tr("voice.rt_openai"), self.edit_rt_openai)
        rform.addRow(i18n.tr("voice.rt_voice"), self.edit_rt_voice)
        rform.addRow(self.chk_rt_audio)
        rform.addRow(self.chk_rt_aec)
        rform.addRow(self.chk_rt_wake)
        rform.addRow(self.chk_rt_fallback)
        rform.addRow(self.chk_barge)
        form.addRow(rt)
        return w

    # ------------------------------------------------------------------- TTS
    def _build_tts_tab(self) -> QWidget:
        """Голос озвучки: движок, локаль, голос, скорость/громкость, пример."""
        self._tts_voices: list[tuple[str, str, str]] = [
            _voice_parts(v) for v in _FALLBACK_VOICES
        ]
        # голоса каждого движка кэшируются: возврат к движку не грузит список заново
        self._tts_voices_by_engine: dict[str, list[tuple[str, str, str]]] = {}
        self._tts_full_by_engine: set[str] = set()
        self._tts_desired_voice = ""
        self._voices_worker: _VoicesWorker | None = None
        self._preview_worker: _TTSPreviewWorker | None = None

        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        self.cb_tts_engine = QComboBox()
        for engine_id in ("edge-tts", "openai", "system"):
            self.cb_tts_engine.addItem(
                i18n.tr(f"tts.engine_{engine_id.replace('-', '_')}"), engine_id
            )
        self.cb_tts_engine.currentIndexChanged.connect(self._on_tts_engine_changed)
        form.addRow(i18n.tr("tts.engine"), self.cb_tts_engine)

        self.cb_tts_lang = QComboBox()
        self.cb_tts_lang.currentIndexChanged.connect(self._fill_tts_voices)
        form.addRow(i18n.tr("tts.lang"), self.cb_tts_lang)

        self.cb_tts_voice = QComboBox()
        form.addRow(i18n.tr("tts.voice"), self.cb_tts_voice)

        self.spin_tts_rate = QSpinBox()
        self.spin_tts_rate.setRange(-100, 100)
        self.spin_tts_rate.setSuffix(" %")
        self.spin_tts_volume = QSpinBox()
        self.spin_tts_volume.setRange(-100, 100)
        self.spin_tts_volume.setSuffix(" %")
        form.addRow(i18n.tr("tts.rate"), self.spin_tts_rate)
        form.addRow(i18n.tr("tts.volume"), self.spin_tts_volume)

        hint = QLabel(i18n.tr("tts.hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT)
        form.addRow("", hint)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.btn_tts_load = QPushButton(i18n.tr("tts.load"))
        self.btn_tts_load.setObjectName("Ghost")
        self.btn_tts_load.clicked.connect(self._load_voices)
        self.btn_tts_preview = QPushButton(i18n.tr("tts.preview"))
        self.btn_tts_preview.setObjectName("Ghost")
        self.btn_tts_preview.clicked.connect(self._preview_voice)
        row.addWidget(self.btn_tts_load)
        row.addWidget(self.btn_tts_preview)
        row.addStretch(1)
        form.addRow("", row)

        self.lbl_tts_status = QLabel("")
        self.lbl_tts_status.setWordWrap(True)
        self.lbl_tts_status.setStyleSheet(_HINT)
        form.addRow("", self.lbl_tts_status)

        self._fill_tts_langs()
        self._fill_tts_voices()
        return w

    def _tts_engine_id(self) -> str:
        """Машинный id выбранного движка синтеза."""
        return str(self.cb_tts_engine.currentData() or "edge-tts")

    @staticmethod
    def _default_tts_voices(engine: str) -> list[tuple[str, str, str]]:
        """Базовый набор голосов движка до загрузки полного списка."""
        if engine == "openai":
            return [_voice_parts(v) for v in OPENAI_VOICES]
        if engine == "system":
            return []  # системные голоса узнаются локально при загрузке
        return [_voice_parts(v) for v in _FALLBACK_VOICES]

    def _on_tts_engine_changed(self) -> None:
        """Смена движка: показывает его голоса и догружает список при необходимости."""
        engine = self._tts_engine_id()
        voices = self._tts_voices_by_engine.get(engine) or self._default_tts_voices(engine)
        self._tts_voices = voices
        self._tts_voices_by_engine[engine] = voices
        if engine == "openai":
            self._tts_full_by_engine.add(engine)  # список фиксирован — грузить нечего
        self._fill_tts_langs()
        self._fill_tts_voices()
        if engine in self._tts_full_by_engine:
            self.lbl_tts_status.setText(i18n.tr("tts.loaded", n=len(voices)))
        else:
            self._load_voices()

    def _fill_tts_langs(self) -> None:
        """Перезаполняет фильтр локалей, сохраняя текущий выбор."""
        keep = str(self.cb_tts_lang.currentData() or "")
        # голоса без локали (OpenAI, системные) не участвуют в фильтре
        locales = sorted({locale for _, locale, _ in self._tts_voices if locale})
        self.cb_tts_lang.blockSignals(True)
        self.cb_tts_lang.clear()
        self.cb_tts_lang.addItem(i18n.tr("tts.all_langs"), "")
        for locale in locales:
            self.cb_tts_lang.addItem(locale, locale)
        self._select_by_data(self.cb_tts_lang, keep)
        self.cb_tts_lang.blockSignals(False)

    def _fill_tts_voices(self) -> None:
        """Список голосов отфильтрован по выбранной локали."""
        locale = str(self.cb_tts_lang.currentData() or "")
        keep = str(self.cb_tts_voice.currentData() or self._tts_desired_voice)
        self.cb_tts_voice.clear()
        added: set[str] = set()
        for short, voice_locale, gender in self._tts_voices:
            if locale and voice_locale != locale:
                continue
            label = f"{short} ({gender})" if gender else short
            self.cb_tts_voice.addItem(label, short)
            added.add(short)
        # голос из конфига остаётся в списке, даже если его ещё нет
        # в загруженном наборе (иначе он потерялся бы после загрузки)
        wanted = self._tts_desired_voice
        if wanted and wanted not in added and (
            not locale or _voice_parts(wanted)[1] == locale
        ):
            self.cb_tts_voice.addItem(wanted, wanted)
        if keep:
            self._select_by_data(self.cb_tts_voice, keep)

    def _on_tab_changed(self, index: int) -> None:
        """При первом открытии вкладки TTS подтягивает полный список голосов."""
        widget = self._tabs.widget(index)
        if widget is self._tts_page and self._tts_engine_id() not in self._tts_full_by_engine:
            self._load_voices()

    def _load_voices(self) -> None:
        """Запрашивает полный список голосов выбранного движка (фоновый поток)."""
        if self._voices_worker is not None and self._voices_worker.isRunning():
            return
        self.btn_tts_load.setEnabled(False)
        self.lbl_tts_status.setText(i18n.tr("tts.loading"))
        engine = self._tts_engine_id()
        api_key = resolve_openai_key(self.config.llm.providers)
        worker = _VoicesWorker(engine, api_key)
        worker.done.connect(lambda voices, eng=engine: self._on_voices_loaded(voices, eng))
        self._voices_worker = worker
        worker.start()

    def _on_voices_loaded(self, voices: Any, engine: str) -> None:
        self.btn_tts_load.setEnabled(True)
        parsed = [_voice_parts(v) for v in (voices or [])]
        parsed = [p for p in parsed if p[0]]
        if not parsed:
            if engine == self._tts_engine_id():
                self.lbl_tts_status.setText(i18n.tr("tts.load_failed"))
            return
        self._tts_voices_by_engine[engine] = parsed
        self._tts_full_by_engine.add(engine)
        if engine != self._tts_engine_id():
            return  # пользователь уже переключился на другой движок
        keep_lang = str(self.cb_tts_lang.currentData() or "")
        keep_voice = str(self.cb_tts_voice.currentData() or "")
        self._tts_voices = parsed
        self._fill_tts_langs()
        self._select_by_data(self.cb_tts_lang, keep_lang)
        self._fill_tts_voices()
        self._select_by_data(self.cb_tts_voice, keep_voice)
        self.lbl_tts_status.setText(i18n.tr("tts.loaded", n=len(parsed)))

    def _preview_voice(self) -> None:
        """Озвучивает короткую фразу выбранным голосом в фоне."""
        voice = str(self.cb_tts_voice.currentData() or "")
        if not voice:
            return
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.cancel()
            return
        engine = self._tts_engine_id()
        worker = _TTSPreviewWorker(
            voice=voice,
            rate=_pct(self.spin_tts_rate.value()),
            volume=_pct(self.spin_tts_volume.value()),
            text=i18n.tr("tts.preview_text"),
            engine=engine,
            model=self.config.voice.tts.openai_model,
            api_key=resolve_openai_key(self.config.llm.providers) if engine == "openai" else "",
        )
        worker.done.connect(self._on_preview_done)
        self._preview_worker = worker
        self.btn_tts_preview.setEnabled(False)
        worker.start()

    def _on_preview_done(self, error: str) -> None:
        self.btn_tts_preview.setEnabled(True)
        if error:
            self.lbl_tts_status.setText(i18n.tr("tts.preview_failed", msg=error))

    # ------------------------------------------------------------------- MCP
    # Готовые пресеты MCP-серверов живут в nemus.core.mcp.MCP_PRESETS (GUI лишь читает их).

    def _build_mcp_tab(self) -> QWidget:
        """MCP-серверы: тумблер и ползунок сверху, серверы — карточками."""
        self._mcp_servers: list[MCPServerConfig] = []
        self.mcp_cards: list[_MCPServerCard] = []
        self._mcp_probe_card: _MCPServerCard | None = None
        self._mcp_probe_worker: _MCPProbeWorker | None = None
        self._mcp_refresh_worker: _MCPRefreshWorker | None = None

        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)

        # --- общий тумблер, под ним отдельной строкой период обновления
        # (в одну строку тумблер+подпись+ползунок не влезали по ширине)
        self.chk_mcp_enabled = ToggleSwitch(i18n.tr("mcp.enable"))
        v.addWidget(self.chk_mcp_enabled)

        refresh_row = QHBoxLayout()
        refresh_row.setSpacing(8)
        refresh_row.addWidget(QLabel(i18n.tr("mcp.refresh")))
        self.slider_mcp_refresh = QSlider(Qt.Orientation.Horizontal)
        self.slider_mcp_refresh.setRange(0, len(_REFRESH_STEPS) - 1)
        self.slider_mcp_refresh.setMinimumWidth(140)
        self.slider_mcp_refresh.setMaximumWidth(240)
        self.slider_mcp_refresh.setToolTip(i18n.tr("mcp.refresh_tip_period"))
        self.slider_mcp_refresh.valueChanged.connect(self._mcp_refresh_changed)
        refresh_row.addWidget(self.slider_mcp_refresh, 1)
        self.lbl_mcp_refresh_val = QLabel("")
        self.lbl_mcp_refresh_val.setMinimumWidth(56)
        refresh_row.addWidget(self.lbl_mcp_refresh_val)
        refresh_row.addStretch(1)
        v.addLayout(refresh_row)
        self._mcp_refresh_changed(self.slider_mcp_refresh.value())

        self.lbl_mcp_status = QLabel("")
        self.lbl_mcp_status.setWordWrap(True)
        self.lbl_mcp_status.setStyleSheet(_HINT)
        v.addWidget(self.lbl_mcp_status)

        hint = QLabel(i18n.tr("mcp.hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT)
        hint.setToolTip(i18n.tr("mcp.help"))
        v.addWidget(hint)

        # --- быстрое добавление: готовый пример или импорт чужого конфига
        presets = QHBoxLayout()
        presets.setSpacing(8)
        presets.addWidget(QLabel(i18n.tr("mcp.presets")))
        self.cb_mcp_presets = QComboBox()
        for preset_key, *_ in MCP_PRESETS:
            self.cb_mcp_presets.addItem(i18n.tr(f"mcp.preset.{preset_key}"))
        self.cb_mcp_presets.setCurrentIndex(-1)
        self.cb_mcp_presets.setToolTip(i18n.tr("mcp.presets_tip"))
        presets.addWidget(self.cb_mcp_presets, 1)
        btn_preset = QPushButton(i18n.tr("mcp.preset_add"))
        btn_preset.setObjectName("Ghost")
        btn_preset.setToolTip(i18n.tr("mcp.preset_add_tip"))
        btn_preset.clicked.connect(self._mcp_add_preset)
        presets.addWidget(btn_preset)
        btn_import = QPushButton(i18n.tr("mcp.import"))
        btn_import.setObjectName("Ghost")
        btn_import.setToolTip(i18n.tr("mcp.import_tip"))
        btn_import.clicked.connect(self._mcp_import_file)
        presets.addWidget(btn_import)
        v.addLayout(presets)

        # --- карточки серверов: каждая — своя карточка с тумблером и формой
        self.lbl_mcp_empty = QLabel(i18n.tr("mcp.empty"))
        self.lbl_mcp_empty.setWordWrap(True)
        self.lbl_mcp_empty.setStyleSheet(_HINT)
        self.lbl_mcp_empty.setVisible(False)
        v.addWidget(self.lbl_mcp_empty)

        cards_scroll = QScrollArea()
        cards_scroll.setWidgetResizable(True)
        cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        cards_host = QWidget()
        cards_host.setStyleSheet("background: transparent;")
        self.mcp_cards_layout = QVBoxLayout(cards_host)
        self.mcp_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.mcp_cards_layout.setSpacing(10)
        self.mcp_cards_layout.addStretch(1)
        cards_scroll.setWidget(cards_host)
        v.addWidget(cards_scroll, 1)

        # --- низ: добавление сервера, обновление инструментов, результат
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.btn_mcp_add = QPushButton(i18n.tr("mcp.add"))
        self.btn_mcp_add.setObjectName("Ghost")
        self.btn_mcp_add.setToolTip(i18n.tr("mcp.add_tip"))
        self.btn_mcp_add.clicked.connect(self._mcp_add)
        self.btn_mcp_paste = QPushButton(i18n.tr("mcp.paste"))
        self.btn_mcp_paste.setObjectName("Ghost")
        self.btn_mcp_paste.setToolTip(i18n.tr("mcp.paste_tip"))
        self.btn_mcp_paste.clicked.connect(self._mcp_paste_command)
        self.btn_mcp_refresh = QPushButton(i18n.tr("mcp.refresh_tools"))
        self.btn_mcp_refresh.setObjectName("Ghost")
        self.btn_mcp_refresh.setToolTip(i18n.tr("mcp.refresh_tip"))
        self.btn_mcp_refresh.clicked.connect(self._mcp_refresh_tools)
        bottom.addWidget(self.btn_mcp_add)
        bottom.addWidget(self.btn_mcp_paste)
        bottom.addWidget(self.btn_mcp_refresh)
        bottom.addStretch(1)
        v.addLayout(bottom)

        self.lbl_mcp_result = QLabel("")
        self.lbl_mcp_result.setWordWrap(True)
        self.lbl_mcp_result.setStyleSheet(_HINT)
        v.addWidget(self.lbl_mcp_result)
        return w

    # -- MCP: модель ↔ вид (карточки)
    def _mcp_refresh_changed(self, index: int) -> None:
        """Подпись значения периода рядом со слайдером («5 с», «1 ч», «1 д»)."""
        self.lbl_mcp_refresh_val.setText(_fmt_refresh(_REFRESH_STEPS[index]))

    @staticmethod
    def _mcp_refresh_index(seconds: int) -> int:
        """Индекс ближайшего шага ползунка для периода в секундах."""
        return min(
            range(len(_REFRESH_STEPS)),
            key=lambda i: abs(_REFRESH_STEPS[i] - seconds),
        )

    def _mcp_add_card(self, server: MCPServerConfig) -> _MCPServerCard:
        """Добавляет карточку сервера в конец списка (над растяжкой)."""
        card = _MCPServerCard(server)
        card.probe_requested.connect(self._mcp_probe)
        card.delete_requested.connect(self._mcp_delete)
        self._mcp_servers.append(server)
        self.mcp_cards.append(card)
        self.mcp_cards_layout.insertWidget(self.mcp_cards_layout.count() - 1, card)
        self._mcp_paint_card(card)
        self.lbl_mcp_empty.setVisible(False)
        return card

    def _mcp_rebuild_cards(self, servers: list[MCPServerConfig]) -> None:
        """Пересобирает карточки по списку моделей (загрузка конфига)."""
        for card in self.mcp_cards:
            self.mcp_cards_layout.removeWidget(card)
            card.deleteLater()
        self.mcp_cards.clear()
        self._mcp_servers = []
        for server in servers:
            self._mcp_add_card(server)
        self.lbl_mcp_empty.setVisible(not self.mcp_cards)

    def _mcp_paint_card(self, card: _MCPServerCard) -> None:
        """Красит карточку: счётчик инструментов от менеджера + шапка."""
        manager = self._mcp_manager()
        count: int | None = None
        if manager is not None and card.server.enabled:
            connected, count = manager.server_status(card.server)
            if not connected:
                count = None
        card.set_tools(count)
        card.repaint_header()

    def _mcp_repaint_all(self) -> None:
        """Перекрашивает все карточки (после обновления статусов)."""
        for card in self.mcp_cards:
            self._mcp_paint_card(card)

    def _mcp_push(self) -> None:
        """Карточки пишут в модель сами — остаётся перерисовать шапки."""
        for card in self.mcp_cards:
            card.repaint_header()

    def _mcp_add(self) -> None:
        card = self._mcp_add_card(
            MCPServerConfig(name="", command="", args=[], cwd=".", enabled=False)
        )
        card.set_expanded(True)
        card.edit_name.setFocus()

    def _mcp_paste_command(self) -> None:
        """Принимает вставленную команду, включая Smithery с --client claude."""
        text, ok = QInputDialog.getText(
            self,
            i18n.tr("mcp.paste_title"),
            i18n.tr("mcp.paste_prompt"),
        )
        if not ok or not text.strip():
            return
        try:
            server = parse_mcp_command(text)
        except ValueError as exc:
            self._set_mcp_result(
                i18n.tr("mcp.paste_fail", msg=str(exc)), False
            )
            return
        card = self._mcp_add_card(server)
        card.set_expanded(True)
        self._set_mcp_result(
            i18n.tr("mcp.paste_done", name=server.name), True
        )

    def _mcp_add_preset(self) -> None:
        """Добавляет сервер из готового примера (команды уже прописаны)."""
        index = self.cb_mcp_presets.currentIndex()
        if not (0 <= index < len(MCP_PRESETS)):
            return
        preset_key, command, args, env = MCP_PRESETS[index]
        workspace = getattr(getattr(self.config, "tools", None), "workspace", "")
        resolved = [a.replace("{path}", workspace or ".") for a in args]
        self._mcp_add_card(
            MCPServerConfig(
                name=i18n.tr(f"mcp.preset.{preset_key}"),
                command=command,
                args=resolved,
                env=dict(env),
                enabled=True,
            )
        )
        self.cb_mcp_presets.setCurrentIndex(-1)

    def _mcp_import_file(self) -> None:
        """Импортирует серверы из mcp_config.json (формат Claude Desktop)."""
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.tr("mcp.import"), str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            servers = parse_claude_config(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._set_mcp_result(i18n.tr("mcp.import_fail", msg=str(exc)), False)
            return
        if not servers:
            self._set_mcp_result(i18n.tr("mcp.import_none"), False)
            return
        existing = {(s.command, tuple(s.args)) for s in self._mcp_servers}
        added = 0
        for server in servers:
            if (server.command, tuple(server.args)) in existing:
                continue
            self._mcp_add_card(server)
            existing.add((server.command, tuple(server.args)))
            added += 1
        self._set_mcp_result(i18n.tr("mcp.import_done", n=added), True)

    def _mcp_delete(self, card: _MCPServerCard) -> None:
        """Убирает карточку и её модель из списка."""
        if card not in self.mcp_cards:
            return
        if self._mcp_probe_card is card:
            self._mcp_probe_card = None
        self.mcp_cards.remove(card)
        for index, server in enumerate(self._mcp_servers):
            if server is card.server:
                del self._mcp_servers[index]
                break
        self.mcp_cards_layout.removeWidget(card)
        card.deleteLater()
        self.lbl_mcp_empty.setVisible(not self.mcp_cards)

    # -- MCP: проверка и обновление
    def _mcp_manager(self) -> Any:
        return getattr(self.core, "mcp_manager", None) if self.core else None

    def _set_mcp_result(self, text: str, ok: bool | None) -> None:
        """Показывает результат проверки под формой (цветом успеха/ошибки)."""
        self.lbl_mcp_result.setText(text)
        if not text:
            self.lbl_mcp_result.setStyleSheet(_HINT)
        elif ok:
            self.lbl_mcp_result.setStyleSheet(f"color:{ACCENT_SUCCESS};font-size:12px;")
        else:
            self.lbl_mcp_result.setStyleSheet(f"color:{ACCENT_DANGER};font-size:12px;")

    def _update_mcp_status(self, extra: str = "") -> None:
        from nemus.core.mcp import package_installed

        manager = self._mcp_manager()
        if not package_installed():
            text = i18n.tr("mcp.need_pkg")
        elif manager is None:
            text = i18n.tr("mcp.status_no_core")
        elif manager.enabled:
            text = i18n.tr("mcp.status_on", n=len(manager.tool_names))
        else:
            text = i18n.tr("mcp.status_off")
        self.lbl_mcp_status.setText(f"{text} {extra}".strip())
        self.btn_mcp_refresh.setEnabled(manager is not None and manager.enabled)
        self._mcp_repaint_all()

    def _mcp_probe(self, card: _MCPServerCard) -> None:
        """Проверяет сервер карточки: подключение и список инструментов."""
        server = card.server.model_copy()
        if not server.command:
            self._set_mcp_result(i18n.tr("mcp.need_cmd"), False)
            return
        if self._mcp_probe_worker is not None and self._mcp_probe_worker.isRunning():
            return
        self._set_mcp_result(i18n.tr("mcp.probe_running"), None)
        worker = _MCPProbeWorker(server)
        worker.done.connect(self._on_mcp_probe_done)
        self._mcp_probe_worker = worker
        self._mcp_probe_card = card
        card.btn_probe.setEnabled(False)
        worker.start()

    def _on_mcp_probe_done(self, ok: bool, message: str) -> None:
        card, self._mcp_probe_card = self._mcp_probe_card, None
        if card is not None:
            card.btn_probe.setEnabled(True)
            card.state = "ok" if ok else "fail"
            card.message = message
            card.repaint_header()
        key = "mcp.probe_ok" if ok else "mcp.probe_fail"
        self._set_mcp_result(i18n.tr(key, tools=message, msg=message), ok)

    def _mcp_refresh_tools(self) -> None:
        manager = self._mcp_manager()
        if manager is None:
            self._update_mcp_status()
            return
        if self._mcp_refresh_worker is not None and self._mcp_refresh_worker.isRunning():
            return
        self._set_mcp_result(i18n.tr("mcp.refresh_running"), None)
        worker = _MCPRefreshWorker(manager)
        worker.done.connect(self._on_mcp_refresh_done)
        self._mcp_refresh_worker = worker
        worker.start()

    def _on_mcp_refresh_done(self, ok: bool, message: str) -> None:
        if ok:
            self._update_mcp_status(extra=i18n.tr("mcp.refresh_done", tools=message))
            self._set_mcp_result(i18n.tr("mcp.refresh_done", tools=message), True)
        else:
            self._set_mcp_result(i18n.tr("mcp.probe_fail", msg=message), False)
        self._mcp_repaint_all()


    # --------------------------------------------------------------- Spotify
    def _build_spotify_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(12)
        self.chk_sp_enabled = QCheckBox(i18n.tr("spotify.enable"))
        v.addWidget(self.chk_sp_enabled)

        info = QLabel(i18n.tr("spotify.info"))
        info.setWordWrap(True)
        info.setStyleSheet(_HINT)
        v.addWidget(info)

        form = QFormLayout()
        form.setSpacing(12)
        self.edit_sp_id = QLineEdit()
        self.edit_sp_secret = QLineEdit()
        self.edit_sp_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_sp_device = QLineEdit()
        self.edit_sp_device.setPlaceholderText(i18n.tr("spotify.device_hint"))
        self.edit_sp_market = QLineEdit()
        self.edit_sp_redirect = QLineEdit()
        redirect_row = QHBoxLayout()
        redirect_row.setContentsMargins(0, 0, 0, 0)
        redirect_row.setSpacing(8)
        redirect_row.addWidget(self.edit_sp_redirect)
        self.btn_sp_copy = QPushButton(i18n.tr("spotify.copy"))
        self.btn_sp_copy.setObjectName("Ghost")
        self.btn_sp_copy.clicked.connect(self._copy_redirect)
        redirect_row.addWidget(self.btn_sp_copy)
        form.addRow("Client ID", self.edit_sp_id)
        form.addRow("Client Secret", self.edit_sp_secret)
        form.addRow(i18n.tr("spotify.device"), self.edit_sp_device)
        form.addRow("Market", self.edit_sp_market)
        form.addRow("Redirect URI", redirect_row)
        v.addLayout(form)

        self.btn_sp_login = QPushButton(i18n.tr("spotify.signin"))
        self.btn_sp_login.setObjectName("Primary")
        self.btn_sp_login.clicked.connect(self._spotify_login)
        v.addWidget(self.btn_sp_login)
        self.lbl_sp_status = QLabel("")
        self.lbl_sp_status.setWordWrap(True)
        self.lbl_sp_status.setStyleSheet(_HINT)
        v.addWidget(self.lbl_sp_status)
        self._sp_worker: _SpotifyAuthWorker | None = None
        v.addStretch(1)
        return w

    def _spotify_login(self) -> None:
        base = self.config.spotify
        redirect = self.edit_sp_redirect.text().strip() or base.redirect_uri
        warning = _redirect_uri_warning(redirect)
        if warning:
            self.lbl_sp_status.setText(i18n.tr("spotify.check_uri") + warning)
            return
        sp = base.model_copy(
            update={
                "client_id": self.edit_sp_id.text().strip(),
                "client_secret": self.edit_sp_secret.text().strip(),
                "redirect_uri": redirect,
            }
        )
        if not sp.client_id or not sp.client_secret:
            self.lbl_sp_status.setText(i18n.tr("spotify.need_ids"))
            return
        self.btn_sp_login.setEnabled(False)
        self.lbl_sp_status.setText(i18n.tr("spotify.opening"))
        worker = _SpotifyAuthWorker(sp)
        worker.done.connect(self._on_spotify_login_done)
        self._sp_worker = worker
        worker.start()

    def _copy_redirect(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = self.edit_sp_redirect.text().strip()
        QApplication.clipboard().setText(text)
        self.lbl_sp_status.setText(i18n.tr("spotify.copied", text=text))

    def _on_spotify_login_done(self, ok: bool, message: str) -> None:
        self.btn_sp_login.setEnabled(True)
        if ok:
            self.lbl_sp_status.setText(i18n.tr("spotify.ok"))
        else:
            self.lbl_sp_status.setText(i18n.tr("spotify.fail", msg=message))

    # --------------------------------------------------------------- Telegram
    def _build_telegram_tab(self) -> QWidget:
        """Настройки бота: токен, кому разрешено писать, статус подключения."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(12)

        self.chk_tg_enabled = QCheckBox(i18n.tr("tg.enable"))
        self.chk_tg_enabled.setToolTip(i18n.tr("tg.enable_tip"))
        v.addWidget(self.chk_tg_enabled)

        info = QLabel(i18n.tr("tg.hint"))
        info.setWordWrap(True)
        info.setStyleSheet(_HINT)
        v.addWidget(info)

        form = QFormLayout()
        form.setSpacing(12)
        self.edit_tg_token = QLineEdit()
        self.edit_tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_tg_token.setPlaceholderText("123456789:AA…")
        form.addRow(i18n.tr("tg.token"), self.edit_tg_token)

        self.edit_tg_users = QLineEdit()
        self.edit_tg_users.setPlaceholderText("123456789, 987654321")
        self.edit_tg_users.setToolTip(i18n.tr("tg.users_tip"))
        form.addRow(i18n.tr("tg.users"), self.edit_tg_users)

        self.chk_tg_groups = QCheckBox(i18n.tr("tg.allow_groups"))
        form.addRow("", self.chk_tg_groups)
        self.chk_tg_voice = QCheckBox(i18n.tr("tg.transcribe"))
        form.addRow("", self.chk_tg_voice)
        self.chk_tg_hints = QCheckBox(i18n.tr("tg.hints"))
        self.chk_tg_hints.setToolTip(i18n.tr("tg.hints_tip"))
        form.addRow("", self.chk_tg_hints)
        self.chk_tg_advanced_ui = QCheckBox(i18n.tr("tg.advanced_ui"))
        self.chk_tg_advanced_ui.setToolTip(i18n.tr("tg.advanced_ui_tip"))
        form.addRow("", self.chk_tg_advanced_ui)

        self.spin_tg_size = QSpinBox()
        self.spin_tg_size.setRange(1, 20)
        self.spin_tg_size.setSuffix(" " + i18n.tr("tg.mb"))
        form.addRow(i18n.tr("tg.max_file"), self.spin_tg_size)
        v.addLayout(form)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.btn_tg_check = QPushButton(i18n.tr("tg.check"))
        self.btn_tg_check.setObjectName("Ghost")
        self.btn_tg_check.clicked.connect(self._check_telegram)
        row.addWidget(self.btn_tg_check)
        btn_father = QPushButton(i18n.tr("tg.botfather"))
        btn_father.setObjectName("Ghost")
        btn_father.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://t.me/BotFather"))
        )
        row.addWidget(btn_father)
        row.addStretch(1)
        v.addLayout(row)

        self.lbl_tg_status = QLabel("")
        self.lbl_tg_status.setWordWrap(True)
        self.lbl_tg_status.setStyleSheet(_HINT)
        v.addWidget(self.lbl_tg_status)

        daemon = QGroupBox(i18n.tr("tg.autostart_group"))
        dform = QVBoxLayout(daemon)
        dform.setSpacing(6)
        self.chk_tg_autostart = QCheckBox(i18n.tr("tg.autostart"))
        self.chk_tg_autostart.setToolTip(i18n.tr("tg.autostart_tip"))
        self.chk_tg_autostart.toggled.connect(self._on_tg_autostart_toggled)
        dform.addWidget(self.chk_tg_autostart)
        self.lbl_tg_autostart = QLabel("")
        self.lbl_tg_autostart.setWordWrap(True)
        self.lbl_tg_autostart.setStyleSheet(_HINT)
        dform.addWidget(self.lbl_tg_autostart)
        v.addWidget(daemon)
        #: реальное состояние задачи на момент открытия — чтобы не трогать её,
        #: если пользователь просто открыл настройки и нажал «Сохранить»
        self._tg_autostart_initial = False
        self._tg_autostart_loading = True

        howto = QLabel(i18n.tr("tg.howto"))
        howto.setWordWrap(True)
        howto.setStyleSheet(_HINT)
        v.addWidget(howto)
        v.addStretch(1)
        return w

    def _telegram_status_text(self) -> str:
        """Строка состояния: работает ли бот прямо сейчас."""
        from nemus.core.telegram_bot import telegram_status

        status = telegram_status()
        if status.get("running"):
            return i18n.tr("tg.status_running", username=status.get("username") or "?")
        if status.get("error"):
            return i18n.tr("tg.status_error", msg=status["error"])
        return i18n.tr("tg.status_off")

    def _check_telegram(self) -> None:
        """Кнопка «Проверить токен»: getMe в фоне."""
        token = self.edit_tg_token.text().strip()
        if not token:
            self.lbl_tg_status.setText(i18n.tr("tg.need_token"))
            return
        self.btn_tg_check.setEnabled(False)
        self.lbl_tg_status.setText(i18n.tr("tg.checking"))
        worker = _TelegramCheckWorker(token)
        worker.done.connect(self._on_telegram_checked)
        self._tg_check_worker = worker
        worker.start()

    def _on_telegram_checked(self, ok: bool, message: str) -> None:
        self.btn_tg_check.setEnabled(True)
        if ok:
            self.lbl_tg_status.setText(i18n.tr("tg.check_ok", username=message))
        else:
            self.lbl_tg_status.setText(i18n.tr("tg.check_fail", msg=message))

    def _on_tg_autostart_toggled(self, checked: bool) -> None:
        """Показывает, что автозапуск применится после «Сохранить»."""
        if self._tg_autostart_loading:
            return
        if not telegram_autostart.supported():
            return
        key = "tg.autostart_pending_on" if checked else "tg.autostart_pending_off"
        self.lbl_tg_autostart.setText(i18n.tr(key))

    def _apply_tg_autostart(self) -> bool:
        """Применяет галочку автозапуска. False — показали ошибку, диалог открыт."""
        if not telegram_autostart.supported():
            return True
        desired = self.chk_tg_autostart.isChecked()
        if desired == self._tg_autostart_initial:
            return True
        ok, message = telegram_autostart.set_enabled(desired)
        if not ok:
            self.lbl_tg_autostart.setText(
                i18n.tr("tg.autostart_fail", msg=message)
            )
            return False
        self._tg_autostart_initial = desired
        self.lbl_tg_autostart.setText(
            i18n.tr("tg.autostart_on" if desired else "tg.autostart_off")
        )
        return True

    # ----------------------------------------------------------------- Tools
    def _build_tools_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)
        self.cb_perm = QComboBox()
        self.cb_perm.addItems(["full-auto", "smart", "confirm-all"])
        form.addRow(i18n.tr("tools.perm_mode"), self.cb_perm)
        self.edit_auto_allow = QLineEdit()
        self.edit_auto_allow.setPlaceholderText("get_time,web_search")
        form.addRow(i18n.tr("tools.auto_allow"), self.edit_auto_allow)
        self.edit_workspace = QLineEdit()
        self.edit_workspace.setPlaceholderText("data/workspace")
        form.addRow(i18n.tr("tools.workspace"), self.edit_workspace)
        hint = QLabel(i18n.tr("tools.workspace_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT)
        form.addRow("", hint)

        cleanup = QGroupBox(i18n.tr("tools.cleanup_group"))
        cform = QFormLayout(cleanup)
        cform.setSpacing(10)
        self.spin_cleanup_days = QSpinBox()
        self.spin_cleanup_days.setRange(1, 3650)
        self.spin_cleanup_days.setValue(DEFAULT_CLEANUP_DAYS)
        self.spin_cleanup_days.setSuffix(i18n.tr("tools.cleanup_days_suffix"))
        cform.addRow(i18n.tr("tools.cleanup_days"), self.spin_cleanup_days)
        self.btn_cleanup = QPushButton(i18n.tr("tools.cleanup_button"))
        self.btn_cleanup.setObjectName("Ghost")
        self.btn_cleanup.clicked.connect(self._cleanup_old_files)
        cform.addRow("", self.btn_cleanup)
        cleanup_hint = QLabel(i18n.tr("tools.cleanup_hint"))
        cleanup_hint.setWordWrap(True)
        cleanup_hint.setStyleSheet(_HINT)
        cform.addRow("", cleanup_hint)
        form.addRow(cleanup)

        limits = QGroupBox(i18n.tr("tools.limits_group"))
        lform = QFormLayout(limits)
        lform.setSpacing(10)
        self.spin_max_steps = QSpinBox()
        self.spin_max_steps.setRange(5, 1000)
        self.spin_max_steps.setSingleStep(5)
        self.spin_time_budget = QSpinBox()
        self.spin_time_budget.setRange(30, 7200)
        self.spin_time_budget.setSingleStep(30)
        self.spin_time_budget.setSuffix(" с")
        lform.addRow(i18n.tr("tools.max_steps"), self.spin_max_steps)
        lform.addRow(i18n.tr("tools.time_budget"), self.spin_time_budget)
        lim_hint = QLabel(i18n.tr("tools.limits_hint"))
        lim_hint.setWordWrap(True)
        lim_hint.setStyleSheet(_HINT)
        lform.addRow("", lim_hint)
        form.addRow(limits)
        return w

    def _cleanup_old_files(self) -> None:
        """Показывает предпросмотр и удаляет старые файлы рабочей папки."""
        days = self.spin_cleanup_days.value()
        raw = self.edit_workspace.text().strip() or "data/workspace"
        workspace = Path(raw).expanduser()
        if not workspace.is_absolute():
            workspace = PROJECT_ROOT / workspace
        preview = cleanup_storage(workspace, days=days, dry_run=True)
        if not preview.candidates:
            QMessageBox.information(
                self,
                i18n.tr("tools.cleanup_title"),
                i18n.tr("tools.cleanup_nothing", days=days),
            )
            return
        answer = QMessageBox.question(
            self,
            i18n.tr("tools.cleanup_title"),
            i18n.tr(
                "tools.cleanup_confirm",
                count=preview.candidates,
                days=days,
                size=human_bytes(preview.candidate_bytes),
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = cleanup_storage(workspace, days=days)
        text = i18n.tr(
            "tools.cleanup_done",
            count=result.removed,
            size=human_bytes(result.freed),
        )
        if result.failed:
            text += "\n" + i18n.tr("tools.cleanup_failed", count=result.failed)
        QMessageBox.information(self, i18n.tr("tools.cleanup_title"), text)

    # ----------------------------------------------------------------- About
    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(12)
        about = QLabel(i18n.tr("about.text", version=__version__))
        about.setWordWrap(True)
        about.setOpenExternalLinks(True)
        about.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(about)
        v.addStretch(1)
        return w

    # --------------------------------------------------------------- load/save
    # ---------------------------------------------------------------- Skills
    def _build_skills_tab(self) -> QWidget:
        """Скиллы Claude Agent Skills: включение, список, папка на диске."""
        self._skill_checks: dict[str, QCheckBox] = {}
        self._skill_rows: dict[str, _SkillRow] = {}

        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.chk_skills_enabled = ToggleSwitch(i18n.tr("skills.enable"))
        self.chk_skills_enabled.setToolTip(i18n.tr("skills.enable_tip"))
        top.addWidget(self.chk_skills_enabled)
        top.addStretch(1)
        btn_refresh = QPushButton(i18n.tr("skills.refresh"))
        btn_refresh.setObjectName("Ghost")
        btn_refresh.clicked.connect(self._skills_refresh)
        top.addWidget(btn_refresh)
        btn_open = QPushButton(i18n.tr("skills.open_folder"))
        btn_open.setObjectName("Ghost")
        btn_open.setToolTip(i18n.tr("skills.open_folder_tip"))
        btn_open.clicked.connect(self._skills_open_folder)
        top.addWidget(btn_open)
        v.addLayout(top)

        hint = QLabel(i18n.tr("skills.hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT)
        v.addWidget(hint)

        self.lbl_skills_path = QLabel("")
        self.lbl_skills_path.setWordWrap(True)
        self.lbl_skills_path.setStyleSheet(_HINT)
        v.addWidget(self.lbl_skills_path)

        self._skills_list_box = QWidget()
        self._skills_list = QVBoxLayout(self._skills_list_box)
        self._skills_list.setSpacing(6)
        self._skills_list.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._skills_list_box)

        self.lbl_skills_empty = QLabel(i18n.tr("skills.empty"))
        self.lbl_skills_empty.setWordWrap(True)
        self.lbl_skills_empty.setStyleSheet(_HINT)
        self.lbl_skills_empty.setVisible(False)
        v.addWidget(self.lbl_skills_empty)

        self.lbl_skills_status = QLabel("")
        self.lbl_skills_status.setWordWrap(True)
        self.lbl_skills_status.setStyleSheet(_HINT)
        v.addWidget(self.lbl_skills_status)
        v.addStretch(1)

        self._skills_refresh()
        return w

    def _skills_root_path(self) -> Path:
        """Папка со скиллами из конфига (относительный путь — от корня проекта)."""
        return skills_root(getattr(self.config.skills, "dir", None))

    def _skills_refresh(self) -> None:
        """Перечитывает скиллы с диска и перерисовывает список галочек."""
        if not hasattr(self, "_skills_list"):
            return
        while self._skills_list.count():
            item = self._skills_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._skill_checks.clear()
        self._skill_rows.clear()

        root = self._skills_root_path()
        self.lbl_skills_path.setText(f"{i18n.tr('skills.folder')}: {root.as_posix()}")
        try:
            # команды плагинов — не скиллы: их выключают/включают вместе с плагином
            skills = [
                skill for skill in load_skills(root) if skill.kind != "command"
            ]
        except OSError as exc:
            self.lbl_skills_empty.setText(i18n.tr("skills.load_fail", msg=str(exc)))
            self.lbl_skills_empty.setVisible(True)
            return

        disabled = {d.strip().lower() for d in self.config.skills.disabled}
        for skill in sorted(skills, key=lambda s: s.name.lower()):
            row = _SkillRow(
                name=skill.name,
                description=skill.description or "",
                path=skill.skill_file.as_posix(),
                skill=skill,
                checked=(
                    skill.name.lower() not in disabled
                    and skill.path.name.lower() not in disabled
                ),
            )
            self._skill_checks[skill.name] = row.box
            self._skill_rows[skill.name] = row
            self._skills_list.addWidget(row)
        self.lbl_skills_empty.setVisible(not skills)
        self.lbl_skills_status.setText(
            i18n.tr("skills.count", n=len(skills)) if skills else ""
        )

    def _skills_open_folder(self) -> None:
        """Открывает папку со скиллами в проводнике (создав её при необходимости)."""
        root = self._skills_root_path()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.lbl_skills_status.setText(i18n.tr("skills.open_fail", msg=str(exc)))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(root.as_posix()))

    def _load(self) -> None:
        cfg = self.config
        self.name_input.setText(cfg.assistant.name)
        ch = cfg.character
        self.chk_character_enabled.setChecked(bool(ch.enabled))
        for attr, spin in self._character_spins.items():
            spin.setValue(int(getattr(ch, attr)))
        for attr, spin in self._character_feelings.items():
            spin.setValue(int(getattr(ch, attr)))
        self.chk_character_proactive.setChecked(bool(ch.proactive_enabled))
        self.edit_character_quiet_start.setText(ch.quiet_hours_start)
        self.edit_character_quiet_end.setText(ch.quiet_hours_end)
        mode_index = self.cb_character_mode.findData(
            "advanced" if ch.advanced else "basic"
        )
        self.cb_character_mode.setCurrentIndex(max(0, mode_index))
        self._character_stack.setCurrentIndex(max(0, mode_index))
        self.edit_character_prompt.setPlainText(ch.custom_prompt)
        self._refresh_character_state()
        self.cb_provider.setCurrentText(cfg.llm.default_provider)
        self._update_provider_visibility()
        self._fill_models(cfg.llm.default_provider, keep=cfg.llm.default_model)
        self.edit_temp.setText(str(cfg.llm.temperature))
        self.edit_max_tokens.setValue(cfg.llm.max_tokens)
        for name, le in self.key_edits.items():
            pc = cfg.llm.providers.get(name)
            le.setText(pc.api_key if pc else "")
        lc = cfg.llm.llama_cpp
        self.edit_lc_path.setText(lc.model_path)
        self.edit_hf_repo.setText(lc.hf_repo)
        self.edit_hf_file.setText(lc.hf_file)
        self.edit_hf_revision.setText(lc.hf_revision or "main")
        self.chk_hf_auto.setChecked(bool(lc.auto_download))
        self.edit_lc_host.setText(lc.host)
        self.spin_lc_port.setValue(int(lc.port or 8081))
        self.spin_lc_ctx.setValue(int(lc.n_ctx or 8192))
        self.spin_lc_gpu.setValue(int(lc.n_gpu_layers))
        self.spin_lc_threads.setValue(int(lc.n_threads))
        self.chk_lc_autostart.setChecked(bool(lc.auto_start))
        self._select_by_data(
            self.cb_reply_lang,
            normalize_lang(getattr(cfg.assistant, "reply_language", REPLY_AUTO)),
        )
        self._select_by_data(self.cb_ui_lang, normalize_lang(cfg.gui.language) or "ru")
        self._select_by_data(
            self.cb_os_family,
            str(getattr(cfg.assistant, "os_family", "auto") or "auto"),
        )
        self.edit_os_details.setText(
            str(getattr(cfg.assistant, "os_details", "") or "")
        )
        self.chk_wake.setChecked(cfg.voice.wake_word.enabled)
        self.cb_wake_engine.setCurrentText(cfg.voice.wake_word.engine)
        self.edit_wake_words.setText(", ".join(cfg.voice.wake_word.words))
        rt = cfg.voice.realtime
        self.chk_realtime.setChecked(rt.enabled)
        self.cb_rt_provider.setCurrentText(rt.provider)
        self.edit_rt_gemini.setText(rt.gemini_model)
        self.edit_rt_openai.setText(rt.openai_model)
        self.edit_rt_voice.setText(rt.openai_voice)
        self.chk_rt_audio.setChecked(rt.audio)
        self.chk_rt_aec.setChecked(rt.aec)
        self.chk_rt_wake.setChecked(rt.wake_activates)
        self.chk_rt_fallback.setChecked(rt.offline_fallback)
        self.chk_barge.setChecked(cfg.voice.barge_in)
        self.cb_perm.setCurrentText(cfg.tools.permission_mode)
        self.edit_auto_allow.setText(", ".join(cfg.tools.auto_allow))
        self.edit_workspace.setText(cfg.tools.workspace)
        self.spin_max_steps.setValue(
            int(getattr(cfg.tools, "max_steps", 100) or 100)
        )
        self.spin_time_budget.setValue(
            int(getattr(cfg.tools, "run_budget_seconds", 600) or 600)
        )
        tg = cfg.telegram
        self.chk_tg_enabled.setChecked(tg.enabled)
        self.edit_tg_token.setText(tg.token)
        self.edit_tg_users.setText(", ".join(str(uid) for uid in tg.allowed_users))
        self.chk_tg_groups.setChecked(tg.allow_groups)
        self.chk_tg_voice.setChecked(tg.transcribe_voice)
        self.chk_tg_hints.setChecked(tg.hints_enabled)
        self.chk_tg_advanced_ui.setChecked(tg.advanced_ui)
        self.spin_tg_size.setValue(max(1, int(tg.max_download_mb or 20)))
        self.lbl_tg_status.setText(self._telegram_status_text())

        # Автозапуск демона: не хранится в YAML, читается из задачи и ярлыка.
        self._tg_autostart_loading = True
        try:
            if telegram_autostart.supported():
                enabled, error = telegram_autostart.status()
                self._tg_autostart_initial = enabled
                self.chk_tg_autostart.setChecked(enabled)
                self.chk_tg_autostart.setEnabled(True)
                if error:
                    self.lbl_tg_autostart.setText(
                        i18n.tr("tg.autostart_fail", msg=error)
                    )
                else:
                    self.lbl_tg_autostart.setText(
                        i18n.tr(
                            "tg.autostart_on" if enabled else "tg.autostart_off"
                        )
                    )
            else:
                self.chk_tg_autostart.setChecked(False)
                self.chk_tg_autostart.setEnabled(False)
                self.lbl_tg_autostart.setText(i18n.tr("tg.autostart_unsupported"))
        finally:
            self._tg_autostart_loading = False

        sp = cfg.spotify
        self.chk_sp_enabled.setChecked(sp.enabled)
        self.edit_sp_id.setText(sp.client_id)
        self.edit_sp_secret.setText(sp.client_secret)
        self.edit_sp_device.setText(sp.default_device)
        self.edit_sp_market.setText(sp.market)
        self.edit_sp_redirect.setText(sp.redirect_uri)

        # TTS: голос озвучки
        tts = cfg.voice.tts
        self._tts_desired_voice = tts.voice
        # смена движка сама заполняет список голосов (если он изменился)
        self._select_by_data(self.cb_tts_engine, tts.engine or "edge-tts")
        self._select_by_data(self.cb_tts_lang, _voice_parts(tts.voice)[1])
        self._fill_tts_voices()
        self._select_by_data(self.cb_tts_voice, tts.voice)
        self.spin_tts_rate.setValue(_pct_value(tts.rate))
        self.spin_tts_volume.setValue(_pct_value(tts.volume))

        # MCP: серверы и общий режим
        self.chk_mcp_enabled.setChecked(cfg.mcp.enabled)
        self.slider_mcp_refresh.setValue(
            self._mcp_refresh_index(max(5, int(cfg.mcp.refresh_seconds)))
        )
        self._mcp_rebuild_cards(
            [s.model_copy(deep=True) for s in cfg.mcp.servers]
        )
        self._update_mcp_status()

        # Скиллы: общий тумблер + список с диска (галочки = включённые)
        self.chk_skills_enabled.setChecked(cfg.skills.enabled)
        self._skills_refresh()

    def _save(self) -> None:
        if self._lc_install_running:
            self.lbl_lc_pkg.setText(i18n.tr("llm.local_install_wait"))
            return
        if self._hf_worker is not None and self._hf_worker.isRunning():
            self.lbl_hf_status.setText(i18n.tr("llm.hf_wait"))
            return
        cfg = self.config
        name = self.name_input.text().strip()
        if name:
            cfg.assistant.name = name
        ch = cfg.character
        ch.enabled = self.chk_character_enabled.isChecked()
        for attr, spin in self._character_spins.items():
            setattr(ch, attr, spin.value())
        for attr, spin in self._character_feelings.items():
            setattr(ch, attr, spin.value())
        ch.proactive_enabled = self.chk_character_proactive.isChecked()
        ch.quiet_hours_start = (
            self.edit_character_quiet_start.text().strip() or "23:00"
        )
        ch.quiet_hours_end = self.edit_character_quiet_end.text().strip() or "08:00"
        ch.advanced = self.cb_character_mode.currentData() == "advanced"
        ch.custom_prompt = self.edit_character_prompt.toPlainText().strip()[:4000]
        cfg.llm.default_provider = self.cb_provider.currentText()
        cfg.llm.default_model = self._current_model() or cfg.llm.default_model
        try:
            cfg.llm.temperature = float(self.edit_temp.text())
        except ValueError:
            pass
        cfg.llm.max_tokens = self.edit_max_tokens.value()
        cfg.assistant.reply_language = str(self.cb_reply_lang.currentData() or REPLY_AUTO)
        cfg.gui.language = str(self.cb_ui_lang.currentData() or "ru")
        cfg.assistant.os_family = str(self.cb_os_family.currentData() or "auto")
        cfg.assistant.os_details = self.edit_os_details.text().strip()
        for name, le in self.key_edits.items():
            v = le.text().strip()
            if not cfg.llm.providers.get(name):
                cfg.llm.providers[name] = ProviderConfig()
            if v:
                cfg.llm.providers[name].api_key = v
            set_secret(f"llm/{name}/api_key", v)
        lc = cfg.llm.llama_cpp
        lc.model_path = self.edit_lc_path.text().strip()
        lc.hf_repo = self.edit_hf_repo.text().strip()
        lc.hf_file = self.edit_hf_file.text().strip()
        lc.hf_revision = self.edit_hf_revision.text().strip() or "main"
        lc.auto_download = self.chk_hf_auto.isChecked()
        lc.host = self.edit_lc_host.text().strip() or "127.0.0.1"
        lc.port = self.spin_lc_port.value()
        lc.n_ctx = self.spin_lc_ctx.value()
        lc.n_gpu_layers = self.spin_lc_gpu.value()
        lc.n_threads = self.spin_lc_threads.value()
        lc.auto_start = self.chk_lc_autostart.isChecked()
        cfg.voice.wake_word.enabled = self.chk_wake.isChecked()
        cfg.voice.wake_word.engine = self.cb_wake_engine.currentText()
        words = [
            w.strip().lower()
            for w in self.edit_wake_words.text().split(",")
            if w.strip()
        ]
        if words:
            cfg.voice.wake_word.words = words
        rt = cfg.voice.realtime
        rt.enabled = self.chk_realtime.isChecked()
        rt.provider = self.cb_rt_provider.currentText()
        rt.gemini_model = self.edit_rt_gemini.text().strip() or rt.gemini_model
        rt.openai_model = self.edit_rt_openai.text().strip() or rt.openai_model
        rt.openai_voice = self.edit_rt_voice.text().strip() or rt.openai_voice
        rt.audio = self.chk_rt_audio.isChecked()
        rt.aec = self.chk_rt_aec.isChecked()
        rt.wake_activates = self.chk_rt_wake.isChecked()
        rt.offline_fallback = self.chk_rt_fallback.isChecked()
        cfg.voice.barge_in = self.chk_barge.isChecked()
        cfg.tools.permission_mode = self.cb_perm.currentText()
        auto = [w.strip() for w in self.edit_auto_allow.text().split(",") if w.strip()]
        cfg.tools.auto_allow = auto
        ws = self.edit_workspace.text().strip()
        if ws:
            cfg.tools.workspace = _resolve_path(ws)
        cfg.tools.max_steps = self.spin_max_steps.value()
        cfg.tools.run_budget_seconds = self.spin_time_budget.value()
        tg = cfg.telegram
        tg.enabled = self.chk_tg_enabled.isChecked()
        tg.token = self.edit_tg_token.text().strip()
        tg.allowed_users = _parse_user_ids(self.edit_tg_users.text())
        tg.allow_groups = self.chk_tg_groups.isChecked()
        tg.transcribe_voice = self.chk_tg_voice.isChecked()
        tg.hints_enabled = self.chk_tg_hints.isChecked()
        tg.advanced_ui = self.chk_tg_advanced_ui.isChecked()
        tg.max_download_mb = self.spin_tg_size.value()
        set_secret("telegram/token", tg.token)

        # Автозапуск демона — состояние Task Scheduler, а не поле YAML.
        if not self._apply_tg_autostart():
            return

        sp = cfg.spotify
        sp.enabled = self.chk_sp_enabled.isChecked()
        sp.client_id = self.edit_sp_id.text().strip()
        sp.client_secret = self.edit_sp_secret.text().strip()
        sp.default_device = self.edit_sp_device.text().strip()
        sp.market = self.edit_sp_market.text().strip() or sp.market
        sp.redirect_uri = self.edit_sp_redirect.text().strip() or sp.redirect_uri
        if sp.client_secret:
            set_secret("spotify/client_secret", sp.client_secret)

        # TTS: голос озвучки
        cfg.voice.tts.engine = self._tts_engine_id()
        selected_voice = str(self.cb_tts_voice.currentData() or "")
        if selected_voice:
            cfg.voice.tts.voice = selected_voice
        cfg.voice.tts.rate = _pct(self.spin_tts_rate.value())
        cfg.voice.tts.volume = _pct(self.spin_tts_volume.value())

        # MCP: общий режим + список серверов
        self._mcp_push()
        cfg.mcp.enabled = self.chk_mcp_enabled.isChecked()
        cfg.mcp.refresh_seconds = _REFRESH_STEPS[self.slider_mcp_refresh.value()]
        cfg.mcp.servers = [s.model_copy(deep=True) for s in self._mcp_servers]

        # Скиллы: общий режим и список отключённых
        cfg.skills.enabled = self.chk_skills_enabled.isChecked()
        cfg.skills.disabled = [
            name for name, box in self._skill_checks.items() if not box.isChecked()
        ]

        self._write_yaml(cfg)
        get_config(reload=True)
        self.accept()

    def done(self, r: int) -> None:
        """Перед закрытием гасит/дожидается фоновых воркеров.

        Работающий QThread, чей Python-объект уничтожен, роняет Qt — поэтому
        не успевшие завершиться воркеры переезжают в модульный реестр и живут
        до завершения, а их сигналы на уже закрытый диалог отключаются сами.
        """
        preview = getattr(self, "_preview_worker", None)
        if preview is not None and preview.isRunning():
            preview.cancel()
        names = (
            "_voices_worker",
            "_preview_worker",
            "_mcp_probe_worker",
            "_mcp_refresh_worker",
            "_tg_check_worker",
            "_hf_search_worker",
            "_hf_files_worker",
            "_hf_worker",
            "_lc_install_worker",
        )
        workers = [getattr(self, name, None) for name in names]
        workers.extend(self._models_workers)
        seen: set[int] = set()
        for worker in workers:
            if worker is None or id(worker) in seen or not worker.isRunning():
                continue
            seen.add(id(worker))
            if not worker.wait(3000):
                worker.finished.connect(lambda w=worker: _DETACHED.discard(w))
                _DETACHED.add(worker)
        super().done(r)

    @staticmethod
    def _select_by_data(combo: QComboBox, data: str) -> None:
        """Выбирает пункт выпадающего списка по хранимому значению."""
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    @staticmethod
    def _write_yaml(cfg) -> None:
        # Сериализуем весь конфиг целиком: так в файле сохраняются и секции,
        # которые пока не редактируются в GUI (например, mcp), и новые поля.
        save_config(cfg)


class ConfirmDialog(QDialog):
    """Диалог подтверждения действия (принимает Tool или имя инструмента)."""

    def __init__(
        self, tool: Tool | str, args: dict[str, Any], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("confirm.window"))
        self.setStyleSheet(qss())
        self.setMinimumWidth(480)
        self.result_value = False

        name = tool if isinstance(tool, str) else tool.name

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(28, 24, 28, 24)

        head = QVBoxLayout()
        head.setSpacing(3)
        title = QLabel(i18n.tr("confirm.title"))
        title.setObjectName("HeaderLabel")
        sub = QLabel(i18n.tr("confirm.tool", name=html.escape(name)))
        sub.setObjectName("SubLabel")
        head.addWidget(title)
        head.addWidget(sub)
        layout.addLayout(head)

        if isinstance(tool, Tool):
            risk_key, risk_color = _RISK_KEYS.get(tool.risk, ("—", TEXT_DIM))
            risk_text = i18n.tr(risk_key) if risk_key != "—" else "—"
            risk = QLabel(i18n.tr("confirm.risk", risk=risk_text))
            risk.setStyleSheet(f"color:{risk_color};font-size:12px;font-weight:600;")
            layout.addWidget(risk)

            desc = QLabel(tool.description)
            desc.setWordWrap(True)
            desc.setStyleSheet(_HINT)
            layout.addWidget(desc)

        card = QFrame()
        card.setObjectName("Card")
        card_box = QVBoxLayout(card)
        card_box.setContentsMargins(16, 14, 16, 14)
        card_box.setSpacing(6)

        if isinstance(args, dict) and args:
            for k, v in args.items():
                row = QHBoxLayout()
                row.setSpacing(10)
                # значения показываем как есть (PlainText): иначе HTML-сущности
                # ломают перенос по невидимым пробелам в длинных токенах
                raw_key, raw_value = str(k), str(v)
                key_lbl = QLabel(_soft_wrap(raw_key, 24))
                key_lbl.setTextFormat(Qt.TextFormat.PlainText)
                key_lbl.setWordWrap(True)
                key_lbl.setToolTip(raw_key)
                key_lbl.setStyleSheet(
                    f"color:{TEXT_DIM};font-size:12px;font-family:monospace;"
                )
                val_lbl = QLabel(_soft_wrap(raw_value))
                val_lbl.setTextFormat(Qt.TextFormat.PlainText)
                val_lbl.setWordWrap(True)
                val_lbl.setToolTip(raw_value)
                val_lbl.setStyleSheet(
                    f"color:{TEXT};font-size:12px;font-family:monospace;"
                )
                row.addWidget(key_lbl, 0)
                row.addWidget(val_lbl, 1)
                card_box.addLayout(row)
        else:
            try:
                args_text = json.dumps(args, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                args_text = str(args)
            val_lbl = QLabel(_soft_wrap(args_text))
            val_lbl.setTextFormat(Qt.TextFormat.PlainText)
            val_lbl.setWordWrap(True)
            val_lbl.setToolTip(args_text)
            val_lbl.setStyleSheet(
                f"color:{TEXT};font-size:12px;font-family:monospace;"
            )
            card_box.addWidget(val_lbl)

        card_head = QLabel(i18n.tr("confirm.params"))
        card_head.setObjectName("SectionLabel")
        layout.addWidget(card_head)

        # параметры могут быть огромными (содержимое файла, длинная команда):
        # ограничиваем высоту и прокручиваем, иначе кнопки уезжают за экран
        card_scroll = QScrollArea()
        card_scroll.setWidgetResizable(True)
        card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        card_scroll.setWidget(card)
        card_scroll.setMaximumHeight(320)
        layout.addWidget(card_scroll)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        yes_btn = QPushButton(i18n.tr("confirm.allow"))
        yes_btn.setObjectName("Primary")
        no_btn = QPushButton(i18n.tr("confirm.deny"))
        no_btn.setObjectName("Ghost")
        yes_btn.clicked.connect(lambda: self._accept_with_result(True))
        no_btn.clicked.connect(lambda: self._accept_with_result(False))
        buttons.addStretch(1)
        buttons.addWidget(no_btn)
        buttons.addWidget(yes_btn)
        layout.addLayout(buttons)

    def _accept_with_result(self, value: bool) -> None:
        self.result_value = value
        self.accept()

    @staticmethod
    def ask(
        tool: Tool | str, args: dict[str, Any], parent: QWidget | None = None
    ) -> bool:
        """Показывает диалог и возвращает решение пользователя."""
        import gc

        dialog = ConfirmDialog(tool, args, parent)
        gc.disable()
        dialog.exec()
        return bool(dialog.result_value)


__all__ = ["PROVIDER_OPTIONS", "SettingsDialog", "ConfirmDialog"]
