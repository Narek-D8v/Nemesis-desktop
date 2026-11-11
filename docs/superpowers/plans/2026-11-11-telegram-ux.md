# Telegram UX Hints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать Telegram-бота понятным новичку: первый экран, примеры, подсказки по контексту, кнопки действий под вложениями и восстановление после ошибок, с общим выключателем в настройках.

**Architecture:** Новый чистый модуль `nemus/core/telegram_ux.py` держит тексты, строки inline-кнопок и промпты действий. `TelegramBot` остаётся мостом Telegram ↔ агент: он строит клавиатуры, хранит состояние подсказок в `telegram.db` и запускает задачи из callback через единый `_run_task_for`.

**Tech Stack:** Python 3.13, python-telegram-bot, PySide6, pydantic, SQLite, существующие скрипты-тесты.

**Spec:** `docs/superpowers/specs/2026-11-11-telegram-ux-design.md`

## Global Constraints

- Весь пользовательский текст — по-русски, живой и короткий; технические стектрейсы в чат не попадают.
- Один общий переключатель `telegram.hints_enabled`, по умолчанию `true`; отдельный тумблер для примеров не делать.
- При выключенных подсказках `/help`, подтверждения, прогресс, ошибки и кнопка повтора продолжают работать.
- Существующий callback-формат подтверждений `tg:...` не менять.
- Каждый фикс — отдельный коммит; после каждого коммита `ruff check .` и `python -m compileall -q nemus scripts` должны быть чистыми.
- Живую проверку Telegram и Docker не выполнять.
- Тесты запускать с `$env:PYTHONIOENCODING='utf-8'`.

## Review Focus

- Пользователь нажимает кнопку вложения из старого сообщения: вложение уже истекло, задача не должна запускаться с чужим файлом.
- Два чата присылают вложения одновременно: кнопка первого чата не должна забрать файл второго.
- Подсказки выключены глобально или замьючены в чате: не должно появляться ни примеров, ни автоматических подсказок, ни кнопок действий.
- Ошибка агента приходит с техническим текстом/путями: в чат должен уйти короткий понятный текст, а кнопки — остаться рабочими.
- `/new` или повтор нажимают во время работающей задачи: контекст не сбрасывается, задача не стартует параллельно.

---

### Task 1: Настройка подсказок в конфиге и GUI

**Files:**
- Modify: `nemus/config.py:302-320`
- Modify: `config/config.example.yaml:290-302`
- Modify: `nemus/gui/dialogs.py:1616-1625`, `nemus/gui/dialogs.py:1943-1950`, `nemus/gui/dialogs.py:2060-2067`
- Modify: `nemus/gui/i18n.py:160-163`, `nemus/gui/i18n.py:706-709`
- Test: `scripts/test_telegram.py:52-59`, `scripts/test_telegram.py:1036-1043`

**Interfaces:**
- Produces: `TelegramConfig.hints_enabled: bool = True`
- Produces: `SettingsDialog.chk_tg_hints: QCheckBox` — загружается из `cfg.telegram.hints_enabled`, сохраняется обратно в `_save`.

- [ ] **Step 1: Написать падающие тесты**

В `test_config_defaults` добавить:

```python
assert cfg.telegram.hints_enabled is True, "подсказки должны быть включены по умолчанию"
```

Добавить новый тест:

```python
def test_gui_hints_toggle() -> None:
    """Галочка подсказок читается из конфига и пишется обратно при сохранении."""
    from PySide6.QtWidgets import QApplication

    import nemus.gui.dialogs as dialogs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    dlg = dialogs.SettingsDialog(None, core=None)
    old_secret, old_get = dialogs.set_secret, dialogs.get_config
    try:
        assert hasattr(dlg, "chk_tg_hints"), "нет галочки подсказок"
        dlg.config.telegram.hints_enabled = True
        dlg._load()
        assert dlg.chk_tg_hints.isChecked()

        dlg.config.telegram.hints_enabled = False
        dlg._load()
        assert not dlg.chk_tg_hints.isChecked()

        dlg.chk_tg_hints.setChecked(True)
        captured = []
        dialogs.set_secret = lambda *a, **k: None
        dialogs.get_config = lambda *a, **k: None
        dlg._write_yaml = lambda cfg: captured.append(cfg)
        dlg._apply_tg_autostart = lambda: True
        dlg.accept = lambda: None
        dlg._save()
        assert captured and captured[0].telegram.hints_enabled is True
    finally:
        dialogs.set_secret, dialogs.get_config = old_secret, old_get
        dlg.done(0)
```

Добавить `test_gui_hints_toggle()` в `main()`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: FAIL — `TelegramConfig` не имеет `hints_enabled`, у `SettingsDialog` нет `chk_tg_hints`.

- [ ] **Step 3: Реализовать настройку**

В `TelegramConfig` после `transcribe_voice`:

```python
#: показывать в чате примеры и контекстные подсказки
hints_enabled: bool = True
```

В `config/config.example.yaml` после `transcribe_voice`:

```yaml
  # Показывать примеры задач и контекстные подсказки в чате
  hints_enabled: true
```

В `_build_telegram_tab` после `chk_tg_voice`:

