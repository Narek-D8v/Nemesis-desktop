"""Диалоги: настройки (в т.ч. вход в Spotify) и подтверждение вызова."""

from __future__ import annotations

import html
import json
import shlex
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from nemus import __version__
from nemus.config import (
    CONFIG_DIR,
    MCPServerConfig,
    ProviderConfig,
    _resolve_path,
    get_config,
    load_config,
    relativize_paths,
)
from nemus.core.language import REPLY_AUTO, REPLY_CHOICES
from nemus.core.language import normalize as normalize_lang
from nemus.core.mcp import MCP_PRESETS, parse_claude_config
from nemus.core.providers import PROVIDER_SPECS
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
from nemus.gui.widgets import ToggleSwitch
from nemus.utils.secrets import set_secret
from nemus.voice.tts import OPENAI_VOICES, TTSEngine, list_engine_voices, resolve_openai_key

PROVIDER_OPTIONS = list(PROVIDER_SPECS.keys())

_HINT = f"color:{TEXT_DIM};font-size:12px;"

#: дискретные периоды обновления инструментов MCP (секунды) — их ходит ползунок
_REFRESH_STEPS: tuple[int, ...] = (5, 15, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400)


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
        self.lbl_name = QLabel()
        self.lbl_name.setStyleSheet("font-weight:600;font-size:13px;")
        self.lbl_cmd = QLabel()
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


