"""Панель настроек Немуси в Telegram.

Панель повторяет вкладки GUI и служит inline-резервом, пока Mini App не
настроен. Здесь нет PySide6 — модуль можно использовать в headless-демоне.

Значения читаются и пишутся по путям ``Config``. Изменение сразу сохраняется
в ``config/config.yaml``; часть настроек (модель, инструменты, токен бота)
начнёт действовать после перезапуска процесса.
"""

from __future__ import annotations

import html
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from nemus.config import Config, MCPServerConfig, ProviderConfig, save_config
from nemus.core.providers import PROVIDER_SPECS
from nemus.utils.secrets import set_secret

Kind = Literal["bool", "choice", "step", "pct", "text", "long", "ids"]


@dataclass(frozen=True)
class SettingField:
    """Одно редактируемое поле панели."""

    id: str
    path: tuple[str, ...]
    label: str
    kind: Kind = "text"
    choices: tuple[Any, ...] = ()
    secret: bool = False
    minimum: float = 0
    maximum: float = 0
    step: float = 1
    unit: str = ""
    hint: str = ""

    def choices_labels(self) -> list[str]:
        return [str(value) for value in self.choices]


@dataclass(frozen=True)
class SettingSection:
    id: str
    title: str
    icon: str
    fields: tuple[SettingField, ...] = field(default_factory=tuple)
    note: str = ""


def _provider_fields() -> tuple[SettingField, ...]:
    # У ollama и llama_cpp нет API-ключей: они работают локально. В GUI
    # группа ключей для llama.cpp тоже скрыта, а для ollama ключ не нужен.
    remote = ("openrouter", "gemini", "anthropic", "openai")
    return tuple(
        SettingField(
            id=f"key_{name}",
            path=("llm", "providers", name, "api_key"),
            label=f"Ключ {name}",
            kind="text",
            secret=True,
        )
        for name in remote
        if name in PROVIDER_SPECS
    )


_LLM_FIELDS = (
    SettingField("name", ("assistant", "name"), "Имя ассистента"),
    SettingField(
        "provider",
        ("llm", "default_provider"),
        "Провайдер",
        "choice",
        tuple(PROVIDER_SPECS),
    ),
    SettingField("model", ("llm", "default_model"), "Модель"),
    SettingField(
        "model_path",
        ("llm", "llama_cpp", "model_path"),
        "Файл локальной модели",
        hint="Путь к .gguf; можно выбрать после загрузки с Hugging Face.",
    ),
    SettingField(
        "temp",
        ("llm", "temperature"),
        "Температура",
        "step",
        minimum=0,
        maximum=2,
        step=0.1,
    ),
    SettingField(
        "max_tokens",
        ("llm", "max_tokens"),
        "Max tokens",
        "step",
        minimum=256,
        maximum=200000,
        step=256,
    ),
    SettingField(
        "os_family",
        ("assistant", "os_family"),
        "Семейство ОС",
        "choice",
        ("auto", "windows", "linux", "macos"),
    ),
    SettingField("os_details", ("assistant", "os_details"), "Уточнение ОС"),
    SettingField(
        "reply_lang",
        ("assistant", "reply_language"),
        "Язык ответов",
        "choice",
        ("auto", "ru", "en"),
    ),
    SettingField(
        "ui_lang",
        ("gui", "language"),
        "Язык интерфейса",
        "choice",
        ("ru", "en"),
    ),
    *_provider_fields(),
)

