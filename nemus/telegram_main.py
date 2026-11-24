"""Headless-режим: только Telegram-бот, без окна и без PySide6.

Нужен, чтобы Немуся жила не на ноутбуке, а на постоянном хосте (VPS,
Raspberry Pi, старый мини-ПК, Termux). Запуск::

    python -m nemus --telegram [--config путь/к/config.yaml]

Бот поднимается long polling'ом, работает с тем же ядром инструментов, что и
GUI, и остаётся на связи, пока живёт процесс. В отличие от GUI-версии здесь
нет голосового ввода/экрана — только текстовые команды, файлы и то, что
умеют инструменты на этой машине.

Проверка перед запуском: ``telegram.enabled``, токен и хотя бы один ID в
``telegram.allowed_users``. Иначе бот не стартует — это защита от того,
чтобы команды на сервере принимал любой желающий.
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from pathlib import Path

from nemus.config import Config, get_config, load_config
from nemus.core.factory import Core, build_core
from nemus.core.llm import warm_up
from nemus.core.telegram_bot import TelegramBot
from nemus.utils.logger import logger, setup_logging

#: heartbeat для Docker healthcheck: обновляется, пока жив polling
HEALTH_FILE = "telegram.health"
HEALTH_MAX_AGE = 90.0

#: паузы между попытками запуска: 10, 20, 30, 60, 120, дальше каждые 300 с
RETRY_DELAYS = (10.0, 20.0, 30.0, 60.0, 120.0, 300.0)
#: сколько секунд Telegram может быть недоступен до полного перезапуска бота
RESTART_AFTER = 180.0
#: как часто пробовать поднять polling на месте, пока идёт простой
RECOVER_EVERY = 15.0
#: как часто проверять, отвечает ли Bot API (не только жив ли поток)
PING_EVERY = 60.0
#: как часто напоминать в логе о продолжающемся простое
NOTICE_EVERY = 30.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m nemus --telegram",
        description="Запуск Telegram-бота Немуси без графического интерфейса.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="путь к config.yaml (по умолчанию config/config.yaml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить конфигурацию и выйти",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="проверить heartbeat работающего бота (для Docker healthcheck)",
    )
    return parser.parse_args(argv)


def _health_path(config: Config) -> Path:
    """Файл рядом с базой: один том — один heartbeat."""
    return Path(config.memory.database).parent / HEALTH_FILE


def _write_health(path: Path) -> None:
    """Пишет метку времени атомарно, чтобы healthcheck не прочитал половину."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(time.time()), encoding="utf-8")
    os.replace(tmp, path)


def _health_ok(path: Path, max_age: float = HEALTH_MAX_AGE) -> bool:
    try:
        stamp = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return 0 <= time.time() - stamp <= max_age


def _load(argv: list[str] | None) -> tuple[Config, argparse.Namespace]:
    args = _parse_args(argv)
    config = load_config(args.config) if args.config else get_config()
    setup_logging(config.logging.level, config.logging.file)
    return config, args


def _validate(config: Config) -> str:
    """Возвращает текст ошибки конфигурации или пустую строку."""
    tg = config.telegram
    if not tg.enabled:
        return (
            "Telegram-бот выключен: включи telegram.enabled (в config.yaml "
            "или NEMUS_TELEGRAM__ENABLED=true)."
        )
    if not (tg.token or "").strip():
        return (
            "Не задан токен бота: telegram.token или "
            "NEMUS_TELEGRAM__TOKEN=... (токен от @BotFather)."
        )
    if not tg.allowed_users:
        return (
            "Не задан ни один ID пользователя: telegram.allowed_users или "
            "NEMUS_TELEGRAM__ALLOWED_USERS=123456789 (иначе бот не запустится)."
        )
    return ""


def _install_signals(stop: threading.Event) -> None:
    """Останавливает бота по Ctrl+C / SIGTERM (Docker stop, systemd)."""

    def _handler(signum: int, _frame: object) -> None:
        logger.info(f"Получен сигнал {signum} — останавливаю Telegram-бота.")
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # не главный поток или платформа без сигнала — не критично
            pass


def _retry_delay(attempt: int) -> float:
    """Пауза перед следующей попыткой: растёт до 5 минут и не бесконечна."""
    index = min(max(attempt, 1), len(RETRY_DELAYS)) - 1
    return RETRY_DELAYS[index]


def _start_bot_with_retry(bot: TelegramBot, stop: threading.Event) -> bool:
    """Запускает бота, повторяя попытки, пока Telegram не ответит.

    Telegram/DNS иногда отвечают дольше минуты, а сеть может пропасть на
    несколько минут. Раньше после трёх неудач процесс завершался, и демон
    оставался лежать до следующего входа в систему. Теперь пауза между
    попытками растёт до 5 минут, но попытки не заканчиваются, пока процесс
    не остановят вручную. ``False`` возвращается только при остановке.
    """
    attempt = 0
    while not stop.is_set():
        attempt += 1
        if bot.start(wait=True):
            if attempt > 1:
                logger.info(
                    f"Telegram: бот поднялся с попытки {attempt} после простоя."
                )
            return True
        if stop.is_set():
            return False
        delay = _retry_delay(attempt)
        logger.warning(
            f"Telegram: запуск не удался (попытка {attempt}) — "
            f"повтор через {int(delay)} с. Процесс не завершаю."
        )
        if stop.wait(delay):
            return False
        # После таймаута поток мог остаться в сетевом вызове. stop()
        # дожидается его и освобождает замок перед новой попыткой.
        try:
            bot.stop()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Telegram: не удалось очистить состояние после неудачного старта"
            )
    return False


