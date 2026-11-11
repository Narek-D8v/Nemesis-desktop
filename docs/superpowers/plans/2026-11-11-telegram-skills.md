# Telegram Skills List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать в Telegram, какие скиллы, команды плагинов и MCP-серверы установлены, и дать короткую справку по каждому скиллу.

**Architecture:** Чистые тексты и группировка живут в `nemus/core/telegram_ux.py`. `TelegramBot` только собирает установленные скиллы через существующий `load_skills`, имена MCP-серверов из конфига и подключает команду `/skills`, callback `ux:skills` и кнопку в меню.

**Tech Stack:** Python 3.13, python-telegram-bot, pydantic, существующий `scripts/test_telegram.py`.

**Spec:** `docs/superpowers/specs/2026-11-11-telegram-skills-design.md`

## Global Constraints

- Весь пользовательский текст — по-русски, коротко и без технических внутренностей.
- `/skills` работает при выключенных подсказках и при мьюте чата: это явная справка, а не автоматическая подсказка.
- Кнопка «🧩 Скиллы» показывается только когда подсказки активны.
- MCP-серверы не проверяются по сети: только `enabled`-серверы из конфига.
- Описания скиллов не переводятся и обрезаются до 300 символов.
- Длинный список режется существующим `start_message` / `html_chunks`.
- Коммиты локальные, без `push` без отдельного разрешения.
- После каждого коммита: `ruff check .` и `python -m compileall -q nemus scripts` чистые.
- Живой Telegram и Docker не проверяются.

## Review Focus

- `skills.dir` указывает на отсутствующую или недоступную папку: `/skills` не падает, показывает пустой список и MCP-блок.
- Подсказки выключены глобально или чат замьючен: `/skills` и `ux:skills` продолжают работать, кнопки в меню нет.
- Команда плагина содержит `:`: она попадает в группу своего плагина и находится через `/skills плагин:команда`.
- Список очень длинный: сообщение режется на части, текст не теряется и бот не падает.
- MCP выключен, список пуст или сервер недоступен: бот не делает сетевых вызовов, пишет «не подключены» или только имена включённых серверов.

---

### Task 1: Тексты списка и справки

**Files:**
- Modify: `nemus/core/telegram_ux.py`
- Test: `scripts/test_telegram.py`

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces:
  - `skills_list_text(entries: list[tuple[str, str, str]], mcp_servers: list[str]) -> str`, где элемент — `(имя, плагин, kind)`, `plugin=""` — без плагина.
  - `skill_details_text(name: str, description: str, plugin: str, kind: str) -> str`.
  - `skill_not_found_text() -> str`.

- [ ] **Step 1: Написать падающие тесты**

В импорт из `nemus.core.telegram_ux` в `scripts/test_telegram.py` вставить после
`main_menu_rows` и перед `start_text`:

```python
    skill_details_text,
    skill_not_found_text,
    skills_list_text,
```

Добавить тесты рядом с `test_ux_texts`:

```python
def test_skills_text() -> None:
    """Список скиллов группируется по плагинам, MCP идёт отдельно."""
    entries = [
        ("brainstorming", "superpowers", "skill"),
        ("hyperframes", "hyperframes", "skill"),
        ("pdf-reader", "", "skill"),
        ("superpowers:review", "superpowers", "command"),
    ]
    text = skills_list_text(entries, ["git", "time"])
    assert "**Установлено скиллов: 4**" in text
    assert text.index("**hyperframes**") < text.index("**superpowers**")
    assert text.index("**superpowers**") < text.index("**Без плагина**")
    assert "/brainstorming, /superpowers:review" in text
    assert "/pdf-reader" in text
    assert "git, time" in text
    assert "Подробнее: /skills <имя>" in text

    empty = skills_list_text([], [])
    assert "Скиллы не установлены" in empty
    assert "не подключены" in empty
    assert "Подробнее" not in empty

    many = skills_list_text(
        [(f"skill-{i:03d}-" + "x" * 40, "", "skill") for i in range(120)],
        [],
    )
    assert len(split_message(many)) > 1, "длинный список должен резаться"


def test_skill_details_text() -> None:
    """Справка показывает имя, плагин, тип, описание и команду запуска."""
    text = skill_details_text("pdf-reader", "Read PDFs", "", "skill")
    assert "**/pdf-reader**" in text
    assert "Плагин: нет" in text
    assert "Тип: скилл" in text
    assert "Read PDFs" in text
    assert "Как запустить: `/pdf-reader`" in text

    command = skill_details_text(
        "superpowers:review", "Review code", "superpowers", "command"
    )
    assert "Плагин: superpowers" in command
    assert "Тип: команда плагина" in command
    assert "`/superpowers:review`" in command

    assert "Без описания." in skill_details_text("x", "", "", "skill")
    assert "/skills" in skill_not_found_text()
```