_VOICE_FIELDS = (
    SettingField(
        "wake", ("voice", "wake_word", "enabled"), "Wake word включён", "bool"
    ),
    SettingField(
        "wake_engine",
        ("voice", "wake_word", "engine"),
        "Движок wake word",
        "choice",
        ("vosk", "porcupine", "off"),
    ),
    SettingField("wake_words", ("voice", "wake_word", "words"), "Слова активации", "ids"),
    SettingField("realtime", ("voice", "realtime", "enabled"), "Realtime включён", "bool"),
    SettingField(
        "rt_provider",
        ("voice", "realtime", "provider"),
        "Провайдер Realtime",
        "choice",
        ("gemini", "openai", "local"),
    ),
    SettingField(
        "rt_gemini", ("voice", "realtime", "gemini_model"), "Модель Gemini Live"
    ),
    SettingField(
        "rt_openai", ("voice", "realtime", "openai_model"), "Модель OpenAI Realtime"
    ),
    SettingField("rt_voice", ("voice", "realtime", "openai_voice"), "Голос OpenAI"),
    SettingField("rt_audio", ("voice", "realtime", "audio"), "Передавать аудио", "bool"),
    SettingField(
        "rt_wake",
        ("voice", "realtime", "wake_activates"),
        "Realtime по слову активации",
        "bool",
    ),
    SettingField(
        "rt_fallback",
        ("voice", "realtime", "offline_fallback"),
        "Офлайн-резерв Realtime",
        "bool",
    ),
    SettingField("barge_in", ("voice", "barge_in"), "Прерывать голосом", "bool"),
    SettingField(
        "companion",
        ("voice", "companion", "enabled"),
        "Голосовой компаньон",
        "bool",
    ),
    SettingField(
        "companion_ack",
        ("voice", "companion", "wake_ack"),
        "Ответ на активацию",
    ),
    SettingField(
        "companion_followup",
        ("voice", "companion", "follow_up_seconds"),
        "Ждать продолжение",
        "step",
        minimum=5,
        maximum=300,
        step=5,
        unit=" с",
    ),
    SettingField(
        "companion_events",
        ("voice", "companion", "speak_events"),
        "Озвучивать автопилот",
        "bool",
    ),
    SettingField(
        "companion_quiet_start",
        ("voice", "companion", "quiet_hours_start"),
        "Тихие часы с",
    ),
    SettingField(
        "companion_quiet_end",
        ("voice", "companion", "quiet_hours_end"),
        "Тихие часы до",
    ),
)

_CHARACTER_FIELDS = (
    SettingField("ch_enabled", ("character", "enabled"), "Характер включён", "bool"),
    SettingField(
        "ch_advanced",
        ("character", "advanced"),
        "Расширенный режим",
        "bool",
        hint="Добавляет свободное описание характера к ползункам.",
    ),
    SettingField(
        "ch_prompt",
        ("character", "custom_prompt"),
        "Описание характера",
        "long",
        hint="Работает только в расширенном режиме.",
    ),
    SettingField(
        "ch_playfulness",
        ("character", "playfulness"),
        "Игривость",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_sensitivity",
        ("character", "sensitivity"),
        "Обидчивость",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_attachment",
        ("character", "attachment"),
        "Привязанность",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_independence",
        ("character", "independence"),
        "Самостоятельность",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_curiosity",
        ("character", "curiosity"),
        "Любопытство",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_mood",
        ("character", "base_mood"),
        "Базовое настроение",
        "step",
        minimum=-100,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_energy",
        ("character", "base_energy"),
        "Базовая энергия",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_trust",
        ("character", "base_trust"),
        "Базовое доверие",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_hurt",
        ("character", "base_hurt"),
        "Базовая обида",
        "step",
        minimum=0,
        maximum=100,
        step=5,
    ),
    SettingField(
        "ch_proactive",
        ("character", "proactive_enabled"),
        "Писать первой",
        "bool",
    ),
    SettingField(
        "ch_quiet_start", ("character", "quiet_hours_start"), "Тихие часы с"
    ),
    SettingField(
        "ch_quiet_end", ("character", "quiet_hours_end"), "Тихие часы до"
    ),
)

_TTS_FIELDS = (
    SettingField(
        "tts_engine",
        ("voice", "tts", "engine"),
        "Движок синтеза",
        "choice",
        ("edge-tts", "openai", "system"),
    ),
    SettingField("tts_voice", ("voice", "tts", "voice"), "Голос"),
    SettingField(
        "tts_rate",
        ("voice", "tts", "rate"),
        "Скорость речи",
        "pct",
        minimum=-100,
        maximum=100,
        step=5,
        unit="%",
    ),
    SettingField(
        "tts_volume",
        ("voice", "tts", "volume"),
        "Громкость",
        "pct",
        minimum=-100,
        maximum=100,
        step=5,
        unit="%",
    ),
)

_SPOTIFY_FIELDS = (
    SettingField("sp_enabled", ("spotify", "enabled"), "Spotify включён", "bool"),
    SettingField("sp_id", ("spotify", "client_id"), "Client ID"),
    SettingField(
        "sp_secret", ("spotify", "client_secret"), "Client Secret", secret=True
    ),
    SettingField("sp_device", ("spotify", "default_device"), "Устройство"),
    SettingField("sp_market", ("spotify", "market"), "Market"),
    SettingField("sp_redirect", ("spotify", "redirect_uri"), "Redirect URI"),
)

