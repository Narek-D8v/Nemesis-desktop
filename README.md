# Немуся (Nemesis)

[![CI Tests & Checks](https://github.com/Narek-D8v/Nemesis-desktop/actions/workflows/tests.yml/badge.svg)](https://github.com/Narek-D8v/Nemesis-desktop/actions/workflows/tests.yml)

Голосовой ассистент для Windows на Python с графическим интерфейсом (PySide6),
LLM-агентом с вызовом инструментов, локальным и облачным голосом.

**Исходный код:** <https://github.com/Narek-D8v/Nemesis-desktop> ·

**Автор:** Narek Haroyan (Narek-D8v) · 

**Лицензия:** GNU GPLv3 (см. `LICENSE`)

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
  - wake word «Немуся» (Vosk, офлайн). Слушается постоянно — с запуска
    приложения и даже когда кнопка «Слушать» выключена; по слову активации
    запускается Realtime-диалог (или обычное прослушивание, если
    `voice.realtime.wake_activates: false`);
  - по слову активации можно запускать навыки (навыки wake word): время,
    YouTube, Spotify (открыть/воспроизвести/пауза) и свои через
    `voice.wake_word.skills` в `config.yaml` — опасные действия отдаются
    агенту на подтверждение;
  - распознавание речи — faster-whisper (офлайн);
  - синтез речи — edge-tts: сотни голосов на десятках языков, выбираются во
    вкладке «Озвучка (TTS)» окна настроек (локаль, голос, скорость, громкость,
    кнопка «Прослушать»);
  - подавление шума — builtin / RNNoise (офлайн) / Krisp SDK.
- **Режим реального времени** (непрерывный голосовой диалог): Gemini Live, OpenAI Realtime
  или локальный офлайн-аналог (STT → LLM → TTS).
- **Память** на SQLite: история диалога и долговременные факты.
- **Заметки о пользователе**: Немуся сама записывает, когда посчитает нужным
  (имя, предпочтения, планы, договорённости), инструменты `write_note` /
  `read_notes`, файл — `data/notes.md` (`memory.notes_file`). Это обычный
  markdown: его можно открыть и поправить руками, а свежие заметки
  подкладываются в системный промпт, чтобы она помнила между сессиями.
  Выключается через `memory.notes: false`.
- **Плагины**: свои инструменты в `plugins/`. Ассистент умеет писать их сам по просьбе (`create_tool`): просите «научись делать X» — Немуся создаст и сразу подключит новый инструмент.
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

## Готовые сборки (exe)

Готовый `Nemesis.exe` для Windows собирается автоматически в GitHub Actions
(`.github/workflows/build.yml`) при публикации релиза. Скачать: вкладка
**Releases** → `Nemesis-Windows.zip`.

1. Распакуйте архив в любую папку на любом диске (путь нигде не зашит).
2. Запустите `Nemesis.exe`. При первом запуске рядом появятся `config/`, `data/`
   и `plugins/` — там лежат конфиг (`config/config.yaml`), логи (`data/nemus.log`)
   и скачанные модели.
3. При первом использовании микрофона/синтеза речи скачаются модели Vosk и
   faster-whisper в `data/models/`.

Сборка сделана через PyInstaller в режиме `onedir`: рядом с `Nemesis.exe` лежит
папка `_internal/` — её нужно сохранять вместе с exe. Браузерный инструмент
(`playwright`) в exe не входит; для его работы запускайте приложение из исходников.

Локально собрать так же, как в CI, можно командой `pyinstaller Nemesis.spec`
(единый источник настроек сборки — `Nemesis.spec`).

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
> `pyproject.toml`, `requirements*.txt`, `run.bat`, `.gitignore`, `.github/`.

## Разрешения и безопасность

- `tools.permission_mode`: `full-auto` | `smart` | `confirm-all`. Рекомендуется `smart` —
  безопасные инструменты выполняются сразу, опасные (`run_shell`, `run_python`,
  `delete_path`, `install_package`, `create_tool`) требуют подтверждения в GUI.
- `tools.workspace` — рабочая папка ассистента по умолчанию. Чтение файлов не
  ограничивается; запись/запуск ограничены этой папкой, пока не включите
  `tools.allow_outside_workspace: true` (в этом режиме ассистент работает с
  файлами в любом месте системы).
- `fetch_url` не позволяет обращаться к локальным/внутренним адресам (защита от SSRF).
- `create_tool` статически проверяет код плагина: запрещены сеть (`socket`,
  `requests`, `urllib`…), низкоуровневый доступ (`ctypes`, `signal`, `pty`,
  `importlib`, `pickle`), `eval`/`exec`/`__import__` и dunder-хаки. Файлы, ОС и
  консоль (`os`, `subprocess`, `pathlib`, `shutil`, `tempfile`, `glob`, `open`)
  разрешены — плагин не должен быть бесполезнее `run_python`. Инструмент остаётся
  DANGEROUS — подтверждайте только понятный вам код. Имя в аргументе
  `name` должно совпадать с `name = '...'` внутри класса-наследника `Tool`.
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
venv\Scripts\python.exe scripts\test_notes.py         # заметки о пользователе (NoteBook)
venv\Scripts\python.exe scripts\test_realtime.py      # протоколы Gemini/OpenAI Realtime
venv\Scripts\python.exe scripts\test_spotify.py       # Spotify Web API
venv\Scripts\python.exe scripts\test_browser.py       # browser_open и fallback на системный браузер
venv\Scripts\python.exe scripts\test_wake_skills.py   # навыки wake word
venv\Scripts\python.exe scripts\smoke_gui.py          # сборка окна без показа
venv\Scripts\python.exe scripts\smoke_realtime_app.py # интеграция realtime и GUI
```

Для GUI-скриптов в headless-среде задайте `QT_QPA_PLATFORM=offscreen`.
Часть скриптов (`test_voice.py`, `test_wake_*.py`, `test_local_live.py`)
требует микрофон, модель wake word или API-ключ. Офлайн-скрипты
(`test_language.py`, `test_tools.py`, `test_realtime.py`,
`smoke_gui.py`, `smoke_realtime_app.py`) запускаются автоматически в CI
(`.github/workflows/tests.yml`).

## MCP-серверы

Немуся умеет подключать внешние [MCP](https://modelcontextprotocol.io)‑серверы
по stdio (Node.js, Python и т. п.). Инструменты сервера автоматически
попадают в список инструментов агента.

Включение в `config/config.yaml`:

```yaml
mcp:
  enabled: true
  refresh_seconds: 60
  servers:
    - command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "data"]
      cwd: "."
      env: {}
      enabled: true
```

- `command`, `args`, `cwd`, `env` — как у обычного процесса.
- `refresh_seconds` — период обновления списка инструментов.
- В GUI: вкладка **«MCP»** в окне настроек — добавление/правка серверов
  (команда, аргументы, рабочая папка, переменные окружения), кнопка
  «Проверить» (живое подключение и список инструментов), «Обновить
  инструменты» и статус уже подключённых инструментов.
- Все MCP-инструменты имеют уровень риска `DANGEROUS`: это внешний код,
  который не проверяется правилами разрешений, поэтому в режимах `smart` и
  `confirm-all` вызов требует подтверждения.
- Если пакет `mcp` не установлен, интеграция полностью отключается, а
  остальные функции продолжают работать.

Проверка подключения (запустит тестовый сервер и вызовет его инструменты):

```bash
python scripts/test_mcp.py
```

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
    mcp.py          # MCP-серверы (stdio) и их инструменты
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
Nemesis.spec        # единая конфигурация сборки PyInstaller (локально и в CI)
.github/workflows/  # CI: тесты (tests.yml) и сборка exe (build.yml)
```

## Автор

**Narek Haroyan (Narek-D8v)** — <https://github.com/Narek-D8v>\
Исходный код: <https://github.com/Narek-D8v/Nemesis-desktop>

## Лицензия

Проект распространяется под лицензией **GNU General Public License v3.0 or later**
(GPLv3+). Полный текст — в файле [LICENSE](LICENSE).

Copyright (C) 2026 Narek Haroyan (Narek-D8v).