class SettingsDialog(QDialog):
    """Диалог настроек: провайдеры, ключи API, голос, инструменты."""

    def __init__(self, parent: QWidget | None = None, core: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("set.window"))
        self.setStyleSheet(qss())
        self.resize(780, 600)
        self.config = load_config()
        #: живое ядро приложения (для статуса/обновления MCP) — может быть None
        self.core = core
        self._build_ui()
        self._load()

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
        tabs.addTab(self._tts_page, i18n.tr("tab.tts"))
        tabs.addTab(
            self._scrollable(self._build_spotify_tab()), i18n.tr("tab.spotify")
        )
        tabs.addTab(self._scrollable(self._build_tools_tab()), i18n.tr("tab.tools"))
        tabs.addTab(self._mcp_page, i18n.tr("tab.mcp"))
        tabs.addTab(self._scrollable(self._build_about_tab()), i18n.tr("tab.about"))
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
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    # ------------------------------------------------------------------ LLM
    def _build_llm_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(12)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Немуся")

        self.cb_provider = QComboBox()
        self.cb_provider.addItems(PROVIDER_OPTIONS)
        self.edit_model = QLineEdit()
        self.edit_temp = QLineEdit()
        self.edit_max_tokens = QSpinBox()
        self.edit_max_tokens.setRange(256, 200000)
        self.edit_max_tokens.setSingleStep(256)

        form.addRow(i18n.tr("llm.name"), self.name_input)
        form.addRow(i18n.tr("llm.provider"), self.cb_provider)
        form.addRow(i18n.tr("llm.model"), self.edit_model)
        form.addRow(i18n.tr("llm.temp"), self.edit_temp)
        form.addRow(i18n.tr("llm.max_tokens"), self.edit_max_tokens)

        keys = QGroupBox(i18n.tr("llm.keys"))
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
        form.addRow(i18n.tr("voice.words"), self.edit_wake_words)

        rt = QGroupBox(i18n.tr("voice.rt_group"))
        rform = QFormLayout(rt)
        rform.setSpacing(10)
        self.chk_realtime = QCheckBox(i18n.tr("voice.rt_enable"))
        self.cb_rt_provider = QComboBox()
        self.cb_rt_provider.addItems(["gemini", "openai", "local"])
        self.edit_rt_gemini = QLineEdit()
        self.edit_rt_openai = QLineEdit()
        self.edit_rt_voice = QLineEdit()
        self.chk_rt_audio = QCheckBox(i18n.tr("voice.rt_audio"))
        self.chk_rt_wake = QCheckBox(i18n.tr("voice.rt_wake"))
        self.chk_rt_fallback = QCheckBox(i18n.tr("voice.rt_fallback"))
        self.chk_barge = QCheckBox(i18n.tr("voice.barge_in"))
        rform.addRow(self.chk_realtime)
        rform.addRow(i18n.tr("voice.rt_provider"), self.cb_rt_provider)
        rform.addRow(i18n.tr("voice.rt_gemini"), self.edit_rt_gemini)
        rform.addRow(i18n.tr("voice.rt_openai"), self.edit_rt_openai)
        rform.addRow(i18n.tr("voice.rt_voice"), self.edit_rt_voice)
        rform.addRow(self.chk_rt_audio)
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

        # --- общий тумблер + ползунок периода обновления
        top = QHBoxLayout()
        top.setSpacing(8)
        self.chk_mcp_enabled = ToggleSwitch(i18n.tr("mcp.enable"))
        top.addWidget(self.chk_mcp_enabled)
        top.addStretch(1)
        top.addWidget(QLabel(i18n.tr("mcp.refresh")))
        self.slider_mcp_refresh = QSlider(Qt.Orientation.Horizontal)
        self.slider_mcp_refresh.setRange(0, len(_REFRESH_STEPS) - 1)
        self.slider_mcp_refresh.setFixedWidth(180)
        self.slider_mcp_refresh.setToolTip(i18n.tr("mcp.refresh_tip_period"))
        self.slider_mcp_refresh.valueChanged.connect(self._mcp_refresh_changed)
        top.addWidget(self.slider_mcp_refresh)
        self.lbl_mcp_refresh_val = QLabel("")
        self.lbl_mcp_refresh_val.setMinimumWidth(56)
        top.addWidget(self.lbl_mcp_refresh_val)
        self._mcp_refresh_changed(self.slider_mcp_refresh.value())
        v.addLayout(top)

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
        self.btn_mcp_refresh = QPushButton(i18n.tr("mcp.refresh_tools"))
        self.btn_mcp_refresh.setObjectName("Ghost")
        self.btn_mcp_refresh.setToolTip(i18n.tr("mcp.refresh_tip"))
        self.btn_mcp_refresh.clicked.connect(self._mcp_refresh_tools)
        bottom.addWidget(self.btn_mcp_add)
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
        return w

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
    def _load(self) -> None:
        cfg = self.config
        self.name_input.setText(cfg.assistant.name)
        self.cb_provider.setCurrentText(cfg.llm.default_provider)
        self.edit_model.setText(cfg.llm.default_model)
        self.edit_temp.setText(str(cfg.llm.temperature))
        self.edit_max_tokens.setValue(cfg.llm.max_tokens)
        for name, le in self.key_edits.items():
            pc = cfg.llm.providers.get(name)
            le.setText(pc.api_key if pc else "")
        self._select_by_data(
            self.cb_reply_lang,
            normalize_lang(getattr(cfg.assistant, "reply_language", REPLY_AUTO)),
        )
        self._select_by_data(self.cb_ui_lang, normalize_lang(cfg.gui.language) or "ru")
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
        self.chk_rt_wake.setChecked(rt.wake_activates)
        self.chk_rt_fallback.setChecked(rt.offline_fallback)
        self.chk_barge.setChecked(cfg.voice.barge_in)
        self.cb_perm.setCurrentText(cfg.tools.permission_mode)
        self.edit_auto_allow.setText(", ".join(cfg.tools.auto_allow))
        self.edit_workspace.setText(cfg.tools.workspace)
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

    def _save(self) -> None:
        cfg = self.config
        name = self.name_input.text().strip()
        if name:
            cfg.assistant.name = name
        cfg.llm.default_provider = self.cb_provider.currentText()
        cfg.llm.default_model = self.edit_model.text().strip() or cfg.llm.default_model
        try:
            cfg.llm.temperature = float(self.edit_temp.text())
        except ValueError:
            pass
        cfg.llm.max_tokens = self.edit_max_tokens.value()
        cfg.assistant.reply_language = str(self.cb_reply_lang.currentData() or REPLY_AUTO)
        cfg.gui.language = str(self.cb_ui_lang.currentData() or "ru")
        for name, le in self.key_edits.items():
            v = le.text().strip()
            if not cfg.llm.providers.get(name):
                cfg.llm.providers[name] = ProviderConfig()
            if v:
                cfg.llm.providers[name].api_key = v
            set_secret(f"llm/{name}/api_key", v)
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
        rt.wake_activates = self.chk_rt_wake.isChecked()
        rt.offline_fallback = self.chk_rt_fallback.isChecked()
        cfg.voice.barge_in = self.chk_barge.isChecked()
        cfg.tools.permission_mode = self.cb_perm.currentText()
        auto = [w.strip() for w in self.edit_auto_allow.text().split(",") if w.strip()]
        cfg.tools.auto_allow = auto
        ws = self.edit_workspace.text().strip()
        if ws:
            cfg.tools.workspace = _resolve_path(ws)
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
        )
        for name in names:
            worker = getattr(self, name, None)
            if worker is None or not worker.isRunning():
                continue
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
        # Пути в памяти абсолютные — перед записью возвращаем их к виду
        # относительно корня проекта, чтобы не плодить хардкод D:\Nemesis.
        relativize_paths(cfg)
        out = cfg.model_dump()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / "config.yaml"
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False)


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
                key_lbl = QLabel(str(k))
                key_lbl.setStyleSheet(
                    f"color:{TEXT_DIM};font-size:12px;font-family:monospace;"
                )
                val_lbl = QLabel(html.escape(str(v)))
                val_lbl.setWordWrap(True)
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
            val_lbl = QLabel(html.escape(args_text))
            val_lbl.setWordWrap(True)
            val_lbl.setStyleSheet(
                f"color:{TEXT};font-size:12px;font-family:monospace;"
            )
            card_box.addWidget(val_lbl)

        card_head = QLabel(i18n.tr("confirm.params"))
        card_head.setObjectName("SectionLabel")
        layout.addWidget(card_head)
        layout.addWidget(card)

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