Добавить вызовы в `main()`:

```python
    test_skills_text()
    test_skill_details_text()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `venv\Scripts\python.exe scripts\test_telegram.py`
Expected: FAIL на импорте — `skills_list_text` ещё нет.

- [ ] **Step 3: Реализовать чистые тексты**

В `nemus/core/telegram_ux.py` добавить три функции.

`skills_list_text`:
- заголовок ровно `**Установлено скиллов: {len(entries)}**`;
- группы: `plugin or "Без плагина"`;
- плагины сортируются по имени без учёта регистра, `Без плагина` всегда последняя;
- имена внутри группы сортируются без учёта регистра;
- строка группы: `**{группа}**`, затем `/имя` через запятую;
- блок `**MCP-серверы**`, затем `", ".join(mcp_servers)` или `не подключены`;
- если записей нет — строка `Скиллы не установлены` и без строки `Подробнее`;
- если записи есть — в конце `Подробнее: /skills <имя>`.

`skill_details_text`:
- заголовок `**/{name}**`;
- `Плагин: {plugin}` или `Плагин: нет`;
- `Тип: скилл` или `Тип: команда плагина` при `kind == "command"`;
- описание `description.strip()[:300]` или `Без описания.`;
- `Как запустить: `/{name}``.

`skill_not_found_text` возвращает ровно:

```text
Не нашёл такой скилл. Список: /skills
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `venv\Scripts\python.exe scripts\test_telegram.py`
Expected: `TELEGRAM TESTS PASSED`.

- [ ] **Step 5: Проверка и коммит**

Run: `venv\Scripts\python.exe -m ruff check .`
Run: `venv\Scripts\python.exe -m compileall -q nemus scripts`
Expected: без ошибок.

```bash
git add nemus/core/telegram_ux.py scripts/test_telegram.py
git commit -m "feat: добавить тексты списка скиллов"
```

---

### Task 2: Команда `/skills`, кнопка и справка

**Files:**
- Modify: `nemus/core/telegram_bot.py`
- Modify: `scripts/test_telegram.py`
- Modify: `README.md`
- Test: `scripts/test_telegram.py`

**Interfaces:**
- Consumes:
  - `skills_list_text(entries, mcp_servers)`
  - `skill_details_text(name, description, plugin, kind)`
  - `skill_not_found_text()`
  - `load_skills(directory, disabled)` и `skill_plugin(path, directory)` из `nemus.core.skills`.
- Produces:
  - `TelegramBot._skills() -> list[Any]`
  - `TelegramBot._skill_plugin_name(skill: Any) -> str`
  - `TelegramBot._mcp_names() -> list[str]`
  - `TelegramBot._find_skill(name: str) -> Any | None`
  - `TelegramBot._send_skills(chat_id: int, reply_to: int | None = None, reply_markup: Any | None = None) -> None`
  - `TelegramBot._cmd_skills(update: Any, context: Any) -> None`

- [ ] **Step 1: Написать падающие тесты**

Обновить `test_ux_texts`:

```python
    assert "/skills" in with_hints
    assert "/skills" in without_hints
    assert {data for row in menu for _, data in row} == {
        "ux:examples",
        "ux:help",
        "ux:skills",
        "ux:new",
        "ux:status",
    }
```

Добавить после `test_ux_texts`:

```python
def test_skills_command_and_callback() -> None:
    """Команда /skills, справка, callback и меню видят установленные скиллы."""
    import asyncio

    class _FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []
            self.commands: list[object] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            self.sent.append((text, kwargs))

        async def set_my_commands(self, commands: list[object]) -> None:
            self.commands = commands

    class _Query:
        data = "ux:skills"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace(chat_id=42, message_id=5)

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **kwargs: object) -> None:
            pass

    cfg = Config()
    cfg.telegram.allowed_users = [42]
    cfg.mcp.enabled = False
    bot = object.__new__(TelegramBot)
    bot.cfg = cfg.telegram
    bot.config = cfg
    bot.memory = None
    bot._hints_shown = set()
    bot._hints_muted = set()
    bot._last_tasks = {}
    bot._skill_names = set()
    fake = _FakeBot()
    bot._app = SimpleNamespace(bot=fake)

    skill = SimpleNamespace(
        name="pdf-reader",
        description="Read PDFs",
        path=Path("pdf-reader"),
        kind="skill",
    )
    command = SimpleNamespace(
        name="superpowers:review",
        description="Review code",
        path=Path("superpowers/commands/review.md"),
        kind="command",
    )
    bot._skills = lambda: [skill, command]  # type: ignore[method-assign]
    bot._skill_plugin_name = lambda item: (  # type: ignore[method-assign]
        "superpowers" if item.kind == "command" else ""
    )
    bot._mcp_names = lambda: ["git"]  # type: ignore[method-assign]

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(message_id=11, text="привет"),
    )

    async def _reply(text: str, **kwargs: object) -> None:
        fake.sent.append((text, kwargs))

    update.message.reply_text = _reply  # type: ignore[attr-defined]

    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=[])))
    text, kwargs = fake.sent[-1]
    assert "**Установлено скиллов: 2**" in text
    assert "/pdf-reader" in text and "/superpowers:review" in text
    assert "git" in text
    assert kwargs.get("reply_markup") is not None

    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=["superpowers:review"])))
    text, _ = fake.sent[-1]
    assert "Тип: команда плагина" in text
    assert "Review code" in text
    assert "`/superpowers:review`" in text

    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=["missing"])))
    assert "Не нашёл такой скилл" in fake.sent[-1][0]

    bot._hints_muted = {42}
    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=[])))
    assert fake.sent
    assert fake.sent[-1][1].get("reply_markup") is None

    cfg.telegram.hints_enabled = False
    bot._hints_muted = set()
    fake.sent.clear()
    asyncio.run(bot._cmd_skills(update, SimpleNamespace(args=[])))
    assert fake.sent
    assert fake.sent[-1][1].get("reply_markup") is None

    fake.sent.clear()
    query = _Query()
    asyncio.run(
        bot._on_callback(
            SimpleNamespace(
                callback_query=query,
                effective_user=query.from_user,
                effective_chat=SimpleNamespace(id=42, type="private"),
            ),
            None,
        )
    )
    assert fake.sent and "/pdf-reader" in fake.sent[-1][0]

    fake.sent.clear()
    fake.commands.clear()
    asyncio.run(bot._cmd_unknown(update, None))
    assert "/skills" in fake.sent[-1][0]

    asyncio.run(bot._set_commands())
    assert any(getattr(item, "command", "") == "skills" for item in fake.commands)
    print("ok  /skills, справка, callback и меню показывают скиллы")


def test_skills_helpers_are_safe() -> None:
    """Нет папки скиллов — пустой список; MCP не трогается по сети."""
    from unittest.mock import patch

    cfg = Config()
    bot = object.__new__(TelegramBot)
    bot.config = cfg

    with patch("nemus.core.skills.load_skills", side_effect=OSError("denied")):
        assert bot._skills() == []

    cfg.mcp.enabled = False
    assert bot._mcp_names() == []
    cfg.mcp.enabled = True
    cfg.mcp.servers = [
        SimpleNamespace(name="git", enabled=True),
        SimpleNamespace(name="off", enabled=False),
    ]
    assert bot._mcp_names() == ["git"]
```

Добавить вызовы в `main()`:

```python
    test_skills_command_and_callback()
    test_skills_helpers_are_safe()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `venv\Scripts\python.exe scripts\test_telegram.py`
Expected: FAIL — у `TelegramBot` нет `_cmd_skills`.

- [ ] **Step 3: Подключить команду и callback**

В `nemus/core/telegram_bot.py`:

1. Импортировать из `nemus.core.telegram_ux` после `main_menu_rows` и перед
   `start_text`: `skill_details_text`, `skill_not_found_text`, `skills_list_text`.

2. Добавить методы рядом с `_is_skill_command`:

```python
def _skills(self) -> list[Any]:
    """Установленные скиллы; битая папка не должна ломать /skills."""
    cfg = getattr(self.config, "skills", None)
    try:
        from nemus.core.skills import load_skills

        return load_skills(
            getattr(cfg, "dir", None) if cfg else None,
            disabled=getattr(cfg, "disabled", ()) if cfg else (),
        )
    except OSError as exc:
        logger.warning(f"Telegram: не удалось прочитать скиллы: {exc}")
        return []