```python
self.chk_tg_hints = QCheckBox(i18n.tr("tg.hints"))
self.chk_tg_hints.setToolTip(i18n.tr("tg.hints_tip"))
form.addRow("", self.chk_tg_hints)
```

В `_load` после `chk_tg_voice.setChecked`:

```python
self.chk_tg_hints.setChecked(tg.hints_enabled)
```

В `_save` после `tg.transcribe_voice`:

```python
tg.hints_enabled = self.chk_tg_hints.isChecked()
```

В `i18n.py` в русский блок:

```python
"tg.hints": "Показывать подсказки и примеры",
"tg.hints_tip": (
    "Примеры задач, первая подсказка и кнопки действий под вложениями. "
    "/help остаётся доступной всегда."
),
```

В английский блок:

```python
"tg.hints": "Show hints and examples",
"tg.hints_tip": (
    "Task examples, a first-run hint and action buttons under attachments. "
    "/help always remains available."
),
```

- [ ] **Step 4: Прогнать тест и обе проверки**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: PASS, включая `test_gui_hints_toggle`.

Run: `ruff check .`
Run: `python -m compileall -q nemus scripts`
Expected: без ошибок.

- [ ] **Step 5: Коммит**

```bash
git add nemus/config.py config/config.example.yaml nemus/gui/dialogs.py nemus/gui/i18n.py scripts/test_telegram.py
git commit -m "feat: добавить настройку подсказок Telegram"
```

---

### Task 2: Первый экран и примеры в Telegram

**Files:**
- Create: `nemus/core/telegram_ux.py`
- Modify: `nemus/core/telegram_bot.py:411-435`, `nemus/core/telegram_bot.py:1156-1195`, `nemus/core/telegram_bot.py:1704-1744`
- Test: `scripts/test_telegram.py:477-538`, `scripts/test_telegram.py:1052-1081`

**Interfaces:**
- Consumes: `TelegramConfig.hints_enabled` из Task 1.
- Produces: `start_text(full_name: str, aliases: str, hints: bool) -> str`
- Produces: `help_text(hints: bool) -> str`
- Produces: `examples_text() -> str`
- Produces: `main_menu_rows(hints: bool) -> list[list[tuple[str, str]]]`
- Produces: `TelegramBot._keyboard(rows: list[list[tuple[str, str]]]) -> Any | None`
- Produces: `TelegramBot._on_ux_callback(update: Any, query: Any) -> None` — на этом шаге обрабатывает `ux:help` и `ux:examples`.
- Produces: `start_message(..., reply_markup: Any | None = None) -> None` — клавиатура ставится на последний чанк.

- [ ] **Step 1: Написать падающие тесты**

Добавить импорт новых функций и тест:

```python
from nemus.core.telegram_ux import (
    examples_text,
    help_text,
    main_menu_rows,
    start_text,
)


def test_ux_texts() -> None:
    """Тексты первого экрана и примеров зависят от переключателя."""
    with_hints = start_text("Немесис", "Немо", True)
    without_hints = start_text("Немесис", "Немо", False)
    assert "Примеры" in with_hints and "сделай из этого видео кружок" in with_hints
    assert "Примеры" not in without_hints
    assert "Фишка" in help_text(True)
    assert "Фишка" not in help_text(False)
    assert "PDF" in examples_text()
    assert main_menu_rows(True)
    assert main_menu_rows(False) == []
```

Добавить тест команд:

```python
def test_start_help_respect_hints() -> None:
    """При выключенных подсказках нет примеров и кнопок-подсказок."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append((text, kwargs))

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(message_id=11),
    )

    asyncio.run(bot._cmd_start(update, None))
    text, kwargs = fake.sent[-1]
    assert "Примеры" in text
    assert kwargs.get("reply_markup") is not None

    fake.sent.clear()
    cfg.telegram.hints_enabled = False
    asyncio.run(bot._cmd_start(update, None))
    text, kwargs = fake.sent[-1]
    assert "Примеры" not in text
    assert kwargs.get("reply_markup") is None

    fake.sent.clear()
    asyncio.run(bot._cmd_help(update, None))
    assert "Фишка" not in fake.sent[-1][0]
```

Добавить тест callback:

```python
def test_ux_examples_callback() -> None:
    """Кнопка «Примеры задач» отправляет примеры, а не молчит."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append(text)

    class _Query:
        data = "ux:examples"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(chat_id=42)

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    core = SimpleNamespace(
        llm=None,
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        config=cfg,
    )
    bot = TelegramBot(cfg, core)
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)
    query = _Query()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(update, None))
    assert fake.sent and "Примеры задач" in fake.sent[-1], fake.sent
```

Добавить оба теста в `main()`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: FAIL — модуля `nemus.core.telegram_ux` нет, `reply_markup` не передаётся.

- [ ] **Step 3: Создать `nemus/core/telegram_ux.py`**

Реализовать:

```python
def start_text(full_name: str, aliases: str, hints: bool) -> str: ...
def help_text(hints: bool) -> str: ...
def examples_text() -> str: ...
def main_menu_rows(hints: bool) -> list[list[tuple[str, str]]]: ...
```

Требования к тексту:

- `start_text(..., True)` содержит блок «Примеры» и четыре примера из спеки; `False` — тот же экран без блока.
- `help_text(True)` содержит «Фишка: пришли видео…»; `False` — без него и без блока примеров.
- `examples_text()` содержит «PDF» и минимум четыре примера.
- `main_menu_rows(True)` возвращает одну строку:
  `[("📋 Примеры задач", "ux:examples"), ("🧰 Что я умею", "ux:help")]`.
- `main_menu_rows(False)` возвращает `[]`.

- [ ] **Step 4: Подключить тексты и клавиатуру в боте**

В `telegram_bot.py` импортировать функции из `nemus.core.telegram_ux`.

Добавить:

```python
@property
def _hints_enabled(self) -> bool:
    """Включены ли подсказки глобально."""
    return bool(getattr(self.cfg, "hints_enabled", True))

def _keyboard(self, rows: list[list[tuple[str, str]]]) -> Any | None:
    """Собирает inline-клавиатуру из пар «подпись, callback»."""
    if not rows:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )
```

Расширить `start_message`:

```python
async def start_message(
    bot: Any,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    reply_markup: Any | None = None,
) -> None:
```

В цикле чанков передавать `reply_markup` только на последнем чанке:

```python
"reply_markup": reply_markup if index == len(chunks) - 1 else None,
```

В `_cmd_start` заменить текст на `start_text(self._names()[1], self._aliases(), self._hints_enabled)` и передать `reply_markup=self._keyboard(main_menu_rows(self._hints_enabled))`.

В `_cmd_help` заменить текст на `help_text(self._hints_enabled)` и передать такую же клавиатуру.

- [ ] **Step 5: Добавить callback-роутинг**

В `_on_callback` перед разбором `tg:`:

```python
data = query.data or ""
if data.startswith("ux:"):
    await self._on_ux_callback(update, query)
    return
```

Реализовать:

```python
async def _on_ux_callback(self, update: Any, query: Any) -> None:
    """Кнопки подсказок: помощь и примеры."""
    action = (query.data or "")[3:]
    if action == "help":
        await query.answer()
        await start_message(
            self._app.bot,
            query.message.chat_id,
            help_text(self._hints_enabled),
            reply_markup=self._keyboard(main_menu_rows(self._hints_enabled)),
        )
        return
    if action == "examples":
        if not self._hints_enabled:
            await query.answer("Подсказки выключены в настройках", show_alert=True)
            return
        await query.answer()
        await start_message(
            self._app.bot,
            query.message.chat_id,
            examples_text(),
            reply_markup=self._keyboard(main_menu_rows(True)),
        )
        return
    await query.answer("Кнопка устарела")
```

Не ломать существующий `tg:...`: его проверка должна идти после `ux:`.

- [ ] **Step 6: Прогнать тесты и проверки**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: PASS.

Run: `ruff check .`
Run: `python -m compileall -q nemus scripts`
Expected: без ошибок.

- [ ] **Step 7: Коммит**

```bash
git add nemus/core/telegram_ux.py nemus/core/telegram_bot.py scripts/test_telegram.py
git commit -m "feat: показать примеры в Telegram"
```

---

### Task 3: Кнопки действий под вложениями

**Files:**
- Modify: `nemus/core/telegram_ux.py`
- Modify: `nemus/core/telegram_bot.py:471-585`, `nemus/core/telegram_bot.py:1286-1387`, `nemus/core/telegram_bot.py:1548-1648`, `nemus/core/telegram_bot.py:1704-1744`
- Test: `scripts/test_telegram.py:541-634`, `scripts/test_telegram.py:637-715`, `scripts/test_telegram.py:1052-1081`

**Interfaces:**
- Consumes: `start_message(..., reply_markup=...)`, `_keyboard` из Task 2.
- Produces: `attachment_action_rows(kind: str) -> list[list[tuple[str, str]]]`
- Produces: `attachment_prompt(action: str) -> str | None`
- Produces: `_attachment_question(paths: list[Path], short: bool = False) -> str`
- Produces: `TelegramBot._pending_attachments: dict[int, tuple[str, list[str], float]]`
- Produces: `TelegramBot._remember_pending_attachment(chat_id: int, text: str, images: list[str], at: float | None = None) -> None`
- Produces: `TelegramBot._peek_pending_attachment(chat_id: int) -> tuple[str, list[str]]`
- Produces: `TelegramBot._take_pending_attachment(chat_id: int) -> tuple[str, list[str]]`
- Produces: `TelegramBot._clear_pending_attachments(chat_id: int | None = None) -> None`
- Produces: `TelegramBot._run_task_for(chat_id: int, user_id: int | None, message_id: int | None, text: str, images: list[str] | None = None) -> None`
- Produces: `_on_ux_callback` дополнительно обрабатывает `ux:attach:<action>`.

- [ ] **Step 1: Написать падающие тесты**

Добавить импорт:

```python
from nemus.core.telegram_ux import attachment_action_rows, attachment_prompt
```

Добавить тесты:

```python
def test_attachment_actions() -> None:
    """Для каждого типа вложения есть понятные кнопки и промпт."""
    photo = attachment_action_rows("photo")
    assert any("Описать" in label for row in photo for label, _ in row)
    assert any(data == "ux:attach:photo_describe" for row in photo for _, data in row)
    assert "кружок" in attachment_prompt("video_note").lower()
    assert "отчёт" in attachment_prompt("document_report").lower()
    assert attachment_prompt("unknown") is None


def test_pending_attachments_are_per_chat() -> None:
    """Вложение одного чата не забирает другой чат."""
    bot = object.__new__(TelegramBot)
    bot._pending_attachments = {}
    bot._remember_pending_attachment(1, "чат 1", ["img1"])
    bot._remember_pending_attachment(2, "чат 2", ["img2"])

    assert bot._peek_pending_attachment(1)[0] == "чат 1"
    assert bot._peek_pending_attachment(2)[0] == "чат 2"
    assert bot._take_pending_attachment(1)[0] == "чат 1"
    assert bot._peek_pending_attachment(1) == ("", [])
    assert bot._peek_pending_attachment(2)[0] == "чат 2"
    bot._clear_pending_attachments(2)
    assert bot._peek_pending_attachment(2) == ("", [])
```

Добавить тест callback вложения:

```python
def test_attachment_callback_runs_task() -> None:
    """Кнопка под фото сразу запускает задачу с этим фото."""
    import asyncio

    class _Query:
        data = "ux:attach:photo_describe"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(chat_id=42, message_id=5)

        def __init__(self) -> None:
            self.answers: list[str] = []
            self.edits: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.edits.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot._pending_attachments = {}
    bot._remember_pending_attachment(42, "Пользователь прислал файл(ы):\n- photo.jpg", [])
    query = _Query()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    seen: list[dict] = []

    async def _run_task_for(**kwargs: object) -> None:
        seen.append(kwargs)

    bot._run_task_for = _run_task_for  # type: ignore[method-assign]
    asyncio.run(bot._on_callback(update, None))
    assert seen and "photo.jpg" in seen[0]["text"]
    assert "Опиши" in seen[0]["text"]
    assert bot._peek_pending_attachment(42) == ("", [])

    stale = _Query()
    asyncio.run(bot._on_callback(
        SimpleNamespace(
            callback_query=stale,
            effective_user=stale.from_user,
            effective_chat=SimpleNamespace(id=42, type="private"),
        ),
        None,
    ))
    assert len(seen) == 1, "старая кнопка не должна запускать вторую задачу"
    assert "уже не актуально" in stale.answers[-1]
```

Обновить `test_attachment_without_caption`:

- `_Message.reply_text` должен сохранять `kwargs`, а не только текст:
  ```python
  self.replies.append((text, kwargs))
  ```
- после `_on_media` проверить, что у вопроса есть `reply_markup`.
- проверить, что при включённых подсказках текст вопроса короткий (начинается с «Я получила изображение. Что с ним сделать?»).
- проверить, что `bot._peek_pending_attachment(42)` видит путь, а не глобальное поле.

Обновить `test_stt_failure_does_not_ask_attachment`: заменить `bot._pending_attachment == ""` на `bot._peek_pending_attachment(42) == ("", [])`.

Добавить тесты в `main()`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: FAIL — нет `attachment_action_rows`, `_pending_attachments`, `_run_task_for`.

- [ ] **Step 3: Добавить действия вложений в `telegram_ux.py`**

Реализовать словарь действий:

```python
_ATTACHMENT_ACTIONS = {
    "photo_describe": ("📝 Описать", "Опиши, что на этом изображении."),
    "photo_ocr": ("🔎 Распознать текст", "Распознай текст на изображении и пришли его текстом."),
    "photo_what": ("❓ Что на фото?", "Что на этом фото? Опиши главное."),
    "video_note": ("⭕ Сделать кружок", "Сделай из этого видео кружок."),
    "video_audio": ("🔊 Извлечь звук", "Извлеки звук из этого видео и пришли аудиофайлом."),
    "video_trim": ("✂️ Обрезать", "Обрежь это видео до 30 секунд."),
    "audio_transcribe": ("📝 Распознать речь", "Распознай речь в этом аудио и пришли текст."),
    "audio_what": ("❓ Что здесь?", "Кратко опиши, что это за аудио, и распознай речь, если можешь."),
    "document_read": ("📖 Прочитать", "Прочитай этот файл и кратко перескажи."),
    "document_report": ("📄 Сделать отчёт", "Сделай краткий отчёт по этому файлу."),
    "document_send": ("📤 Отправить обратно", "Обработай этот файл и отправь результат обратно."),
}
```

`attachment_action_rows(kind)` возвращает строки по типу:

- `photo`: `photo_describe`, `photo_ocr` в первой строке, `photo_what` во второй.
- `video`: `video_note`, `video_audio` в первой, `video_trim` во второй.
- `audio`: `audio_transcribe`, `audio_what`.
- `document`: `document_read`, `document_report` в первой, `document_send` во второй.
- иначе `[]`.

Callback-данные: `f"ux:attach:{key}"`.

`attachment_prompt(action)` возвращает промпт или `None`.

- [ ] **Step 4: Перевести вложения на per-chat хранение**

В `__init__` заменить три поля `_pending_attachment*` на:

```python
#: вложение без подписи по чатам: текст, изображения, время
self._pending_attachments: dict[int, tuple[str, list[str], float]] = {}
```

Реализовать:

```python
def _remember_pending_attachment(
    self, chat_id: int, text: str, images: list[str], at: float | None = None
) -> None:
    self._pending_attachments[chat_id] = (
        text,
        list(images),
        time.time() if at is None else at,
    )

def _peek_pending_attachment(self, chat_id: int) -> tuple[str, list[str]]:
    item = self._pending_attachments.get(chat_id)
    if item is None:
        return "", []
    text, images, at = item
    if time.time() - at > ATTACHMENT_CONTEXT_TTL:
        self._pending_attachments.pop(chat_id, None)
        return "", []
    return text, list(images)

def _take_pending_attachment(self, chat_id: int) -> tuple[str, list[str]]:
    text, images = self._peek_pending_attachment(chat_id)
    self._pending_attachments.pop(chat_id, None)
    return text, images

def _clear_pending_attachments(self, chat_id: int | None = None) -> None:
    if chat_id is None:
        self._pending_attachments.clear()
    else:
        self._pending_attachments.pop(chat_id, None)
```

Обновить вызовы:

- `_handle_text`: `self._take_pending_attachment(chat_id)`.
- `_handle_media` с подписью: `self._take_pending_attachment(chat_id)`.
- `_handle_media` без подписи: `self._remember_pending_attachment(chat_id, _attachment_context(saved), image_urls)`.
- `_cmd_new`: `self._clear_pending_attachments(update.effective_chat.id)`.

- [ ] **Step 5: Показывать кнопки под вложением**

В `_handle_media` без подписи:

```python
rows = (
    attachment_action_rows(guess_kind(saved[0]))
    if len(saved) == 1 and self._hints_enabled
    else []
)
await message.reply_text(
    _attachment_question(saved, short=bool(rows)),
    reply_markup=self._keyboard(rows),
)
```

Расширить `_attachment_question(paths, short: bool = False)`. При `short=True` и одном файле вернуть короткий вопрос из спеки:

- фото: `Я получила изображение. Что с ним сделать?`
- видео: `Я получила видео. Что с ним сделать?`
- аудио: `Я получила аудио. Что с ним сделать?`
- файл: `Я получила файл «{path.name}». Что с ним сделать?`

Для нескольких файлов кнопок нет — остаётся текущий текстовый вопрос.

- [ ] **Step 6: Вынести запуск задачи в `_run_task_for`**

Сделать `_run_task` обёрткой:

```python
async def _run_task(
    self, update: Any, text: str, images: list[str] | None = None
) -> None:
    await self._run_task_for(
        chat_id=update.effective_chat.id,
        user_id=getattr(update.effective_user, "id", None),
        message_id=getattr(update.message, "message_id", None),
        text=text,
        images=images,
    )
```

Перенести тело текущего `_run_task` в `_run_task_for` с сигнатурой из блока Interfaces. Вместо `update.effective_chat.id` использовать `chat_id`, вместо `update.message.reply_text` для очереди — `self._app.bot.send_message(chat_id, "⏳ Ещё выполняю предыдущую задачу — это сообщение в очереди.")`.

- [ ] **Step 7: Обработать `ux:attach:<action>`**

В `_on_ux_callback` перед `ux:help`/`ux:examples` добавить ветку:

```python
if action.startswith("attach:"):
    key = action.split(":", 1)[1]
    prompt = attachment_prompt(key)
    chat_id = query.message.chat_id
    if prompt is None:
        await query.answer("Неизвестное действие")
        return
    pending, images = self._take_pending_attachment(chat_id)
    if not pending:
        await query.answer("Вложение уже не актуально — пришли его ещё раз", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return
    await query.answer("Запускаю")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    text = f"{pending}\n\nЗадача пользователя: {prompt}"
    await self._run_task_for(
        chat_id=chat_id,
        user_id=getattr(query.from_user, "id", None),
        message_id=getattr(query.message, "message_id", None),
        text=text,
        images=images,
    )
    return
```

- [ ] **Step 8: Прогнать тесты и проверки**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: PASS.

Run: `ruff check .`
Run: `python -m compileall -q nemus scripts`
Expected: без ошибок.

- [ ] **Step 9: Коммит**

```bash
git add nemus/core/telegram_ux.py nemus/core/telegram_bot.py scripts/test_telegram.py
git commit -m "feat: добавить кнопки действий для вложений"
```

---

### Task 4: Первая подсказка, ошибки, повтор и статус

**Files:**
- Modify: `nemus/core/telegram_ux.py`
- Modify: `nemus/core/telegram_bot.py:340-408`, `nemus/core/telegram_bot.py:442-507`, `nemus/core/telegram_bot.py:1156-1245`, `nemus/core/telegram_bot.py:1548-1648`, `nemus/core/telegram_bot.py:1704-1744`
- Test: `scripts/test_telegram.py:435-538`, `scripts/test_telegram.py:861-954`, `scripts/test_telegram.py:1052-1081`

