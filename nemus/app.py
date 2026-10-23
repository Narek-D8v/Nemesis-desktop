"""Сборка и запуск приложения Немуся."""

from __future__ import annotations

import gc
import sys
import threading
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from nemus import __version__
from nemus.config import get_config
from nemus.core.agent import build_system_prompt
from nemus.core.factory import Core, build_core
from nemus.core.state import AssistState
from nemus.core.tools.base import Tool
from nemus.gui import i18n
from nemus.gui.dialogs import ConfirmDialog
from nemus.gui.main_window import MainWindow
from nemus.gui.tray import Tray
from nemus.utils.logger import logger, setup_logging
from nemus.voice.realtime import RealtimeCallbacks, RealtimeState
from nemus.voice.wake_skills import WakeSkill, build_skills, match_skill


def _install_excepthook() -> None:
    """Логирует необработанные исключения вместо тихого падения приложения.

    Без этого любая ошибка в слоте Qt или в фоновом потоке либо уносит
    процесс (PyQt), либо оставляет приложение в сломанном состоянии с
    трейсбеком в stderr, который пользователь никогда не видит.
    """
    previous = sys.excepthook

    def _hook(exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            logger.opt(exception=(exc_type, exc, tb)).error(
                "Необработанное исключение в главном потоке"
            )
        except Exception:  # noqa: BLE001
            pass
        previous(exc_type, exc, tb)

    def _thread_hook(args: Any) -> None:
        try:
            name = getattr(getattr(args, "thread", None), "name", "?")
            logger.opt(
                exception=(args.exc_type, args.exc_value, args.traceback)
            ).error(f"Исключение в фоновом потоке {name}")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


class VoiceBridge(QObject):
    """Мост потокобезопасных событий голоса в главный поток Qt."""

    text_ready = Signal(str)
    state_changed = Signal(object)
    amplitude_changed = Signal(float)
    wake_detected = Signal()


_STATE_MAP = {
    RealtimeState.CONNECTING: AssistState.THINKING,
    RealtimeState.READY: AssistState.LISTENING,
    RealtimeState.LISTENING: AssistState.LISTENING,
    RealtimeState.THINKING: AssistState.THINKING,
    RealtimeState.SPEAKING: AssistState.SPEAKING,
    RealtimeState.ERROR: AssistState.ERROR,
    RealtimeState.CLOSED: AssistState.IDLE,
}


class RealtimeBridge(QObject):
    """Мост событий realtime-сессии в GUI + потокобезопасное подтверждение."""

    user_text = Signal(str)
    assistant_text = Signal(str)
    state_changed = Signal(object)
    error = Signal(str)
    confirm_requested = Signal(object)

    def __init__(self, window: MainWindow, parent: Any = None) -> None:
        super().__init__(parent)
        self.window = window
        self._ready = threading.Event()
        self._value = False
        self.confirm_requested.connect(self._on_confirm_requested)

    def confirm(self, tool: Tool, args: dict[str, Any]) -> bool:
        """Вызывается из потока realtime-сессии (блокирует его до ответа GUI)."""
        self._ready.clear()
        self._value = False
        self.confirm_requested.emit((tool, dict(args)))
        self._ready.wait()
        return self._value

    def _on_confirm_requested(self, payload: Any) -> None:
        tool, args = payload
        self._value = ConfirmDialog.ask(tool, args, self.window)
        self._ready.set()



class NemusApp:
    """Оркестратор жизненного цикла приложения."""

    def __init__(self, argv: list[str] | None = None) -> None:
        # cyclic GC разбирает Qt-виджеты и нативные ресурсы (PyAV/RNNoise) в
        # неподходящий момент и роняет процесс — отключаем его на всё время
        # жизни приложения. Объекты освобождаются обычным счётчиком ссылок.
        gc.disable()
        self.config = get_config()
        i18n.set_language(self.config.gui.language)
        self._ui_language = i18n.current_language()
        self.qapp = QApplication(argv if argv is not None else sys.argv)
        self.qapp.setApplicationName(self.config.assistant.name)
        self.qapp.setApplicationVersion(__version__)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.aboutToQuit.connect(self._shutdown)

        self.core: Core = build_core(self.config)
        self.window = MainWindow(self.core, self.config.assistant.name)
        self._apply_window_flags()
        self.window.listen_toggled.connect(self._handle_listen_toggle)
        self.window.settings_saved.connect(self._on_settings_saved)
        self.window.speak_request.connect(self._handle_speak_request)
        self.window.stop_speech.connect(self._handle_stop_speech)

        # --- голос ---
        self.voice = None
        self._session_from_wake = False
        self._wake_skills = build_skills(self.config.voice.wake_word)
        self.bridge = VoiceBridge(self.window)
        self.bridge.text_ready.connect(self._on_voice_text)
        self.bridge.state_changed.connect(self.window.set_state)
        self.bridge.wake_detected.connect(self._on_wake_detected)
        self.bridge.amplitude_changed.connect(
            lambda a: self.window.orb.set_amplitude(a)
        )
        self._setup_tts()

        # --- режим реального времени ---
        self.realtime = None
        self.rt_bridge = RealtimeBridge(self.window, self.window)
        self.rt_bridge.user_text.connect(self.window.show_realtime_user)
        self.rt_bridge.assistant_text.connect(self.window.show_realtime_assistant)
        self.rt_bridge.state_changed.connect(self._on_realtime_state)
        self.rt_bridge.error.connect(self._on_realtime_error)
        self.window.realtime_toggled.connect(self._handle_realtime_toggle)

        self.tray: Tray | None = None
        self._setup_tray()

    def _apply_window_flags(self) -> None:
        """Применяет оконные флаги из конфига (окно поверх других)."""
        self.window.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint, self.config.gui.always_on_top
        )
        if self.window.isVisible():
            self.window.show()

    def _setup_tts(self) -> None:
        from nemus.voice.tts import TTSEngine, resolve_openai_key

        vc = self.config.voice.tts
        self.tts = TTSEngine(
            voice=vc.voice,
            rate=vc.rate,
            volume=vc.volume,
            engine=vc.engine,
            model=vc.openai_model,
            api_key=resolve_openai_key(self.config.llm.providers),
        )

    def _handle_speak_request(self, text: str) -> None:
        if self.tts is not None:
            self.tts.speak(text, block=False)

    # --------------------------------------------------------- wake skills
    def _on_voice_text(self, text: str) -> None:
        """Текст локального STT: сначала навык wake word, иначе — агент."""
        if not text.strip():
            return
        skill = match_skill(text, self._wake_skills)
        if skill is not None and self._run_wake_skill(skill, text):
            return
        self.window.submit_text(text)

    def _run_wake_skill(self, skill: WakeSkill, text: str) -> bool:
        """Выполняет навык напрямую, если инструмент разрешён политикой."""
        from nemus.core.permissions import Decision

        tool = self.core.registry.get(skill.tool)
        if tool is None:
            logger.warning(
                f"Навык «{skill.name}»: инструмент '{skill.tool}' не найден."
            )
            return False
        if self.core.permissions.check(tool) != Decision.ALLOW:
            # Опасное действие — пусть агент покажет подтверждение.
            logger.info(f"Навык «{skill.name}» требует подтверждения — отдаю агенту.")
            return False
        logger.info(f"Навык wake word «{skill.name}» → {skill.tool}")
        self.window.chat.add_user(text)
        self.window.set_state(AssistState.THINKING)
        result = self.core.registry.execute(skill.tool, skill.arguments)
        reply = result.content if result.ok else f"Не получилось: {result.content}"
        self.window.chat.add_assistant(reply, self.config.assistant.name)
        self.window.set_state(AssistState.IDLE)
        self._handle_speak_request(reply)
        return True

    def _handle_stop_speech(self) -> None:
        if self.tts is not None:
            self.tts.stop()

    # ------------------------------------------------------------------ tray
    def _setup_tray(self) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("Системный трей недоступен.")
            self.window._tray_available = False
            return

        self.window._tray_available = True
        self.tray = Tray(self.window)
        self.tray.show_window.connect(self._show_window)
        self.tray.quit_requested.connect(self._quit)
        self.tray.show()

    def _show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _quit(self) -> None:
        self._shutdown()
        if self.tray:
            self.tray.hide()
        self.qapp.quit()

    def _shutdown(self) -> None:
        """Гасит фоновые аудио-потоки при выходе (иначе падение при завершении)."""
        if self.realtime is not None:
            try:
                self.realtime.stop()
            except Exception:  # noqa: BLE001
                pass
            self.realtime = None
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception:  # noqa: BLE001
                pass
            self.voice = None

    # ------------------------------------------------------------- handlers
    def _handle_listen_toggle(self, active: bool) -> None:
        if active:
            self._stop_realtime(rearm=False)
            self._start_voice()
        else:
            self._stop_voice()
            self._start_wake_only()

    def _start_wake_only(self) -> None:
        """Держит микрофон в режиме слежения только за wake-word'ом."""
        if self.realtime is not None:
            return
        if not self._should_run_wake_only():
            return
        if self.voice is not None:
            return
        try:
            from nemus.voice.manager import VoiceManager
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Голос недоступен: {exc}")
            return

        on_wake = self.bridge.wake_detected.emit
        self.voice = VoiceManager(
            config=self.config,
            on_text=self.bridge.text_ready.emit,
            on_state=self.bridge.state_changed.emit,
            on_amplitude=self.bridge.amplitude_changed.emit,
            on_wake=on_wake,
            wake_only=True,
        )
        if not self.voice.start():
            self.voice = None
            return

    def _should_run_wake_only(self) -> bool:
        """Нужен ли фоновый wake-only слушатель (микрофон слушает только wake word)."""
        ww = self.config.voice.wake_word
        return bool(ww.enabled and ww.engine not in ("off", ""))

    def _stop_wake_only(self) -> None:
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Остановка wake-слушателя: {exc}")
            self.voice = None

    def _start_voice(self) -> bool:
        if self.voice is not None:
            self._stop_wake_only()
        try:
            from nemus.voice.manager import VoiceManager
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Голос недоступен: {exc}")
            self.window.chat.add_error(i18n.tr("app.voice_error", exc=exc))
            return False

        on_wake = (
            self.bridge.wake_detected.emit
            if self.config.voice.realtime.wake_activates
            else None
        )
        self.voice = VoiceManager(
            config=self.config,
            on_text=self.bridge.text_ready.emit,
            on_state=self.bridge.state_changed.emit,
            on_amplitude=self.bridge.amplitude_changed.emit,
            on_wake=on_wake,
            wake_only=False,
        )
        if not self.voice.start():
            self.voice = None
            self.window.chat.add_error(i18n.tr("app.listen_failed"))
            self.window.set_state(AssistState.IDLE)
            self.window.listen_btn.setChecked(False)
            self.window.listen_btn.setText(i18n.tr("btn.listen"))
            self._start_wake_only()
            return False
        return True

    def _stop_voice(self) -> None:
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Остановка голосового ввода: {exc}")
            self.voice = None
        if self.tts is not None:
            self.tts.stop()
        self.window.set_state(AssistState.IDLE)
        self.window.listen_btn.setChecked(False)
        self.window.listen_btn.setText(i18n.tr("btn.listen"))

    # -------------------------------------------------------------- realtime
    def _handle_realtime_toggle(self, active: bool) -> None:
        if active:
            self._session_from_wake = False
            self._start_realtime()
        else:
            self._stop_realtime(rearm=False)
            self._start_wake_only()

    def _realtime_callbacks(self) -> RealtimeCallbacks:
        return RealtimeCallbacks(
            on_state=self.rt_bridge.state_changed.emit,
            on_user_text=self.rt_bridge.user_text.emit,
            on_assistant_text=self.rt_bridge.assistant_text.emit,
            on_error=self.rt_bridge.error.emit,
        )

    def _start_realtime(self) -> None:
        """Кнопка «Realtime»: облачная сессия или локальный аналог по конфигу."""
        if self.realtime is not None:
            return
        self._stop_voice()
        if self.config.voice.realtime.provider == "local":
            self._start_local_live()
            return
        from nemus.voice.realtime import RealtimeManager

        try:
            self.realtime = RealtimeManager(
                config=self.config,
                registry=self.core.registry,
                permissions=self.core.permissions,
                callbacks=self._realtime_callbacks(),
                confirm=self.rt_bridge.confirm,
                system_prompt=build_system_prompt(self.config, self.core.registry),
            )
            self.window.set_realtime(True)
            started = self.realtime.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось запустить realtime-сессию")
            self.realtime = None
            self.window.set_realtime(False)
            self.window.set_state(AssistState.IDLE)
            self.window.chat.add_error(
                i18n.tr("app.realtime_start_failed", msg=str(exc))
            )
            self._maybe_rearm_after_fail()
            return
        if not started:
            self.realtime = None
            self.window.set_realtime(False)
            self.window.set_state(AssistState.IDLE)
            self._maybe_rearm_after_fail()

    def _start_local_live(self) -> None:
        """Запускает локальный аналог Realtime (офлайн-диалог)."""
        if self.realtime is not None:
            return
        from nemus.voice.local_live import LocalLiveSession

        try:
            self.realtime = LocalLiveSession(
                config=self.config,
                callbacks=self._realtime_callbacks(),
                registry=self.core.registry,
                permissions=self.core.permissions,
                confirm=self.rt_bridge.confirm,
                system_prompt=build_system_prompt(self.config, self.core.registry),
                barge_in=self.config.voice.barge_in,
                llm=self.core.llm,
                memory=self.core.memory,
            )
            self.window.set_realtime(True)
            self.window.chat.add_system(i18n.tr("app.local_mode"))
            started = self.realtime.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось запустить локальный режим диалога")
            self.realtime = None
            self.window.set_realtime(False)
            self.window.set_state(AssistState.IDLE)
            self.window.chat.add_error(
                i18n.tr("app.realtime_start_failed", msg=str(exc))
            )
            self._maybe_rearm_after_fail()
            return
        if not started:
            self.realtime = None
            self.window.set_realtime(False)
            self.window.set_state(AssistState.IDLE)
            self.window.chat.add_error(i18n.tr("app.local_failed"))
            self._maybe_rearm_after_fail()

    def _maybe_rearm_after_fail(self) -> None:
        """Если переключение по слову активации сорвалось — вернуть прослушивание."""
        if self._session_from_wake:
            self._session_from_wake = False
            self._rearm_voice()

    def _stop_realtime(self, rearm: bool = True) -> None:
        from_wake = self._session_from_wake
        self._session_from_wake = False  # событие CLOSED не должно rearm-ить повторно
        if self.realtime is not None:
            self.realtime.stop()
            self.realtime = None
        self.window.set_realtime(False)
        self.window.set_state(AssistState.IDLE)
        if rearm and from_wake:
            self._rearm_voice()

    # ------------------------------------------------------------- wake word
    def _on_wake_detected(self) -> None:
        """Слово активации: облачный Realtime, иначе локальный аналог."""
        if self.realtime is not None:
            return
        wake_only = self.voice is not None and self.voice.wake_only
        if self.voice is not None and not wake_only:
            # Уже идёт обычное прослушивание команд — не переключаем режим.
            return
        rc = self.config.voice.realtime
        if not rc.wake_activates:
            # wake word не запускает диалог — просто переходим в обычное
            # прослушивание команд (кнопка «Слушать»)
            self._stop_wake_only()
            if self._start_voice():
                self.window.listen_btn.setChecked(True)
                self.window.listen_btn.setText(i18n.tr("btn.listen_stop"))
            return
        logger.info("Слово активации: запускаю режим диалога.")
        self._stop_wake_only()
        self._session_from_wake = True
        if rc.provider == "local":
            self._start_local_live()
            return
        if self._realtime_available():
            self._start_realtime()
        elif rc.offline_fallback:
            self.window.chat.add_system(i18n.tr("app.local_no_net"))
            self._start_local_live()
        else:
            self.window.chat.add_error(i18n.tr("app.realtime_off_local"))
            self._session_from_wake = False
            self._start_wake_only()

    def _realtime_available(self) -> bool:
        rc = self.config.voice.realtime
        if not rc.enabled:
            return False
        if not self._provider_key(rc.provider):
            logger.info("Realtime: нет API-ключа — локальный режим.")
            return False
        if not self._has_internet():
            logger.info("Realtime: нет интернета — локальный режим.")
            return False
        return True

    def _provider_key(self, provider: str) -> str:
        from nemus.core.providers.registry import PROVIDER_SPECS

        pc = self.config.llm.providers.get(provider)
        if pc and pc.api_key:
            return pc.api_key
        spec = PROVIDER_SPECS.get(provider)
        if spec and spec.env_key:
            from os import environ

            return environ.get(spec.env_key, "")
        return ""

    @staticmethod
    def _has_internet(timeout: float = 1.5) -> bool:
        import socket

        result = {"ok": False}

        def probe() -> None:
            for host, port in (
                ("generativelanguage.googleapis.com", 443),
                ("api.openai.com", 443),
                ("8.8.8.8", 53),
            ):
                try:
                    socket.create_connection((host, port), timeout=timeout).close()
                    result["ok"] = True
                    return
                except Exception:  # noqa: BLE001
                    continue

        worker = threading.Thread(target=probe, daemon=True, name="net-probe")
        worker.start()
        worker.join(timeout=timeout + 0.5)
        return result["ok"]

    def _rearm_voice(self) -> None:
        """Возвращает микрофон к фоновому прослушиванию слова активации."""
        if self.realtime is not None:
            return
        self._start_wake_only()


    def _on_realtime_state(self, state: Any) -> None:
        if state == RealtimeState.CLOSED:
            from_wake = self._session_from_wake
            self.realtime = None
            self._session_from_wake = False
            self.window.set_realtime(False)
            self.window.set_state(AssistState.IDLE)
            if from_wake:
                self._rearm_voice()
            else:
                self._start_wake_only()
            return
        mapped = _STATE_MAP.get(state)
        if mapped is not None:
            self.window.set_state(mapped)

    def _on_realtime_error(self, message: str) -> None:
        self.window.chat.add_error(i18n.tr("app.realtime_state", msg=message))

    def _on_settings_saved(self) -> None:
        """Перезагружает конфиг и ядро после сохранения настроек."""
        try:
            self._apply_settings()
        except Exception as exc:  # noqa: BLE001
            # Слот Qt: необработанный exception здесь убивал бы приложение.
            logger.exception("Не удалось применить настройки")
            self.window.chat.add_error(
                i18n.tr("app.settings_failed", msg=str(exc))
            )

    def _apply_settings(self) -> None:
        self.config = get_config(reload=True)
        self.core.reload(self.config)
        self._wake_skills = build_skills(self.config.voice.wake_word)
        self.window.core = self.core
        self.window.model_label.setText(self.window._model_text())
        self._apply_window_flags()
        if self.realtime is not None:
            self._stop_realtime(rearm=False)
            self._start_realtime()
        elif self.voice is not None and self.voice.wake_only:
            # настройки wake word/навыков применяются сразу
            self._stop_voice()
            self._start_wake_only()
        new_language = i18n.normalize_language(self.config.gui.language)
        if new_language != self._ui_language:
            self._ui_language = new_language
            self.window.chat.add_system(i18n.tr("app.lang_restart"))
        logger.info("Настройки обновлены; ядро пересобрано.")

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if not self.config.gui.start_minimized:
            self.window.show()
        # запускаем wake-слушателя уже в event loop, чтобы не блокировать старт
        QTimer.singleShot(0, self._start_wake_only)
        logger.info(f"Немуся v{__version__} запущена.")
        return self.qapp.exec()


def main() -> int:
    config = get_config()
    setup_logging(config.logging.level, config.logging.file)
    _install_excepthook()
    gc.disable()
    try:
        app = NemusApp()
    except Exception as exc:  # pragma: no cover
        logger.exception("Не удалось запустить приложение")
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