_TELEGRAM_FIELDS = (
    SettingField("tg_enabled", ("telegram", "enabled"), "Бот включён", "bool"),
    SettingField("tg_token", ("telegram", "token"), "Токен бота", secret=True),
    SettingField("tg_users", ("telegram", "allowed_users"), "ID пользователей", "ids"),
    SettingField(
        "tg_groups", ("telegram", "allow_groups"), "Отвечать в группах", "bool"
    ),
    SettingField(
        "tg_voice",
        ("telegram", "transcribe_voice"),
        "Распознавать голосовые",
        "bool",
    ),
    SettingField("tg_hints", ("telegram", "hints_enabled"), "Подсказки и примеры", "bool"),
    SettingField(
        "tg_advanced", ("telegram", "advanced_ui"), "Живой ответ и rich", "bool"
    ),
    SettingField(
        "tg_size",
        ("telegram", "max_download_mb"),
        "Максимальный файл",
        "step",
        minimum=1,
        maximum=20,
        step=1,
        unit=" МБ",
    ),
    SettingField("tg_web_url", ("telegram", "web_app_url"), "Web App URL"),
    SettingField(
        "tg_web_provider",
        ("telegram", "web_app_tunnel_provider"),
        "Провайдер туннеля",
        "choice",
        ("cloudflared", "ngrok"),
    ),
    SettingField(
        "tg_web_ngrok_token",
        ("telegram", "web_app_ngrok_authtoken"),
        "ngrok authtoken",
        secret=True,
    ),
    SettingField(
        "tg_web_tunnel", ("telegram", "web_app_tunnel"), "Автотуннель Web App", "bool"
    ),
    SettingField(
        "tg_web_download",
        ("telegram", "web_app_tunnel_download"),
        "Скачивать cloudflared",
        "bool",
    ),
    SettingField("tg_web_host", ("telegram", "web_app_host"), "Web App host"),
    SettingField(
        "tg_web_port",
        ("telegram", "web_app_port"),
        "Web App порт",
        "step",
        minimum=1,
        maximum=65535,
        step=1,
    ),
)

_TOOLS_FIELDS = (
    SettingField(
        "perm",
        ("tools", "permission_mode"),
        "Режим разрешений",
        "choice",
        ("full-auto", "smart", "confirm-all"),
    ),
    SettingField("auto_allow", ("tools", "auto_allow"), "Авторазрешение", "ids"),
    SettingField("workspace", ("tools", "workspace"), "Рабочая папка"),
    SettingField(
        "max_steps",
        ("tools", "max_steps"),
        "Лимит шагов",
        "step",
        minimum=5,
        maximum=1000,
        step=5,
    ),
    SettingField(
        "budget",
        ("tools", "run_budget_seconds"),
        "Бюджет времени",
        "step",
        minimum=30,
        maximum=7200,
        step=30,
        unit=" с",
    ),
)

_AUTOPILOT_FIELDS = (
    SettingField("auto_enabled", ("autopilot", "enabled"), "Автопилот включён", "bool"),
    SettingField(
        "auto_autonomy",
        ("autopilot", "autonomy", "enabled"),
        "Самостоятельность",
        "bool",
    ),
    SettingField(
        "auto_session",
        ("autopilot", "autonomy", "session_minutes"),
        "Перерыв после, минут",
        "step",
        minimum=15,
        maximum=480,
        step=15,
    ),
    SettingField(
        "auto_idle",
        ("autopilot", "autonomy", "idle_minutes"),
        "Простой считается перерывом",
        "step",
        minimum=1,
        maximum=120,
        step=1,
        unit=" мин",
    ),
    SettingField(
        "auto_autonomy_cooldown",
        ("autopilot", "autonomy", "cooldown_minutes"),
        "Пауза между репликами",
        "step",
        minimum=5,
        maximum=360,
        step=5,
        unit=" мин",
    ),
    SettingField(
        "auto_same_app",
        ("autopilot", "autonomy", "same_app_minutes"),
        "Заметить одно приложение через",
        "step",
        minimum=10,
        maximum=480,
        step=10,
        unit=" мин",
    ),
    SettingField(
        "auto_daily",
        ("autopilot", "autonomy", "daily_limit_minutes"),
        "Дневной лимит экрана",
        "step",
        minimum=0,
        maximum=1440,
        step=30,
        unit=" мин",
    ),
    SettingField(
        "auto_titles",
        ("autopilot", "autonomy", "read_window_titles"),
        "Читать заголовок окна",
        "bool",
    ),
    SettingField(
        "auto_poll",
        ("autopilot", "poll_seconds"),
        "Интервал проверки",
        "step",
        minimum=2,
        maximum=300,
        step=2,
        unit=" с",
    ),
    SettingField(
        "auto_limit",
        ("autopilot", "max_notifications_per_hour"),
        "Лимит в час",
        "step",
        minimum=1,
        maximum=100,
        step=1,
    ),
    SettingField("auto_quiet_start", ("autopilot", "quiet_hours_start"), "Тихие часы с"),
    SettingField("auto_quiet_end", ("autopilot", "quiet_hours_end"), "Тихие часы до"),
)

