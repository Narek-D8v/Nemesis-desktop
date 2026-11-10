# Немуся в Telegram 24/7

Бот умеет работать без окна: `python -m nemus --telegram`. Это отдельный
headless-процесс, который поднимает long polling и живёт, пока живёт процесс.
Так его можно держать на ноутбуке при закрытом приложении или на постоянном
хосте, который не выключается.

## Сначала важное: где работает бот, там и инструменты

Бот — это не «пульт» к ноутбуку. Инструменты (`read_file`, `run_shell`,
`open_application`, `see_screen`, Spotify и т.д.) выполняются **на той машине,
где запущен процесс бота**:

| Где запущен бот | Что он видит и умеет |
|---|---|
| Ноутбук с Windows | Все файлы, программы, экран, буфер обмена, Spotify |
| VPS/контейнер | Только файлы и команды сервера; экран/окна ноутбука недоступны |
| Raspberry Pi дома | Файлы Pi и команды в его сети; ноутбук — только по сети (SSH/WoL) |

Поэтому «бот на VPS» и «управление выключенным ноутбуком» — взаимоисключающие
вещи. Ниже — два честных сценария.

## Сценарий A. Ноутбук включён, приложение закрыто

Это то, что делает «Связь с Windows»: фоновая служба работает, пока включён
компьютер. Бот можно поднять отдельно от GUI и автозапускать при входе.

1. Проверь конфиг:

   ```bash
   python -m nemus --telegram --check
   ```

   Должно быть `telegram.enabled: true`, токен и хотя бы один ID в
   `telegram.allowed_users`.

2. Включи автозапуск. Это можно сделать в приложении:
   **Настройки → Telegram → Демон без окна → «Запускать Telegram-бота при
   входе в Windows»**, затем «Сохранить». Галочка отражает реальное
   состояние автозапуска: задача `NemusTelegram` **и** ярлык в папке
   «Автозагрузка».

   Либо зарегистрируй задачу вручную (PowerShell, без админа):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_telegram_autostart.ps1
   ```

   В «Автозагрузке» Windows появляется ярлык **«Немуся Telegram»**. При
   входе он скрыто запускает задачу `NemusTelegram`, а та поднимает
   `pythonw -m nemus --telegram` и перезапускает процесс при падении.
   Консольного окна нет, логи — в файле из `logging.file` (по умолчанию
   `data/nemus.log`).

   Если автозапуск включали в старой версии, задача могла остаться без
   ярлыка: выключи и снова включи галочку, чтобы создать его.

   Внутри процесса тоже есть сторож: раз в 5 секунд проверяется, жив ли
   поток polling. Если он умер, процесс завершается с кодом 1, и Task
   Scheduler поднимает его снова. Ошибки сторонних библиотек (telegram,
   httpx) пишутся в тот же файл, а не теряются без консоли.

3. Удалить автозапуск можно той же галочкой в настройках или вручную:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_telegram_autostart.ps1 -Uninstall
   ```

Важно: Telegram разрешает только один `getUpdates` на токен. Если запущены и
GUI, и автозапуск, второй процесс вежливо откажется стартовать (файл-замок
`data/telegram.lock`) и напишет об этом в лог. Оставь что-то одно.

Если всё же нужно держать GUI и headless одновременно, база у них общая:
SQLite работает в WAL-режиме и ждёт освобождения записи до 30 секунд, а
одновременная миграция старой схемы безопасна. Это не делает SQLite
многописательным — записи по-прежнему идут по очереди, — но убирает
`database is locked` при коротких операциях.

Чтобы ноутбук не засыпал при закрытой крышке (питание от сети):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

Это меняет системные настройки питания — применяй осознанно. Если ноутбук
всё равно выключен или спит, бот на нём работать не будет: это ограничение
железа, а не приложения.

## Сценарий B. Ноутбук выключен — бот на постоянном хосте

