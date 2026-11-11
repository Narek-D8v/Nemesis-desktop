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
  Короткие реплики идут по быстрому пути: без 20k системного промпта, схем
  инструментов и долгих повторов; прямое «Немуся?» отвечает мгновенно.
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
    `voice.realtime.wake_activates: false`). Слова можно вводить кириллицей
    или латиницей; имена вне словаря Vosk разбиваются на близкие слова
    («немуся» → «не муся»), а незнакомые слушаются в open-vocabulary режиме;
  - по слову активации можно запускать навыки (навыки wake word): время,
    YouTube, Spotify (открыть/воспроизвести/пауза) и свои через
    `voice.wake_word.skills` в `config.yaml` — опасные действия отдаются
    агенту на подтверждение;
  - распознавание речи — облачный Gemini (`gemini-3.5-transcribe`) при наличии
    ключа Gemini, затем OpenAI (`gpt-4o-mini-transcribe`): голосовые Telegram
    уходят в API напрямую (OGG/Opus, без ffmpeg и скачивания модели), ответ —
    за секунды; без ключей или при сбое сети автоматически включается
    локальный faster-whisper. Режим задаётся
    `voice.stt.engine: auto | gemini | openai | faster-whisper`;
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
- **Изображения**: настоящая генерация через `generate_image` с цепочкой
  провайдеров. Без ключей и кредитов работает бесплатный Pollinations
  (текст→картинка; русский промпт переводится через Gemini). Если передано фото,
  бесплатный режим сначала описывает его через Gemini и рисует чиби/стилизацию
  по описанию — сходство приблизительное; точное сохранение лица даёт провайдер
  с кредитами (Gemini, OpenRouter или OpenAI). Рисование кружками/фигурами
  через Pillow запрещено промптом и считается невыполненной задачей.
- **Презентации**: `create_presentation` собирает PPTX 16:9 из плана — тёмная
  тема, карточки-тезисы, крупные цифры, графики и картинки; 8–12 содержательных
  слайдов вместо пустых заглушек.
- **Видео**: `create_video` собирает MP4 из 3–12 содержательных сцен (заголовок,
  текст, картинка, длительность) — быстрый предсказуемый ролик без пустых
  кадров; для сложной анимации и титров остаётся скилл `hyperframes`.
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

### Telegram-бот без окна (24/7)

```bat
venv\Scripts\python.exe -m nemus --telegram --check
venv\Scripts\python.exe -m nemus --telegram
```

Так бот работает без GUI — на ноутбуке при закрытом приложении или на
постоянном хосте в Docker. Автозапуск в Windows и развёртывание на VPS:
[docs/telegram-24-7.md](docs/telegram-24-7.md).

Подсказки и примеры в чате включаются галочкой **«Показывать подсказки и
примеры»** на вкладке Telegram в настройках. При выключении бот остаётся
рабочим: `/help`, подтверждения, прогресс и восстановление после ошибок
доступны.

В чате: `/skills` — список установленных скиллов и MCP-серверов,
`/skills <имя>` — что делает скилл.

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
venv\Scripts\python.exe scripts\test_shell.py         # run_shell: таймауты, фон, кодировка
venv\Scripts\python.exe scripts\test_process.py      # запуск процессов и приложений
venv\Scripts\python.exe scripts\test_content_tools.py # изображения, презентации, видео, скиллы
venv\Scripts\python.exe scripts\test_notes.py         # заметки о пользователе (NoteBook)
venv\Scripts\python.exe scripts\test_realtime.py     # протоколы Gemini/OpenAI Realtime
venv\Scripts\python.exe scripts\test_history.py       # история: пары tool-calls ↔ результаты
venv\Scripts\python.exe scripts\test_sessions.py     # сессии диалогов и переключение агента
venv\Scripts\python.exe scripts\test_llm_fallback.py  # обрыв стрима LLM и маскировка ключей
venv\Scripts\python.exe scripts\test_model_catalog.py # каталог моделей: бесплатно/платно, поиск и фильтр в настройках
venv\Scripts\python.exe scripts\test_reminders.py    # отложенные напоминания (remind)
venv\Scripts\python.exe scripts\test_skills.py       # скиллы Claude Agent Skills (SKILL.md)
venv\Scripts\python.exe scripts\test_skill_store.py  # магазин скиллов: каталог, установка, обновление
venv\Scripts\python.exe scripts\test_context_budget.py # бюджеты контекста и быстрый путь реплик
venv\Scripts\python.exe scripts\test_spotify.py       # Spotify Web API
venv\Scripts\python.exe scripts\test_browser.py       # browser_open и fallback на системный браузер
venv\Scripts\python.exe scripts\test_telegram.py       # Telegram-бот: доступ, файлы, кружки, инструмент (без сети)
venv\Scripts\python.exe scripts\test_wake_skills.py   # навыки wake word
venv\Scripts\python.exe scripts\smoke_agent.py        # агентный цикл с фиктивным LLM
venv\Scripts\python.exe scripts\smoke_gui.py          # сборка окна без показа
venv\Scripts\python.exe scripts\smoke_realtime_app.py # интеграция realtime и GUI
```

Для GUI-скриптов в headless-среде задайте `QT_QPA_PLATFORM=offscreen`.
Часть скриптов (`test_voice.py`, `test_wake_*.py`, `test_local_live.py`,
`test_spotify.py`) требует микрофон, модель wake word или API-ключ.
Офлайн-скрипты запускаются автоматически в CI
(`.github/workflows/tests.yml`): `test_language.py`, `test_tools.py`,
`test_shell.py`, `test_process.py`, `test_mcp.py`, `test_mcp_layout.py`,
`test_browser.py`, `test_realtime.py`, `test_history.py`,
`test_sessions.py`, `test_memory_concurrency.py`, `test_headless.py`,
`test_llm_fallback.py`, `test_model_catalog.py`, `test_reminders.py`,
`test_notes.py`, `test_skills.py`, `test_skill_store.py`,
`test_content_tools.py`, `test_context_budget.py`, `test_telegram.py`,
`smoke_agent.py`, `smoke_gui.py`, `smoke_realtime_app.py`.

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

## Скиллы (Claude Agent Skills)

Скилл — это папка с инструкцией, которую Немуся подгружает по мере
надобности. Формат совместим с Claude Code: папки вида
`.agents/skills/<имя>/SKILL.md` читаются и другими агентами, так что
скиллы можно переиспользовать между инструментами.

Устройство скилла:

```
.agents/skills/pdf-tools/
  SKILL.md        # шапка name/description между «---» + пошаговая инструкция
  merge.py        # что угодно: скрипты, шаблоны — лежат рядом