_HEALING_FIELDS = (
    SettingField("heal_enabled", ("healing", "enabled"), "Self-healing включён", "bool"),
    SettingField(
        "heal_propose",
        ("healing", "auto_propose"),
        "Генерировать патч",
        "bool",
    ),
    SettingField(
        "heal_auto_apply",
        ("healing", "auto_apply"),
        "Применять без спроса",
        "bool",
    ),
    SettingField(
        "heal_threshold",
        ("healing", "error_threshold"),
        "Порог ошибок",
        "step",
        minimum=1,
        maximum=50,
        step=1,
    ),
    SettingField(
        "heal_window",
        ("healing", "window_minutes"),
        "Окно, минут",
        "step",
        minimum=1,
        maximum=1440,
        step=1,
    ),
    SettingField("heal_test", ("healing", "test_command"), "Команда проверки"),
)

_MCP_FIELDS = (
    SettingField("mcp_enabled", ("mcp", "enabled"), "MCP включён", "bool"),
    SettingField(
        "mcp_refresh",
        ("mcp", "refresh_seconds"),
        "Обновление MCP",
        "choice",
        (5, 15, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400),
    ),
)

SECTIONS: tuple[SettingSection, ...] = (
    SettingSection("llm", "LLM", "🤖", _LLM_FIELDS, "Ключи хранятся в keyring."),
    SettingSection("voice", "Голос", "🎙", _VOICE_FIELDS),
    SettingSection(
        "character",
        "Характер",
        "💗",
        _CHARACTER_FIELDS,
        "Черты и базовые чувства. Текущее состояние показывает /emotions.",
    ),
    SettingSection(
        "autopilot",
        "Автопилот",
        "🤖",
        _AUTOPILOT_FIELDS,
        "Правила и самостоятельность: замечает активность и говорит первой.",
    ),
    SettingSection("healing", "Self-healing", "🩹", _HEALING_FIELDS),
    SettingSection("tts", "Озвучка", "🔊", _TTS_FIELDS),
    SettingSection("spotify", "Spotify", "🎵", _SPOTIFY_FIELDS),
    SettingSection("telegram", "Telegram", "✈️", _TELEGRAM_FIELDS),
    SettingSection("tools", "Инструменты", "🛠", _TOOLS_FIELDS),
    SettingSection("mcp", "MCP", "🧩", _MCP_FIELDS),
    SettingSection("skills", "Скиллы", "📚"),
    SettingSection("about", "О программе", "ℹ️"),
)

_BY_ID = {section.id: section for section in SECTIONS}


def section(section_id: str) -> SettingSection | None:
    return _BY_ID.get(section_id)


def find_field(section_id: str, field_id: str) -> SettingField | None:
    item = _BY_ID.get(section_id)
    if item is None:
        return None
    return next((f for f in item.fields if f.id == field_id), None)


# ---------------------------------------------------------------- значения
def _get(cfg: Any, path: Iterable[str]) -> Any:
    cursor: Any = cfg
    for key in path:
        if isinstance(cursor, dict):
            cursor = cursor.get(key)
        else:
            cursor = getattr(cursor, key, None)
        if cursor is None:
            return None
    return cursor


