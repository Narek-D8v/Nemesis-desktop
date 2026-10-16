# Немуся (Nemesis)

Голосовой ассистент для Windows на Python с графическим интерфейсом (PySide6),
LLM-агентом с вызовом инструментов, локальным и облачным голосом.

**Исходный код:** <https://github.com/Narek-D8v/Nemesis-desktop> ·
**Автор:** Narek Haroyan (Narek-D8v) · **Лицензия:** GNU GPLv3 (см. `LICENSE`)

## Возможности

- **Диалог с LLM** через [LiteLLM](https://github.com/BerriAI/litellm): Gemini, OpenAI,
  Anthropic, OpenRouter, Ollama. Есть автоматический fallback между провайдерами.
- **Инструменты (function calling)**: файлы, оболочка, Python, веб-поиск и загрузка страниц,
  запуск приложений, скриншоты, клавиатура/медиа-клавиши, Spotify, память и саморасширение.
- **Взгляд на экран и управление им**: ассистент может «увидеть» экран
  (`see_screen`) и взаимодействовать с ним (`control_screen` — мышь, клики,
  перетаскивание, прокрутка, ввод текста и горячие клавиши). Если модель умеет
  принимать изображения — ей уходит снимок (`gemini/gemini-2.0-flash`, `gpt-4o`).
  Если модель только текстовая — включается локальный конвейер cv2 + Windows OCR:
  снимок обрабатывается OpenCV, текст и координаты элементов распознаются офлайн,
  и модель работает с ними как с текстом (`screen.vision_mode: auto`).
  Снимок по умолчанию 1:1 с экраном, а `see_screen` отдаёт нумерованный список
  элементов, поэтому `control_screen` умеет нажимать `click_element(id=…)` или
  `click_text(text=…)` — без угадывания координат.
- **Голос**:
  - wake word «Немуся» (Vosk, офлайн);
  - распознавание речи — faster-whisper (офлайн);
  - синтез речи — edge-tts;
  - подавление шума — builtin / RNNoise (офлайн) / Krisp SDK.
- **Режим реального времени** (непрерывный голосовой диалог): Gemini Live, OpenAI Realtime
  или локальный офлайн-аналог (STT → LLM → TTS).
- **Память** на SQLite: история диалога и долговременные факты.
- **Плагины**: свои инструменты в `plugins/`.
- **Язык**: ответы на языке собеседника (`assistant.reply_language: auto`) без
  «внутренних рассуждений» в выводе, либо принудительно русский/английский.
  Интерфейс переключается между русским и английским (`gui.language`).

## Требования

- Python **3.13** (см. `run.bat`).
- Микрофон и колонки для голосовых режимов.
- API-ключ выбранного LLM-провайдера (для облачных режимов).

## Установка и запуск

```bat
run.bat
```

Скрипт создаст виртуальное окружение `venv`, установит зависимости из
`requirements.txt` и запустит приложение. Ручной вариант:

```bat
py -3.13 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m nemus
```

## Настройка

1. Скопируйте `config/config.example.yaml` в `config/config.yaml` (или правьте прямо в GUI).
2. Ключи API удобнее хранить в **OS keyring** (окно «Настройки» в GUI). Если ключ задан в
   `config.yaml`, файл не должен попадать в git (он уже в `.gitignore`).
3. Модель wake word скачается автоматически при первом запуске (Vosk, `data/models/`).

Основные секции конфига: `llm`, `voice` (wake word / stt / tts / realtime / noise),
`tools` (разрешения, workspace), `memory`, `spotify`, `gui`, `logging`.

> **Публикация репозитория.** В `config/config.yaml`, `data/` и `venv/` могут
> лежать ваши API-ключи, токены Spotify, история диалогов и модели — они уже
> перечислены в `.gitignore`, но **при загрузке файлов через веб-интерфейс GitHub
> `.gitignore` не действует**. Загружайте только исходники: `nemus/`, `plugins/`,
> `scripts/`, `config/config.example.yaml`, `README.md`, `LICENSE`,
> `pyproject.toml`, `requirements*.txt`, `run.bat`, `.gitignore`.

## Разрешения и безопасность

- `tools.permission_mode`: `full-auto` | `smart` | `confirm-all`. Рекомендуется `smart` —
  безопасные инструменты выполняются сразу, опасные (`run_shell`, `run_python`,
  `delete_path`, `install_package`, `create_tool`) требуют подтверждения в GUI.
- `tools.workspace` — рабочая папка ассистента. Запись, удаление и запуск скриптов
  **ограничены** этой папкой. Чтение файлов не ограничивается. Отключить ограничение:
  `tools.allow_outside_workspace: true`.
- `fetch_url` не позволяет обращаться к локальным/внутренним адресам (защита от SSRF).
- `create_tool` статически проверяет код плагина (запрет `os`/`sys`/`subprocess`/`eval`/dunder
  и т.п.), но остаётся DANGEROUS — подтверждайте только понятный вам код.
- `see_screen` — риск SAFE (только снимок), `control_screen` — DANGEROUS (управляет ПК).
  Чтобы ассистент работал с экраном без постоянных подтверждений, добавьте
  `see_screen` и/или `control_screen` в `tools.auto_allow` либо используйте
  `tools.permission_mode: full-auto`. Раздел `screen` (`enabled`, `max_width`)
  настраивает это поведение.

## Разработка

```bat
venv\Scripts\python.exe -m pip install ruff
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m compileall -q nemus scripts
```

Тесты — это автономные скрипты в `scripts/` (у каждого в конце печатается
«ВСЕ ТЕСТЫ ПРОШЛИ»/«PASSED», код возврата `0`):

```bat
venv\Scripts\python.exe scripts\test_language.py      # язык, фильтр рассуждений, i18n
venv\Scripts\python.exe scripts\test_tools.py         # инструменты и sandbox
venv\Scripts\python.exe scripts\test_realtime.py      # протоколы Gemini/OpenAI Realtime
venv\Scripts\python.exe scripts\test_spotify.py       # Spotify Web API
venv\Scripts\python.exe scripts\smoke_gui.py          # сборка окна без показа
venv\Scripts\python.exe scripts\smoke_agent.py        # агентный цикл (заглушка LLM)
venv\Scripts\python.exe scripts\smoke_realtime_app.py # интеграция realtime и GUI
```

Для GUI-скриптов в headless-среде задайте `QT_QPA_PLATFORM=offscreen`.
Часть скриптов (`test_voice.py`, `test_wake_*.py`, `test_local_live.py`)
требует микрофон, модель wake word или API-ключ.

## Структура проекта

```
nemus/
  app.py            # сборка и запуск приложения, мосты Qt-сигналов
  config.py         # Pydantic-конфиг (YAML + NEMUS_* env + keyring)
  core/
    agent.py        # агентный цикл: LLM + инструменты + память
    llm.py          # обёртка LiteLLM со стримингом и fallback
    language.py     # определение языка, инструкции языка и запрет рассуждений
    factory.py      # сборка ядра, инъекция настроек в инструменты
    memory.py       # SQLite: история и факты
    permissions.py  # политика разрешений
    tools/          # встроенные инструменты + sandbox + volume
    providers/      # единый слой сообщений/tool-calls
  voice/            # wake word, STT, TTS, шумодав, local_live, realtime/
  gui/              # окно, чат, орб, настройки, трей, worker, i18n
  utils/            # логгер, keyring-хранилище секретов, redact_secrets
plugins/            # пользовательские инструменты (авто-загрузка)
scripts/            # ручные проверки и smoke-тесты
config/             # config.yaml (локальный) и пример
data/               # БД, логи, модели, workspace (в git не попадает)
```

## Автор

**Narek Haroyan (Narek-D8v)** — <https://github.com/Narek-D8v>\
Исходный код: <https://github.com/Narek-D8v/Nemesis-desktop>

## Лицензия

Проект распространяется под лицензией **GNU General Public License v3.0 or later**
(GPLv3+). Полный текст — в файле [LICENSE](LICENSE).

Copyright (C) 2026 Narek Haroyan (Narek-D8v).

