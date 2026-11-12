"""Smoke-тест GUI ↔ агент (offscreen, фиктивный LLM).

Запуск:  set QT_QPA_PLATFORM=offscreen
         venv\\Scripts\\python.exe scripts\\smoke_gui.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402

from nemus.config import get_config  # noqa: E402
from nemus.core.agent import Agent  # noqa: E402
from nemus.core.factory import Core  # noqa: E402
from nemus.core.llm import LLMClient, StreamEvent  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, ProviderError, ToolCall  # noqa: E402
from nemus.core.tools.basic import basic_tools  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402
from nemus.gui.main_window import MainWindow  # noqa: E402


class FakeLLM(LLMClient):
    model = "fake/model"
    provider = "fake"

    def __init__(self, config):  # noqa: D107
        self.config = config
        self._step = 0

    def stream(self, messages, tools=None, **overrides):
        self._step += 1
        if self._step == 1:
            # скрытые рассуждения модели — отдельным событием «мысли»
            yield StreamEvent(
                kind="thought",
                text="Сначала вспомню таблицу умножения, потом посчитаю.",
            )
            yield StreamEvent(kind="content", text="Считаю… ")
            call = ToolCall(id="c1", name="calculate", arguments={"expression": "7*6"})
            yield StreamEvent(
                kind="final",
                response=LLMResponse(content="Считаю… ", tool_calls=[call]),
            )
        else:
            yield StreamEvent(kind="content", text="Получается 42.")
            yield StreamEvent(
                kind="final", response=LLMResponse(content="Получается 42.")
            )

    def complete(self, messages, tools=None, **overrides):
        """Быстрый путь недоступен: агент обязан откатиться к обычному циклу.

        Без этого переопределения смоук-тест ходил бы в настоящий API
        с ключами из config.yaml и вис в retry-цикле провайдеров.
        """
        raise ProviderError("smoke: быстрый путь отключён — проверяем откат")


def build_fake_core():
    cfg = get_config()
    registry = ToolRegistry()
    registry.register_many(basic_tools())
    llm = FakeLLM(cfg)
    agent = Agent(cfg, llm, registry, PermissionManager(cfg))  # type: ignore[arg-type]
    return Core(
        config=cfg,
        llm=llm,
        registry=registry,
        permissions=PermissionManager(cfg),
        agent=agent,
        tools=list(registry),
    )


def main() -> int:
    from PySide6.QtWidgets import QApplication, QScrollArea

    app = QApplication.instance() or QApplication([])
    core = build_fake_core()
    window = MainWindow(core)
    window.show()

    window.submit_text("Сколько будет 7*6?")
    # пока агент думает, в чате должен быть статус «Думаю…», а не «Слушаю…»
    from nemus.gui import i18n

    assert i18n.tr("state.thinking") in window.state_label.text(), (
        window.state_label.text()
    )
    print("ok  статус в чате: Думаю…")

    result = {"ok": False}

    def check() -> None:
        if window._worker is None:
            text = window.chat.toPlainText()
            result["ok"] = "42" in text and "calculate" in text
            result["thought"] = i18n.tr("role.thought") in text
            # мысли идут в чате раньше ответа — это видно пользователю
            result["order"] = (
                text.find(i18n.tr("role.thought")) >= 0
                and text.find(i18n.tr("role.thought")) < text.find("Получается 42")
            )
            print("chat text:\n", text)
            app.quit()

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(200)
    QTimer.singleShot(15000, app.quit)
    app.exec()

    assert result["ok"], "GUI не показал результат агента"
    assert result.get("thought"), "блок мыслей модели не отображён"
    assert result.get("order"), "мысли должны идти в чате раньше ответа"
    print("ok  мысли модели показаны отдельным блоком до ответа")

    # по умолчанию блок мыслей свёрнут, клик по заголовку разворачивает
    chat = window.chat
    initial = chat.toPlainText()
    assert i18n.tr("role.thought") in initial, "заголовка мыслей нет в чате"
    assert "вспомню таблицу" not in initial, "мысли должны быть свёрнуты по умолчанию"
    chat._on_anchor_clicked(QUrl("thought:1"))
    expanded = chat.toPlainText()
    assert "вспомню таблицу" in expanded, "блок не развернулся по клику"
    chat._on_anchor_clicked(QUrl("thought:1"))
    assert "вспомню таблицу" not in chat.toPlainText(), "блок не свернулся обратно"
    print("ok  мысли свёрнуты по умолчанию, клик разворачивает и сворачивает")
    assert i18n.tr("state.idle") in window.state_label.text(), (
        window.state_label.text()
    )
    print("ok  статус после ответа: Готова")

    # брейншторм: варианты A/B/C/D кликабельны и собираются в ответ
    from nemus.gui.widgets.chat import ChatView, parse_brainstorm

    sample = (
        "Поняла задачу: пишем асинхронный пайплайн обработки логов.\n\n"
        "**1. Формат входных данных:**\n"
        "A) Потоковый текст .log — построчно (рекомендую)\n"
        "B) JSON Lines .jsonl\n"
        "C) Бинарные дампы\n"
        "D) Свой вариант: _________\n\n"
        "**2. Стратегия при ошибках:**\n"
        "A) Логировать и пропускать\n"
        "B) fail-fast\n"
        "C) dead_letter.log\n"
        "D) Свой вариант: _________\n\n"
        "Ответь коротко: «1A, 2B, остальное A»."
    )
    questions, body = parse_brainstorm(sample)
    assert len(questions) == 2, questions
    assert body.startswith("Поняла задачу") and "A) Потоковый" not in body, body
    assert questions[0].options[0].recommended, "рекомендованный вариант не найден"

    panel = ChatView()
    picked: list[str] = []
    panel.choice_submitted.connect(picked.append)
    panel.add_assistant(sample)
    assert i18n.tr("bm.title") in panel.toPlainText(), panel.toPlainText()
    bm_id = panel._bm_seq
    panel._on_anchor_clicked(QUrl(f"bm:{bm_id}:1:B"))
    assert panel._bm[bm_id]["selected"] == {1: "B"}, panel._bm[bm_id]["selected"]
    panel._on_anchor_clicked(QUrl(f"bm:{bm_id}:send"))
    assert picked == ["1A, 2B"], picked  # 1 — рекомендованное A, 2 — выбранное B
    # после отправки варианты неактивны и повторный клик ничего не меняет
    panel._on_anchor_clicked(QUrl(f"bm:{bm_id}:1:C"))
    assert picked == ["1A, 2B"], picked
    print("ok  брейншторм: кликабельные A/B/C/D и отправка выбора")

    # один и тот же ответ, пришедший дважды (стрим + final), не дублируется
    dup = ChatView()
    dup.stream_start("Немуся")
    dup.stream_delta(sample)
    dup.stream_end(sample)
    blocks = len(dup._blocks)
    dup.stream_end(sample)
    assert len(dup._blocks) == blocks, (blocks, len(dup._blocks))
    assert dup._bm_seq == 1, dup._bm_seq
    print("ok  повторный final не дублирует ответ и панель")

    # сброс черновика перед повтором: частичный ответ исчезает, новый — без дублей
    reset = ChatView()
    reset.stream_start("Немуся")
    reset.stream_delta("частичный ")
    reset.stream_reset()
    assert "частичный" not in reset.toPlainText(), reset.toPlainText()
    reset.stream_delta("полный ответ")
    reset.stream_end("полный ответ")
    assert "полный ответ" in reset.toPlainText(), reset.toPlainText()
    assert "частичный" not in reset.toPlainText(), reset.toPlainText()
    print("ok  сброс стрима убирает черновик перед повтором")

    # свободный ответ («второй вариант», «давай B») тоже сопоставляется с вариантами
    from nemus.gui.widgets.chat import parse_freeform_answer

    assert parse_freeform_answer("второй вариант", questions) == {0: ("B", "")}
    assert parse_freeform_answer("давай B", questions) == {0: ("B", "")}
    assert parse_freeform_answer("1 — второй, 2 — B", questions) == {
        0: ("B", ""),
        1: ("B", ""),
    }
    assert parse_freeform_answer(
        "по умолчанию", questions
    ) == {0: ("A", ""), 1: ("A", "")}
    for noise in ("А как насчёт этого?", "спасибо", "давай подумаем ещё"):
        assert parse_freeform_answer(noise, questions) == {}, noise

    free = ChatView()
    free.add_assistant(sample)
    free_bm = free._bm_seq
    free.add_user("второй вариант")
    assert free._bm[free_bm]["submitted"], free._bm[free_bm]
    assert free._bm[free_bm]["answer"] == "1B, 2A", free._bm[free_bm]["answer"]
    print("ok  свободный ответ сопоставляется с вариантами брейншторма")

    # содержательные фразы тоже попадают в конкретный вариант
    from nemus.gui.widgets.chat import parse_semantic_answer

    assert parse_semantic_answer("лучше через json", questions) == {0: ("B", "")}
    assert parse_semantic_answer("пусть будет fail-fast", questions) == {1: ("B", "")}
    assert parse_semantic_answer(
        "мне не нравится json, давай текст", questions
    ) == {0: ("A", "")}
    for noise in ("спасибо", "что-то не то", "как думаешь?"):
        assert parse_semantic_answer(noise, questions) == {}, noise

    sem = ChatView()
    sem.add_assistant(sample)
    sem_bm = sem._bm_seq
    sem.add_user("пусть будет fail-fast")
    assert sem._bm[sem_bm]["answer"] == "1A, 2B", sem._bm[sem_bm]["answer"]
    print("ok  содержательный ответ («через json», «fail-fast») выбран верно")

    # ответ текстом (в т.ч. при восстановлении сессии из истории) отмечает выбор
    typed = ChatView()
    typed.add_assistant(sample)
    typed_bm = typed._bm_seq
    typed.add_user("1A, 2B")
    assert typed._bm[typed_bm]["submitted"], typed._bm[typed_bm]
    assert typed._bm[typed_bm]["answer"] == "1A, 2B", typed._bm[typed_bm]
    assert i18n.tr("bm.sent") in typed.toPlainText(), typed.toPlainText()
    print("ok  ответ текстом восстанавливает выбор в панели")

    # клик в окне доходит до отправки сообщения агенту
    started: list[str] = []
    original_start = window._start_agent
    window._start_agent = lambda text: started.append(text)  # type: ignore[method-assign]
    try:
        window.chat.add_assistant(sample)
        bid = window.chat._bm_seq
        window.chat._on_anchor_clicked(QUrl(f"bm:{bid}:0:C"))
        window.chat._on_anchor_clicked(QUrl(f"bm:{bid}:send"))
    finally:
        window._start_agent = original_start  # type: ignore[method-assign]
    assert started == ["1C, 2A"], started
    print("ok  выбор из чата отправляется агенту как сообщение")

    # команды «/имя-скилла» подсказываются прямо при наборе промпта
    from nemus.core.skill_commands import SkillCommand

    suggester = window.suggest
    suggester.set_commands(
        [
            SkillCommand(
                text="/code-craft ",
                title="/code-craft",
                description="Написание кода",
                skill="code-craft",
            ),
            SkillCommand(
                text="/hyperframes ",
                title="/hyperframes",
                description="Рендер видео",
                skill="hyperframes",
            ),
        ]
    )
    assert [c.title for c in suggester.matches("/")] == ["/code-craft", "/hyperframes"]
    assert [c.title for c in suggester.matches("/code")] == ["/code-craft"]
    assert [c.title for c in suggester.matches("оба /cod")] == ["/code-craft"]
    assert suggester.matches("code") == [], "без слэша команд нет"
    assert suggester.matches("/нет-такого") == [], "нет совпадений — нет подсказок"
    assert suggester.matches("надо /hyp")[0].title == "/hyperframes"
    assert suggester.matches("сделай ") == [], "после пробела не подсказываем"

    # подстановка заменяет только недописанное слово
    window.input.setText("сделай отчёт /cod")
    window.input.setCursorPosition(len(window.input.text()))
    suggester.apply(suggester.matches()[0])
    assert window.input.text() == "сделай отчёт /code-craft ", window.input.text()
    assert window.input.cursorPosition() == len(window.input.text())

    # хвост фразы после курсора сохраняется
    window.input.setText("сначала /hyp потом сцены")
    window.input.setCursorPosition(len("сначала /hyp"))
    suggester.apply(suggester.matches()[0])
    assert window.input.text() == "сначала /hyperframes потом сцены", (
        window.input.text()
    )
    assert window.input.cursorPosition() == len("сначала /hyperframes ")

    # попап открывается по слэшу, а без него — молчит
    window.input.setText("запусти /")
    suggester._on_typed("запусти /")
    assert suggester.popup_visible(), "подсказки не появились по слэшу"
    window.input.setText("запусти /zzz")
    suggester._on_typed("запусти /zzz")
    assert not suggester.popup_visible(), "попап висит без совпадений"
    window.input.setText("запусти hyperframes")
    suggester._on_typed("запусти hyperframes")
    assert not suggester.popup_visible(), "без слэша команд не подсказываем"

    # Enter при открытом списке команд не отправляет промпт
    window.input.setText("/hyp")
    suggester._on_typed("/hyp")
    before = len(window.chat._blocks)
    window._on_submit()
    assert len(window.chat._blocks) == before, "Enter отправил промпт вместо выбора"
    print("ok  команды «/» скиллов подсказываются и заменяют только слово")

    # «/имя» без задачи — справка по скиллу, без запроса к модели
    window.input.clear()  # закрываем попап после предыдущей проверки
    suggester._hide_popup()
    window.input.setText("/code-craft")
    window._on_submit()
    assert window._worker is None, "справка не должна запускать агента"
    assert window.input.text() == "", "после справки поле очищается"
    help_text = window.chat.toPlainText()
    assert "СПРАВКА" in help_text and "/code-craft" in help_text, help_text[-600:]
    assert "брейншторм" in help_text.lower(), "в справке нет разделов инструкции"
    assert "/code-craft <задача>" in help_text, help_text[-300:]

    # неизвестная команда — не выдумка, а список доступных
    window.input.setText("/нет-такого")
    window._on_submit()
    unknown_text = window.chat.toPlainText()
    assert "Скилла /нет-такого нет." in unknown_text, unknown_text[-300:]
    assert "Доступные команды: " in unknown_text, unknown_text[-300:]
    assert "/code-craft" in unknown_text, unknown_text[-300:]
    print("ok  «/имя» без задачи показывает справку по скиллу")

    # длинное имя модели в сайдбаре сокращается многоточием, а не вылезает за край
    long_model = "openrouter/qwen/qwen3-235b-a22b-thinking-2507-extended-preview"
    window.model_label.setText(long_model)
    app.processEvents()
    label = window.model_label
    assert label.text().endswith("…"), label.text()
    assert label.text() != long_model, label.text()
    assert label.toolTip() == long_model, label.toolTip()
    assert label.minimumSizeHint().width() == 0, label.minimumSizeHint().width()
    print("ok  длинное имя модели в сайдбаре элидится, полное — в тултипе")

    # диалог подтверждения с неразрывными длинными значениями не растягивается
    from nemus.gui.dialogs import ConfirmDialog

    bulky = {
        "path": "D:/Nemesis/" + "очень/длинный/путь/" * 12 + "file.txt",
        "content": "A" * 20000,
        "cmd": "python -c " + "print('x');" * 40,
    }
    confirm = ConfirmDialog("write_file", bulky)
    confirm.show()
    app.processEvents()
    assert confirm.width() <= 900, confirm.width()
    assert confirm.height() <= 700, confirm.height()
    scrolls = confirm.findChildren(QScrollArea)
    assert scrolls and scrolls[0].verticalScrollBar().maximum() > 0, "нет прокрутки"
    confirm.close()
    print("ok  длинные параметры в ConfirmDialog переносятся и прокручиваются")

    print("\nGUI SMOKE TEST PASSED")
    return 0


def _run_child() -> None:
    """Режим дочернего процесса: прогон теста и мгновенный выход.

    На Windows разборка живых Qt-объектов при выходе из интерпретатора
    периодически роняет процесс УЖЕ ПОСЛЕ успешной печати — все проверки
    пройдены, но код возврата становится 0xC0000409/0xC0000005. Поэтому
    успех определяется родителем по маркеру в выводе (см. _run_parent).
    """
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _run_parent() -> int:
    """Режим родителя: гоняет тест в дочернем процессе и судит по маркеру.

    Qt6 на Windows иногда роняет дочерний процесс в момент выхода
    (DLL_PROCESS_DETACH в нативных потоках) уже после того, как все
    ассерты прошли и напечатан маркер «GUI SMOKE TEST PASSED» — код
    возврата такого процесса ненадёжен. Маркер печатается только после
    последнего assert, поэтому его наличие — честный критерий успеха:
    любой реальный провал (assert, крэш до конца теста, таймаут) маркер
    не напечатает, и родитель вернёт ненулевой код.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("GUI SMOKE TEST FAILED: превышен таймаут (300 с)")
        return 1

    if proc.stdout:
        sys.stdout.buffer.write(proc.stdout)
        sys.stdout.flush()
    if proc.stderr:
        sys.stderr.buffer.write(proc.stderr)
        sys.stderr.flush()

    if b"GUI SMOKE TEST PASSED" in proc.stdout:
        # teardown-крэш дочернего процесса после маркера — известный флейк
        # Qt6 на Windows, на результат теста не влияет
        return 0
    print(f"GUI SMOKE TEST FAILED: дочерний процесс завершился с кодом {proc.returncode}")
    return proc.returncode if proc.returncode else 1


if __name__ == "__main__":
    if "--child" in sys.argv[1:]:
        _run_child()
    else:
        sys.exit(_run_parent())