def _set(cfg: Any, path: tuple[str, ...], value: Any) -> None:
    cursor: Any = cfg
    for key in path[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor[key]
        else:
            cursor = getattr(cursor, key)
    last = path[-1]
    if isinstance(cursor, dict):
        cursor[last] = value
    else:
        setattr(cursor, last, value)


def _ensure_llm_provider(cfg: Config, path: tuple[str, ...]) -> None:
    """``llm.providers`` — словарь; пустого провайдера создаём при записи."""
    if len(path) == 4 and path[:2] == ("llm", "providers"):
        name = path[2]
        if name not in cfg.llm.providers:
            cfg.llm.providers[name] = ProviderConfig()


def field_value(cfg: Config, field: SettingField) -> Any:
    """Текущее значение поля (для Web App и внешних API)."""
    return _get(cfg, field.path)


def value_text(field: SettingField, value: Any) -> str:
    """Значение для кнопки/строки панели."""
    if field.kind == "bool":
        return "включено" if bool(value) else "выключено"
    if field.kind == "pct":
        return _pct_value(value)
    if field.kind == "ids":
        return ", ".join(str(item) for item in (value or []))
    if field.secret:
        return "••••••" if value else "не задан"
    if field.kind == "long":
        body = str(value or "").strip()
        if not body:
            return "не задано"
        line = body.splitlines()[0]
        return line[:60] + ("…" if len(line) > 60 or "\n" in body else "")
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _pct_value(raw: Any) -> str:
    try:
        return str(int(str(raw or "+0%").strip().rstrip("%")))
    except ValueError:
        return "0"


def _pct_text(number: int) -> str:
    return f"{number:+d}%"


def _parse_ids(raw: str) -> list[str]:
    return [part for part in raw.replace(";", ",").replace("\n", ",").split(",") if part.strip()]


def apply_text(cfg: Config, field: SettingField, raw: str) -> tuple[bool, str]:
    """Применяет текстовый ввод; возвращает (успех, сообщение)."""
    text = (raw or "").strip()
    if field.kind == "ids":
        values = [part.strip() for part in _parse_ids(text) if part.strip()]
        if not values:
            return False, "Нужно хотя бы одно значение."
        if field.path == ("telegram", "allowed_users"):
            try:
                ids = [int(part) for part in values]
            except ValueError:
                return False, "ID пользователей — это числа через запятую."
            if not ids:
                return False, "Нужен хотя бы один ID."
            _set(cfg, field.path, ids)
        else:
            _set(cfg, field.path, values)
        return True, "Сохранено."
    if field.kind == "step":
        try:
            parsed: float | int = float(text.replace(",", "."))
        except ValueError:
            return False, "Нужно число."
        if not field.minimum <= parsed <= field.maximum:
            return False, f"Допустимо от {field.minimum:g} до {field.maximum:g}."
        if field.path == ("llm", "temperature"):
            _set(cfg, field.path, round(parsed, 2))
        elif isinstance(parsed, float) and parsed.is_integer():
            _set(cfg, field.path, int(parsed))
        else:
            _set(cfg, field.path, parsed)
        return True, "Сохранено."
    if field.kind == "pct":
        try:
            number = int(float(text.replace("%", "").replace(",", ".").strip()))
        except ValueError:
            return False, "Нужно число от -100 до 100."
        if not field.minimum <= number <= field.maximum:
            return False, "Допустимо от -100 до 100."
        _set(cfg, field.path, _pct_text(number))
        return True, "Сохранено."
    if field.kind == "choice":
        match = next(
            (choice for choice in field.choices if str(choice).lower() == text.lower()),
            None,
        )
        if match is None:
            return False, "Недопустимое значение."
        _set(cfg, field.path, match)
        return True, "Сохранено."
    if field.kind == "bool":
        lowered = text.lower()
        if lowered not in ("да", "нет", "вкл", "выкл", "true", "false", "1", "0"):
            return False, "Напишите «да» или «нет»."
        _set(cfg, field.path, lowered in ("да", "вкл", "true", "1"))
        return True, "Сохранено."
    # text / secret
    if field.path in (
        ("assistant", "name"),
        ("llm", "default_model"),
        ("tools", "workspace"),
    ) and not text:
        return False, "Значение не может быть пустым."
    _ensure_llm_provider(cfg, field.path)
    _set(cfg, field.path, text)
    if field.path == ("telegram", "token"):
        set_secret("telegram/token", text)
    elif field.secret and len(field.path) == 4 and field.path[:2] == ("llm", "providers"):
        set_secret(f"llm/{field.path[2]}/api_key", text)
    elif field.path == ("spotify", "client_secret"):
        set_secret("spotify/client_secret", text)
    return True, "Сохранено."


def cycle_field(cfg: Config, field: SettingField) -> tuple[bool, str]:
    """Переключает следующее значение choice."""
    if field.kind != "choice" or not field.choices:
        return False, "Нечего переключать."
    current = _get(cfg, field.path)
    values = list(field.choices)
    try:
        index = values.index(current)
    except ValueError:
        index = -1
    new = values[(index + 1) % len(values)]
    _set(cfg, field.path, new)
    return True, str(new)


def step_field(cfg: Config, field: SettingField, delta: int) -> tuple[bool, str]:
    """Меняет числовое значение на шаг."""
    if field.kind == "pct":
        current = int(_pct_value(_get(cfg, field.path)))
        new = max(int(field.minimum), min(int(field.maximum), current + delta * int(field.step)))
        _set(cfg, field.path, _pct_text(new))
        return True, f"{new:+d}%"
    if field.kind != "step":
        return False, "Не число."
    value = _get(cfg, field.path)
    try:
        current = float(value)
    except (TypeError, ValueError):
        current = float(field.minimum)
    new = current + delta * field.step
    new = max(field.minimum, min(field.maximum, new))
    if field.path == ("llm", "temperature"):
        _set(cfg, field.path, round(new, 2))
        return True, f"{round(new, 2):g}"
    result = int(round(new))
    _set(cfg, field.path, result)
    return True, f"{result}{field.unit}"


def toggle_field(cfg: Config, field: SettingField) -> tuple[bool, str]:
    if field.kind != "bool":
        return False, "Это не переключатель."
    _set(cfg, field.path, not bool(_get(cfg, field.path)))
    return True, "включено" if _get(cfg, field.path) else "выключено"


def save(cfg: Config) -> None:
    """Сохраняет конфиг без изменения рабочей копии путей."""
    save_config(cfg)


# --------------------------------------------------------------- отрисовка
def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def field_line(cfg: Config, item: SettingField) -> str:
    value = value_text(item, _get(cfg, item.path))
    if item.kind == "bool":
        return f"{'✅' if bool(_get(cfg, item.path)) else '⬜'} {_esc(item.label)}"
    if item.secret:
        return f"• <b>{_esc(item.label)}</b>: {_esc(value)}"
    return f"• <b>{_esc(item.label)}</b>: {_esc(value) or '—'}"


def render(
    cfg: Config,
    section_id: str,
    *,
    page: int = 0,
    skills: list[tuple[str, bool]] | None = None,
    pending: tuple[str, str] | None = None,
    autostart: bool | None = None,
) -> str:
    """Текст страницы панели в HTML."""
    if section_id == "root":
        lines = [
            "<b>⚙️ Настройки Немуси</b>",
            "Те же разделы, что в GUI. Изменения сохраняются сразу; "
            "модель и инструменты подхватятся после перезапуска.",
            "",
        ]
        for item in SECTIONS:
            lines.append(f"{item.icon} {item.title}")
        if not (cfg.telegram.web_app_url or "").strip():
            lines.extend(
                [
                    "",
                    "⚠️ Web App не настроен: задай публичный HTTPS-адрес "
                    "в разделе Telegram → Web App URL.",
                ]
            )
        return "\n".join(lines)

    item = _BY_ID.get(section_id)
    if item is None:
        return "<b>Раздел не найден</b>"
    lines = [f"<b>{item.icon} {_esc(item.title)}</b>", ""]
    if pending:
        lines.append(
            f"✏️ Отправь новое значение для <b>{_esc(pending[1])}</b> "
            "одним сообщением."
        )
        lines.append("Отмена — кнопка ниже или /cancel.")
        lines.append("")
    if item.note:
        lines.append(f"<i>{_esc(item.note)}</i>")
        lines.append("")

    if section_id == "character":
        try:
            from nemus.core.emotions import EmotionStore, summary_text

            state = EmotionStore(cfg.character.state_file).snapshot()
            lines.append(_esc(summary_text(cfg, state)))
            lines.append("")
        except Exception:  # noqa: BLE001
            pass

    if section_id == "skills":
        lines.append(f"Скиллы включены: {'да' if cfg.skills.enabled else 'нет'}")
        entries = skills or []
        if not entries:
            lines.append("Скиллы не найдены.")
        else:
            size = 8
            start = page * size
            shown = entries[start : start + size]
            lines.append(f"Найдено: {len(entries)}. Страница {page + 1}.")
            for name, enabled in shown:
                lines.append(f"{'✅' if enabled else '⬜'} {_esc(name)}")
        return "\n".join(lines)

    if section_id == "mcp":
        lines.append("Серверы MCP:")
        if not cfg.mcp.servers:
            lines.append("Пока ни одного сервера.")
        else:
            size = 5
            start = page * size
            for index, server in enumerate(cfg.mcp.servers[start : start + size], start):
                name = server.name or server.command or f"сервер {index + 1}"
                mark = "✅" if server.enabled else "⬜"
                lines.append(f"{mark} {index + 1}. {_esc(name)}")
        lines.append("")
        lines.append(f"MCP включён: {'да' if cfg.mcp.enabled else 'нет'}")
        lines.append(f"Обновление: {cfg.mcp.refresh_seconds} с")
        return "\n".join(lines)

    if section_id == "about":
        from nemus import __version__

        lines.extend(
            [
                f"Немуся {__version__}",
                "",
                "Настройки повторяют GUI. Токены и ключи скрыты.",
                "Основной интерфейс — Telegram Mini App; inline-кнопки — "
                "резерв, если Web App ещё не настроен.",
                "",
                "Перезапуск демона: scripts/install_telegram_autostart.ps1 "
                "или галочка в GUI.",
            ]
        )
        return "\n".join(lines)

    for setting in item.fields:
        lines.append(field_line(cfg, setting))
    if section_id == "telegram":
        if autostart is not None:
            lines.append(f"• <b>Автозапуск демона</b>: {'да' if autostart else 'нет'}")
        lines.append("")
        lines.append(
            "Изменение токена, ID и Web App URL применяется после перезапуска бота."
        )
    if section_id == "llm":
        lines.append("")
        lines.append("Часть настроек LLM применяется после перезапуска.")
    return "\n".join(lines)


# --------------------------------------------------------------- клавиатура
def root_keyboard() -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    for index in range(0, len(SECTIONS), 2):
        row = [
            (f"{item.icon} {item.title}", f"pn:s:{item.id}:0")
            for item in SECTIONS[index : index + 2]
        ]
        rows.append(row)
    rows.append([("✖️ Закрыть", "pn:c")])
    return rows


def section_keyboard(
    cfg: Config,
    section_id: str,
    *,
    page: int = 0,
    skills: list[tuple[str, bool]] | None = None,
    pending: bool = False,
    autostart: bool | None = None,
) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    if pending:
        rows.append([("✖️ Отменить ввод", "pn:x")])
    item = _BY_ID.get(section_id)
    if item is None:
        return root_keyboard()

    if section_id == "skills":
        rows.append(
            [
                (
                    f"{'✅' if cfg.skills.enabled else '⬜'} Скиллы включены",
                    "pn:ske",
                )
            ]
        )
        entries = skills or []
        size = 8
        start = page * size
        for offset, (name, enabled) in enumerate(entries[start : start + size]):
            index = start + offset
            mark = "✅" if enabled else "⬜"
            rows.append([(f"{mark} {name[:36]}", f"pn:k:{page}:{index}")])
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("⬅️", f"pn:s:skills:{page - 1}"))
        if (page + 1) * size < len(entries):
            nav.append(("➡️", f"pn:s:skills:{page + 1}"))
        if nav:
            rows.append(nav)
    elif section_id == "mcp":
        size = 5
        start = page * size
        for index, server in enumerate(cfg.mcp.servers[start : start + size], start):
            name = server.name or server.command or f"Сервер {index + 1}"
            mark = "✅" if server.enabled else "⬜"
            rows.append([(f"{mark} {name[:32]}", f"pn:m:{index}")])
        rows.append(
            [
                (
                    f"{'✅' if cfg.mcp.enabled else '⬜'} MCP включён",
                    "pn:t:mcp:mcp_enabled",
                )
            ]
        )
        rows.append(
            [
                (
                    f"Обновление: {cfg.mcp.refresh_seconds} с ▸",
                    "pn:y:mcp:mcp_refresh",
                )
            ]
        )
        rows.append([("➕ Добавить сервер", "pn:ma")])
        nav = []
        if page > 0:
            nav.append(("⬅️", f"pn:s:mcp:{page - 1}"))
        if (page + 1) * size < len(cfg.mcp.servers):
            nav.append(("➡️", f"pn:s:mcp:{page + 1}"))
        if nav:
            rows.append(nav)
    elif section_id != "about":
        for setting in item.fields:
            if setting.kind == "bool":
                value = bool(_get(cfg, setting.path))
                mark = "✅" if value else "⬜"
                rows.append([(f"{mark} {setting.label}", f"pn:t:{section_id}:{setting.id}")])
            elif setting.kind in ("choice", "step", "pct"):
                current = value_text(setting, _get(cfg, setting.path))
                if setting.kind == "choice":
                    rows.append(
                        [
                            (
                                f"{setting.label}: {current} ▸",
                                f"pn:y:{section_id}:{setting.id}",
                            )
                        ]
                    )
                else:
                    rows.append(
                        [
                            ("−", f"pn:-:{section_id}:{setting.id}"),
                            (
                                f"{setting.label}: {current}{setting.unit}",
                                f"pn:e:{section_id}:{setting.id}",
                            ),
                            ("+", f"pn:+:{section_id}:{setting.id}"),
                        ]
                    )
            else:
                current = value_text(setting, _get(cfg, setting.path))
                if setting.secret:
                    current = "••••" if current not in ("", "не задан") else "не задан"
                if len(current) > 28:
                    current = current[:27] + "…"
                rows.append(
                    [
                        (
                            f"✏️ {setting.label}: {current}",
                            f"pn:e:{section_id}:{setting.id}",
                        )
                    ]
                )
        if section_id == "telegram" and autostart is not None:
            rows.append(
                [
                    (
                        f"{'✅' if autostart else '⬜'} Автозапуск демона",
                        "pn:auto",
                    )
                ]
            )
        if section_id == "tools":
            rows.append([("🧹 Очистить старые файлы", "pn:cleanup")])

    rows.append(
        [
            ("⬅️ Разделы", "pn:r"),
            ("🏠 В начало", "pn:r"),
            ("✖️ Закрыть", "pn:c"),
        ]
    )
    return rows