def _skill_plugin_name(self, skill: Any) -> str:
    """Имя плагина скилла или пустая строка."""
    cfg = getattr(self.config, "skills", None)
    try:
        from nemus.core.skills import skill_plugin

        return skill_plugin(
            getattr(skill, "path", Path()),
            getattr(cfg, "dir", None) if cfg else None,
        )
    except OSError:
        return ""

def _mcp_names(self) -> list[str]:
    """Только включённые MCP-серверы из конфига, без сети."""
    mcp = getattr(self.config, "mcp", None)
    if mcp is None or not getattr(mcp, "enabled", False):
        return []
    return [
        str(server.name)
        for server in getattr(mcp, "servers", ())
        if getattr(server, "enabled", False)
    ]

def _find_skill(self, name: str) -> Any | None:
    """Точное имя скилла без учёта регистра; ведущий / не мешает."""
    wanted = (name or "").strip().lstrip("/").lower()
    for skill in self._skills():
        if str(getattr(skill, "name", "")).lower() == wanted:
            return skill
    return None

async def _send_skills(
    self,
    chat_id: int,
    reply_to: int | None = None,
    reply_markup: Any | None = None,
) -> None:
    """Список установленных скиллов и MCP-серверов."""
    entries = [
        (skill.name, self._skill_plugin_name(skill), getattr(skill, "kind", "skill"))
        for skill in self._skills()
    ]
    await start_message(
        self._app.bot,
        chat_id,
        skills_list_text(entries, self._mcp_names()),
        reply_to=reply_to,
        reply_markup=reply_markup,
    )

async def _cmd_skills(self, update: Any, context: Any) -> None:
    """`/skills` — список; `/skills имя` — короткая справка."""
    if not self._authorized(update):
        await self._deny(update)
        return
    chat_id = update.effective_chat.id
    args = list(getattr(context, "args", None) or ())
    keyboard = self._keyboard(main_menu_rows(self._hints_active(chat_id)))
    if not args:
        await self._send_skills(
            chat_id,
            reply_to=update.message.message_id,
            reply_markup=keyboard,
        )
        return
    skill = self._find_skill(str(args[0]))
    if skill is None:
        text = skill_not_found_text()
    else:
        text = skill_details_text(
            skill.name,
            getattr(skill, "description", ""),
            self._skill_plugin_name(skill),
            getattr(skill, "kind", "skill"),
        )
    await start_message(
        self._app.bot,
        chat_id,
        text,
        reply_to=update.message.message_id,
        reply_markup=keyboard,
    )
```

3. Зарегистрировать `CommandHandler("skills", self._cmd_skills)` рядом с
   `CommandHandler("help", ...)`.

4. В `_set_commands` добавить `BotCommand("skills", "Скиллы и плагины")`.

5. В `_cmd_unknown` заменить список на:
   `"Не знаю такой команды. Список: /start, /skills, /new, /status, /id, /help."`

6. В `_on_ux_callback` добавить ветку **до** проверки подсказок:

```python
if action == "skills":
    chat_id = query.message.chat_id
    await query.answer()
    await self._send_skills(
        chat_id,
        reply_markup=self._keyboard(main_menu_rows(self._hints_active(chat_id))),
    )
    return
```

Она должна работать и при выключенных подсказках, и при мьюте.

7. В `main_menu_rows` добавить строку:

```python
[
    ("🧩 Скиллы", "ux:skills"),
    ("🆕 Новый диалог", "ux:new"),
    ("ℹ️ Статус", "ux:status"),
],
```

8. В `start_text` и `help_text` добавить `/skills` в строку команд:
   `**Команды:** /skills — скиллы, /new — новый диалог, ...`.

9. В README после абзаца про подсказки добавить:
   `В чате: /skills — список установленных скиллов и MCP-серверов, /skills <имя> — что делает скилл.`

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `venv\Scripts\python.exe scripts\test_telegram.py`
Expected: `TELEGRAM TESTS PASSED`.

- [ ] **Step 5: Проверка и коммит**

Run: `venv\Scripts\python.exe -m ruff check .`
Run: `venv\Scripts\python.exe -m compileall -q nemus scripts`
Expected: без ошибок.

```bash
git add nemus/core/telegram_bot.py scripts/test_telegram.py README.md
git commit -m "feat: показать скиллы и плагины в Telegram"
```
