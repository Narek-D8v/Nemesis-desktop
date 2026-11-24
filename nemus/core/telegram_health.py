"""Самодиагностика и отчёт о состоянии компьютера для Telegram."""

from __future__ import annotations

import os
import platform
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_CHECK = "✅"
_WARN = "⚠️"
_FAIL = "❌"


def _size(value: float | int) -> str:
    """Размер в удобных единицах."""
    number = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if number < 1024 or unit == "ТБ":
            return f"{number:.0f} {unit}" if unit == "Б" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} ТБ"


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def pc_report(workspace: str | Path | None = None) -> str:
    """CPU, память, диски, батарея и топ процессов."""
    try:
        import psutil
    except ImportError:
        return "Не установлен psutil: `pip install psutil`."

    lines = ["**Компьютер**", f"• хост: `{platform.node() or '?'}`"]
    try:
        cpu = psutil.cpu_percent(interval=0.4)
        cores = psutil.cpu_count(logical=True) or 0
        physical = psutil.cpu_count(logical=False) or cores
        freq = psutil.cpu_freq()
        freq_text = f", {freq.current / 1000:.1f} ГГц" if freq and freq.current else ""
        lines.append(
            f"• CPU: **{cpu:.0f}%** ({physical} ядер / {cores} потоков{freq_text})"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"• CPU: не удалось прочитать ({exc})")

    try:
        memory = psutil.virtual_memory()
        lines.append(
            f"• RAM: **{memory.percent:.0f}%** — "
            f"{_size(memory.used)} из {_size(memory.total)}"
        )
        swap = psutil.swap_memory()
        if swap.total:
            lines.append(
                f"• swap: {swap.percent:.0f}% — {_size(swap.used)} из {_size(swap.total)}"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"• RAM: не удалось прочитать ({exc})")

    seen: set[str] = set()
    anchors: list[str] = []
    if workspace:
        try:
            anchor = Path(workspace).resolve().anchor
            if anchor:
                anchors.append(anchor)
        except OSError:
            pass
    if os.name == "nt":
        anchors.append(os.environ.get("SystemDrive", "C:") + "\\")
    else:
        anchors.append("/")
    for anchor in anchors:
        if anchor in seen:
            continue
        seen.add(anchor)
        try:
            usage = psutil.disk_usage(anchor)
            lines.append(
                f"• диск `{anchor}`: **{usage.percent:.0f}%** занято — "
                f"свободно {_size(usage.free)} из {_size(usage.total)}"
            )
        except Exception:  # noqa: BLE001
            continue

    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            state = "заряжается" if battery.power_plugged else "от батареи"
            seconds = battery.secsleft
            left = (
                ""
                if seconds in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN)
                else f", осталось ~{_duration(seconds)}"
            )
            lines.append(f"• батарея: **{battery.percent:.0f}%** ({state}{left})")
        elif os.name == "nt":
            lines.append("• батарея: не определена")
    except Exception:  # noqa: BLE001
        pass

    try:
        boot = datetime.fromtimestamp(psutil.boot_time())
        lines.append(
            f"• аптайм: {_duration(time.time() - psutil.boot_time())} "
            f"(с {boot:%d.%m %H:%M})"
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        processes = []
        for process in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = process.info
                rss = getattr(info.get("memory_info"), "rss", 0) or 0
                processes.append((rss, info.get("pid"), info.get("name") or "?"))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        processes.sort(reverse=True)
        if processes:
            lines += ["", "**Топ по памяти**"]
            for rss, pid, name in processes[:5]:
                lines.append(f"• {name} (`{pid}`) — {_size(rss)}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def _recent_issues(path: str, token: str = "", limit: int = 3) -> list[str]:
    """Последние ERROR/WARNING из лога без токена."""
    file = Path(path)
    if not file.is_absolute():
        file = Path.cwd() / file
    if not file.is_file():
        return []
    try:
        with file.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-400:]
    except OSError:
        return []
    issues: list[str] = []
    for line in reversed(lines):
        if not re.search(r"\b(ERROR|CRITICAL|WARNING)\b", line):
            continue
        text = line.strip()
        if token:
            text = text.replace(token, "***")
        text = re.sub(r"\s+", " ", text)
        if len(text) > 220:
            text = text[:217] + "…"
        issues.append(text)
        if len(issues) >= limit:
            break
    return issues


def doctor_report(bot: Any) -> str:
    """Проверяет Telegram, Web App, туннель, LLM, MCP и место на диске."""
    cfg = getattr(bot, "config", None)
    telegram = getattr(bot, "cfg", None)
    core = getattr(bot, "core", None)
    lines = ["**Самодиагностика**"]

    running = bool(getattr(bot, "running", False))
    username = getattr(bot, "username", "") or "?"
    started = float(getattr(bot, "started_at", 0.0) or 0.0)
    uptime = _duration(time.monotonic() - started) if started else "только запускается"
    lines.append(f"{_CHECK if running else _FAIL} бот: {'работает' if running else 'остановлен'}")
    lines.append(f"• аккаунт: @{username}, аптайм: {uptime}")
    token = str(getattr(telegram, "token", "") or "")
    lines.append(
        f"{_CHECK if token else _FAIL} токен Telegram: "
        f"{'задан' if token else 'не задан'}"
    )
    allowed = list(getattr(telegram, "allowed_users", []) or [])
    lines.append(
        f"{_CHECK if allowed else _WARN} доступ: "
        f"{len(allowed)} пользовател(ь/я) в allowlist"
    )

    server = getattr(bot, "_webapp", None)
    if server is not None and getattr(server, "running", False):
        port = getattr(server, "bound_port", 0)
        lines.append(f"{_CHECK} Web App: http://127.0.0.1:{port}")
    else:
        lines.append(f"{_FAIL} Web App: не запущен — /panel не откроется")

    tunnel = getattr(bot, "_tunnel", None)
    if tunnel is None:
        lines.append(f"{_WARN} туннель: не создан")
    elif getattr(tunnel, "public_url", ""):
        lines.append(f"{_CHECK} туннель: {tunnel.public_url}")
    elif getattr(tunnel, "failed", False):
        error = str(getattr(tunnel, "error", "") or "неизвестная ошибка")
        lines.append(f"{_FAIL} туннель: ошибка — {error[:180]}")
    elif getattr(tunnel, "starting", False):
        lines.append(f"{_WARN} туннель: запускается (обычно 10–30 с)")
    else:
        lines.append(f"{_WARN} туннель: не активен")

    web_url = str(getattr(telegram, "web_app_url", "") or "").strip()
    if web_url:
        lines.append(f"• URL для Mini App: {web_url}")

    llm = getattr(core, "llm", None)
    provider = str(getattr(llm, "provider", "?") or "?")
    model = str(getattr(llm, "model", "?") or "?")
    lines.append(f"{_CHECK} LLM: {provider} / `{model}`")
    providers = getattr(getattr(cfg, "llm", None), "providers", {}) or {}
    provider_cfg = providers.get(provider) if isinstance(providers, dict) else None
    api_key = str(getattr(provider_cfg, "api_key", "") or "")
    if provider.lower() not in {"ollama", "local"}:
        lines.append(
            f"{_CHECK if api_key else _FAIL} ключ провайдера: "
            f"{'задан' if api_key else 'не задан'}"
        )

    mcp_cfg = getattr(cfg, "mcp", None)
    mcp_enabled = bool(getattr(mcp_cfg, "enabled", False))
    servers = list(getattr(mcp_cfg, "servers", []) or [])
    active_mcp = sum(1 for item in servers if getattr(item, "enabled", False))
    lines.append(
        f"{_CHECK if mcp_enabled else _WARN} MCP: "
        f"{'включён' if mcp_enabled else 'выключен'}, "
        f"серверов: {active_mcp}/{len(servers)}"
    )

    workspace = Path(str(getattr(getattr(cfg, "tools", None), "workspace", ".")))
    try:
        usage = shutil.disk_usage(workspace if workspace.exists() else Path.cwd())
        free_gb = usage.free / (1024**3)
        mark = _CHECK if free_gb >= 2 else _WARN
        lines.append(f"{mark} диск: свободно {_size(usage.free)}")
    except OSError as exc:
        lines.append(f"{_WARN} диск: не удалось проверить ({exc})")

    autopilot = getattr(bot, "_autopilot", None)
    if autopilot is not None:
        lines.append(
            f"{_CHECK if autopilot.enabled else _WARN} автопилот: "
            f"{'включён' if autopilot.enabled else 'выключен'}, "
            f"правил: {len(getattr(autopilot.cfg, 'rules', []))}"
        )
    healing = getattr(bot, "_healing", None)
    if healing is not None:
        lines.append(
            f"{_CHECK if healing.enabled else _WARN} self-healing: "
            f"{'включён' if healing.enabled else 'выключен'}"
        )

    jobs = getattr(bot, "_jobs", None)
    if jobs is not None:
        try:
            count = len(jobs.running())
            lines.append(f"{_CHECK} фоновые команды: {count} выполняется")
        except Exception:  # noqa: BLE001
            pass

    log_file = str(getattr(getattr(cfg, "logging", None), "file", "") or "")
    issues = _recent_issues(log_file, token=token) if log_file else []
    lines += ["", "**Последние предупреждения**"]
    if issues:
        for issue in issues:
            lines.append(f"• `{issue}`")
    else:
        lines.append("• за последние 400 строк лога проблем нет")

    lines += [
        "",
        "**Проверка**",
        "• /status — состояние диалога",
        "• /pc — ресурсы компьютера",
        "• /panel — настройки Mini App",
    ]
    return "\n".join(lines)


def human_ago(timestamp: float) -> str:
    """«5 мин назад» для журнала фоновых задач."""
    delta = max(0, time.time() - timestamp)
    return f"{_duration(delta)} назад" if delta >= 60 else "только что"


def iso_time(timestamp: float) -> str:
    """Время в локальном формате для отчётов."""
    return datetime.fromtimestamp(timestamp).strftime("%d.%m %H:%M:%S")


__all__ = [
    "doctor_report",
    "human_ago",
    "iso_time",
    "pc_report",
]
