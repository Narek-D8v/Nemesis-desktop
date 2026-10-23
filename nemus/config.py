"""Загрузка и валидация конфигурации Немуси.

Приоритет источников (от низкого к высокому):
  1. Значения по умолчанию (модели ниже).
  2. Файл ``config/config.yaml`` (если существует).
  3. Переменные окружения с префиксом ``NEMUS_`` (вложенность через ``__``).

API-ключи могут храниться в OS keyring (см. ``nemus.utils.secrets``);
если ключ не задан в конфиге, он подтягивается из keyring.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

if getattr(sys, "frozen", False):
    # Собранный exe: config/, data/, plugins/ лежат рядом с Nemesis.exe.
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

ProviderName = Literal["openrouter", "gemini", "anthropic", "openai", "ollama"]
PermissionMode = Literal["full-auto", "smart", "confirm-all"]


# --------------------------------------------------------------------------
#  Секции конфигурации
# --------------------------------------------------------------------------
class AssistantConfig(BaseModel):
    name: str = "Немуся"
    full_name: str = "Немесис"
    #: язык распознавания/озвучки (STT, TTS, Gemini Live speechConfig)
    language: str = "ru"
    #: язык ответов: auto — язык собеседника, либо фиксированный тег (ru/en/…)
    reply_language: str = "auto"
    personality: str = (
        "Ты — Немуся (полное имя Немесис), дружелюбный и эффективный голосовой "
        "ассистент. Отвечай кратко и по делу, на языке собеседника. У тебя есть "
        "доступ к инструментам — используй их, когда это помогает выполнить задачу."
    )


class ProviderConfig(BaseModel):
    api_key: str = ""
    api_base: str = ""

    @field_validator("api_key", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v


class FallbackEntry(BaseModel):
    provider: str
    model: str


class LLMConfig(BaseModel):
    default_provider: str = "gemini"
    default_model: str = "gemini/gemini-2.0-flash"
    temperature: float = 0.7
    max_tokens: int = 4096
    request_timeout: int = 120
    fallback: list[FallbackEntry] = Field(default_factory=list)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class WakeSkillConfig(BaseModel):
    """Навык слова активации: фразы → вызов инструмента из реестра."""

    name: str = ""
    #: фразы-триггеры; достаточно вхождения любой из них
    phrases: list[str] = Field(default_factory=list)
    #: имя инструмента (get_time, open_url, spotify, …)
    tool: str = ""
    #: аргументы вызова инструмента
    arguments: dict[str, Any] = Field(default_factory=dict)


def default_wake_skills() -> list[WakeSkillConfig]:
    """Полезные навыки «из коробки» (работают локально, без модели)."""
    return [
        WakeSkillConfig(
            name="time",
            phrases=["который час", "сколько времени", "время сейчас"],
            tool="get_time",
        ),
        WakeSkillConfig(
            name="youtube",
            phrases=["открой ютуб", "открой youtube", "включи ютуб"],
            tool="open_url",
            arguments={"url": "https://www.youtube.com"},
        ),
        WakeSkillConfig(
            name="spotify_open",
            phrases=["открой spotify", "открой спотифай", "запусти spotify"],
            tool="spotify",
            arguments={"action": "open"},
        ),
        WakeSkillConfig(
            name="music_play",
            phrases=["включи музыку", "играй музыку", "продолжи музыку"],
            tool="spotify",
            arguments={"action": "play"},
        ),
        WakeSkillConfig(
            name="music_pause",
            phrases=["пауза", "останови музыку", "выключи музыку"],
            tool="spotify",
            arguments={"action": "pause"},
        ),
    ]


class WakeWordConfig(BaseModel):
    enabled: bool = True
    engine: Literal["vosk", "porcupine", "off"] = "vosk"
    words: list[str] = Field(default_factory=lambda: ["немуся", "немесис", "немо"])
    vosk_model_path: str = "data/models/vosk-model-small-ru-0.22"
    porcupine_access_key: str = ""
    #: навыки: короткие фразы, выполняющие инструмент без обращения к LLM
    skills: list[WakeSkillConfig] = Field(default_factory=lambda: default_wake_skills())


class STTConfig(BaseModel):
    engine: str = "faster-whisper"
    model: str = "small"
    language: str = "ru"
    device: str = "auto"
    #: ширина лучевого поиска: больше — точнее, но медленнее
    beam_size: int = 5
    #: дополнительные подсказки-слова для распознавания (через пробел/запятую)
    hotwords: str = ""


class TTSConfig(BaseModel):
    # edge-tts (облачный, бесплатный) | openai (нужен ключ) | system (Windows SAPI)
    engine: str = "edge-tts"
    voice: str = "ru-RU-DmitryNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    #: модель OpenAI TTS (движок «openai»)
    openai_model: str = "gpt-4o-mini-tts"


#: Голос по умолчанию для Gemini Live (дублируется дефолтами realtime-сессий).
DEFAULT_GEMINI_VOICE = "Aoede"
#: Голос по умолчанию для OpenAI Realtime (дублируется дефолтами realtime-сессий).
DEFAULT_OPENAI_VOICE = "marin"


class RealtimeConfig(BaseModel):
    """Настройки режима реального времени (Gemini Live / OpenAI Realtime / локальный)."""

    enabled: bool = True
    # gemini | openai — облачные; local — офлайн-аналог на локальных STT/TTS/LLM
    provider: Literal["gemini", "openai", "local"] = "gemini"
    gemini_model: str = "gemini-2.5-flash-native-audio-latest"
    gemini_voice: str = DEFAULT_GEMINI_VOICE
    openai_model: str = "gpt-realtime"
    openai_voice: str = DEFAULT_OPENAI_VOICE
    # Отправлять/принимать аудио (иначе — только текст)
    audio: bool = True
    # Слово активации запускает диалог реального времени
    wake_activates: bool = True
    # Без интернета/ключа — переключаться на локальный аналог
    offline_fallback: bool = True


class NoiseConfig(BaseModel):
    """Подавление шума микрофона (встроенный фильтр или Krisp SDK)."""

    # builtin | rnnoise | krisp | off
    provider: str = "builtin"
    # Krisp SDK (коммерческий): API-ключ лицензии + модель .kef + нативный SDK.
    krisp_api_key: str = ""
    krisp_model_path: str = ""
    krisp_frame_ms: int = 10
    krisp_suppression: float = 100.0


class VoiceConfig(BaseModel):
    enabled: bool = True
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    barge_in: bool = True
    noise: NoiseConfig = Field(default_factory=NoiseConfig)


class ToolsConfig(BaseModel):
    enabled: bool = True
    permission_mode: PermissionMode = "smart"
    auto_allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    #: рабочая папка для собственных скриптов и файлов ассистента
    workspace: str = "data/workspace"
    #: разрешить инструментам писать/удалять файлы вне рабочей папки
    allow_outside_workspace: bool = False


class ScreenConfig(BaseModel):
    """Возможности «компьютерного зрения»: просмотр экрана и управление им."""

    enabled: bool = True
    #: максимальная ширина снимка, отдаваемого vision-модели (0 — 1:1 с экраном,
    #: так координаты картинки совпадают с пикселями экрана и не «плывут»)
    max_width: int = 0
    #: как «показывать» экран: auto (по возможностям модели), image, ocr
    vision_mode: Literal["auto", "image", "ocr"] = "auto"
    #: максимальная ширина снимка для локального OCR (текстовые модели)
    ocr_max_width: int = 2200
    #: язык распознавания текста на экране
    ocr_lang: str = "ru-RU"
    #: искать ли области/элементы интерфейса через cv2
    detect_regions: bool = True
    #: ограничение числа распознанных строк в ответе
    max_lines: int = 80
    #: рисовать ли координатную сетку, если снимок уменьшается
    draw_grid: bool = True


class MemoryConfig(BaseModel):
    database: str = "data/nemus.db"
    #: долговременные факты (инструменты remember_fact/recall_fact).
    #: false — агент ведёт только историю диалога, без сохранения фактов.
    long_term: bool = True
    #: заметки о пользователе в markdown-файле (write_note/read_notes);
    #: Немуся записывает их по своему усмотрению, файл можно править руками.
    notes: bool = True
    notes_file: str = "data/notes.md"


class SpotifyConfig(BaseModel):
    """Доступ к Spotify через Web API (spotipy, OAuth)."""

    enabled: bool = True
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8888/callback"
    default_device: str = ""
    market: str = "RU"
    cache_path: str = "data/spotify_token.json"


class GUIConfig(BaseModel):
    theme: str = "dark"
    #: держать окно поверх других окон
    always_on_top: bool = False
    start_minimized: bool = False
    #: язык интерфейса: ru | en (применяется при следующем запуске)
    language: str = "ru"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/nemus.log"


class MCPConfig(BaseModel):
    """Настройки Model Context Protocol (инструменты сторонних серверов).

    Серверы — stdio-прокси на Python/Node; секция «mcp» в config.yaml.
    Без серверов интеграция отключается, даже если enabled: true.
    """

    enabled: bool = True
    refresh_seconds: int = 60
    servers: list["MCPServerConfig"] = Field(default_factory=list)


class MCPServerConfig(BaseModel):
    """Один MCP-сервер (stdio-прокси)."""

    #: человекочитаемое название для GUI-списка (пусто — показывается команда)
    name: str = ""
    command: str = "node"
    args: list[str] = Field(default_factory=list)
    cwd: str = "."
    enabled: bool = True
    env: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    gui: GUIConfig = Field(default_factory=GUIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)


# --------------------------------------------------------------------------
#  Загрузка
# --------------------------------------------------------------------------
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _env_overrides(prefix: str = "NEMUS_") -> dict[str, Any]:
    """Читает переменные окружения ``NEMUS_SECTION__KEY=value`` в словарь."""
    result: dict[str, Any] = {}
    for raw_key, value in os.environ.items():
        if not raw_key.startswith(prefix):
            continue
        path = raw_key[len(prefix):].lower().split("__")
        cursor = result
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(value)
    return result


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _resolve_path(path_str: str) -> str:
    p = Path(path_str)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def to_relative_path(path_str: str) -> str:
    """Возвращает путь относительно корня проекта, если он лежит внутри него.

    Нужна для сохранения конфига: в памяти пути абсолютные (``load_config``
    разворачивает их), а в ``config.yaml`` они должны оставаться переносимыми.
    Пути вне проекта не трогаем — они заданы пользователем осознанно.
    """
    if not path_str:
        return path_str
    p = Path(path_str)
    if not p.is_absolute():
        return path_str
    try:
        return p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(p)


#: Поля-пути, которые при сохранении конфига возвращаются в относительный вид.
PATH_FIELDS: tuple[tuple[str, ...], ...] = (
    ("memory", "database"),
    ("memory", "notes_file"),
    ("logging", "file"),
    ("voice", "wake_word", "vosk_model_path"),
    ("spotify", "cache_path"),
    ("tools", "workspace"),
)


def relativize_paths(cfg: Config) -> None:
    """Преобразует известные абсолютные пути конфига обратно в относительные."""
    for field_path in PATH_FIELDS:
        target: Any = cfg
        for name in field_path[:-1]:
            target = getattr(target, name)
        last = field_path[-1]
        setattr(target, last, to_relative_path(getattr(target, last)))


def load_config(config_path: Path | None = None) -> Config:
    """Загружает конфигурацию, объединяя YAML, env и значения по умолчанию."""
    data: dict[str, Any] = {}
    path = config_path or (CONFIG_DIR / "config.yaml")
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
            if isinstance(loaded, dict):
                data = loaded

    data = _deep_merge(data, _env_overrides())
    cfg = Config.model_validate(data)

    # Пути к данным — абсолютные
    cfg.memory.database = _resolve_path(cfg.memory.database)
    cfg.memory.notes_file = _resolve_path(cfg.memory.notes_file)
    cfg.logging.file = _resolve_path(cfg.logging.file)
    cfg.voice.wake_word.vosk_model_path = _resolve_path(
        cfg.voice.wake_word.vosk_model_path
    )
    cfg.spotify.cache_path = _resolve_path(cfg.spotify.cache_path)
    cfg.tools.workspace = _resolve_path(cfg.tools.workspace)

    _inject_keys_from_keyring(cfg)
    return cfg


def _inject_keys_from_keyring(cfg: Config) -> None:
    """Дозаполняет пустые API-ключи из OS keyring (если доступен)."""
    try:
        from nemus.utils.secrets import get_secret
    except Exception:  # pragma: no cover
        return
    for name, provider in cfg.llm.providers.items():
        if not provider.api_key:
            secret = get_secret(f"llm/{name}/api_key")
            if secret:
                provider.api_key = secret
    if not cfg.voice.wake_word.porcupine_access_key:
        secret = get_secret("wakeword/porcupine/access_key")
        if secret:
            cfg.voice.wake_word.porcupine_access_key = secret
    if cfg.spotify.enabled and not cfg.spotify.client_secret:
        secret = get_secret("spotify/client_secret")
        if secret:
            cfg.spotify.client_secret = secret
    if cfg.voice.noise.provider == "krisp" and not cfg.voice.noise.krisp_api_key:
        secret = get_secret("voice/noise/krisp_api_key")
        if secret:
            cfg.voice.noise.krisp_api_key = secret


_cached: Config | None = None


def get_config(reload: bool = False) -> Config:
    """Возвращает кэшированный экземпляр конфигурации."""
    global _cached
    if _cached is None or reload:
        _cached = load_config()
    return _cached