def mcp_detail_keyboard(index: int) -> list[list[tuple[str, str]]]:
    rows = [
        [("✏️ Название", f"pn:me:{index}:name")],
        [("✏️ Команда", f"pn:me:{index}:command")],
        [("✏️ Аргументы", f"pn:me:{index}:args")],
        [("✏️ Рабочая папка", f"pn:me:{index}:cwd")],
        [("✏️ Переменные окружения", f"pn:me:{index}:env")],
        [("✅ Включён / выключить", f"pn:mt:{index}")],
        [("🗑 Удалить сервер", f"pn:md:{index}")],
        [("⬅️ К списку MCP", "pn:s:mcp:0"), ("✖️ Закрыть", "pn:c")],
    ]
    return rows


def mcp_detail_text(cfg: Config, index: int) -> str | None:
    if not 0 <= index < len(cfg.mcp.servers):
        return None
    server = cfg.mcp.servers[index]
    args = " ".join(server.args)
    env = ", ".join(f"{key}={value}" for key, value in server.env.items())
    return "\n".join(
        [
            f"<b>🧩 MCP-сервер {index + 1}</b>",
            f"Название: {_esc(server.name) or '—'}",
            f"Команда: <code>{_esc(server.command)}</code>",
            f"Аргументы: <code>{_esc(args)}</code>",
            f"Рабочая папка: <code>{_esc(server.cwd)}</code>",
            f"Переменные: <code>{_esc(env)}</code>",
            f"Включён: {'да' if server.enabled else 'нет'}",
        ]
    )