**Interfaces:**
- Consumes: `_run_task_for`, `_pending_attachments`, `start_message(..., reply_markup=...)` из Task 3.
- Produces: `HINT_SHOWN_PREFIX = "telegram_hint_shown:"`, `HINT_MUTED_PREFIX = "telegram_hints_muted:"`
- Produces: `first_hint_text() -> str`, `first_hint_rows() -> list[list[tuple[str, str]]]`
- Produces: `error_text(error: str) -> str`, `error_rows(can_retry: bool) -> list[list[tuple[str, str]]]`
- Produces: `TelegramBot._hints_shown: set[int]`, `_hints_muted: set[int]`
- Produces: `_hint_shown(chat_id) -> bool`, `_mark_hint_shown(chat_id) -> None`, `_hint_muted(chat_id) -> bool`, `_set_hint_muted(chat_id, muted: bool) -> None`, `_hints_active(chat_id) -> bool`
- Produces: `_maybe_show_first_hint(chat_id) -> None`
- Produces: `_last_tasks: dict[int, tuple[str, list[str] | None, int | None, int | None]]`
- Produces: `_error_keyboard(chat_id) -> Any | None`
- Produces: `_status_text(chat_id: int | None = None) -> str`
- Produces: `_on_ux_callback` дополнительно обрабатывает `ux:mute`, `ux:new`, `ux:status`, `ux:retry`.
- Produces: `_Progress.fail(text: str, reply_markup: Any | None = None) -> None`

- [ ] **Step 1: Написать падающие тесты**

Добавить импорт:

```python
from nemus.core.telegram_bot import _Progress
from nemus.core.telegram_ux import error_rows, error_text, first_hint_rows
```

Добавить тест первой подсказки и мьюта:

```python
def test_first_hint_once_and_mute() -> None:
    """Первая подсказка показывается один раз; «Больше не показывать» молчит."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append(text)

    class _Memory:
        def __init__(self) -> None:
            self.meta: dict[str, str] = {}

        def get_meta(self, key: str, default: str | None = None) -> str | None:
            return self.meta.get(key, default)

        def set_meta(self, key: str, value: str) -> None:
            self.meta[key] = value

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.memory = _Memory()
    bot._app = SimpleNamespace(bot=_FakeBot())
    bot._hints_shown = set()
    bot._hints_muted = set()

    asyncio.run(bot._maybe_show_first_hint(7))
    asyncio.run(bot._maybe_show_first_hint(7))
    assert len(bot._app.bot.sent) == 1, bot._app.bot.sent
    assert bot.memory.meta["telegram_hint_shown:7"] == "1"

    bot._set_hint_muted(7, True)
    asyncio.run(bot._maybe_show_first_hint(7))
    assert len(bot._app.bot.sent) == 1
```

Добавить тест ошибок и `_Progress`:

```python
def test_error_text_and_progress_keyboard() -> None:
    """Ошибка короткая, без стектрейса, с кнопками восстановления."""
    import asyncio

    text = error_text("Traceback (most recent call last):\nFile \"x.py\", line 1\nValueError: плохой файл")
    assert "плохой файл" in text and "Traceback" not in text
    rows = error_rows(True)
    assert any(data == "ux:retry" for row in rows for _, data in row)
    assert not any(data == "ux:retry" for row in error_rows(False) for _, data in row)

    class _FakeBot:
        def __init__(self) -> None:
            self.edits: list[tuple[str, dict]] = []
            self.sent: list[tuple[str, dict]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> object:
            self.sent.append((text, kwargs))
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            self.edits.append((text, kwargs))

    fake = _FakeBot()
    progress = _Progress(fake, 7)
    marker = object()
    asyncio.run(progress.start("⏳ Думаю…"))
    asyncio.run(progress.fail("Не получилось", reply_markup=marker))
    assert fake.edits and fake.edits[-1][1]["reply_markup"] is marker
```

Добавить тест retry и new:

```python
def test_retry_and_new_callbacks() -> None:
    """Повтор берёт последнюю задачу, /new не сбрасывает работающий контекст."""
    import asyncio

    class _Query:
        def __init__(self, data: str) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=42)
            self.message = SimpleNamespace(chat_id=42, message_id=5)
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

        async def edit_message_text(self, text: str, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot._last_tasks = {42: ("найди отчёт", [], 11, 42)}
    bot._task_lock = None
    seen: list[dict] = []

    async def _run_task_for(**kwargs: object) -> None:
        seen.append(kwargs)

    bot._run_task_for = _run_task_for  # type: ignore[method-assign]

    query = _Query("ux:retry")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(update, None))
    assert seen and seen[0]["text"] == "найди отчёт"

    resets: list[int] = []
    bot._new_session_for_chat = lambda chat_id: resets.append(chat_id)  # type: ignore[method-assign]
    bot._task_lock = asyncio.Lock()
    asyncio.run(bot._task_lock.acquire())
    blocked = _Query("ux:new")
    blocked_update = SimpleNamespace(
        callback_query=blocked,
        effective_user=blocked.from_user,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    asyncio.run(bot._on_callback(blocked_update, None))
    assert not resets, "нельзя сбрасывать контекст во время задачи"
    assert "дождись" in blocked.answers[-1]
```

Добавить тест статуса:

```python
def test_status_context_lines() -> None:
    """Статус показывает реплики, выжимку и состояние подсказок."""
    bot = object.__new__(TelegramBot)
    bot.cfg = Config().telegram
    bot.memory = None
    bot.agent = SimpleNamespace(
        history=[
            SimpleNamespace(role="system"),
            SimpleNamespace(role="user"),
            SimpleNamespace(role="assistant"),
        ],
        _summary="краткая выжимка",
    )
    bot.core = SimpleNamespace(llm=SimpleNamespace(model="m", provider="p"))
    bot.username = "bot"
    bot.started_at = 0.0
    bot._hints_shown, bot._hints_muted = set(), set()
    text = bot._status_text(42)
    assert "реплик: 1" in text
    assert "сжатая выжимка" in text
    assert "подсказки: включены" in text
```

Обновить `test_ux_texts`: `main_menu_rows(True)` теперь содержит четыре кнопки:
`ux:examples`, `ux:help`, `ux:new`, `ux:status`.

Добавить все новые тесты в `main()`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: FAIL — нет `first_hint_text`, `error_text`, `_maybe_show_first_hint`, `_status_text`.

- [ ] **Step 3: Добавить тексты и строки кнопок**

В `telegram_ux.py`:

```python
HINT_SHOWN_PREFIX = "telegram_hint_shown:"
HINT_MUTED_PREFIX = "telegram_hints_muted:"

def first_hint_text() -> str: ...
def first_hint_rows() -> list[list[tuple[str, str]]]: ...
def error_text(error: str) -> str: ...
def error_rows(can_retry: bool) -> list[list[tuple[str, str]]]: ...
```

Требования:

- `first_hint_text()`: «💡 Я умею ещё работать с файлами и медиа. Показать примеры?»
- `first_hint_rows()`: `[("Показать примеры", "ux:examples"), ("Больше не показывать", "ux:mute")]`.
- `error_text` берёт последнюю непустую строку, убирает `Traceback`, `File "...", line ...`, префикс `SomeError:`, обрезает до 300 символов и возвращает:
  `"Не получилось выполнить задачу.\n\n<причина>\n\nПопробуй повторить или сформулируй проще."`
- `error_rows(True)`: `[("🔁 Повторить", "ux:retry"), ("🆕 Новый диалог", "ux:new"), ("❓ Помощь", "ux:help")]`.
- `error_rows(False)`: без `ux:retry`.
- `main_menu_rows(True)` расширить второй строкой `[("🆕 Новый диалог", "ux:new"), ("ℹ️ Статус", "ux:status")]`.

- [ ] **Step 4: Добавить состояние подсказок**

В `__init__`:

```python
self._hints_shown: set[int] = set()
self._hints_muted: set[int] = set()
self._last_tasks: dict[int, tuple[str, list[str] | None, int | None, int | None]] = {}
```

Реализовать:

```python
def _hint_shown(self, chat_id: int) -> bool: ...
def _mark_hint_shown(self, chat_id: int) -> None: ...
def _hint_muted(self, chat_id: int) -> bool: ...
def _set_hint_muted(self, chat_id: int, muted: bool) -> None: ...
def _hints_active(self, chat_id: int) -> bool: ...
```

Правила:

- При наличии `self.memory` читать/писать `HINT_SHOWN_PREFIX + str(chat_id)` и `HINT_MUTED_PREFIX + str(chat_id)`; значения `"1"`/`"0"`.
- Без памяти — использовать `self._hints_shown` и `self._hints_muted`.
- `_hints_active(chat_id)` = `self._hints_enabled and not self._hint_muted(chat_id)`.

В `_handle_media` заменить условие показа кнопок вложений с `self._hints_enabled` на `self._hints_active(chat_id)`, чтобы мьют в чате скрывал и их.

В `_cmd_start`:

```python
chat_id = update.effective_chat.id
self._set_hint_muted(chat_id, False)
self._mark_hint_shown(chat_id)
```

В `_cmd_new` дополнительно очистить последнюю задачу: `self._last_tasks.pop(update.effective_chat.id, None)`.

- [ ] **Step 5: Показывать первую подсказку**

Добавить:

```python
async def _maybe_show_first_hint(self, chat_id: int) -> None:
    if not self._hints_active(chat_id) or self._hint_shown(chat_id):
        return
    self._mark_hint_shown(chat_id)
    await start_message(
        self._app.bot,
        chat_id,
        first_hint_text(),
        reply_markup=self._keyboard(first_hint_rows()),
    )
```

В `_run_task_for` после успешного `start_message` с финальным ответом:

```python
await self._maybe_show_first_hint(chat_id)
```

- [ ] **Step 6: Добавить восстановление после ошибок**

Расширить `_Progress.fail`:

```python
async def fail(self, text: str, reply_markup: Any | None = None) -> None:
    if self.message_id is None:
        await start_message(self.bot, self.chat_id, text, reply_markup=reply_markup)
        return
    try:
        await self.bot.edit_message_text(
            f"❌ {text}"[:3900],
            chat_id=self.chat_id,
            message_id=self.message_id,
            reply_markup=reply_markup,
        )
    except Exception:  # noqa: BLE001
        await start_message(self.bot, self.chat_id, f"❌ {text}", reply_markup=reply_markup)
```