def main(argv: list[str] | None = None) -> int:
    """Точка входа headless-бота. Возвращает код выхода процесса."""
    config, args = _load(argv)
    problem = _validate(config)
    if problem:
        logger.error(problem)
        return 2
    if args.check:
        logger.info(
            "Конфигурация бота в порядке: токен и {} ID пользователей заданы.",
            len(config.telegram.allowed_users),
        )
        return 0
    if args.health:
        path = _health_path(config)
        if _health_ok(path):
            logger.info("Heartbeat бота свежий — polling жив.")
            return 0
        logger.error(f"Нет свежего heartbeat ({path}) — бот не работает.")
        return 1

    logger.info("Запуск Telegram-бота без GUI…")
    core: Core | None = None
    bot: TelegramBot | None = None
    stop = threading.Event()
    _install_signals(stop)
    try:
        # MCP не должен задерживать готовность polling: подключим в фоне.
        core = build_core(config, system_suffix="", mcp_async=True)
        # Импорт LiteLLM (~10 с) греем после сборки ядра: бот успевает
        # подняться, а первый запрос при необходимости дождётся импорта.
        threading.Thread(target=warm_up, name="llm-warmup", daemon=True).start()
        bot = TelegramBot(config, core)
        if not _start_bot_with_retry(bot, stop):
            if stop.is_set():
                return 0
            logger.error("Telegram-бот не запустился — смотри лог выше.")
            return 1
        if core.reminder is not None:
            # В headless-режиме напоминание должно прийти в Telegram, а не
            # потеряться: в GUI его показывает окно, здесь — сообщение бота.
            def _notify(text: str) -> None:
                if not bot.send_text(f"⏰ {text}", owner=True):
                    logger.error(
                        "Напоминание сработало, но отправить в Telegram не "
                        "удалось: бот не запущен или не известен чат."
                    )

            core.reminder.on_fire = _notify

        health = _health_path(config)
        try:
            _write_health(health)
        except OSError as exc:
            logger.warning(f"Не удалось записать heartbeat {health}: {exc}")

        logger.info("Telegram-бот работает. Ctrl+C — остановка.")
        down_since = 0.0
        next_recover = 0.0
        next_notice = 0.0
        api_ok = True
        last_ping = time.monotonic()
        while not stop.wait(5.0):
            now = time.monotonic()
            process_ok = bot.healthy
            if process_ok and now - last_ping >= PING_EVERY:
                last_ping = now
                api_ok = bot.ping()
                if not api_ok:
                    logger.warning(
                        "Telegram: Bot API не отвечает — проверяю ещё раз "
                        "перед перезапуском."
                    )

            if process_ok and api_ok:
                if down_since:
                    logger.info(
                        "Telegram: связь восстановлена после %.0f с простоя.",
                        now - down_since,
                    )
                down_since = 0.0
                next_notice = 0.0
                try:
                    _write_health(health)
                except OSError as exc:
                    logger.warning(f"Не удалось обновить heartbeat {health}: {exc}")
                continue

            if not down_since:
                down_since = now
                next_recover = now
                next_notice = now + NOTICE_EVERY
                if process_ok:
                    logger.error(
                        "Telegram: Bot API не отвечает — держу процесс и "
                        "перезапущу бота, если простой затянется."
                    )
                else:
                    logger.error(
                        "Telegram: polling/поток бота не отвечает (%s) — "
                        "держу процесс и восстанавливаю.",
                        bot.health_report(),
                    )

            down_for = now - down_since
            if down_for >= RESTART_AFTER:
                logger.error(
                    "Telegram: недоступен %.0f с — перезапускаю бота.",
                    down_for,
                )
                try:
                    bot.stop()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Telegram: не удалось остановить бота перед перезапуском"
                    )
                if not _start_bot_with_retry(bot, stop):
                    return 0 if stop.is_set() else 1
                try:
                    _write_health(health)
                except OSError as exc:
                    logger.warning(f"Не удалось записать heartbeat {health}: {exc}")
                logger.info(
                    "Telegram: бот снова работает после простоя %.0f с.",
                    down_for,
                )
                down_since = 0.0
                next_notice = 0.0
                next_recover = 0.0
                api_ok = True
                last_ping = time.monotonic()
                continue

            if not process_ok and now >= next_recover:
                next_recover = now + RECOVER_EVERY
                if bot.recover_polling():
                    # Сеть при восстановлении ответила; следующий ping
                    # подтвердит это, а полный перезапуск не нужен.
                    api_ok = True
                    last_ping = time.monotonic()
                    logger.info(
                        "Telegram: polling восстановлен без перезапуска — "
                        "задача и бот продолжают работу."
                    )
                    # Таймер простоя не сбрасываем: если polling снова упадёт,
                    # полный перезапуск всё равно случится через 3 минуты.
                    # Сбросит его только подтверждённый bot.healthy выше.
                    continue

            if now >= next_notice:
                next_notice = now + NOTICE_EVERY
                logger.warning(
                    "Telegram: недоступен %.0f с — перезапуск через %.0f с.",
                    down_for,
                    RESTART_AFTER - down_for,
                )
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        logger.exception("Headless-бот упал")
        return 1
    finally:
        if bot is not None:
            try:
                bot.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось остановить Telegram-бота")
        if core is not None:
            try:
                core.llm.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                core.reminder.clear()
            except Exception:  # noqa: BLE001
                pass
            if core.mcp_manager is not None:
                try:
                    core.mcp_manager.close()
                except Exception:  # noqa: BLE001
                    pass
            if core.memory is not None:
                try:
                    core.memory.close()
                except Exception:  # noqa: BLE001
                    pass
            if core.telegram_memory is not None:
                try:
                    core.telegram_memory.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            _health_path(config).unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("Headless-бот остановлен.")


if __name__ == "__main__":
    raise SystemExit(main())