def mcp_apply_text(
    cfg: Config, index: int, field_id: str, raw: str
) -> tuple[bool, str]:
    if not 0 <= index < len(cfg.mcp.servers):
        return False, "Сервер исчез — открой список MCP заново."
    server = cfg.mcp.servers[index]
    text = (raw or "").strip()
    if field_id == "name":
        server.name = text
    elif field_id == "command":
        if not text:
            return False, "Команда не может быть пустой."
        server.command = text
    elif field_id == "args":
        try:
            server.args = shlex.split(text)
        except ValueError as exc:
            return False, f"Не удалось разобрать аргументы: {exc}"
    elif field_id == "cwd":
        server.cwd = text or "."
    elif field_id == "env":
        env: dict[str, str] = {}
        for part in text.replace("\n", ",").split(","):
            if not part.strip():
                continue
            key, sep, value = part.partition("=")
            if not sep or not key.strip():
                return False, "Переменные — по одной: KEY=значение."
            env[key.strip()] = value.strip()
        server.env = env
    else:
        return False, "Неизвестное поле."
    return True, "Сохранено."


def add_mcp_server(cfg: Config, raw: str) -> tuple[bool, str]:
    """Добавляет сервер из строки ``npx -y ...`` как в GUI."""
    from nemus.core.mcp import parse_mcp_command

    try:
        server = parse_mcp_command(raw)
    except Exception as exc:  # noqa: BLE001
        return False, f"Не удалось разобрать команду: {exc}"
    if not isinstance(server, MCPServerConfig):
        return False, "Не удалось разобрать команду."
    cfg.mcp.servers.append(server)
    return True, "Сервер добавлен."


def skills_entries(cfg: Config) -> list[tuple[str, bool]]:
    """Имена и включённость скиллов; битая папка не ломает панель."""
    from nemus.core.skills import load_skills

    try:
        # Один скан вместо двух: load_skills с disabled=() возвращает и
        # выключенные скиллы, а галочку считаем по списку disabled.
        all_skills = load_skills(cfg.skills.dir, disabled=())
    except OSError:
        return []
    disabled = {str(name).lower() for name in cfg.skills.disabled}
    return [
        (str(skill.name), str(skill.name).lower() not in disabled)
        for skill in all_skills
    ]


def toggle_skill(cfg: Config, name: str) -> tuple[bool, str]:
    disabled = list(cfg.skills.disabled)
    if name in disabled:
        disabled.remove(name)
        cfg.skills.disabled = disabled
        return True, f"Скилл {name} включён."
    disabled.append(name)
    cfg.skills.disabled = disabled
    return True, f"Скилл {name} выключен."