Подойдёт любой VPS/VDS с Docker (1 vCPU, 1–2 ГБ RAM достаточно), Raspberry Pi
или старый мини-ПК. Бот будет отвечать всегда, но работать с файлами хоста,
а не ноутбука. Для задач «скачай, посчитай, собери отчёт, отправь файл» этого
хватает; для «открой программу на ноутбуке» — нет.

### Docker

```bash
cp .env.telegram.example .env.telegram
# заполни токен, allowed_users и ключ модели
docker compose -f docker-compose.telegram.yml up -d --build
docker compose -f docker-compose.telegram.yml logs -f
```

Проверка статуса:

```bash
docker compose -f docker-compose.telegram.yml ps
docker inspect --format '{{.State.Health.Status}}' nemus-telegram
```

`HEALTHCHECK` смотрит на heartbeat `data/telegram.health`, который бот
обновляет каждые 5 секунд, — одного `--check` недостаточно, он проверяет
только конфиг. Если polling/поток умер, процесс сам выйдет с кодом 1
(сторож проверяет это раз в 5 секунд), и `restart: unless-stopped` поднимет
его заново. Если завис весь процесс и heartbeat протух, healthcheck пометит
контейнер `unhealthy`; Docker сам по себе unhealthy не перезапускает,
поэтому основной механизм — сторож внутри процесса.

Heartbeat можно проверить вручную:

```bash
docker compose -f docker-compose.telegram.yml exec nemus-telegram \
  python -m nemus --telegram --health
```

Остановка/обновление:

```bash
docker compose -f docker-compose.telegram.yml down
git pull
docker compose -f docker-compose.telegram.yml up -d --build
```

История, рабочая папка и логи лежат в `./data` на хосте и переживают
пересборку. Секреты — только в `.env.telegram` (он в `.gitignore`).

### Без Docker (systemd на Linux)

```ini
# /etc/systemd/system/nemus-telegram.service
[Unit]
Description=Nemus Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/nemus
EnvironmentFile=/opt/nemus/.env.telegram
ExecStart=/opt/nemus/venv/bin/python -m nemus --telegram
Restart=always
RestartSec=5
# watchdog внутри процесса завершает его при смерти polling,
# systemd поднимает снова
User=nemus

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nemus-telegram
journalctl -u nemus-telegram -f
```

## Ограничения headless-режима

- Нет GUI, голосового ввода с микрофона и realtime-режима.
- Голосовые сообщения в Telegram по умолчанию распознаются облачным Gemini
  (`gemini-3.5-transcribe`): нужен ключ Gemini и интернет, зато не нужны
  `faster-whisper`, модель и ffmpeg — OGG/Opus уходит в API напрямую. Если
  ключа Gemini нет или он не сработал, пробуется OpenAI; затем локальный
  faster-whisper (тогда нужны пакет и скачанная модель; в лёгком Docker-образе
  их нет). Принудительно: `voice.stt.engine: gemini | openai | faster-whisper`.
- Генерация изображений работает без кредитов через бесплатный Pollinations:
  текст→картинка, а для фото — приближённая чиби/стилизация по описанию
  (Gemini описывает фото, Pollinations рисует). Точное сохранение лица дают
  только провайдеры с кредитами (Gemini/OpenRouter/OpenAI); они выбираются
  автоматически или через `images.provider`.
- Инструменты экрана/мыши/буфера обмена на Linux-сервере вернут ошибку —
  это нормально, они для Windows-машины с рабочим столом.
- Для видео-кружков нужен `ffmpeg` (в Docker-образе уже есть).
- Ключи из OS keyring на сервере недоступны — передавай их переменными
  окружения (`NEMUS_LLM__PROVIDERS__...__API_KEY`).

## Безопасность

- `telegram.allowed_users` обязателен: без него бот не запускается.
- Токен и API-ключи не коммить. Старый `data/workspace/telegram_bridge.py`
  содержал токен в открытом виде — удали его и перевыпусти токен у @BotFather,
  если он ещё где-то лежит.
- Бот выполняет команды на хосте с правами своего пользователя. На VPS не
  запускай его от root: создай отдельного пользователя и рабочую папку.
