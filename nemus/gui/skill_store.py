"""Магазин скиллов и плагинов: отдельное окно с карточками и установкой.

Две части: «Скиллы» (витрина, поиск и скачанное — как раньше; скиллы из
скилл-паков с плагинами тоже здесь) и «Плагины» (плагины Claude Code
целиком: скиллы, команды и агенты внутри). Установка — один клик по «+»:
пакет скачивается в фоне и сразу работает со следующего запроса.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from nemus.core.skill_store import (
    GitHubClient,
    InstalledPlugin,
    StoreError,
    StorePlugin,
    StoreSkill,
    forget_origin,
    install,
    install_plugin,
    install_plugin_local,
    install_skill_local,
    installed_plugins,
    is_installed,
    is_plugin_installed,
    origin_of,
    recommended_plugins,
    recommended_pool,
    remove_plugin,
    search,
    search_plugins,
    sort_skills,
    update_installed,
    update_plugin,
)
from nemus.core.skills import (
    Skill,
    get_skill,
    load_skills,
    remove_skill,
    skill_plugin,
)
from nemus.gui import i18n
from nemus.gui.theme import qss

#: воркеры, не успевшие завершиться к закрытию окна: держим в живых, иначе
#: Python уничтожит работающий QThread и Qt упадёт
_DETACHED: set[QThread] = set()

#: витрина показывается порциями: догружается при прокрутке до конца
REC_PAGE = 24
#: каждый шаг догрузки расширяет охват на столько репозиториев
REC_REPOS_STEP = 6
#: сколько шагов расширения всего (8 → 14 → 20 → 26 репозиториев)
REC_STEPS = 3


def _clip(text: str, limit: int = 220) -> str:
    """Обрезает описание для карточки, полный текст остаётся в подсказке."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _CatalogWorker(QThread):
    """Загружает витрину или результаты поиска в фоне."""

    done = Signal(str, int, object, str)  # режим, номер запроса, скиллы, ошибка

    def __init__(
        self,
        mode: str,
        query: str,
        client: GitHubClient,
        seq: int = 0,
        refresh: bool = False,
        expand: int = 0,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._query = query
        self._client = client
        self._seq = seq
        self._refresh = refresh
        self._expand = expand

    def run(self) -> None:  # noqa: D102
        try:
            skills = (
                recommended_pool(
                    self._client,
                    limit=REC_PAGE,
                    repos_limit=8 + REC_REPOS_STEP * self._expand,
                    refresh=self._refresh,
                )
                if self._mode == "recommended"
                else search(
                    self._client,
                    self._query,
                    limit_repos=5 + 4 * self._expand,
                    per_repo=8 + 8 * self._expand,
                )
            )
            self.done.emit(self._mode, self._seq, list(skills), "")
        except StoreError as exc:
            self.done.emit(self._mode, self._seq, [], str(exc))
        except Exception as exc:  # noqa: BLE001 — воркер не должен ронять окно
            self.done.emit(self._mode, self._seq, [], str(exc))


class _InstallWorker(QThread):
    """Скачивает скилл в папку скиллов в фоне."""

    done = Signal(object, object, str)  # скилл-карточка, установленный Skill, ошибка

    def __init__(
        self, skill: StoreSkill, directory: str | Path | None, client: GitHubClient
    ) -> None:
        super().__init__()
        self._skill = skill
        self._directory = directory
        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            installed = install(self._skill, self._directory, self._client)
            self.done.emit(self._skill, installed, "")
        except StoreError as exc:
            self.done.emit(self._skill, None, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(self._skill, None, str(exc))


class _UpdateWorker(QThread):
    """Обновляет установленный скилл с GitHub в фоне."""

    done = Signal(str, bool, str)  # имя, изменился ли, ошибка

    def __init__(
        self, name: str, directory: str | Path | None, client: GitHubClient
    ) -> None:
        super().__init__()
        self._name = name
        self._directory = directory
        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            _, changed = update_installed(self._name, self._directory, self._client)
            self.done.emit(self._name, changed, "")
        except StoreError as exc:
            self.done.emit(self._name, False, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(self._name, False, str(exc))


class _PluginCatalogWorker(QThread):
    """Загружает витрину или результаты поиска плагинов в фоне."""

    done = Signal(str, int, object, str)  # режим, номер запроса, плагины, ошибка

    def __init__(
        self,
        mode: str,
        query: str,
        client: GitHubClient,
        seq: int = 0,
        refresh: bool = False,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._query = query
        self._client = client
        self._seq = seq
        self._refresh = refresh

    def run(self) -> None:  # noqa: D102
        try:
            found = (
                recommended_plugins(
                    self._client,
                    limit=REC_PAGE * 3,
                    refresh=self._refresh,
                )
                if self._mode == "recommended"
                else search_plugins(self._client, self._query, limit_repos=5, per_repo=12)
            )
            self.done.emit(self._mode, self._seq, list(found), "")
        except StoreError as exc:
            self.done.emit(self._mode, self._seq, [], str(exc))
        except Exception as exc:  # noqa: BLE001 — воркер не должен ронять окно
            self.done.emit(self._mode, self._seq, [], str(exc))


class _PluginInstallWorker(QThread):
    """Ставит плагин целиком в папку скиллов в фоне."""

    done = Signal(object, str, str)  # плагин-карточка, папка, ошибка

    def __init__(
        self, plugin: StorePlugin, directory: str | Path | None, client: GitHubClient
    ) -> None:
        super().__init__()
        self._plugin = plugin
        self._directory = directory
        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            folder = install_plugin(self._plugin, self._directory, self._client)
            self.done.emit(self._plugin, folder, "")
        except StoreError as exc:
            self.done.emit(self._plugin, "", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(self._plugin, "", str(exc))


class _PluginLocalWorker(QThread):
    """Ставит плагин из локального архива или папки в фоне."""

    done = Signal(str, str)  # папка или "", ошибка

    def __init__(self, source: str, directory: str | Path | None) -> None:
        super().__init__()
        self._source = source
        self._directory = directory

    def run(self) -> None:  # noqa: D102
        try:
            folder, _ = install_plugin_local(self._source, self._directory)
            self.done.emit(folder, "")
        except StoreError as exc:
            self.done.emit("", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit("", str(exc))


class _SkillLocalWorker(QThread):
    """Ставит отдельный скилл из локального архива или папки в фоне."""

    done = Signal(object, str)  # Skill, ошибка

    def __init__(self, source: str, directory: str | Path | None) -> None:
        super().__init__()
        self._source = source
        self._directory = directory

    def run(self) -> None:  # noqa: D102
        try:
            skill, _ = install_skill_local(self._source, self._directory)
            self.done.emit(skill, "")
        except StoreError as exc:
            self.done.emit(None, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(None, str(exc))


class _PluginUpdateWorker(QThread):
    """Обновляет установленный плагин с GitHub в фоне."""

    done = Signal(str, bool, str)  # папка, изменился ли, ошибка

    def __init__(
        self, folder: str, directory: str | Path | None, client: GitHubClient
    ) -> None:
        super().__init__()
        self._folder = folder
        self._directory = directory
        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            _, changed = update_plugin(self._folder, self._directory, self._client)
            self.done.emit(self._folder, changed, "")
        except StoreError as exc:
            self.done.emit(self._folder, False, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(self._folder, False, str(exc))


class SkillStoreDialog(QDialog):
    """Окно магазина скиллов: витрина, поиск, уже скачанное."""

    #: имя установленного скилла — чтобы главное окно обновило подсказки
    installed = Signal(str)
    #: имя удалённого скилла — подсказки и системный промпт теряют его сразу
    removed = Signal(str)
    #: имя обновлённого скилла — модели нужен свежий текст инструкции
    updated = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        directory: str | Path | None = None,
        client: GitHubClient | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SkillStore")
        self.setWindowTitle(i18n.tr("store.title"))
        self.setStyleSheet(qss())
        self.resize(880, 640)
        self.setMinimumSize(640, 460)

        self._directory = directory
        self._client = client or GitHubClient()
        #: имена скиллов в папке (обновляется после установки и по кнопке)
        #: карточки по ключу скилла: одна установка красит все дубли
        self._buttons: dict[str, list[QPushButton]] = {}
        #: ключ карточки → имя скилла (для пересчёта галочек после удаления)
        self._card_names: dict[str, StoreSkill] = {}
        #: кнопки «обновить» в разделе «Скачанные»
        self._update_buttons: dict[str, QPushButton] = {}
        self._workers: list[QThread] = []
        self._search_seq = 0
        #: результаты поиска тоже растут порциями при прокрутке
        self._search_pool: list[StoreSkill] = []
        self._search_keys: set[str] = set()
        self._search_expand = 0
        self._search_want = False
        self._search_loading = False
        self._install_worker: _InstallWorker | None = None
        #: все скиллы витрины (по звёздам) и ключи уже показанных карточек
        self._rec_pool: list[StoreSkill] = []
        #: текущий фильтр по репозиторию и порядок карточек
        self._rec_repo = ""
        self._rec_sort = "stars"
        #: пул под фильтром — из него доливаются карточки
        self._rec_view: list[StoreSkill] = []
        self._rec_keys: set[str] = set()
        #: очередная загрузка начинает список заново, а не доливает его
        self._rec_reset = True
        #: расширение охвата (0 — базовые репозитории) и идёт ли догрузка
        self._rec_expand = 0
        self._rec_want = False
        self._rec_loading = False

        #: плагины: витрина, поиск и установленные — отдельные от скиллов
        self._plugin_pool: list[StorePlugin] = []
        self._plugin_keys: set[str] = set()
        self._plugin_loading = False
        self._plugin_loaded = False
        self._plugin_search_pool: list[StorePlugin] = []
        self._plugin_search_keys: set[str] = set()
        self._plugin_seq = 0
        #: кнопки «+» на карточках плагинов: одна установка красит все дубли
        self._p_buttons: dict[str, list[QPushButton]] = {}
        self._p_cards: dict[str, StorePlugin] = {}
        self._p_update_buttons: dict[str, QPushButton] = {}
        self._plugin_install_worker: _PluginInstallWorker | _PluginLocalWorker | None = (
            None
        )
        self._skill_local_worker: _SkillLocalWorker | None = None

        self._build()
        self._reload_installed()
        self._reload_plugin_installed()
        self._load_recommended()

        #: витрина обновляется сама: новый случайный набор без перезапуска окна
        self._auto = QTimer(self)
        self._auto.setInterval(60_000)
        self._auto.timeout.connect(self._auto_refresh)
        self._auto.start()

    # ------------------------------------------------------------------ сборка
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 16, 16, 12)
        box.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel(i18n.tr("store.title"))
        title.setObjectName("StoreTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.btn_refresh = QPushButton(i18n.tr("store.refresh"))
        self.btn_refresh.setObjectName("Ghost")
        self.btn_refresh.setToolTip(i18n.tr("store.refresh_tip"))
        self.btn_refresh.clicked.connect(self._refresh_recommended)
        header.addWidget(self.btn_refresh)
        box.addLayout(header)

        #: магазин из двух частей: скиллы (включая скилл-паки) и плагины
        self.sections = QTabWidget()
        self.sections.setObjectName("StoreSections")
        self.section_skills = self.sections.addTab(
            self._skills_page(), i18n.tr("store.section.skills")
        )
        self.section_plugins = self.sections.addTab(
            self._plugins_page(), i18n.tr("store.section.plugins")
        )
        self.sections.currentChanged.connect(self._on_section_changed)
        box.addWidget(self.sections, 1)

        self.status = QLabel("")
        self.status.setObjectName("StoreStatus")
        self.status.setWordWrap(True)
        box.addWidget(self.status)

    def _skills_page(self) -> QWidget:
        """Вкладка «Скиллы»: витрина, поиск и скачанное (как раньше)."""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        hint = QLabel(i18n.tr("store.hint"))
        hint.setObjectName("StoreHint")
        hint.setWordWrap(True)
        top.addWidget(hint, 1)
        self.btn_skill_zip = QPushButton(i18n.tr("store.plugin_local_zip"))
        self.btn_skill_zip.setObjectName("Ghost")
        self.btn_skill_zip.setToolTip(i18n.tr("store.skill_local_zip_tip"))
        self.btn_skill_zip.clicked.connect(self._pick_skill_zip)
        top.addWidget(self.btn_skill_zip, 0, Qt.AlignmentFlag.AlignTop)
        self.btn_skill_folder = QPushButton(i18n.tr("store.plugin_local_folder"))
        self.btn_skill_folder.setObjectName("Ghost")
        self.btn_skill_folder.setToolTip(i18n.tr("store.skill_local_folder_tip"))
        self.btn_skill_folder.clicked.connect(self._pick_skill_folder)
        top.addWidget(self.btn_skill_folder, 0, Qt.AlignmentFlag.AlignTop)
        box.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(i18n.tr("store.search_placeholder"))
        self.search_input.returnPressed.connect(self._run_search)
        row.addWidget(self.search_input, 1)
        self.btn_search = QPushButton(i18n.tr("store.find"))
        self.btn_search.setObjectName("Ghost")
        self.btn_search.clicked.connect(self._run_search)
        row.addWidget(self.btn_search)
        box.addLayout(row)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.repo_filter = QComboBox()
        self.repo_filter.setObjectName("StoreFilter")
        self.repo_filter.setToolTip(i18n.tr("store.filter_tip"))
        self.repo_filter.currentIndexChanged.connect(self._rec_refilter)
        filters.addWidget(self.repo_filter, 1)
        self.sort_mode = QComboBox()
        self.sort_mode.setObjectName("StoreFilter")
        self.sort_mode.setToolTip(i18n.tr("store.sort_tip"))
        self.sort_mode.addItem(i18n.tr("store.sort.stars"), "stars")
        self.sort_mode.addItem(i18n.tr("store.sort.new"), "new")
        self.sort_mode.currentIndexChanged.connect(self._rec_refilter)
        filters.addWidget(self.sort_mode)
        box.addLayout(filters)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("StoreTabs")
        self._rec_area = self._scroll_page("_rec_box")
        self._rec_area.verticalScrollBar().valueChanged.connect(self._on_rec_scroll)
        self.tab_recommended = self.tabs.addTab(
            self._rec_area, i18n.tr("store.tab.recommended")
        )
        self._search_area = self._scroll_page("_search_box")
        self._search_area.verticalScrollBar().valueChanged.connect(
            self._on_search_scroll
        )
        self.tab_search = self.tabs.addTab(
            self._search_area, i18n.tr("store.tab.search")
        )
        self.tab_installed = self.tabs.addTab(
            self._scroll_page("_installed_box"), i18n.tr("store.tab.installed")
        )
        self.tabs.currentChanged.connect(self._on_tab_changed)
        box.addWidget(self.tabs, 1)
        return page

    def _plugins_page(self) -> QWidget:
        """Вкладка «Плагины»: пакеты Claude Code целиком, вместе с командами."""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        hint = QLabel(i18n.tr("store.plugin_hint"))
        hint.setObjectName("StoreHint")
        hint.setWordWrap(True)
        top.addWidget(hint, 1)
        self.btn_plugin_zip = QPushButton(i18n.tr("store.plugin_local_zip"))
        self.btn_plugin_zip.setObjectName("Ghost")
        self.btn_plugin_zip.setToolTip(i18n.tr("store.plugin_local_zip_tip"))
        self.btn_plugin_zip.clicked.connect(self._pick_plugin_zip)
        top.addWidget(self.btn_plugin_zip, 0, Qt.AlignmentFlag.AlignTop)
        self.btn_plugin_folder = QPushButton(i18n.tr("store.plugin_local_folder"))
        self.btn_plugin_folder.setObjectName("Ghost")
        self.btn_plugin_folder.setToolTip(i18n.tr("store.plugin_local_folder_tip"))
        self.btn_plugin_folder.clicked.connect(self._pick_plugin_folder)
        top.addWidget(self.btn_plugin_folder, 0, Qt.AlignmentFlag.AlignTop)
        box.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.plugin_search_input = QLineEdit()
        self.plugin_search_input.setPlaceholderText(
            i18n.tr("store.plugin_search_placeholder")
        )
        self.plugin_search_input.returnPressed.connect(self._run_plugin_search)
        row.addWidget(self.plugin_search_input, 1)
        self.btn_plugin_search = QPushButton(i18n.tr("store.find"))
        self.btn_plugin_search.setObjectName("Ghost")
        self.btn_plugin_search.clicked.connect(self._run_plugin_search)
        row.addWidget(self.btn_plugin_search)
        box.addLayout(row)

        self.plugin_tabs = QTabWidget()
        self.plugin_tabs.setObjectName("StoreTabs")
        self._plugin_rec_area = self._scroll_page("_prec_box")
        self._plugin_rec_area.verticalScrollBar().valueChanged.connect(
            self._on_plugin_scroll
        )
        self.plugin_rec_tab = self.plugin_tabs.addTab(
            self._plugin_rec_area, i18n.tr("store.tab.recommended")
        )
        self._plugin_search_area = self._scroll_page("_psearch_box")
        self.plugin_search_tab = self.plugin_tabs.addTab(
            self._plugin_search_area, i18n.tr("store.tab.search")
        )
        self.plugin_installed_tab = self.plugin_tabs.addTab(
            self._scroll_page("_pinstalled_box"), i18n.tr("store.tab.installed")
        )
        self.plugin_tabs.currentChanged.connect(self._on_plugin_tab_changed)
        box.addWidget(self.plugin_tabs, 1)
        return page

    def _scroll_page(self, attr: str) -> QScrollArea:
        """Страница прокрутки с вертикальным списком карточек."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 4, 8, 4)
        layout.setSpacing(8)
        area.setWidget(inner)
        setattr(self, attr, layout)
        return area

    def _on_section_changed(self, index: int) -> None:
        """Переключили «Скиллы»/«Плагины»: плагины грузим при первом заходе."""
        if index != self.section_plugins:
            return
        if not self._plugin_loaded and not self._plugin_loading:
            self._load_plugin_recommended()

    def _on_plugin_tab_changed(self, index: int) -> None:
        """Возврат на витрину плагинов — перечитываем установленное."""
        if index == self.plugin_installed_tab:
            self._reload_plugin_installed()

    # ------------------------------------------------------------------ карточки
    def _card(self, skill: StoreSkill) -> QFrame:
        """Карточка скилла в витрине: имя, источник, описание, «+»."""
        card = QFrame()
        card.setObjectName("StoreCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)

        texts = QVBoxLayout()
        texts.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(skill.name)
        name.setObjectName("StoreCardTitle")
        top.addWidget(name)
        source = QLabel(skill.source)
        source.setObjectName("StoreCardMeta")
        top.addWidget(source)
        top.addStretch(1)
        texts.addLayout(top)

        desc = QLabel(_clip(skill.description) or i18n.tr("skills.no_desc"))
        desc.setObjectName("StoreCardDesc")
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.PlainText)
        if skill.description:
            desc.setToolTip(skill.description)
        texts.addWidget(desc)

        meta = i18n.tr("store.files", n=len(skill.files))
        if skill.plugin:
            meta = f"{i18n.tr('store.plugin', name=skill.plugin)} · {meta}"
        if skill.path:
            meta = f"{skill.path} · {meta}"
        if skill.updated:
            meta = f"{meta} · {i18n.tr('store.fresh', date=skill.updated[:10])}"
        files = QLabel(meta)
        files.setObjectName("StoreCardFiles")
        texts.addWidget(files)
        row.addLayout(texts, 1)

        btn = QPushButton("+")
        btn.setObjectName("StoreInstall")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(34, 34)
        btn.setToolTip(i18n.tr("store.install", name=skill.name))
        btn.clicked.connect(lambda _=False, s=skill: self._install(s))
        row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        alive = [b for b in self._buttons.get(skill.key, []) if isValid(b)]
        self._buttons[skill.key] = [*alive, btn]
        self._card_names[skill.key] = skill
        self._mark_installed(skill.key, is_installed(skill, self._directory))
        return card

    def _sync_buttons(self) -> None:
        """Перекрашивает «+»/«✓» по текущему содержимому папки скиллов."""
        for key, skill in self._card_names.items():
            self._mark_installed(key, is_installed(skill, self._directory))

    def _mark_installed(self, key: str, done: bool) -> None:
        """Ставит на кнопки карточки «+» или «✓» (и перекрашивает их)."""
        alive: list[QPushButton] = []
        for btn in self._buttons.get(key, []):
            if not isValid(btn):  # карточка уже перестроена и удалена Qt
                continue
            alive.append(btn)
            btn.setText("✓" if done else "+")
            btn.setEnabled(not done)
            btn.setProperty("installed", "true" if done else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._buttons[key] = alive

    def _clear(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _notice(self, layout: QVBoxLayout, text: str, *, loading: bool = False) -> QLabel:
        """Сообщение вместо карточек: «загружаем…», пусто, ошибка."""
        label = QLabel(text)
        label.setObjectName("StoreStatus")
        label.setWordWrap(True)
        label.setProperty("loading", "true" if loading else "false")
        layout.addWidget(label)
        layout.addStretch(1)
        return label

    def _fill(self, layout: QVBoxLayout, skills: list[StoreSkill], empty: str) -> None:
        self._clear(layout)
        if not skills:
            self._notice(layout, empty)
            return
        for skill in skills:
            layout.addWidget(self._card(skill))
        layout.addStretch(1)

    # ------------------------------------------------------------ витрина/поиск
    def _load_recommended(
        self, refresh: bool = False, silent: bool = False, more: bool = False
    ) -> None:
        """Перезагружает витрину «Рекомендуемые» (refresh — минуя кэш).

        ``silent`` — фоновая пересборка: старые карточки не убираем, чтобы
        список не мигал на месте. ``more`` — доливка: расширяем охват
        репозиториев и дописываем карточки к уже показанным.
        """
        if more:
            if self._rec_loading or self._rec_expand >= REC_STEPS:
                return
            self._rec_expand += 1
            self._rec_want = True
            self._rec_loading = True
            self.status.setText(i18n.tr("store.rec_loading"))
            self._start_catalog("recommended", "", expand=self._rec_expand)
            return
        self._rec_reset = not silent
        if not silent:
            self._rec_expand = 0
            self._clear(self._rec_box)
            self._notice(self._rec_box, i18n.tr("store.loading"), loading=True)
        self._rec_want = False
        # тихая пересборка не сужает уже расширенный охват репозиториев
        self._start_catalog(
            "recommended", "", refresh=refresh, expand=self._rec_expand
        )

    def _on_rec_scroll(self, value: int) -> None:
        """Прокрутили почти до конца — подсовываем следующую порцию."""
        bar = self._rec_area.verticalScrollBar()
        if bar.maximum() and value >= bar.maximum() - 240:
            self._load_more_recommended()

    def _load_more_recommended(self) -> None:
        """Конец списка: доливаем порцию — сразу из пула или с GitHub."""
        if self.tabs.currentIndex() != self.tab_recommended or self._rec_loading:
            return
        if self._rec_add_page():
            return
        if self._rec_expand >= REC_STEPS:
            self.status.setText(i18n.tr("store.rec_end"))
            return
        self._load_recommended(more=True)

    def _rec_add_page(self) -> int:
        """Дописывает на витрину порцию ещё не показанных карточек."""
        added = 0
        for skill in self._rec_view:
            if added >= REC_PAGE:
                break
            if skill.key in self._rec_keys:
                continue
            self._rec_keys.add(skill.key)
            last = max(0, self._rec_box.count() - 1)  # перед растяжкой внизу
            self._rec_box.insertWidget(last, self._card(skill))
            added += 1
        return added

    def _rec_filtered(self) -> list[StoreSkill]:
        """Пул витрины под выбранный репозиторий и выбранный порядок."""
        pool = [
            skill
            for skill in self._rec_pool
            if not self._rec_repo or skill.repo == self._rec_repo
        ]
        return sort_skills(pool, self._rec_sort)

    def _rec_render(self, empty: str = "") -> None:
        """Перерисовывает витрину с начала — под текущий фильтр и порядок."""
        self._clear(self._rec_box)
        self._rec_keys.clear()
        self._rec_view = self._rec_filtered()
        if not self._rec_view:
            self._notice(self._rec_box, empty or i18n.tr("store.rec_empty"))
            return
        self._rec_box.addStretch(1)
        self._rec_add_page()

    def _fill_repo_filter(self) -> None:
        """Обновляет список репозиториев в фильтре (карточки не трогает)."""
        repos = sorted({skill.repo for skill in self._rec_pool}, key=str.lower)
        self.repo_filter.blockSignals(True)
        self.repo_filter.clear()
        self.repo_filter.addItem(
            i18n.tr("store.filter_all", n=len(self._rec_pool)), ""
        )
        for repo in repos:
            count = sum(1 for skill in self._rec_pool if skill.repo == repo)
            self.repo_filter.addItem(f"{repo} ({count})", repo)
        index = self.repo_filter.findData(self._rec_repo)
        self.repo_filter.setCurrentIndex(max(0, index))
        self._rec_repo = str(self.repo_filter.currentData() or "")
        self.repo_filter.blockSignals(False)

    def _rec_refilter(self, _index: int = 0) -> None:
        """Выбрали другой репозиторий или порядок — пересобираем витрину."""
        if not self._rec_pool:
            return
        self._rec_repo = str(self.repo_filter.currentData() or "")
        self._rec_sort = str(self.sort_mode.currentData() or "stars")
        self._rec_render()

    def _auto_refresh(self) -> None:
        """Тикает раз в минуту: витрина пересобирается из кэша за доли секунды."""
        if self.sections.currentIndex() != self.section_skills:
            return
        if self.tabs.currentIndex() != self.tab_recommended or self._rec_loading:
            return
        if self._install_worker is not None and self._install_worker.isRunning():
            return
        self._load_recommended(silent=True)

    def _on_tab_changed(self, index: int) -> None:
        """Возврат на «Рекомендуемые» — сразу свежая перемешанная подборка."""
        if (
            index == self.tab_recommended
            and self.sections.currentIndex() == self.section_skills
        ):
            self._load_recommended(silent=True)

    def _refresh_recommended(self) -> None:
        """Кнопка «Обновить»: перекачивает каталог текущей части магазина."""
        if self.sections.currentIndex() == self.section_plugins:
            self._load_plugin_recommended(refresh=True)
            return
        self._load_recommended(True)

    def _run_search(self) -> None:
        """Ищет скиллы по введённому запросу и показывает результаты."""
        query = self.search_input.text().strip()
        self.tabs.setCurrentIndex(self.tab_search)
        self._search_seq += 1
        seq = self._search_seq
        self._clear(self._search_box)
        self._search_pool = []
        self._search_keys.clear()
        self._search_expand = 0
        self._search_want = False
        if not query:
            self._search_loading = False
            self._notice(self._search_box, i18n.tr("store.search_hint"))
            return
        self._search_loading = True
        self._notice(self._search_box, i18n.tr("store.loading"), loading=True)
        self._start_catalog("search", query, seq)

    def _start_catalog(
        self,
        mode: str,
        query: str,
        seq: int = 0,
        refresh: bool = False,
        expand: int = 0,
    ) -> None:
        worker = _CatalogWorker(mode, query, self._client, seq, refresh, expand)
        worker.done.connect(self._on_catalog)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._workers.append(worker)
        worker.start()

    def _on_catalog(self, mode: str, seq: int, skills: object, error: str) -> None:
        items = [s for s in skills if isinstance(s, StoreSkill)] if skills else []
        if mode == "recommended":
            self._on_recommended(items, error)
            return
        if seq != self._search_seq:
            return  # ответ на устаревший запрос
        self._on_search(items, error)

    def _on_search(self, items: list[StoreSkill], error: str) -> None:
        """Результаты поиска: первая порция или доливка при прокрутке."""
        self._search_loading = False
        empty = error or i18n.tr(
            "store.search_empty", query=self.search_input.text().strip()
        )
        if not self._search_want:
            self._search_pool = items
            self._search_render(empty)
            return
        self._search_want = False
        self._search_pool = items or self._search_pool
        if error and not items:
            self.status.setText(error)
            self._search_expand = REC_STEPS  # не повторяем неудачу
            return
        if self._search_add_page():
            if self.status.text() == i18n.tr("store.search_loading"):
                self.status.clear()
            return
        if self._search_expand < REC_STEPS:
            self._load_more_search()
        else:
            self.status.setText(i18n.tr("store.search_end"))

    def _search_render(self, empty: str = "") -> None:
        """Показывает результаты поиска с начала — первой порцией."""
        self._clear(self._search_box)
        self._search_keys.clear()
        if not self._search_pool:
            self._notice(self._search_box, empty or i18n.tr("store.search_hint"))
            return
        self._search_box.addStretch(1)
        self._search_add_page()

    def _search_add_page(self) -> int:
        """Дописывает порцию ещё не показанных результатов поиска."""
        added = 0
        for skill in self._search_pool:
            if added >= REC_PAGE:
                break
            if skill.key in self._search_keys:
                continue
            self._search_keys.add(skill.key)
            last = max(0, self._search_box.count() - 1)
            self._search_box.insertWidget(last, self._card(skill))
            added += 1
        return added

    def _on_search_scroll(self, value: int) -> None:
        """Прокрутили результаты до конца — доливаем следующую порцию."""
        bar = self._search_area.verticalScrollBar()
        if bar.maximum() and value >= bar.maximum() - 240:
            self._load_more_search()

    def _load_more_search(self) -> None:
        """Конец результатов: дописываем из пула или ищем шире по GitHub."""
        if self.tabs.currentIndex() != self.tab_search or self._search_loading:
            return
        if not self._search_pool:
            return
        if self._search_add_page():
            return
        if self._search_expand >= REC_STEPS:
            self.status.setText(i18n.tr("store.search_end"))
            return
        self._search_expand += 1
        self._search_want = True
        self._search_loading = True
        self.status.setText(i18n.tr("store.search_loading"))
        self._start_catalog(
            "search",
            self.search_input.text().strip(),
            self._search_seq,
            expand=self._search_expand,
        )

    def _on_recommended(self, items: list[StoreSkill], error: str) -> None:
        """Каталог витрины пришёл: первая порция, доливка или тихое обновление."""
        self._rec_loading = False
        if self._rec_reset:
            self._rec_reset = False
            self._rec_pool = items
            self._fill_repo_filter()
            self._rec_render(error or i18n.tr("store.rec_empty"))
            return
        self._rec_pool = items or self._rec_pool
        self._fill_repo_filter()
        self._rec_view = self._rec_filtered()
        if not self._rec_want:
            return  # тихая пересборка: карточки остаются на месте
        self._rec_want = False
        if error and not items:
            self.status.setText(error)
            self._rec_expand = REC_STEPS  # не долбим GitHub одним и тем же
            return
        if self._rec_add_page():
            if self.status.text() == i18n.tr("store.rec_loading"):
                self.status.clear()
            return
        if self._rec_expand < REC_STEPS:
            self._load_recommended(more=True)
        else:
            self.status.setText(i18n.tr("store.rec_end"))

    # ------------------------------------------------------------------ плагины
    def _plugin_card(self, plugin: StorePlugin) -> QFrame:
        """Карточка плагина: имя, версия, состав пакета, «+»."""
        card = QFrame()
        card.setObjectName("StoreCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)

        texts = QVBoxLayout()
        texts.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(plugin.name)
        name.setObjectName("StoreCardTitle")
        top.addWidget(name)
        if plugin.version:
            version = QLabel(i18n.tr("store.version", version=plugin.version))
            version.setObjectName("StoreCardMeta")
            top.addWidget(version)
        source = QLabel(plugin.source)
        source.setObjectName("StoreCardMeta")
        top.addWidget(source)
        top.addStretch(1)
        texts.addLayout(top)

        desc = QLabel(_clip(plugin.description) or i18n.tr("skills.no_desc"))
        desc.setObjectName("StoreCardDesc")
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.PlainText)
        if plugin.description:
            desc.setToolTip(plugin.description)
        texts.addWidget(desc)

        meta = i18n.tr("store.files", n=len(plugin.files))
        if plugin.skills or plugin.commands or plugin.agents:
            parts = i18n.tr(
                "store.plugin_parts",
                skills=plugin.skills,
                commands=plugin.commands,
                agents=plugin.agents,
            )
            meta = f"{parts} · {meta}"
        if plugin.path:
            meta = f"{plugin.path} · {meta}"
        if plugin.updated:
            meta = f"{meta} · {i18n.tr('store.fresh', date=plugin.updated[:10])}"
        files = QLabel(meta)
        files.setObjectName("StoreCardFiles")
        files.setWordWrap(True)
        texts.addWidget(files)
        row.addLayout(texts, 1)

        btn = QPushButton("+")
        btn.setObjectName("StoreInstall")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(34, 34)
        btn.setToolTip(i18n.tr("store.plugin_install", name=plugin.name))
        btn.clicked.connect(lambda _=False, p=plugin: self._install_plugin(p))
        row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        alive = [b for b in self._p_buttons.get(plugin.key, []) if isValid(b)]
        self._p_buttons[plugin.key] = [*alive, btn]
        self._p_cards[plugin.key] = plugin
        self._mark_plugin(plugin.key, is_plugin_installed(plugin, self._directory))
        return card

    def _mark_plugin(self, key: str, done: bool) -> None:
        """Перекрашивает «+»/«✓» на всех карточках одного плагина."""
        alive: list[QPushButton] = []
        for btn in self._p_buttons.get(key, []):
            if not isValid(btn):
                continue
            alive.append(btn)
            btn.setText("✓" if done else "+")
            btn.setEnabled(not done)
            btn.setProperty("installed", "true" if done else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._p_buttons[key] = alive

    def _sync_plugins(self) -> None:
        """Сверяет «+» на витрине с папкой скиллов (после установки/удаления)."""
        for key, plugin in self._p_cards.items():
            self._mark_plugin(key, is_plugin_installed(plugin, self._directory))

    def _plugin_pending(self, key: str, pending: bool) -> None:
        """Кнопка карточки на время скачивания: «…» и заблокирована."""
        alive: list[QPushButton] = []
        for btn in self._p_buttons.get(key, []):
            if not isValid(btn):
                continue
            alive.append(btn)
            btn.setEnabled(not pending)
            if pending:
                btn.setText("…")
        self._p_buttons[key] = alive

    def _plugin_parts(self, plugin: StorePlugin | InstalledPlugin) -> str:
        """Состав пакета одной строкой (без нулевых счётчиков)."""
        return i18n.tr(
            "store.plugin_parts",
            skills=plugin.skills,
            commands=plugin.commands,
            agents=plugin.agents,
        )

    def _origin_text(self, origin: dict[str, str] | None) -> str:
        """Строка «источник: …» для карточки установленного скилла или плагина."""
        if not origin:
            return i18n.tr("store.source_manual")
        if origin.get("local"):
            return i18n.tr("store.local_source", path=origin["local"])
        repo = origin.get("repo") or ""
        path = origin.get("path") or ""
        if not repo:
            return i18n.tr("store.source_manual")
        if path:
            return i18n.tr("store.source_repo_path", repo=repo, path=path)
        return i18n.tr("store.source_repo", repo=repo)

    # ---------------------------------------------------------- витрина плагинов
    def _load_plugin_recommended(self, refresh: bool = False) -> None:
        """Загружает рекомендуемые плагины (при refresh — минуя кэш)."""
        self._plugin_loading = True
        self._clear(self._prec_box)
        self._notice(
            self._prec_box, i18n.tr("store.plugin_loading"), loading=True
        )
        worker = _PluginCatalogWorker(
            "recommended", "", self._client, refresh=refresh
        )
        worker.done.connect(self._on_plugin_catalog)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._workers.append(worker)
        worker.start()

    def _plugin_render(self, empty: str = "") -> None:
        """Показывает витрину плагинов с начала, порциями по REC_PAGE."""
        self._clear(self._prec_box)
        self._plugin_keys.clear()
        if not self._plugin_pool:
            self._notice(self._prec_box, empty or i18n.tr("store.plugin_rec_empty"))
            return
        self._prec_box.addStretch(1)
        self._add_plugin_page(self._prec_box, self._plugin_keys, self._plugin_pool)

    def _add_plugin_page(
        self, layout: QVBoxLayout, keys: set[str], pool: list[StorePlugin]
    ) -> int:
        """Дописывает порцию ещё не показанных карточек плагинов."""
        added = 0
        for plugin in pool:
            if added >= REC_PAGE:
                break
            if plugin.key in keys:
                continue
            keys.add(plugin.key)
            last = max(0, layout.count() - 1)
            layout.insertWidget(last, self._plugin_card(plugin))
            added += 1
        return added

    def _on_plugin_scroll(self, value: int) -> None:
        """Прокрутили витрину плагинов до конца — доливаем следующую порцию."""
        if self.plugin_tabs.currentIndex() != self.plugin_rec_tab:
            return
        bar = self._plugin_rec_area.verticalScrollBar()
        if not bar.maximum() or value < bar.maximum() - 240:
            return
        if not self._add_plugin_page(
            self._prec_box, self._plugin_keys, self._plugin_pool
        ):
            self.status.setText(i18n.tr("store.rec_end"))

    # -------------------------------------------------------------- поиск плагинов
    def _run_plugin_search(self) -> None:
        """Ищет плагины по запросу и показывает результаты."""
        query = self.plugin_search_input.text().strip()
        self.plugin_tabs.setCurrentIndex(self.plugin_search_tab)
        self._plugin_seq += 1
        seq = self._plugin_seq
        self._clear(self._psearch_box)
        self._plugin_search_pool = []
        self._plugin_search_keys.clear()
        if not query:
            self._notice(self._psearch_box, i18n.tr("store.search_hint"))
            return
        self._notice(self._psearch_box, i18n.tr("store.plugin_loading"), loading=True)
        worker = _PluginCatalogWorker("search", query, self._client, seq)
        worker.done.connect(self._on_plugin_catalog)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._workers.append(worker)
        worker.start()

    def _on_plugin_catalog(
        self, mode: str, seq: int, plugins: object, error: str
    ) -> None:
        """Ответ воркера: витрина или результаты поиска плагинов."""
        items = (
            [p for p in plugins if isinstance(p, StorePlugin)] if plugins else []
        )
        if mode == "recommended":
            self._plugin_loading = False
            self._plugin_loaded = True
            self._plugin_pool = items
            self._plugin_render(error or i18n.tr("store.plugin_rec_empty"))
            return
        if seq != self._plugin_seq:
            return  # ответ на устаревший запрос
        self._plugin_search_pool = items
        self._clear(self._psearch_box)
        self._plugin_search_keys.clear()
        empty = error or i18n.tr(
            "store.search_empty", query=self.plugin_search_input.text().strip()
        )
        if not items:
            self._notice(self._psearch_box, empty)
            return
        self._psearch_box.addStretch(1)
        self._add_plugin_page(
            self._psearch_box, self._plugin_search_keys, self._plugin_search_pool
        )

    # ---------------------------------------------------------- установленные
    def _reload_plugin_installed(self) -> None:
        """Перечитывает папку скиллов и обновляет «Установленные» плагины."""
        self._clear(self._pinstalled_box)
        self._p_update_buttons.clear()
        try:
            plugins = installed_plugins(self._directory)
        except OSError as exc:
            self._notice(
                self._pinstalled_box, i18n.tr("store.load_fail", msg=str(exc))
            )
            return
        self._sync_plugins()
        self.plugin_tabs.setTabText(
            self.plugin_installed_tab,
            i18n.tr("store.tab.installed_count", n=len(plugins)),
        )
        if not plugins:
            self._notice(
                self._pinstalled_box, i18n.tr("store.plugin_installed_empty")
            )
            return
        for plugin in plugins:
            self._pinstalled_box.addWidget(self._plugin_installed_card(plugin))
        self._pinstalled_box.addStretch(1)

    def _plugin_installed_card(self, plugin: InstalledPlugin) -> QFrame:
        """Карточка установленного плагина: состав, путь, обновление и удаление."""
        card = QFrame()
        card.setObjectName("StoreCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)

        texts = QVBoxLayout()
        texts.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(plugin.name)
        name.setObjectName("StoreCardTitle")
        top.addWidget(name)
        badge = QLabel(i18n.tr("store.installed"))
        badge.setObjectName("StoreCardBadge")
        top.addWidget(badge)
        if plugin.version:
            version = QLabel(i18n.tr("store.version", version=plugin.version))
            version.setObjectName("StoreCardMeta")
            top.addWidget(version)
        top.addStretch(1)
        texts.addLayout(top)

        desc = QLabel(_clip(plugin.description) or i18n.tr("skills.no_desc"))
        desc.setObjectName("StoreCardDesc")
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.PlainText)
        if plugin.description:
            desc.setToolTip(plugin.description)
        texts.addWidget(desc)

        meta = self._plugin_parts(plugin)
        if plugin.folder != plugin.name:
            meta = f"{plugin.folder} · {meta}"
        parts = QLabel(meta)
        parts.setObjectName("StoreCardFiles")
        parts.setWordWrap(True)
        texts.addWidget(parts)

        path = QLabel(i18n.tr("store.path", path=plugin.path.as_posix()))
        path.setObjectName("StoreCardFiles")
        path.setWordWrap(True)
        path.setTextFormat(Qt.TextFormat.PlainText)
        texts.addWidget(path)

        source_text = self._origin_text(origin_of(self._directory, plugin.folder))
        source = QLabel(source_text)
        source.setObjectName("StoreCardMeta")
        source.setWordWrap(True)
        source.setTextFormat(Qt.TextFormat.PlainText)
        source.setToolTip(source_text)
        texts.addWidget(source)
        row.addLayout(texts, 1)

        check = QLabel("✓")
        check.setObjectName("StoreInstalledMark")
        row.addWidget(check, 0, Qt.AlignmentFlag.AlignTop)

        if plugin.tracked or plugin.local:
            refresh = QPushButton("⟳")
            refresh.setObjectName("StoreUpdate")
            refresh.setCursor(Qt.CursorShape.PointingHandCursor)
            refresh.setFixedSize(34, 34)
            refresh.setToolTip(i18n.tr("store.plugin_update_tip", name=plugin.name))
            refresh.clicked.connect(
                lambda _=False, f=plugin.folder: self._update_plugin(f)
            )
            row.addWidget(refresh, 0, Qt.AlignmentFlag.AlignTop)
            self._p_update_buttons[plugin.folder.lower()] = refresh

        remove = QPushButton("×")
        remove.setObjectName("StoreRemove")
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setFixedSize(34, 34)
        remove.setToolTip(i18n.tr("store.plugin_remove_tip", name=plugin.name))
        remove.clicked.connect(
            lambda _=False, f=plugin.folder, n=plugin.name: self._remove_plugin(f, n)
        )
        row.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)
        return card

    def _install_plugin(self, plugin: StorePlugin) -> None:
        """Ставит плагин целиком в фоне."""
        if (
            self._plugin_install_worker is not None
            and self._plugin_install_worker.isRunning()
        ):
            return
        self._plugin_pending(plugin.key, True)
        self.status.setText(i18n.tr("store.plugin_installing", name=plugin.name))
        worker = _PluginInstallWorker(plugin, self._directory, self._client)
        worker.done.connect(self._on_plugin_installed)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._plugin_install_worker = worker
        self._workers.append(worker)
        worker.start()

    def _pick_skill_zip(self) -> None:
        """Выбор архива с одним скиллом."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            i18n.tr("store.skill_local_zip_title"),
            "",
            i18n.tr("store.skill_local_filter"),
        )
        if path:
            self._install_local_skill(path)

    def _pick_skill_folder(self) -> None:
        """Выбор папки скилла (с SKILL.md)."""
        path = QFileDialog.getExistingDirectory(
            self, i18n.tr("store.skill_local_folder_title")
        )
        if path:
            self._install_local_skill(path)

    def _install_local_skill(self, source: str) -> None:
        """Ставит скилл из файла или папки в фоне."""
        if self._install_worker is not None and self._install_worker.isRunning():
            return
        if (
            self._skill_local_worker is not None
            and self._skill_local_worker.isRunning()
        ):
            return
        self.status.setText(i18n.tr("store.installing", name=Path(source).name))
        worker = _SkillLocalWorker(source, self._directory)
        worker.done.connect(self._on_skill_local_installed)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._skill_local_worker = worker
        self._workers.append(worker)
        worker.start()

    def _on_skill_local_installed(self, skill: object, error: str) -> None:
        if error:
            self.status.setText(i18n.tr("store.skill_local_fail", msg=error))
            return
        name = getattr(skill, "name", "")
        self.status.setText(i18n.tr("store.installed_status", name=name))
        self._reload_installed()
        self.tabs.setCurrentIndex(self.tab_installed)
        self.installed.emit(name)

    def _pick_plugin_zip(self) -> None:
        """Выбор архива с плагином: установка целиком, как из магазина."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            i18n.tr("store.plugin_local_zip_title"),
            "",
            i18n.tr("store.plugin_local_filter"),
        )
        if path:
            self._install_local_plugin(path)

    def _pick_plugin_folder(self) -> None:
        """Выбор папки плагина (с .claude-plugin/plugin.json)."""
        path = QFileDialog.getExistingDirectory(
            self, i18n.tr("store.plugin_local_folder_title")
        )
        if path:
            self._install_local_plugin(path)

    def _install_local_plugin(self, source: str) -> None:
        """Ставит плагин из файла или папки в фоне."""
        if (
            self._plugin_install_worker is not None
            and self._plugin_install_worker.isRunning()
        ):
            return
        self.status.setText(
            i18n.tr("store.plugin_installing", name=Path(source).name)
        )
        worker = _PluginLocalWorker(source, self._directory)
        worker.done.connect(self._on_plugin_local_installed)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._plugin_install_worker = worker
        self._workers.append(worker)
        worker.start()

    def _on_plugin_local_installed(self, folder: str, error: str) -> None:
        if error:
            self.status.setText(i18n.tr("store.plugin_local_fail", msg=error))
            return
        self.status.setText(i18n.tr("store.plugin_installed_status", name=folder))
        self._reload_plugin_installed()
        self._reload_installed()
        self.plugin_tabs.setCurrentIndex(self.plugin_installed_tab)
        self.installed.emit(folder)

    def _on_plugin_installed(
        self, plugin: object, folder: str, error: str
    ) -> None:
        if isinstance(plugin, StorePlugin):
            self._plugin_pending(plugin.key, False)
            self._mark_plugin(plugin.key, not error)
        name = getattr(plugin, "name", "")
        if error:
            self.status.setText(
                i18n.tr("store.plugin_install_fail", name=name, msg=error)
            )
            return
        self.status.setText(i18n.tr("store.plugin_installed_status", name=name))
        self._reload_plugin_installed()
        self._reload_installed()
        self.plugin_tabs.setCurrentIndex(self.plugin_installed_tab)
        self.installed.emit(folder or name)

    def _update_plugin(self, folder: str) -> None:
        """Перекачивает плагин с GitHub и заменяет папку, если версия изменилась."""
        btn = self._p_update_buttons.get(folder.lower())
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("…")
        self.status.setText(i18n.tr("store.updating", name=folder))
        worker = _PluginUpdateWorker(folder, self._directory, self._client)
        worker.done.connect(self._on_plugin_updated)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._workers.append(worker)
        worker.start()

    def _on_plugin_updated(self, folder: str, changed: bool, error: str) -> None:
        if error:
            self.status.setText(i18n.tr("store.update_fail", name=folder, msg=error))
        elif changed:
            self.status.setText(i18n.tr("store.update_done", name=folder))
        else:
            self.status.setText(i18n.tr("store.update_fresh", name=folder))
        self._reload_plugin_installed()
        self._reload_installed()
        if changed and not error:
            self.updated.emit(folder)

    def _remove_plugin(self, folder: str, name: str) -> None:
        """Удаляет папку плагина целиком (с подтверждением)."""
        answer = QMessageBox.question(
            self,
            i18n.tr("store.remove_title"),
            i18n.tr("store.remove_ask", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not remove_plugin(folder, self._directory):
            self.status.setText(i18n.tr("store.remove_fail", name=name))
            return
        self.status.setText(i18n.tr("store.remove_done", name=name))
        self._reload_plugin_installed()
        self._reload_installed()
        self.removed.emit(folder)

    # ---------------------------------------------------------------- установка
    def _install(self, skill: StoreSkill) -> None:
        """Запускает скачивание скилла в фоне."""
        if self._install_worker is not None and self._install_worker.isRunning():
            return
        self._buttons_pending(skill.key, True)
        self.status.setText(i18n.tr("store.installing", name=skill.name))
        worker = _InstallWorker(skill, self._directory, self._client)
        worker.done.connect(self._on_installed)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._install_worker = worker
        self._workers.append(worker)
        worker.start()

    def _buttons_pending(self, key: str, pending: bool) -> None:
        alive: list[QPushButton] = []
        for btn in self._buttons.get(key, []):
            if not isValid(btn):
                continue
            alive.append(btn)
            btn.setEnabled(not pending)
            if pending:
                btn.setText("…")
        self._buttons[key] = alive

    def _on_installed(self, skill: object, installed: object, error: str) -> None:
        if isinstance(skill, StoreSkill):
            self._buttons_pending(skill.key, False)
            self._mark_installed(skill.key, not error)
        if error:
            name = getattr(skill, "name", "")
            self.status.setText(i18n.tr("store.install_fail", name=name, msg=error))
            return

        name = getattr(installed, "name", "") or getattr(skill, "name", "")
        self.status.setText(i18n.tr("store.installed_status", name=name))
        self._reload_installed()
        self.tabs.setCurrentIndex(self.tab_installed)
        self.installed.emit(name)

    # ---------------------------------------------------------------- скачанные
    def _reload_installed(self) -> None:
        """Перечитывает папку скиллов и обновляет раздел «Скачанные»."""
        self._clear(self._installed_box)
        self._update_buttons.clear()
        try:
            # команды плагинов — не отдельные скиллы: они живут в части «Плагины»
            skills = [
                skill
                for skill in load_skills(self._directory, builtin=False)
                if skill.kind != "command"
            ]
        except OSError as exc:
            self._notice(self._installed_box, i18n.tr("store.load_fail", msg=str(exc)))
            return
        self._sync_buttons()  # удалённый скилл снова получает «+» в витрине
        self.tabs.setTabText(
            self.tab_installed,
            i18n.tr("store.tab.installed_count", n=len(skills)),
        )
        if not skills:
            self._notice(self._installed_box, i18n.tr("store.installed_empty"))
            return
        # самостоятельные скиллы и скиллы из плагинов — двумя группами:
        # у вторых своих «⟳»/«×» нет, ими управляет плагин целиком
        standalone: list[Skill] = []
        members: list[Skill] = []
        for skill in skills:
            if skill_plugin(getattr(skill, "path", ""), self._directory):
                members.append(skill)
            else:
                standalone.append(skill)
        for title, group in (
            ("store.group.standalone", standalone),
            ("store.group.plugin_members", members),
        ):
            if not group:
                continue
            self._installed_box.addWidget(self._group_header(i18n.tr(title, n=len(group))))
            for skill in group:
                self._installed_box.addWidget(self._installed_card(skill))
        self._installed_box.addStretch(1)

    def _group_header(self, text: str) -> QLabel:
        """Заголовок группы карточек: «Самостоятельные скиллы (3)»."""
        label = QLabel(text)
        label.setObjectName("StoreGroup")
        return label

    def _installed_card(self, skill: Skill) -> QFrame:
        """Карточка уже установленного скилла: имя, описание, путь."""
        card = QFrame()
        card.setObjectName("StoreCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)

        texts = QVBoxLayout()
        texts.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(str(getattr(skill, "name", "")))
        name.setObjectName("StoreCardTitle")
        top.addWidget(name)
        badge = QLabel(i18n.tr("store.installed"))
        badge.setObjectName("StoreCardBadge")
        top.addWidget(badge)
        top.addStretch(1)
        texts.addLayout(top)

        description = str(getattr(skill, "description", "") or "")
        desc = QLabel(_clip(description) or i18n.tr("skills.no_desc"))
        desc.setObjectName("StoreCardDesc")
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.PlainText)
        if description:
            desc.setToolTip(description)
        texts.addWidget(desc)

        # скилл из установленного плагина — часть пакета: отдельно не трогаем
        path = getattr(skill, "path", None)
        member = skill_plugin(getattr(skill, "path", ""), self._directory)
        if member:
            badge_text = i18n.tr("store.plugin_member", name=member)
            badge = QLabel(badge_text)
            badge.setObjectName("StoreCardMeta")
            texts.addWidget(badge)
        else:
            folder = Path(path).name if path else ""
            source_text = self._origin_text(origin_of(self._directory, folder))
            source = QLabel(source_text)
            source.setObjectName("StoreCardMeta")
            source.setWordWrap(True)
            source.setTextFormat(Qt.TextFormat.PlainText)
            source.setToolTip(source_text)
            texts.addWidget(source)
        files = QLabel(i18n.tr("store.path", path=Path(path).as_posix() if path else ""))
        files.setObjectName("StoreCardFiles")
        files.setWordWrap(True)
        files.setTextFormat(Qt.TextFormat.PlainText)
        texts.addWidget(files)
        row.addLayout(texts, 1)

        check = QLabel("✓")
        check.setObjectName("StoreInstalledMark")
        row.addWidget(check, 0, Qt.AlignmentFlag.AlignTop)

        if member:
            return card  # обновление и удаление — у плагина целиком

        name = str(getattr(skill, "name", ""))
        refresh = QPushButton("⟳")
        refresh.setObjectName("StoreUpdate")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setFixedSize(34, 34)
        refresh.setToolTip(i18n.tr("store.update_tip", name=name))
        refresh.clicked.connect(lambda _=False, n=name: self._update(n))
        row.addWidget(refresh, 0, Qt.AlignmentFlag.AlignTop)
        self._update_buttons[name.lower()] = refresh

        remove = QPushButton("×")
        remove.setObjectName("StoreRemove")
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setFixedSize(34, 34)
        remove.setToolTip(i18n.tr("store.remove_tip", name=name))
        remove.clicked.connect(lambda _=False, n=name: self._remove(n))
        row.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)
        return card

    def _update(self, name: str) -> None:
        """Перекачивает скилл с GitHub и заменяет папку, если версия изменилась."""
        btn = self._update_buttons.get(name.lower())
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("…")
        self.status.setText(i18n.tr("store.updating", name=name))
        worker = _UpdateWorker(name, self._directory, self._client)
        worker.done.connect(self._on_updated)
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._workers.append(worker)
        worker.start()

    def _on_updated(self, name: str, changed: bool, error: str) -> None:
        if error:
            self.status.setText(i18n.tr("store.update_fail", name=name, msg=error))
        elif changed:
            self.status.setText(i18n.tr("store.update_done", name=name))
        else:
            self.status.setText(i18n.tr("store.update_fresh", name=name))
        self._reload_installed()
        if changed and not error:
            self.updated.emit(name)

    def _remove(self, name: str) -> None:
        """Удаляет папку скачанного скилла (с подтверждением)."""
        answer = QMessageBox.question(
            self,
            i18n.tr("store.remove_title"),
            i18n.tr("store.remove_ask", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = get_skill(name, self._directory)
        if not remove_skill(name, self._directory):
            self.status.setText(i18n.tr("store.remove_fail", name=name))
            return
        if removed is not None:
            forget_origin(self._directory, removed.path.name)
        self.status.setText(i18n.tr("store.remove_done", name=name))
        self._reload_installed()
        self.removed.emit(name)

    # ------------------------------------------------------------------ служебное
    def _forget(self, worker: QThread) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Не даём Qt уничтожить незавершённые воркеры."""
        self._auto.stop()
        for worker in list(self._workers):
            if not worker.isRunning():
                continue
            worker.finished.connect(lambda w=worker: _DETACHED.discard(w))
            _DETACHED.add(worker)
        super().closeEvent(event)


__all__ = ["SkillStoreDialog"]
