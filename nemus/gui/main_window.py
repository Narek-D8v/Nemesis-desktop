"""Основное окно Немуси: сайдбар с волновым индикатором, чат, пилюля ввода."""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
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
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nemus.config import save_config
from nemus.core.agent import AgentEvent
from nemus.core.skill_commands import parse_skill_commands
from nemus.core.skills import load_skills, skill_highlights
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
from nemus.gui.widgets.elide import ElidedLabel
from nemus.gui.widgets.skill_suggest import SkillSuggester
from nemus.gui.widgets.waveform import WaveformWidget
from nemus.gui.worker import AgentWorker


class _SessionList(QListWidget):
    """Список диалогов в сайдбаре: клик переключает, ПКМ — действия."""

    context_requested = Signal(int, object)  # id диалога + глобальная позиция

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.pos())
        session_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(session_id, int):
            return super().contextMenuEvent(event)
        self.context_requested.emit(session_id, event.globalPos())


def _session_context_menu(
    window: MainWindow, parent: QWidget, session_id: int
) -> QMenu:
    """Общее контекстное меню диалога: переименовать/удалить (для списка и для шапки)."""
    menu = QMenu(parent)
    menu.addAction(
        i18n.tr("sessions.rename_one"), lambda: window._rename_session(session_id)
    )
    menu.addAction(
        i18n.tr("sessions.delete_one"), lambda: window._delete_session(session_id)
    )
    return menu