```

```markdown
---
name: pdf-tools
description: Склейка и разбиение PDF-файлов
---

1. Собери список файлов (list_directory).
2. Склей их скриптом …
```

Как это работает:

- В системном промпте — названия и описания скиллов; полный текст
  подгружается, когда задача подходит. Это делает не только модель: для явных
  тем (презентация, картинка, видео, сайт) и команд `/имя` приложение само
  добавляет инструкцию в промпт до первого шага (`match_skills`), поэтому
  «установила скилл, а она его не использует» больше не воспроизводится.
- `skills.always` — скиллы, которые загружены в каждый запрос. В текущем
  `config.yaml` там `i-have-adhd`: стиль ответа для чтения с СДВГ применяется
  постоянно, пока его не уберут из списка.
- Когда задача попадает под описание, модель также может вызвать
  `load_skill(name)` и получить полный текст со списком файлов.
- Свой скилл Немуся создаёт сама: `create_skill(name, description, content)` —
  например, когда её научили повторяющейся процедуре.
- В GUI: вкладка **«Скиллы»** в настройках — список, включение/выключение
  (отключённые пишутся в `skills.disabled`), кнопки «Обновить» и «Открыть
  папку».
- Команды скиллов — это `/имя-скилла` (`/code-craft`, `/hyperframes`,
  `/pptx-design-styles`). `core/skill_commands.py` собирает их из папки
  скиллов (по одной на скилл, с его описанием), а `SkillSuggester` показывает
  список, как только последнее слово в поле ввода начинается с `/`:
  `/` — все скиллы, `/cod` — только `/code-craft`. Выбор заменяет только это
  слово, остальной промпт остаётся: `«сделай отчёт /cod»` →
  `«сделай отчёт /code-craft »`. Без слэша подсказок нет.
- `/имя` без задачи — карточка справки: название, описание и разделы
  инструкции скилла (что он умеет), число вспомогательных файлов и подсказка
  «напиши /имя <задача>». Строится локально, без запроса к модели; неизвестное
  `/имя` — системное сообщение со списком доступных команд.
- Модель тоже понимает такие команды как вызов скилла:
  `/hyperframes сделай ролик` — это «загрузи скилл hyperframes и выполни
  остаток по его инструкции» (правило в системном промпте); неизвестное `/имя`
  она не выдумывает, а перечисляет доступные скиллы.
- Настройки в `config/config.yaml`: `skills.enabled`, `skills.dir`
  (по умолчанию `.agents/skills`), `skills.disabled`, `skills.always`.
- Часть скиллов **встроена** (`nemus/skills`) и работает сразу после
  установки: `code-craft` — как Немуся пишет код (брейншторм из 3–5
  вопросов до первой строки: у каждого три готовых варианта A/B/C с
  рекомендованным по умолчанию и строка D) «Свой вариант», плюс проектная
  записка, стандарты реализации, проверка запуском и чек-лист ревью).
  В GUI эти варианты — кликабельная панель под ответом: A/B/C выбираются
  кликом, D открывает поле для своего варианта, кнопка «Отправить выбор»
  присылает Немусе строку вида «1A, 2B»; ответ текстом работает так же.
  Свободная формулировка тоже понимается: «второй вариант», «давай B»,
  «всё A», «по умолчанию», «лучше через json», «пусть будет fail-fast» —
  сопоставление идёт по словам вариантов, а при неоднозначности вариант
  не угадывается и пункт остаётся открытым.
  Панель — часть диалога: при переключении сессии и после перезапуска вопросы
  и выбранные варианты восстанавливаются из истории (источник — ответ в
  репликах: «1A, 2B», свободный «второй вариант», «давай B», «всё A»,
  «по умолчанию», «в пункте 2 — B» и содержательный — «лучше через json»,
  «пусть будет fail-fast»), а неотвеченный брейншторм остаётся
  кликабельным — диалог продолжается с того же места, а не обрывается.
  Встроенные скиллы видны
  в той же вкладке, отключаются через `skills.disabled`, а свой скилл с тем
  же именем в `skills.dir` перекрывает встроенный.

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
    skills.py       # скиллы Claude Agent Skills (SKILL.md): чтение и запись
    skill_commands.py  # команды из скиллов → подсказки для поля ввода
    permissions.py  # политика разрешений
    tools/          # встроенные инструменты + sandbox + volume
    providers/      # единый слой сообщений/tool-calls
  voice/            # wake word, STT, TTS, шумодав, local_live, realtime/
  gui/              # окно, чат, орб, настройки, трей, worker, i18n
  utils/            # логгер, keyring-хранилище секретов, redact_secrets
  skills/           # встроенные скиллы (code-craft и др.) — едут с приложением
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

