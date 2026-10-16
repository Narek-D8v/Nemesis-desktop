"""Диалоги: настройки (в т.ч. вход в Spotify) и подтверждение вызова."""

from __future__ import annotations

import html
import json
from typing import Any

import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from nemus import __version__
from nemus.config import (
    CONFIG_DIR,
    ProviderConfig,
    _resolve_path,
    get_config,
    load_config,
)
from nemus.core.language import REPLY_AUTO, REPLY_CHOICES
from nemus.core.language import normalize as normalize_lang
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
from nemus.utils.secrets import set_secret

PROVIDER_OPTIONS = list(PROVIDER_SPECS.keys())

_HINT = f"color:{TEXT_DIM};font-size:12px;"

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


class SettingsDialog(QDialog):
    """Диалог настроек: провайдеры, ключи API, голос, инструменты."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("set.window"))
        self.setStyleSheet(qss())
        self.resize(660, 580)
        self.config = load_config()
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
        tabs.addTab(self._scrollable(self._build_llm_tab()), i18n.tr("tab.llm"))
        tabs.addTab(self._scrollable(self._build_voice_tab()), i18n.tr("tab.voice"))
        tabs.addTab(
            self._scrollable(self._build_spotify_tab()), i18n.tr("tab.spotify")
        )
        tabs.addTab(self._scrollable(self._build_tools_tab()), i18n.tr("tab.tools"))
        tabs.addTab(self._scrollable(self._build_about_tab()), i18n.tr("tab.about"))
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
        self._write_yaml(cfg)
        get_config(reload=True)
        self.accept()

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
        out = {
            "assistant": cfg.assistant.model_dump(),
            "llm": cfg.llm.model_dump(),
            "voice": cfg.voice.model_dump(),
            "tools": cfg.tools.model_dump(),
            "memory": cfg.memory.model_dump(),
            "spotify": cfg.spotify.model_dump(),
            "gui": cfg.gui.model_dump(),
            "logging": cfg.logging.model_dump(),
        }
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
        dialog = ConfirmDialog(tool, args, parent)
        dialog.exec()
        return bool(dialog.result_value)


__all__ = ["PROVIDER_OPTIONS", "SettingsDialog", "ConfirmDialog"]