В `_run_task_for`:

- В начале, до `async with self._task_lock`, сохранить:
  ```python
  self._last_tasks[chat_id] = (text, list(images or []), message_id, user_id)
  ```
- При ошибке:
  ```python
  await progress.fail(
      error_text(self._mask(error)),
      reply_markup=self._error_keyboard(chat_id),
  )
  ```
- При пустом финальном ответе — то же с `self._error_keyboard(chat_id)`.

Реализовать:

```python
def _error_keyboard(self, chat_id: int) -> Any | None:
    return self._keyboard(error_rows(chat_id in self._last_tasks))
```

- [ ] **Step 7: Обработать новые callback-кнопки**

В `_on_ux_callback` добавить:

- `mute`: `_set_hint_muted(chat_id, True)`, `_mark_hint_shown(chat_id)`, убрать клавиатуру, `query.answer("Больше не буду показывать подсказки в этом чате")`.
- `new`: если `_task_lock` занят — `query.answer("Сначала дождись завершения текущей задачи", show_alert=True)` и выйти. Иначе `_clear_pending_attachments(chat_id)`, `_new_session_for_chat(chat_id)`, `self._last_tasks.pop(chat_id, None)`, `query.answer("Контекст очищен")`, отправить `start_message(..., "🧹 Контекст очищен — начинаем заново.", reply_markup=...)`.
- `status`: `query.answer()`, отправить `_status_text(chat_id)`.
- `retry`: взять `self._last_tasks.get(chat_id)`. Если нет — `query.answer("Пока нечего повторять", show_alert=True)`. Иначе `query.answer("Повторяю")` и вызвать `_run_task_for` с сохранёнными `text`, `images`, `message_id`, `user_id`.

- [ ] **Step 8: Добавить статус**

Реализовать `_status_text(chat_id: int | None = None) -> str`:

- Если `chat_id` и задача не выполняется, попробовать `_prepare_session(chat_id)` (ошибку залогировать и продолжить). Для проверки занятости использовать `getattr(self, "_task_lock", None)`, чтобы тесты с `object.__new__` не падали.
- `turns = sum(1 for m in getattr(self.agent, "history", []) if getattr(m, "role", "") == "user")`.
- `summary = bool(getattr(self.agent, "_summary", ""))`; если есть `self.memory`, проверить `memory.get_summary()`.
- `hints = "включены" if self._hints_enabled else "выключены"`; если `chat_id` и `_hint_muted(chat_id)`, добавить `" (для этого чата выключены)"`.
- Вернуть тот же блок «Состояние», что сейчас, плюс:
  ```text
  • диалог: {turns} реплик
  • память: {"сжатая выжимка" if summary else "обычная"}
  • подсказки: {hints}
  ```
  Для строки «сейчас» использовать `getattr(self, "_chat_id", None)`, а не прямое обращение.
- `_cmd_status` заменить сборку текста на `self._status_text(update.effective_chat.id)`.

- [ ] **Step 9: Прогнать тесты и проверки**

Run: `$env:PYTHONIOENCODING='utf-8'; python scripts/test_telegram.py`
Expected: PASS.

Run: `ruff check .`
Run: `python -m compileall -q nemus scripts`
Expected: без ошибок.

- [ ] **Step 10: Коммит**

```bash
git add nemus/core/telegram_ux.py nemus/core/telegram_bot.py scripts/test_telegram.py
git commit -m "feat: добавить подсказки и восстановление после ошибок"
```

---

### Task 5: Документация и финальная проверка

**Files:**
- Modify: `README.md:101-110`
- Modify: `docs/superpowers/specs/2026-11-11-telegram-ux-design.md:1-5`

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: документация настройки и статус «реализовано».

- [ ] **Step 1: Обновить документацию**

В `README.md` в разделе Telegram добавить:

```markdown
Подсказки и примеры в чате включаются галочкой **«Показывать подсказки и
примеры»** на вкладке Telegram в настройках. При выключении бот остаётся
рабочим: `/help`, подтверждения, прогресс и восстановление после ошибок
доступны.
```

В шапке спеки заменить статус на:

```markdown
**Статус:** реализовано 2026-11-11
```

- [ ] **Step 2: Прогнать профильные тесты**

Run:

```powershell
$env:PYTHONIOENCODING='utf-8'
python scripts/test_telegram.py
python scripts/test_context_summary.py
python scripts/test_sessions.py
python scripts/test_headless.py
```

Expected: все заканчиваются `... TESTS PASSED` или `OK`.

- [ ] **Step 3: Прогнать линтер и компиляцию**

Run: `ruff check .`
Run: `python -m compileall -q nemus scripts`
Expected: без ошибок.

- [ ] **Step 4: Коммит**

```bash
git add README.md docs/superpowers/specs/2026-11-11-telegram-ux-design.md
git commit -m "docs: отметить реализацию подсказок Telegram"
```

---

## Final Verification

После всех задач рабочее дерево должно быть чистым, а `git log --oneline -6` — содержать пять коммитов этой фичи. Живую проверку Telegram и Docker не выполняем.
