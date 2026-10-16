"""Основное окно Немуси: сайдбар с волновым индикатором, чат, пилюля ввода."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nemus.core.agent import AgentEvent
from nemus.core.state import AssistState
from nemus.gui import i18n
from nemus.gui.dialogs import ConfirmDialog, SettingsDialog
from nemus.gui.theme import (
    ACCENT_PRIMARY,
    ACCENT_SECONDARY,
    BG,
    STATE_COLORS,
    TEXT_DIM,
    TEXT_DIMMER,
    qss,
)
from nemus.gui.widgets.chat import ChatView
from nemus.gui.widgets.waveform import WaveformWidget
from nemus.gui.worker import AgentWorker


class MainWindow(QMainWindow):
    """Основное окно (редизайн 2025): тёмная тема, сайдбар, пилюля ввода."""

    listen_toggled = Signal(bool)
    realtime_toggled = Signal(bool)
    settings_requested = Signal()
    settings_saved = Signal()
    speak_request = Signal(str)
    stop_speech = Signal()

    def __init__(self, core: Any = None, app_name: str = "Немуся") -> None:
        super().__init__()
        self.core = core
        self._worker: AgentWorker | None = None
        self.setWindowTitle(f"{app_name} — Немесис")
        self.resize(1180, 800)
        self.setStyleSheet(qss())
        self.setWindowIcon(self._make_app_icon())

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        layout.addWidget(self._build_sidebar(app_name), 0)
        layout.addWidget(self._build_chat_area(), 1)

    # ---------------------------------------------------------------- brand
    @staticmethod
    def _make_brand_mark(size: int = 40) -> QPixmap:
        """Скруглённый монограммный знак приложения."""
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0, QColor(ACCENT_PRIMARY))
        grad.setColorAt(1, QColor(ACCENT_SECONDARY))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        r = size * 0.28
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(size), float(size), r, r)
        p.drawPath(path)
        p.setPen(QPen(QColor(BG)))
        font = QFont()
        font.setPointSizeF(size * 0.42)
        font.setBold(True)
        p.setFont(font)
        p.drawText(0, 0, size, size, Qt.AlignmentFlag.AlignCenter, "Н")
        p.end()
        return pm

    def _make_app_icon(self) -> QIcon:
        """Иконка окна — та же монограмма крупнее."""
        return QIcon(self._make_brand_mark(64))

    # -------------------------------------------------------------- sidebar
    def _build_sidebar(self, app_name: str) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Sidebar")
        panel.setFixedWidth(320)
        box = QVBoxLayout(panel)
        box.setContentsMargins(22, 24, 22, 24)
        box.setSpacing(14)

        # Бренд
        brand = QWidget()
        brand_box = QHBoxLayout(brand)
        brand_box.setContentsMargins(0, 0, 0, 0)
        brand_box.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(self._make_brand_mark(40))
        brand_box.addWidget(mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        name = QLabel(app_name)
        name.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {ACCENT_PRIMARY};"
        )
        sub = QLabel(i18n.tr("sidebar.sub"))
        sub.setObjectName("SubLabel")
        brand_text.addWidget(name)
        brand_text.addWidget(sub)
        brand_box.addLayout(brand_text)
        brand_box.addStretch(1)
        box.addWidget(brand)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        box.addWidget(divider)

        # Пустой вертикальный отступ вокруг индикатора
        box.addStretch(1)

        self.orb = WaveformWidget()
        self.orb.set_state(AssistState.IDLE)
        orb_container = QWidget()
        orb_layout = QHBoxLayout(orb_container)
        orb_layout.setContentsMargins(0, 0, 0, 0)
        orb_layout.addWidget(self.orb)
        box.addWidget(orb_container)

        self.state_label = QLabel(i18n.tr(f"state.{AssistState.IDLE.value}"))
        self.state_label.setObjectName("StateLabel")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_state_label(AssistState.IDLE)

        self.model_label = QLabel(self._model_text())
        self.model_label.setObjectName("ModelLabel")
        self.model_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box.addWidget(self.state_label)
        box.addWidget(self.model_label)
        box.addStretch(2)

        self.listen_btn = QPushButton(i18n.tr("btn.listen"))
        self.listen_btn.setObjectName("Primary")
        self.listen_btn.setCheckable(True)
        self.listen_btn.clicked.connect(self._on_listen_clicked)

        self.realtime_btn = QPushButton(i18n.tr("btn.realtime"))
        self.realtime_btn.setObjectName("Ghost")
        self.realtime_btn.setCheckable(True)
        self.realtime_btn.clicked.connect(self._on_realtime_clicked)

        clear_btn = QPushButton(i18n.tr("btn.clear"))
        clear_btn.setObjectName("Ghost")
        clear_btn.clicked.connect(self._on_clear)

        settings_btn = QPushButton(i18n.tr("btn.settings"))
        settings_btn.setObjectName("Ghost")
        settings_btn.clicked.connect(self._on_settings)

        box.addWidget(self.listen_btn)
        box.addWidget(self.realtime_btn)
        box.addWidget(clear_btn)
        box.addWidget(settings_btn)

        return panel

    # ------------------------------------------------------------- chat area
    def _build_chat_area(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)

        header = QWidget()
        header_box = QHBoxLayout(header)
        header_box.setContentsMargins(4, 0, 4, 0)
        header_box.setSpacing(10)
        title = QLabel(i18n.tr("chat.header"))
        title.setObjectName("HeaderLabel")
        header_box.addWidget(title)
        header_box.addStretch(1)
        box.addWidget(header)

        self.chat = ChatView()
        self.chat.add_system(i18n.tr("chat.welcome"))

        # Пилюля ввода + круглая кнопка отправки
        self.input_pill = QWidget()
        self.input_pill.setObjectName("InputPill")
        pill_layout = QHBoxLayout(self.input_pill)
        pill_layout.setContentsMargins(4, 4, 4, 4)
        pill_layout.setSpacing(8)

        self.input = QLineEdit()
        self.input.setObjectName("PillInput")
        self.input.setPlaceholderText(i18n.tr("chat.placeholder"))
        self.input.returnPressed.connect(self._on_submit)
        self.input.installEventFilter(self)

        self.send_btn = QPushButton("↑")
        self.send_btn.setObjectName("SendButton")
        self.send_btn.setFixedSize(38, 38)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_submit)

        pill_layout.addWidget(self.input, 1)
        pill_layout.addWidget(self.send_btn)
        box.addWidget(self.chat, 1)
        box.addWidget(self.input_pill, 0)

        return panel

    def eventFilter(self, obj: Any, event: QEvent) -> bool:  # noqa: N802
        if obj is self.input:
            if event.type() in (
                QEvent.Type.FocusIn,
                QEvent.Type.FocusOut,
            ):
                active = event.type() == QEvent.Type.FocusIn
                self.input_pill.setProperty("active", active)
                self.input_pill.style().unpolish(self.input_pill)
                self.input_pill.style().polish(self.input_pill)
                return False
        return super().eventFilter(obj, event)

    # -------------------------------------------------------------- behaviour
    def _model_text(self) -> str:
        if self.core is not None:
            return i18n.tr("model.prefix", model=self.core.llm.model)
        return i18n.tr("model.demo")

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if not text or self._worker is not None:
            return
        self.input.clear()
        self._start_agent(text)

    def _on_clear(self) -> None:
        if self._worker is not None:
            return
        self.chat.clear_chat()
        if self.core is not None:
            self.core.agent.reset()
        self.chat.add_system(i18n.tr("chat.cleared"))

    def _on_listen_clicked(self, checked: bool) -> None:
        self.listen_btn.setText(i18n.tr("btn.listen_stop" if checked else "btn.listen"))
        self.listen_toggled.emit(checked)

    def _on_realtime_clicked(self, checked: bool) -> None:
        self.realtime_btn.setText(
            i18n.tr("btn.realtime_stop" if checked else "btn.realtime")
        )
        self.realtime_toggled.emit(checked)

    def set_realtime(self, active: bool) -> None:
        """Обновляет вид кнопки Realtime и блокирует классический голос."""
        self.realtime_btn.setChecked(active)
        self.realtime_btn.setText(
            i18n.tr("btn.realtime_stop" if active else "btn.realtime")
        )
        self.listen_btn.setEnabled(not active)

    def show_realtime_user(self, text: str) -> None:
        self.chat.add_user(text)

    def show_realtime_assistant(self, text: str) -> None:
        name = self.core.config.assistant.name if self.core is not None else "Немуся"
        self.chat.add_assistant(text, name)

    # ----------------------------------------------------------------- agent
    def submit_text(self, text: str) -> None:
        """Внешний вход (например, из голосового модуля)."""
        if self._worker is not None or not text.strip():
            return
        self._start_agent(text)

    def _start_agent(self, text: str) -> None:
        self.stop_speech.emit()
        self.chat.add_user(text)
        if self.core is None:
            self.chat.add_error(i18n.tr("chat.no_core"))
            return

        self._set_busy(True)
        self.set_state(AssistState.THINKING)
        self.chat.stream_start()

        self._worker = AgentWorker(self.core.agent, text, self)
        self._worker.event.connect(self._on_agent_event)
        self._worker.confirm_requested.connect(self._on_confirm_requested)
        self._worker.finished_run.connect(self._on_run_finished)
        self._worker.start()

    def _on_agent_event(self, event: AgentEvent) -> None:
        if event.kind == "assistant_delta":
            self.chat.stream_delta(event.text)
            self.set_state(AssistState.SPEAKING)
        elif event.kind == "assistant_text":
            # промежуточный текст (если он не пришёл дельтами) — закрываем поток
            self.chat.stream_end(event.text)
        elif event.kind == "tool_call":
            self.chat.add_tool_card(event.tool_name, event.tool_args)
            self.set_state(AssistState.THINKING)
        elif event.kind == "tool_result":
            ok = event.result.ok if event.result else False
            content = event.result.content if event.result else ""
            self.chat.add_system(
                i18n.tr(
                    "tool.result",
                    name=event.tool_name,
                    status=i18n.tr("tool.ok" if ok else "tool.fail"),
                )
            )
            if not ok:
                self.chat.add_error(content)
        elif event.kind == "final":
            self.chat.stream_end(event.text)
            if event.text.strip():
                self.speak_request.emit(event.text)
            self.set_state(AssistState.IDLE)
        elif event.kind == "error":
            self.chat.add_error(event.error if event.error else event.text)

    def _on_confirm_requested(self, payload: Any) -> None:
        tool, args = payload
        allowed = ConfirmDialog.ask(tool, args, self)
        if self._worker is not None:
            self._worker.resolve_confirmation(allowed)

    def _on_run_finished(self) -> None:
        self.chat.stream_end()
        self._set_busy(False)
        self.set_state(AssistState.IDLE)
        self._worker = None

    def _set_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)
        self.listen_btn.setEnabled(not busy)
        if not busy:
            self.input.setFocus()

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self.settings_saved.emit()

    # ------------------------------------------------------------- state/close
    def _apply_state_label(self, state: AssistState) -> None:
        _, accent = STATE_COLORS.get(state, STATE_COLORS[AssistState.IDLE])
        self.state_label.setText(f"●   {i18n.tr(f'state.{state.value}')}")
        self.state_label.setStyleSheet(f"color: {accent};")

    def set_state(self, state: AssistState) -> None:
        self.orb.set_state(state)
        self._apply_state_label(state)
        if state == AssistState.IDLE:
            self.model_label.setStyleSheet(f"color: {TEXT_DIMMER};")
        else:
            self.model_label.setStyleSheet(f"color: {TEXT_DIM};")

    def closeEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_tray_available", False):
            event.ignore()
            self.hide()
        else:
            event.accept()


__all__ = ["MainWindow"]