class SessionsDialog(QDialog):
    """Отдельное окно со списком диалогов — открывается кнопкой в шапке чата.

    Клик по строке переключает диалог и закрывает окно; ПКМ по строке —
    переименовать/удалить именно её; кнопки под списком — создание нового
    диалога и работа с выделенной строкой.
    """

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("SessionsDialog")
        self.setWindowTitle(i18n.tr("sessions.window"))
        self.setStyleSheet(qss())
        self.setMinimumSize(380, 420)

        self.session_list = _SessionList()
        self.session_list.setObjectName("SessionList")
        self.session_list.itemClicked.connect(self._on_item_clicked)
        self.session_list.context_requested.connect(self._on_context)

        # список — в карточке, кнопки — в отдельной панели: разные зоны
        card = QFrame()
        card.setObjectName("SessionCard")
        card_box = QVBoxLayout(card)
        card_box.setContentsMargins(4, 4, 4, 4)
        card_box.setSpacing(0)
        card_box.addWidget(self.session_list, 1)

        bar = QFrame()
        bar.setObjectName("SessionBar")
        bar_box = QHBoxLayout(bar)
        bar_box.setContentsMargins(6, 5, 6, 5)
        bar_box.setSpacing(6)
        for text, tip, slot in (
            (i18n.tr("sessions.new_short"), "sessions.new", self._new_session),
            ("✏", "sessions.rename", self._rename_selected),
            ("🗑", "sessions.delete", self._delete_selected),
        ):
            button = QPushButton(text)
            button.setObjectName("Ghost")
            button.setToolTip(i18n.tr(tip))
            button.clicked.connect(slot)
            bar_box.addWidget(button)
        bar_box.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(card, 1)
        layout.addWidget(bar)
        self.refresh()

    # ------------------------------------------------------------- список
    def refresh(self) -> None:
        """Перечитывает диалоги из памяти (текущий — с маркером «●»)."""
        memory = self._window._memory
        selected = self._selected_session_id()
        self.session_list.blockSignals(True)
        self.session_list.clear()
        if memory is not None:
            current = memory.current_session_id
            for entry in memory.list_sessions():
                session_id = int(entry["id"])
                title = str(entry["title"])
                # маркер вместо жирного начертания: для шрифта-заглушки
                # DirectWrite не умеет рисовать Bold и сыплет warning в лог
                item = QListWidgetItem(
                    f"● {title}" if session_id == current else title
                )
                item.setData(Qt.ItemDataRole.UserRole, session_id)
                item.setToolTip(
                    i18n.tr(
                        "sessions.count",
                        n=int(entry["messages"]),
                        date=str(entry["updated_at"])[:10],
                    )
                )
                self.session_list.addItem(item)
                if session_id in (selected, current):
                    self.session_list.setCurrentItem(item)
        self.session_list.blockSignals(False)

    def _selected_session_id(self) -> int | None:
        """id выделенной строки (``None`` → кнопки работают с текущим)."""
        item = self.session_list.currentItem()
        session_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return int(session_id) if isinstance(session_id, int) else None

    # ----------------------------------------------------------- действия
    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(session_id, int):
            return
        self._window._switch_session(session_id)
        # окно — выборщик: выбранный диалог стал текущим → возвращаемся к чату
        memory = self._window._memory
        if memory is not None and memory.current_session_id == session_id:
            self.accept()

    def _on_context(self, session_id: int, pos: Any) -> None:
        _session_context_menu(self._window, self, session_id).exec(pos)

    def _new_session(self) -> None:
        self._window._new_session()

    def _rename_selected(self) -> None:
        self._window._rename_session(self._selected_session_id())

    def _delete_selected(self) -> None:
        self._window._delete_session(self._selected_session_id())


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
        self._sessions_dialog: SessionsDialog | None = None
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
        self._init_sessions()

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

        # Диалоги живут в отдельном окне (кнопка в шапке чата) —
        # здесь место отдано только индикатору
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

        # длинное имя модели сокращается, а не обрезается посреди слова
        self.model_label = ElidedLabel(self._model_text())
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

        store_btn = QPushButton(i18n.tr("btn.store"))
        store_btn.setObjectName("Ghost")
        store_btn.setToolTip(i18n.tr("store.button_tip"))
        store_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        store_btn.clicked.connect(self._open_skill_store)

        settings_btn = QPushButton(i18n.tr("btn.settings"))
        settings_btn.setObjectName("Ghost")
        settings_btn.clicked.connect(self._on_settings)

        box.addWidget(self.listen_btn)
        box.addWidget(self.realtime_btn)
        box.addWidget(clear_btn)
        box.addWidget(store_btn)
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

        # переключатель диалогов: кнопка с выпадающим меню сессий
        self.session_btn = QPushButton(i18n.tr("sessions.button", title="…"))
        self.session_btn.setObjectName("Ghost")
        self.session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # одна плашка в шапке — она и открывает окно со списком диалогов
        self.session_btn.clicked.connect(self._open_sessions)
        header_box.addWidget(self.session_btn)
        box.addWidget(header)

        self.chat = ChatView()
        # клик по вариантам брейншторма — это ответ пользователя
        self.chat.choice_submitted.connect(self._on_choice_submitted)
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

        # подсказки команд из скиллов прямо в поле ввода (без лишних кликов)
        self.suggest = SkillSuggester(self.input, self)
        QTimer.singleShot(0, self._reload_skill_commands)

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

    # -------------------------------------------------------------- sessions
    @property
    def _memory(self) -> Any:
        """Хранилище сессий (``None``, если память отключена)."""
        return getattr(self.core, "memory", None) if self.core is not None else None

    @property
    def _agent(self) -> Any:
        return getattr(self.core, "agent", None) if self.core is not None else None

    def _init_sessions(self) -> None:
        self._refresh_session_button()
        self._restore_session_view()

    def _refresh_session_button(self) -> None:
        memory = self._memory
        if memory is None:
            self.session_btn.setText(i18n.tr("sessions.disabled"))
            self.session_btn.setEnabled(False)
            self._refresh_session_list()
            return
        title = memory.current_session_title()
        if len(title) > 26:
            title = title[:25] + "…"
        self.session_btn.setText(i18n.tr("sessions.button", title=title))
        self.session_btn.setToolTip(i18n.tr("sessions.hint"))
        self._refresh_session_list()

    def _refresh_session_list(self) -> None:
        """Обновляет список в окне диалогов (если оно открыто)."""
        dialog = self._sessions_dialog
        if dialog is not None and dialog.isVisible():
            dialog.refresh()

    def _open_sessions(self) -> None:
        """Открывает (или поднимает) окно со списком диалогов."""
        if self._memory is None:
            return
        if self._sessions_dialog is None:
            self._sessions_dialog = SessionsDialog(self)
        self._sessions_dialog.refresh()
        self._sessions_dialog.show()
        self._sessions_dialog.raise_()
        self._sessions_dialog.activateWindow()

    def _session_action_allowed(self) -> bool:
        """Диалоги не переключаются, пока агент занят, и без памяти."""
        if self._worker is not None:
            self.chat.add_system(i18n.tr("sessions.busy"))
            return False
        return self._memory is not None

    def _switch_session(self, session_id: int) -> None:
        if not self._session_action_allowed():
            # список мог уже выделить чужой диалог — возвращаем выделение
            self._refresh_session_list()
            return
        agent, memory = self._agent, self._memory
        if agent is None:
            return
        try:
            agent.switch_session(session_id)
        except ValueError:
            self._refresh_session_list()
            return
        self._render_session(memory.current_session_title())

    def _new_session(self) -> None:
        if not self._session_action_allowed():
            return
        agent, memory = self._agent, self._memory
        if agent is None:
            return
        agent.new_session()
        self._render_session(memory.current_session_title())

    def _rename_session(self, session_id: int | None = None) -> None:
        """Переименовывает диалог (без аргумента — текущий)."""
        if not self._session_action_allowed():
            return
        memory = self._memory
        session_id = (
            memory.current_session_id if session_id is None else int(session_id)
        )
        title, ok = QInputDialog.getText(
            self,
            i18n.tr("sessions.rename"),
            i18n.tr("sessions.rename_prompt"),
            text=memory.session_title(session_id),
        )
        if not ok or not title.strip():
            return
        memory.rename_session(session_id, title)
        self._refresh_session_button()
        self.chat.add_system(
            i18n.tr("sessions.renamed", title=title.strip()),
        )

    def _delete_session(self, session_id: int | None = None) -> None:
        """Удаляет диалог (без аргумента — текущий) с подтверждением."""
        if not self._session_action_allowed():
            return
        agent, memory = self._agent, self._memory
        if agent is None:
            return
        session_id = (
            memory.current_session_id if session_id is None else int(session_id)
        )
        title = memory.session_title(session_id)
        answer = QMessageBox.question(
            self,
            i18n.tr("sessions.delete"),
            i18n.tr("sessions.confirm_delete", title=title),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        was_current = session_id == memory.current_session_id
        memory.delete_session(session_id)
        self.chat.add_system(i18n.tr("sessions.deleted"))
        if was_current:
            # активный диалог удалён — агент и чат переходят на следующий
            agent.switch_session(memory.current_session_id)
            self._render_session(memory.current_session_title())
        else:
            # текущий диалог не тронут — меню перестроится при следующем открытии
            self._refresh_session_button()

    def _restore_session_view(self) -> None:
        """Показывает сохранённые реплики активного диалога после запуска."""
        memory = self._memory
        if memory is None or not memory.recent_messages(limit=200):
            return
        self._render_session(memory.current_session_title())

    def _render_session(self, title: str) -> None:
        """Перерисовывает чат содержимым активного диалога."""
        from nemus.core.memory import load_messages_into

        # кнопка в шапке всегда показывает текущий диалог
        self._refresh_session_button()
        memory = self._memory
        rows = memory.recent_messages(limit=200) if memory is not None else []
        self.chat.clear_chat()
        self.chat.add_system(i18n.tr("sessions.showing", title=title))

        messages: list[Any] = []
        load_messages_into(messages, rows)
        name = self.core.config.assistant.name if self.core is not None else "Немуся"
        for message in messages:
            if message.role == "user":
                if isinstance(message.content, str) and message.content:
                    self.chat.add_user(message.content)
            elif message.role == "assistant":
                for call in message.tool_calls or []:
                    self.chat.add_tool_card(call.name, call.arguments)
                if isinstance(message.content, str) and message.content:
                    self.chat.add_assistant(message.content, name)

    # -------------------------------------------------------------- behaviour
    def _model_text(self) -> str:
        if self.core is not None:
            return i18n.tr("model.prefix", model=self.core.llm.model)
        return i18n.tr("model.demo")

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if not text or self._worker is not None:
            return
        # открыт список подсказок: Enter выбирает команду, а не отправляет
        if self.suggest.popup_visible():
            return
        if self._show_skill_help(text):
            return
        self.input.clear()
        self._start_agent(text)

    def _show_skill_help(self, text: str) -> bool:
        """«/имя» без задачи — карточка справки, без запроса к модели.

        Показывает, что умеет скилл (его описание и разделы инструкции) и как
        запустить его с задачей. Неизвестная команда — список доступных.
        """
        # имя команды без пробелов и без вложенных слэшей («/usr/bin» — путь)
        match = re.fullmatch(r"/([^\s/]{1,64})", text)
        if match is None:
            return False
        cfg = getattr(self.core, "config", None)
        skills_cfg = getattr(cfg, "skills", None)
        if skills_cfg is None or not getattr(skills_cfg, "enabled", False):
            return False
        self.input.clear()
        name = match.group(1)
        try:
            skills = load_skills(
                getattr(skills_cfg, "dir", None),
                disabled=getattr(skills_cfg, "disabled", ()),
            )
        except OSError:
            skills = []
        available = ", ".join(f"/{item.name}" for item in skills[:12]) or "—"
        skill = next((item for item in skills if item.name.lower() == name.lower()), None)
        if skill is None:
            self.chat.add_system(
                f"{i18n.tr('help.unknown', cmd='/' + name)} "
                f"{i18n.tr('help.available', list=available)}"
            )
            return True
        self.chat.add_skill_help(
            name=skill.name,
            description=skill.description,
            capabilities=skill_highlights(skill.body),
            files=len(skill.files),
            hint=i18n.tr("help.hint", cmd=f"/{skill.name}"),
        )
        return True

    def _on_choice_submitted(self, text: str) -> None:
        """Выбор вариантов брейншторма в чате — отправляем как сообщение."""
        if self._worker is not None or not text.strip():
            return
        self._start_agent(text)

    def _on_clear(self) -> None:
        if self._worker is not None:
            return
        self.chat.clear_chat()
        if self.core is not None:
            self.core.agent.reset()
            if self._memory is not None:
                # чистим текущий диалог и в БД — иначе он вернётся после рестарта
                self._memory.clear_messages()
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

    def show_realtime_tool(
        self, name: str, args: dict[str, Any], phase: str, detail: str = ""
    ) -> None:
        """Вызов инструмента в realtime-режиме: карточка и статус в чате.

        Без этого вызовы Gemini Live/OpenAI Realtime оставались бы невидимыми
        в GUI — работа шла, а в чате не было ничего.
        """
        if phase == "start":
            self.chat.add_tool_card(name, args)
            self.set_state(AssistState.THINKING)
            return
        ok = phase == "ok"
        self.chat.add_system(
            i18n.tr(
                "tool.result",
                name=name,
                status=i18n.tr("tool.ok" if ok else "tool.fail"),
            )
        )
        if not ok and detail:
            self.chat.add_error(detail)

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
        # блок ответа откроется лениво (stream_delta), чтобы мысли модели
        # успели встать перед ответом, а не после него

        self._worker = AgentWorker(self.core.agent, text, self)
        self._worker.event.connect(self._on_agent_event)
        self._worker.confirm_requested.connect(self._on_confirm_requested)
        self._worker.finished_run.connect(self._on_run_finished)
        self._worker.start()

    def _on_agent_event(self, event: AgentEvent) -> None:
        if event.kind == "assistant_thought":
            # скрытые рассуждения модели — отдельным блоком «мыслей»
            self.chat.thought_delta(event.text)
            self.set_state(AssistState.THINKING)
        elif event.kind == "assistant_delta":
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
            self.chat.thought_end()
            self.chat.stream_end(event.text)
            if event.text.strip():
                self.speak_request.emit(event.text)
            self.set_state(AssistState.IDLE)
        elif event.kind == "error":
            self.chat.thought_end()
            self.chat.add_error(event.error if event.error else event.text)

    def _on_confirm_requested(self, payload: Any) -> None:
        tool, args = payload
        allowed = ConfirmDialog.ask(tool, args, self)
        if self._worker is not None:
            self._worker.resolve_confirmation(allowed)

    def _on_run_finished(self) -> None:
        self.chat.thought_end()
        self.chat.stream_end()
        self._set_busy(False)
        self.set_state(AssistState.IDLE)
        self._worker = None
        # первая реплика могла назвать диалог — обновляем кнопку и список
        self._refresh_session_button()

    def _set_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)
        self.listen_btn.setEnabled(not busy)
        if not busy:
            self.input.setFocus()

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self, core=self.core)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self._reload_skill_commands()
            self.settings_saved.emit()

    def _open_skill_store(self) -> None:
        """Открывает магазин скиллов в отдельном окне (не модально для чата)."""
        from nemus.gui.skill_store import SkillStoreDialog

        cfg = getattr(self.core, "config", None)
        skills_cfg = getattr(cfg, "skills", None)
        directory = getattr(skills_cfg, "dir", None) if skills_cfg else None
        dialog = SkillStoreDialog(self, directory=directory)
        dialog.installed.connect(self._on_skill_installed)
        dialog.removed.connect(self._on_skill_removed)
        dialog.updated.connect(self._on_skill_updated)
        dialog.exec()
        dialog.deleteLater()

    def _on_skill_installed(self, name: str) -> None:
        """Скилл из магазина сразу включается в работу, без перезапуска."""
        cfg = getattr(self.core, "config", None)
        skills_cfg = getattr(cfg, "skills", None)
        if skills_cfg is not None and not getattr(skills_cfg, "enabled", False):
            skills_cfg.enabled = True
            try:
                save_config(cfg)
            except OSError as exc:
                self.chat.add_error(i18n.tr("app.settings_failed", msg=str(exc)))
            # сам тумблер ничего не включает в ядре — просим приложение
            # пересобрать конфиг и инструменты скиллов
            self.settings_saved.emit()
            self.chat.add_system(i18n.tr("store.enabled_chat"))
        self._reload_skill_commands()
        self.chat.add_system(i18n.tr("store.installed_chat", name=name))

    def _on_skill_removed(self, name: str) -> None:
        """Удалённый скилл сразу пропадает из подсказок и системного промпта."""
        self._reload_skill_commands()
        self.chat.add_system(i18n.tr("store.removed_chat", name=name))

    def _on_skill_updated(self, name: str) -> None:
        """Обновлённый скилл подхватывается сразу — со следующего запроса."""
        self._reload_skill_commands()
        self.chat.add_system(i18n.tr("store.updated_chat", name=name))

    def _reload_skill_commands(self) -> None:
        """Перечитывает скиллы и обновляет подсказки команд в поле ввода."""
        cfg = getattr(self.core, "config", None)
        skills_cfg = getattr(cfg, "skills", None)
        if skills_cfg is None or not getattr(skills_cfg, "enabled", False):
            self.suggest.set_commands([])
            return
        try:
            skills = load_skills(
                getattr(skills_cfg, "dir", None),
                disabled=getattr(skills_cfg, "disabled", ()),
            )
        except OSError:
            skills = []
        self.suggest.set_commands(parse_skill_commands(skills))

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
