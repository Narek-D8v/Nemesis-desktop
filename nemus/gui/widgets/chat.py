"""История диалога: карточки с аватарами, разметка, стриминг."""

from __future__ import annotations

import html
import os
import re
import tempfile
import time
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QInputDialog, QTextBrowser, QWidget

from nemus.gui import i18n
from nemus.gui.theme import (
    ACCENT_DANGER,
    ACCENT_PRIMARY,
    ACCENT_PRIMARY_DIM,
    ACCENT_SECONDARY,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    BG,
    BG_ACTIVE,
    BG_ELEVATED,
    BG_SECONDARY,
    BORDER,
    FONT_FAMILY,
    FONT_FAMILY_MONO,
    TEXT,
    TEXT_DIM,
    TEXT_DIMMER,
)
from nemus.gui.theme import (
    BG_ELEVATED as _CODE_BG,
)

_BLOCK_END = "</div>"

_AVATAR_SIZE = 34
_AVATAR_CACHE: dict[str, str] = {}


def _avatar_path(letter: str, color_a: str, color_b: str) -> str:
    """Круглый аватар с буквой (генерируется один раз во временный файл)."""
    key = f"{letter}_{color_a}_{color_b}".replace("#", "")
    path = _AVATAR_CACHE.get(key)
    if path and os.path.exists(path):
        return path

    path = os.path.join(tempfile.gettempdir(), f"nemus_av_{key}.png")
    try:
        pm = QPixmap(_AVATAR_SIZE, _AVATAR_SIZE)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, _AVATAR_SIZE, _AVATAR_SIZE)
        grad.setColorAt(0, QColor(color_a))
        grad.setColorAt(1, QColor(color_b))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawEllipse(0, 0, _AVATAR_SIZE, _AVATAR_SIZE)
        p.setPen(QPen(QColor("#0a0d14")))
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        p.setFont(font)
        p.drawText(
            0, 0, _AVATAR_SIZE, _AVATAR_SIZE,
            Qt.AlignmentFlag.AlignCenter,
            letter.upper(),
        )
        p.end()
        pm.save(path, "PNG")
    except Exception:  # noqa: BLE001 — без QApplication картинка не создастся
        pass
    _AVATAR_CACHE[key] = path
    return path


def _avatar_html(letter: str, color_a: str, color_b: str) -> str:
    """HTML-тег аватара (локальный файл, Qt грузит его в документ)."""
    path = _avatar_path(letter, color_a, color_b).replace("\\", "/")
    src = QUrl.fromLocalFile(path).toString()
    return (
        f'<img src="{src}" width="{_AVATAR_SIZE}" height="{_AVATAR_SIZE}" '
        f'style="border-radius: {_AVATAR_SIZE // 2}px;"/>'
    )


def render_markdown(text: str) -> str:
    """Преобразует markdown в HTML: код, заголовки, курсив, ссылки."""
    escaped = html.escape(text)

    # Блоки кода — изымаем до прочей разметки, чтобы не ломать их символы
    code_blocks: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    escaped = re.sub(r"```[^\n]*\n(.*?)```", _stash, escaped, flags=re.DOTALL)
    escaped = re.sub(r"```(.*?)```", _stash, escaped, flags=re.DOTALL)

    # Заголовки
    escaped = re.sub(
        r"^### (.+)$",
        rf'<h3 style="color: {ACCENT_PRIMARY}; font-size: 15px; '
        rf'margin: 6px 0 4px 0;">\1</h3>',
        escaped,
        flags=re.MULTILINE,
    )
    escaped = re.sub(
        r"^## (.+)$",
        rf'<h2 style="color: {ACCENT_PRIMARY}; font-size: 17px; '
        rf'margin: 8px 0 4px 0;">\1</h2>',
        escaped,
        flags=re.MULTILINE,
    )
    escaped = re.sub(
        r"^# (.+)$",
        rf'<h1 style="color: {ACCENT_PRIMARY}; font-size: 19px; '
        rf'margin: 8px 0 6px 0;">\1</h1>',
        escaped,
        flags=re.MULTILINE,
    )

    # Жирный текст
    escaped = re.sub(
        r"\*\*(.+?)\*\*",
        rf'<b style="color: {ACCENT_PRIMARY}; font-weight: 700;">\1</b>',
        escaped,
    )
    escaped = re.sub(
        r"__(.+?)__",
        rf'<b style="color: {ACCENT_PRIMARY}; font-weight: 700;">\1</b>',
        escaped,
    )

    # Курсив (после жирного, чтобы не срезать **)
    escaped = re.sub(
        r"\*([^*\n]+)\*",
        rf'<i style="color: {TEXT_DIM};">\1</i>',
        escaped,
    )
    escaped = re.sub(
        r"(?<![\w])_([^_\n]+)_(?![\w])",
        rf'<i style="color: {TEXT_DIM};">\1</i>',
        escaped,
    )

    # Код в строке
    escaped = re.sub(
        r"`([^`]+)`",
        rf'<code style="background: {_CODE_BG}; color: {ACCENT_PRIMARY}; '
        rf"padding: 2px 6px; border-radius: 4px; font-family: {FONT_FAMILY_MONO}; "
        rf'font-size: 12px;">\1</code>',
        escaped,
    )

    # Ссылки
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^\)]+)\)",
        rf'<a href="\2" style="color: {ACCENT_PRIMARY}; text-decoration: none; '
        rf'border-bottom: 1px solid {ACCENT_PRIMARY_DIM};">\1</a>',
        escaped,
    )

    # Возвращаем блоки кода
    def _restore(m: re.Match[str]) -> str:
        code = code_blocks[int(m.group(1))]
        return (
            f'<pre style="background: {_CODE_BG}; color: {TEXT_DIM}; '
            f"padding: 12px; border-radius: 8px; border-left: 3px solid "
            f"{ACCENT_PRIMARY}; font-family: {FONT_FAMILY_MONO}; font-size: 12px; "
            f'margin: 6px 0;">{code}</pre>'
        )

    escaped = re.sub(r"\x00CB(\d+)\x00", _restore, escaped)

    # Переносы строк
    escaped = escaped.replace("\n", "<br>")

    return f'<div style="line-height: 1.6; color: {TEXT};">{escaped}{_BLOCK_END}'


# --------------------------------------------------------------- брейншторм
#: варианты A/B/C/D в ответе модели — кликабельная панель выбора


@dataclass
class _BmOption:
    """Один вариант ответа в панели брейншторма."""

    letter: str
    text: str
    recommended: bool = False
    #: D — свободный ввод (открывает поле ввода вместо выбора)
    custom: bool = False


@dataclass
class _BmQuestion:
    """Один пункт брейншторма: тема и варианты A–D."""

    number: str
    title: str
    options: list[_BmOption] = field(default_factory=list)
    #: номера строк исходного текста (их заменяет панель)
    lines: list[int] = field(default_factory=list)


_BM_QUESTION_RE = re.compile(r"^(\d{1,2})[.)]\s*(.+)$")
_BM_OPTION_RE = re.compile(r"^([A-Da-dАаВвСс])[.)]\s*(.+)$")
_BM_LETTERS = {"а": "A", "в": "B", "с": "C"}
#: ответ-выбор: «1A, 2B, 3D: свой текст» (цифра + буква)
_BM_ANSWER_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*([A-Da-dАаВвСс])(?![\wА-Яа-я])"
    r"(?:\s*[:—-]\s*([^,;\n]+))?"
)


def _bm_clean(line: str) -> str:
    """Срезает markdown-обвес строки: цитаты, жирный, маркер списка."""
    text = line.strip()
    while text.startswith(">"):
        text = text[1:].lstrip()
    text = text.replace("**", "").replace("__", "")
    if text[:2] in ("- ", "* ", "+ "):
        text = text[2:].lstrip()
    return text.strip()


def parse_brainstorm(text: str) -> tuple[list[_BmQuestion], str]:
    """Находит в ответе брейншторм A/B/C/D: вопросы и текст без этих строк.

    Возвращает пустой список, если ответ не похож на брейншторм: нужны
    минимум два пункта, у каждого — минимум два конкретных варианта.
    """
    questions: list[_BmQuestion] = []
    current: _BmQuestion | None = None
    for idx, raw in enumerate(text.splitlines()):
        line = _bm_clean(raw)
        if not line:
            continue
        head = _BM_QUESTION_RE.match(line)
        if head:
            current = _BmQuestion(
                number=head.group(1),
                title=head.group(2).rstrip(":").strip(),
            )
            current.lines.append(idx)
            questions.append(current)
            continue
        opt = _BM_OPTION_RE.match(line)
        if opt is None or current is None:
            continue
        letter = _BM_LETTERS.get(opt.group(1).lower(), opt.group(1).upper())
        body = opt.group(2).strip()
        low = body.lower()
        custom = low.startswith("свой вариант") or low.startswith("свой ответ")
        recommended = any(
            key in low for key in ("рекоменд", "default", "по умолчанию")
        )
        if custom:
            body = body.split(":", 1)[1].strip() if ":" in body else ""
            body = body.strip("_").strip()
        current.options.append(
            _BmOption(
                letter=letter,
                text=body,
                recommended=recommended and not custom,
                custom=custom,
            )
        )
        current.lines.append(idx)

    valid = [q for q in questions if sum(1 for o in q.options if not o.custom) >= 2]
    if len(valid) < 2:
        return [], text
    skip = {idx for q in valid for idx in q.lines}
    kept = [ln for i, ln in enumerate(text.splitlines()) if i not in skip]
    body_text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return valid, body_text


def parse_choice_answer(text: str) -> list[tuple[str, str, str]]:
    """Разбирает ответ-выбор «1A, 2B, 3D: свой текст» в тройки (номер, буква, свой)."""
    picks: list[tuple[str, str, str]] = []
    for match in _BM_ANSWER_RE.finditer(text or ""):
        letter = _BM_LETTERS.get(match.group(2).lower(), match.group(2).upper())
        picks.append((match.group(1), letter, (match.group(3) or "").strip()))
    return picks


#: латинская буква-вариант отдельным словом («давай B»), не часть «1A»
_BM_LATIN_LETTER_RE = re.compile(
    r"(?<![\dA-Za-zА-Яа-я])([A-Da-d])(?![A-Za-zА-Яа-я])"
)
#: кириллическая буква после ключевого слова: «вариант В», «буква С»
_BM_CYR_LETTER_RE = re.compile(
    r"(?:вариант\w*|букв\w*|давай\w*|беру|выбираю)\s*[—-]?\s*([АВСавс])(?![А-Яа-я])",
    re.I,
)
#: порядковые: «второй», «третий вариант» — позиция варианта в пункте
_BM_ORDINAL_RE = re.compile(r"(перв|втор|трет|четв|пят|шест|седьм|восьм)\w*", re.I)
_BM_ORDINAL_STEMS = ("перв", "втор", "трет", "четв", "пят", "шест", "седьм", "восьм")
#: номер пункта в свободном ответе: «в пункте 2», «2-й вопрос», «№1»
_BM_TARGET_RE = re.compile(
    r"(?:пункт\w*|вопрос\w*|номер\w*|№)\s*(\d{1,2})"
    r"|(\d{1,2})\s*[-–]?\s*(?:й|го|му|м)?\s*(?:пункт|вопрос)\w*",
    re.I,
)
#: «всё A», «остальное B» — один вариант на все незакрытые пункты
_BM_BULK_RE = re.compile(r"\b(?:всё|все|остальн\w*)\b", re.I)
_BM_DEFAULT_HINTS = (
    "по умолчанию",
    "рекоменд",
    "как считаешь",
    "решай сам",
    "сама реши",
    "на твоё усмотрение",
    "на твое усмотрение",
)


def _bm_ordinal_pos(low: str) -> int | None:
    """Позиция варианта из порядкового слова: «второй» → 2."""
    match = _BM_ORDINAL_RE.search(low)
    if match is None:
        return None
    stem = match.group(1).lower()
    for pos, item in enumerate(_BM_ORDINAL_STEMS, start=1):
        if stem == item:
            return pos
    return None


def _bm_target_index(clause: str) -> int | None:
    """Явно названный номер пункта в фразе (1-based) или None."""
    match = _BM_TARGET_RE.search(clause)
    if match is None:
        return None
    raw = match.group(1) or match.group(2)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _bm_clause_value(clause: str) -> tuple[str, int | str] | None:
    """Что выбрано во фразе: («pos», 2) — второй вариант, («letter», «B») — буква."""
    if parse_choice_answer(clause):
        return None  # это уже разобрано как «1A»
    low = clause.lower()
    pos = _bm_ordinal_pos(low)
    if pos is not None:
        return ("pos", pos)
    match = _BM_LATIN_LETTER_RE.search(clause)
    if match is not None:
        return ("letter", match.group(1).upper())
    match = _BM_CYR_LETTER_RE.search(clause)
    if match is not None:
        raw = match.group(1)
        return ("letter", _BM_LETTERS.get(raw.lower(), raw.upper()))
    return None


#: служебные слова: сами по себе не указывают на вариант
_BM_STOPWORDS = frozenset({
    "лучше", "пусть", "будет", "давай", "сделай", "сделать", "делаем",
    "нужен", "нужна", "нужно", "надо", "хочу", "вариант", "варианта", "вариантом",
    "пункт", "пункта", "пункте", "можно", "через", "чтобы", "это", "для", "при",
    "как", "или", "еще", "тоже", "так", "всё", "все", "всех", "беру", "выбираю",
    "выбери", "выбрать", "твой", "твое", "усмотрение", "мне", "нам", "бы", "же",
    "очень", "просто", "пожалуйста", "спасибо", "хорошо", "ок", "окей", "норм",
})


def _bm_tokens(text: str) -> list[str]:
    """Значимые слова фразы (без служебных), для сопоставления с вариантами."""
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized)
    return [word for word in normalized.split() if len(word) >= 3]


_BM_NEGATION_WORDS = frozenset({"не", "без", "вместо", "кроме"})


def _bm_negated_tokens(text: str, tokens: list[str]) -> set[str]:
    """Слова, которые фраза отвергает: «не нравится json» → json."""
    words = re.findall(r"[0-9a-zа-я]+", (text or "").lower().replace("ё", "е"))
    negated: set[str] = set()
    for idx, word in enumerate(words):
        if word not in _BM_NEGATION_WORDS:
            continue
        window = " ".join(words[idx + 1: idx + 4])
        negated.update(token for token in tokens if token in window)
    return negated


def _bm_option_score(tokens: list[str], option_text: str) -> float:
    """Насколько фраза попадает в текст варианта: длинные слова весят больше."""
    haystack = (option_text or "").lower().replace("ё", "е")
    score = 0.0
    for token in tokens:
        if token in haystack:
            score += 1.5 if len(token) >= 5 else 1.0
    return score


def parse_semantic_answer(
    text: str,
    questions: list[_BmQuestion],
    exclude: set[int] | frozenset[int] = frozenset(),
) -> dict[int, tuple[str, str]]:
    """Содержательный ответ → вариант: «лучше через json», «пусть fail-fast».

    Сопоставление по словам с текстом вариантов (без LLM — быстро и локально).
    Если фраза одинаково подходит к двум вариантам одного пункта или к
    нескольким пунктам, выбор не угадывается и пункт остаётся открытым.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 120 or "?" in raw or not questions:
        return {}
    tokens = [word for word in _bm_tokens(raw) if word not in _BM_STOPWORDS]
    if not tokens:
        return {}
    # «не нравится json, давай текст» — отвергнутое слово не считается выбором
    rejected = _bm_negated_tokens(raw, tokens)
    tokens = [word for word in tokens if word not in rejected]
    if not tokens:
        return {}
    picks: dict[int, tuple[str, str]] = {}
    remaining = list(tokens)
    for _ in range(len(questions)):
        best: tuple[float, int, _BmOption] | None = None
        by_question: dict[int, float] = {}
        for qi, question in enumerate(questions):
            if qi in exclude or qi in picks:
                continue
            for option in question.options:
                if option.custom:
                    continue
                score = _bm_option_score(remaining, option.text)
                if score <= 0:
                    continue
                by_question[qi] = max(by_question.get(qi, 0.0), score)
                if best is None or score > best[0]:
                    best = (score, qi, option)
        if best is None or best[0] < 1:
            break
        score, qi, option = best
        # та же фраза равна другому пункту — угадывать не из чего
        if any(value == score for other, value in by_question.items() if other != qi):
            break
        # и внутри пункта неоднозначно («json... давай текст») — тоже мимо
        rivals = [
            rival
            for rival in questions[qi].options
            if rival is not option
            and not rival.custom
            and _bm_option_score(remaining, rival.text) >= score
        ]
        if rivals:
            break
        picks[qi] = (option.letter, "")
        haystack = option.text.lower().replace("ё", "е")
        matched = [token for token in remaining if token in haystack]
        if not matched:
            break
        remaining = [token for token in remaining if token not in matched]
        if not remaining:
            break
    return picks


def _bm_letter_for(question: _BmQuestion, value: tuple[str, int | str]) -> str | None:
    """Переводит значение фразы в букву варианта конкретного пункта."""
    kind, raw = value
    if kind == "pos":
        options = question.options
        pos = raw if isinstance(raw, int) else int(raw)
        if not 1 <= pos <= len(options):
            return None
        return options[pos - 1].letter
    letter = str(raw)
    return letter if any(o.letter == letter for o in question.options) else None


def parse_freeform_answer(
    text: str,
    questions: list[_BmQuestion],
    exclude: set[int] | frozenset[int] = frozenset(),
) -> dict[int, tuple[str, str]]:
    """Свободный ответ → выбор: «второй вариант», «давай B», «всё A».

    Возвращает {индекс пункта: (буква, свой текст)}. Осторожен к обычной
    речи: вопросы, длинные фразы и реплики без явного выбора игнорируются,
    чужие буквы внутри слов и цифр не считаются ответом.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 80 or not questions:
        return {}
    # вопрос — это не ответ: без порядкового слова такие фразы («а как насчёт B?»)
    # не считаем выбором
    questioning = "?" in raw
    low = raw.lower()
    free = [qi for qi in range(len(questions)) if qi not in exclude]
    if not free:
        return {}

    # «по умолчанию», «решай сам» — берём рекомендованное во всех пунктах
    if not parse_choice_answer(raw) and any(hint in low for hint in _BM_DEFAULT_HINTS):
        if _bm_ordinal_pos(low) is None and not _BM_LATIN_LETTER_RE.search(raw):
            return {
                qi: (_bm_default_letter(questions[qi]), "")
                for qi in free
                if questions[qi].options
            }

    clauses = [part.strip() for part in re.split(r"[,;.]|\s+и\s+", raw) if part.strip()]
    picks: dict[int, tuple[str, str]] = {}
    cursor = 0
    for clause in clauses:
        if questioning and _bm_ordinal_pos(clause.lower()) is None:
            continue
        value = _bm_clause_value(clause)
        if value is None:
            continue
        target = _bm_target_index(clause)
        bulk = _BM_BULK_RE.search(clause) is not None
        if target is not None:
            candidates = [target - 1]
        elif bulk:
            candidates = list(free)
        else:
            candidates = None
        if candidates is None:
            # фраза без номера — вешаем на первый незакрытый пункт по порядку
            rest = [qi for qi in range(cursor, len(questions)) if qi in free]
            rest += [qi for qi in free if qi not in rest]
            candidates = rest[:1]
            cursor = candidates[0] + 1 if candidates else cursor
        for qi in candidates:
            if qi in exclude or qi in picks or not 0 <= qi < len(questions):
                continue
            letter = _bm_letter_for(questions[qi], value)
            if letter is None:
                continue
            picks[qi] = (letter, "")
    return picks


def _bm_default_letter(question: _BmQuestion) -> str:
    """Буква по умолчанию: рекомендованный вариант, иначе первый обычный."""
    for opt in question.options:
        if opt.recommended and not opt.custom:
            return opt.letter
    for opt in question.options:
        if not opt.custom:
            return opt.letter
    return question.options[0].letter if question.options else ""


class ChatView(QTextBrowser):
    """Карточки сообщений с аватарами.

    Сообщения складываются в блоки и отрисовываются разом — стриминг
    обновляет один блок вместо сообщения на каждый токен.
    """

    #: пользователь выбрал варианты брейншторма (текст вида «1A, 2B»)
    choice_submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ChatView")
        self.setReadOnly(True)
        # внешние ссылки открываем сами — иначе не перехватывается клик по
        # заголовку блока мыслей (свернуть/развернуть)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._on_anchor_clicked)
        self._blocks: list[str] = []
        self._stream_idx: int | None = None
        self._stream_buf: str = ""
        self._stream_name: str = "Немуся"
        #: текущий блок «мыслей» (скрытые рассуждения модели)
        self._thought_idx: int | None = None
        self._thought_buf: str = ""
        self._thought_dirty = False
        self._last_thought_render = 0.0
        #: id блоков мыслей и их свёрнутое состояние / текст
        self._thought_seq = 0
        self._thought_id: int | None = None
        self._thought_collapsed: dict[int, bool] = {}
        self._thought_texts: dict[int, str] = {}
        #: троттлинг перерисовки при стриминге (см. stream_delta)
        self._stream_dirty = False
        self._last_stream_render = 0.0
        #: панели брейншторма: id → состояние выбора
        self._bm: dict[int, dict] = {}
        self._bm_seq = 0
        #: текст последнего ответа: защита от двойной отрисовки одного ответа
        self._last_assistant_text = ""

        font = QFont()
        font.setFamilies([f.strip() for f in FONT_FAMILY.split(",") if f.strip()])
        font.setPointSize(12)
        self.setFont(font)

    # ------------------------------------------------------------- rendering
    def _render(self) -> None:
        self.setHtml("".join(self._blocks))
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add(self, block: str) -> None:
        self._blocks.append(block)
        self._render()

    @staticmethod
    def _card(
        avatar: str,
        name: str,
        body: str,
        card_bg: str,
        accent: str,
        name_color: str,
        indent: bool = False,
    ) -> str:
        margin = "        " if indent else ""
        return f"""
{margin}<table style="margin: 12px 0; border-spacing: 0; width: 100%;"><tr>
{margin}<td style="vertical-align: top; padding: 4px 10px 0 0; width: 34px;">
{margin}    {avatar}
{margin}</td>
{margin}<td style="vertical-align: top;">
{margin}    <div style="background: {card_bg}; border: 1px solid {BORDER};
{margin}                border-left: 3px solid {accent}; border-radius: 12px;
{margin}                padding: 10px 14px;">
{margin}        <span style="color: {name_color}; font-weight: 700; font-size: 12px;">
{margin}            {html.escape(name)}
{margin}        </span>
{margin}        <div style="margin-top: 4px; color: {TEXT}; line-height: 1.6;">
{margin}            {body}
{margin}        </div>
{margin}    </div>
{margin}</td></tr></table>
"""

    # -------------------------------------------------------------- messages
    def add_user(self, text: str) -> None:
        """Добавляет сообщение пользователя."""
        self.thought_end()
        # ответ буквами («1A, 2B») отмечает выбор в висящей панели — и сразу
        # после клика, и при восстановлении сессии из истории
        self._bm_answer_from_text(text)
        # диалог пошёл дальше — остальные панели выбора гасим
        self._bm_freeze_all()
        self._last_assistant_text = ""
        block = self._card(
            _avatar_html(i18n.tr("role.you_letter"), ACCENT_PRIMARY, ACCENT_PRIMARY_DIM),
            i18n.tr("role.you"),
            render_markdown(text),
            "#101826",
            ACCENT_PRIMARY,
            ACCENT_PRIMARY,
        )
        self._add(block)

    def add_assistant(self, text: str, name: str = "Немуся") -> None:
        """Добавляет сообщение ассистента."""
        self.thought_end()
        self._push_assistant(text, name, indent=True)

    def _push_assistant(self, text: str, name: str, indent: bool) -> None:
        """Блок ответа, а для брейншторма — ещё и панель выбора вариантов."""
        key = text.strip()
        if key and key == self._last_assistant_text:
            # тот же ответ пришёл повторно (assistant_text + final) — не дублируем
            return
        self._last_assistant_text = key
        questions, body = parse_brainstorm(text)
        self._add(self._assistant_block(name, body or text, indent))
        if questions:
            self._add_brainstorm(questions)

    # ------------------------------------------------------- brainstorm panel
    def _add_brainstorm(self, questions: list[_BmQuestion]) -> None:
        """Добавляет в чат панель кликабельных вариантов A/B/C/D."""
        self._bm_seq += 1
        bm_id = self._bm_seq
        self._bm[bm_id] = {
            "questions": questions,
            "selected": {},
            "custom": {},
            "submitted": False,
            "frozen": False,
            "answer": "",
        }
        self._blocks.append(self._bm_html(bm_id))
        self._render()

    def _bm_html(self, bm_id: int) -> str:
        """HTML панели выбора: тема, варианты, подвал с кнопкой отправки."""
        state = self._bm[bm_id]
        questions: list[_BmQuestion] = state["questions"]
        selected: dict[int, str] = state["selected"]
        custom: dict[int, str] = state["custom"]
        frozen = bool(state["frozen"] or state["submitted"])

        rows: list[str] = []
        for qi, question in enumerate(questions):
            rows.append(
                f'<tr><td style="padding: 12px 0 3px 0;">'
                f'<span style="color: {TEXT}; font-size: 12.5px; font-weight: 700;">'
                f"{html.escape(question.number)}. {html.escape(question.title)}"
                "</span></td></tr>"
            )
            for opt in question.options:
                chosen = selected.get(qi) == opt.letter
                label = opt.text
                if opt.custom:
                    own = custom.get(qi, "").strip()
                    label = (
                        f"{i18n.tr('bm.custom')}: {own}"
                        if own
                        else i18n.tr("bm.custom_empty")
                    )
                elif opt.recommended and "рекоменд" not in label.lower():
                    label = f"{label} · {i18n.tr('bm.recommended')}"
                rows.append(self._bm_row(bm_id, qi, opt, chosen, label, frozen))

        if state["submitted"]:
            footer = (
                f'<div style="margin-top: 10px; color: {ACCENT_SUCCESS}; '
                f'font-size: 11.5px;">✓ {i18n.tr("bm.sent")}: '
                f'{html.escape(state["answer"])}</div>'
            )
        elif state["frozen"]:
            footer = (
                f'<div style="margin-top: 10px; color: {TEXT_DIMMER}; '
                f'font-size: 11.5px;">{i18n.tr("bm.frozen")}</div>'
            )
        else:
            label = "bm.send" if selected else "bm.send_default"
            footer = (
                '<div style="margin-top: 12px;">'
                f'<a href="bm:{bm_id}:send" style="color: #0a0d14; '
                f'background: {ACCENT_PRIMARY}; font-size: 12px; font-weight: 700; '
                'text-decoration: none;">&nbsp;&nbsp;'
                f'➤ {i18n.tr(label)}&nbsp;&nbsp;</a>'
                f'<span style="color: {TEXT_DIM}; font-size: 11px;">&nbsp;&nbsp;'
                f'{i18n.tr("bm.hint")}</span></div>'
            )
        return f"""
        <!--BM{bm_id}-->
        <div style="margin: 2px 0 14px 34px; padding: 4px 14px 12px 14px;
                    background: {BG_SECONDARY}; border: 1px solid {BORDER};
                    border-left: 3px solid {ACCENT_PRIMARY}; border-radius: 12px;">
            <div style="margin-top: 8px; color: {TEXT_DIM}; font-size: 10.5px;
                        font-weight: 700; letter-spacing: 1.1px;">
                {i18n.tr("bm.title")}
            </div>
            <table width="100%" cellspacing="0" cellpadding="0">
                {''.join(rows)}
            </table>
            {footer}
        </div>
        """

    def _bm_row(
        self,
        bm_id: int,
        qi: int,
        opt: _BmOption,
        chosen: bool,
        label: str,
        frozen: bool,
    ) -> str:
        """Строка одного варианта: ссылка-кнопка или неактивный текст."""
        if chosen:
            bg, color, mark = "#1d2c44", ACCENT_PRIMARY, "✓ "
        else:
            bg, color, mark = "#12151d", TEXT if not frozen else TEXT_DIMMER, ""
        text = html.escape(f"{mark}{opt.letter}) {label}")
        if frozen:
            inner = f'<span style="color: {color}; font-size: 12px;">{text}</span>'
        else:
            inner = (
                f'<a href="bm:{bm_id}:{qi}:{opt.letter}" '
                f'style="color: {color}; font-size: 12px; text-decoration: none;">'
                f"{text}</a>"
            )
        return (
            '<tr><td style="padding: 2px 0;">'
            f'<div style="background: {bg}; padding: 6px 10px;">{inner}</div>'
            "</td></tr>"
        )

    def _bm_repaint(self, bm_id: int) -> None:
        """Перерисовывает панель на месте, сохраняя позицию прокрутки."""
        marker = f"<!--BM{bm_id}-->"
        block = self._bm_html(bm_id)
        for i, current in enumerate(self._blocks):
            if marker in current:
                self._blocks[i] = block
                break
        bar = self.verticalScrollBar()
        pos = bar.value()
        self.setHtml("".join(self._blocks))
        bar.setValue(min(pos, bar.maximum()))

    def _bm_pending_id(self) -> int | None:
        """id самой свежей панели, ждущей ответа (None — таких нет)."""
        for bm_id in sorted(self._bm, reverse=True):
            state = self._bm[bm_id]
            if state["submitted"] or state["frozen"]:
                continue
            return bm_id
        return None

    def _bm_answer_from_text(self, text: str) -> None:
        """Отмечает выбор в висящей панели, если ответ пришёл текстом.

        Ответ — обычное сообщение пользователя: «1A, 2B» или свободный
        «второй вариант», «давай B». Им же отвечают кликом по панели, и он
        же лежит в истории диалога — поэтому разбор текста восстанавливает
        отметки в панели при переключении сессии.
        """
        target = self._bm_pending_id()
        if target is None:
            return
        state = self._bm[target]
        questions: list[_BmQuestion] = state["questions"]
        filled: set[int] = set()
        for number, letter, own in parse_choice_answer(text):
            for qi, question in enumerate(questions):
                if question.number.lstrip("0") != number.lstrip("0"):
                    continue
                if not any(o.letter == letter for o in question.options):
                    continue
                state["selected"][qi] = letter
                if own:
                    state["custom"][qi] = own
                filled.add(qi)
                break
        for qi, (letter, own) in parse_freeform_answer(
            text, questions, exclude=filled
        ).items():
            state["selected"][qi] = letter
            if own:
                state["custom"][qi] = own
            filled.add(qi)
        for qi, (letter, own) in parse_semantic_answer(
            text, questions, exclude=filled
        ).items():
            state["selected"][qi] = letter
            if own:
                state["custom"][qi] = own
            filled.add(qi)
        if not filled:
            return
        state["submitted"] = True
        state["answer"] = self._bm_answer(target)
        self._bm_repaint(target)

    def _bm_freeze_all(self) -> None:
        """Гасит все незакрытые панели: выбор можно отправить только сразу."""
        for bm_id, state in list(self._bm.items()):
            if state["submitted"] or state["frozen"]:
                continue
            state["frozen"] = True
            self._bm_repaint(bm_id)

    def _bm_answer(self, bm_id: int) -> str:
        """Собирает ответ из выбора: «1A, 2B, 3D: свой текст»."""
        state = self._bm[bm_id]
        parts: list[str] = []
        for qi, question in enumerate(state["questions"]):
            letter = state["selected"].get(qi) or _bm_default_letter(question)
            own = state["custom"].get(qi, "").strip()
            if own and letter and any(
                opt.custom and opt.letter == letter for opt in question.options
            ):
                parts.append(f"{question.number}{letter}: {own}")
            else:
                parts.append(f"{question.number}{letter}")
        return ", ".join(parts)

    def _bm_send(self, bm_id: int) -> None:
        """Отправляет выбор наружу (сообщением пользователя)."""
        answer = self._bm_answer(bm_id)
        state = self._bm[bm_id]
        state["submitted"] = True
        state["answer"] = answer
        self._bm_repaint(bm_id)
        self.choice_submitted.emit(answer)

    def _on_bm_clicked(self, raw: str) -> None:
        """Клик по варианту или кнопке отправки панели брейншторма."""
        parts = [part for part in raw.lstrip("/").split(":") if part]
        if len(parts) < 2:
            return
        try:
            bm_id = int(parts[0])
        except ValueError:
            return
        state = self._bm.get(bm_id)
        if state is None or state["frozen"] or state["submitted"]:
            return
        if parts[1] == "send":
            self._bm_send(bm_id)
            return
        if len(parts) != 3:
            return
        try:
            qi = int(parts[1])
        except ValueError:
            return
        letter = parts[2]
        questions: list[_BmQuestion] = state["questions"]
        if not 0 <= qi < len(questions):
            return
        opt = next((o for o in questions[qi].options if o.letter == letter), None)
        if opt is None:
            return
        if opt.custom:
            title = i18n.tr("bm.custom_title")
            prompt = i18n.tr("bm.custom_hint")
            text, ok = QInputDialog.getText(
                self, title, prompt, text=state["custom"].get(qi, "")
            )
            if not ok:
                return
            own = text.strip()
            if own:
                state["custom"][qi] = own
                state["selected"][qi] = letter
            else:
                state["custom"].pop(qi, None)
                if state["selected"].get(qi) == letter:
                    state["selected"].pop(qi, None)
        elif state["selected"].get(qi) == letter:
            state["selected"].pop(qi, None)
        else:
            state["selected"][qi] = letter
        self._bm_repaint(bm_id)

    @staticmethod
    def _assistant_block(name: str, body: str, indent: bool = False) -> str:
        letter = (name.strip() or "Н")[0]
        return ChatView._card(
            _avatar_html(letter, ACCENT_SECONDARY, ACCENT_PRIMARY),
            name,
            render_markdown(body),
            BG_ELEVATED,
            ACCENT_SECONDARY,
            ACCENT_SECONDARY,
            indent=indent,
        )

    def add_system(self, text: str) -> None:
        """Добавляет системное сообщение."""
        self.thought_end()
        block = f"""
        <div style="margin: 10px 0; padding: 9px 12px;
                    background: rgba(94, 234, 165, 0.08);
                    border-radius: 9px; border: 1px solid {ACCENT_SUCCESS};">
            <span style="color: {ACCENT_SUCCESS}; font-weight: 600; font-size: 11px;">
                {i18n.tr("role.system")}
            </span>
            <div style="margin-top: 4px; color: {TEXT_DIM}; font-size: 12px;">
                {render_markdown(text)}
            </div>
        </div>
        """
        self._add(block)

    def add_error(self, text: str) -> None:
        """Добавляет сообщение об ошибке."""
        self.thought_end()
        block = f"""
        <div style="margin: 10px 0; padding: 9px 12px;
                    background: rgba(255, 117, 117, 0.08);
                    border-radius: 9px; border: 1px solid {ACCENT_DANGER};">
            <span style="color: {ACCENT_DANGER}; font-weight: 600; font-size: 11px;">
                {i18n.tr("role.error")}
            </span>
            <div style="margin-top: 4px; color: {TEXT_DIM}; font-size: 12px;">
                {render_markdown(text)}
            </div>
        </div>
        """
        self._add(block)

    def add_skill_help(
        self,
        name: str,
        description: str,
        capabilities: list[str],
        files: int = 0,
        hint: str = "",
    ) -> None:
        """Справка по скиллу: команда ``/имя`` без задачи."""
        self.thought_end()
        body: list[str] = []
        if description:
            body.append(
                f'<div style="margin-top: 6px; color: {TEXT}; font-size: 12.5px; '
                f'line-height: 1.5;">{html.escape(description)}</div>'
            )
        if capabilities:
            items = "".join(
                f'<div style="color: {TEXT_DIM}; font-size: 12px;">• {html.escape(item)}</div>'
                for item in capabilities
            )
            body.append(f'<div style="margin-top: 8px;">{items}</div>')
        if files:
            body.append(
                f'<div style="margin-top: 6px; color: {TEXT_DIMMER}; font-size: 11px;">'
                f'{i18n.tr("help.files", n=files)}</div>'
            )
        if hint:
            body.append(
                f'<div style="margin-top: 8px; color: {ACCENT_SECONDARY}; font-size: 11.5px;">'
                f'{html.escape(hint)}</div>'
            )
        block = f"""
        <div style="margin: 12px 0; padding: 12px 16px; background: {BG_ACTIVE};
                    border-radius: 12px; border: 1px solid {BORDER};
                    border-left: 3px solid {ACCENT_SECONDARY};">
            <span style="color: {ACCENT_SECONDARY}; font-weight: 700; font-size: 11px;
                         letter-spacing: 1.2px;">
                {i18n.tr("help.title", name=f"/{html.escape(name)}")}
            </span>
            {''.join(body)}
        </div>
        """
        self._add(block)

    def add_tool_card(
        self, tool_name: str, args: dict, result: str | None = None, ok: bool = True
    ) -> None:
        """Добавляет карточку инструмента."""
        self.thought_end()
        args_html = "".join(
            f'<div style="margin: 4px 0; color: {TEXT_DIM}; font-size: 11px;">'
            f'<span style="color: {TEXT}; font-weight: 600;">{html.escape(str(k))}:</span> '
            f'<code style="background: {BG}; color: {ACCENT_PRIMARY}; padding: 2px 4px; '
            f'border-radius: 3px; font-family: {FONT_FAMILY_MONO};">{html.escape(str(v))}</code></div>'
            for k, v in args.items()
        )
        status = ""
        if result is not None:
            color = ACCENT_SUCCESS if ok else ACCENT_DANGER
            status = (
                f'<div style="color:{color};font-size:12px;margin-top:6px;">'
                f"→ {html.escape(result[:500])}</div>"
            )

        block = f"""
        <div style="margin: 12px 0; padding: 10px 14px; background: {BG_ACTIVE};
                    border-radius: 11px; border: 1px solid {BORDER};
                    border-left: 3px solid {ACCENT_WARNING};">
            <span style="color: {ACCENT_WARNING}; font-weight: 700; font-size: 11px;
                         letter-spacing: 1.2px;">
                {i18n.tr("role.tool")} · {html.escape(tool_name.upper())}
            </span>
            <div style="margin-top: 8px;">
                {args_html}
            </div>
            {status}
        </div>
        """
        self._add(block)

    def clear_chat(self) -> None:
        """Очищает чат."""
        self._blocks.clear()
        self._bm.clear()
        self._bm_seq = 0
        self._last_assistant_text = ""
        self._stream_idx = None
        self._stream_buf = ""
        self._stream_dirty = False
        self._thought_idx = None
        self._thought_buf = ""
        self._thought_dirty = False
        self._thought_id = None
        self._thought_collapsed.clear()
        self._thought_texts.clear()
        self.setHtml("")

    # ---------------------------------------------------------- thoughts
    def _thought_block(self, body: str, tid: int, collapsed: bool) -> str:
        """Блок «мыслей» модели — визуально отделён от ответа.

        Приглушённый фон, пунктирная рамка, курсив и подпись: так видно,
        что Немуся просто думает, а не отвечает. Заголовок — ссылка:
        клик сворачивает/разворачивает блок, чтобы не засорять чат.
        """
        chevron = "▶" if collapsed else "▼"
        header = (
            f'<a href="thought:{tid}" style="color: {TEXT_DIM}; '
            f'text-decoration: none; font-weight: 700; font-size: 10px; '
            f'letter-spacing: 1.1px;">'
            f'{chevron} 🧠 {i18n.tr("role.thought")}</a>'
        )
        if collapsed:
            preview = (
                '<div style="margin-top: 3px; color: ' + TEXT_DIM
                + '; font-size: 11px;">…</div>'
            )
            body_html = ""
        else:
            preview = ""
            dim_html = render_markdown(body).replace(
                f"color: {TEXT};", f"color: {TEXT_DIM};"
            )
            body_html = (
                f'<div style="margin-top: 3px; font-style: italic; '
                f'font-size: 11.5px; line-height: 1.5;">{dim_html}</div>'
            )
        return f"""
        <!--TH{tid}-->
        <div style="margin: 8px 0 8px 34px; padding: 7px 12px;
                    background: {BG}; border: 1px dashed {BORDER};
                    border-left: 3px solid {TEXT_DIM}; border-radius: 10px;">
            {header}
            {preview}
            {body_html}
        </div>
        """

    def _on_anchor_clicked(self, url: QUrl) -> None:
        """Клик по ссылке в чате: сворачивание мыслей или внешняя ссылка."""
        if url.scheme() == "bm":
            self._on_bm_clicked(url.path() or url.toString()[3:])
            return
        if url.scheme() == "thought":
            key = url.host() or url.path().lstrip("/")
            try:
                tid = int(key)
            except ValueError:
                return
            self._toggle_thought(tid)
            return
        if url.isValid():
            QDesktopServices.openUrl(url)

    def _toggle_thought(self, tid: int) -> None:
        """Сворачивает/разворачивает конкретный блок мыслей по его id."""
        collapsed = not self._thought_collapsed.get(tid, False)
        self._thought_collapsed[tid] = collapsed
        body = self._thought_texts.get(tid, "")
        if self._thought_id == tid and self._thought_idx is not None:
            # блок ещё пишется — обновляем активный буфер
            self._thought_buf = body
        new_html = self._thought_block(body, tid, collapsed)
        marker = f"<!--TH{tid}-->"
        for i, block in enumerate(self._blocks):
            if marker in block:
                self._blocks[i] = new_html
                break
        self._render()

    def thought_start(self) -> None:
        """Открывает блок мыслей (обычно стартует сам через thought_delta).

        По умолчанию блок свёрнут — чат не засоряется, показать мысли
        можно кликом по заголовку.
        """
        if self._thought_idx is not None:
            return
        self._thought_buf = ""
        self._thought_dirty = False
        self._last_thought_render = 0.0
        self._thought_seq += 1
        self._thought_id = self._thought_seq
        self._thought_texts[self._thought_id] = ""
        self._thought_collapsed[self._thought_id] = True
        self._thought_idx = len(self._blocks)
        self._blocks.append(self._thought_block("", self._thought_id, True))
        self._render()

    def thought_delta(self, text: str) -> None:
        """Добавляет фрагмент рассуждений модели в блок мыслей."""
        if not text:
            return
        if self._thought_idx is None:
            self.thought_start()
        assert self._thought_idx is not None
        assert self._thought_id is not None
        self._thought_buf += text
        now = time.monotonic()
        if now - self._last_thought_render < self.STREAM_INTERVAL:
            if not self._thought_dirty:
                self._thought_dirty = True
                QTimer.singleShot(
                    int(self.STREAM_INTERVAL * 1000), self._flush_thought
                )
            return
        self._thought_dirty = False
        self._last_thought_render = now
        self._thought_texts[self._thought_id] = self._thought_buf
        collapsed = self._thought_collapsed.get(self._thought_id, False)
        self._blocks[self._thought_idx] = self._thought_block(
            self._thought_buf, self._thought_id, collapsed
        )
        self._render()

    def _flush_thought(self) -> None:
        """Дорисовывает накопленный буфер мыслей (таймер после паузы)."""
        if (
            self._thought_idx is None
            or self._thought_id is None
            or not self._thought_dirty
        ):
            return
        self._thought_dirty = False
        self._last_thought_render = time.monotonic()
        self._thought_texts[self._thought_id] = self._thought_buf
        collapsed = self._thought_collapsed.get(self._thought_id, False)
        self._blocks[self._thought_idx] = self._thought_block(
            self._thought_buf, self._thought_id, collapsed
        )
        self._render()

    def thought_end(self) -> None:
        """Закрывает блок мыслей (пустой блок убирается)."""
        if self._thought_idx is None:
            return
        idx = self._thought_idx
        tid = self._thought_id
        assert tid is not None  # id ставится и снимается вместе с idx
        self._thought_dirty = False
        if not self._thought_buf.strip():
            self._blocks.pop(idx)
            if tid is not None:
                self._thought_texts.pop(tid, None)
                self._thought_collapsed.pop(tid, None)
            if self._stream_idx is not None and self._stream_idx > idx:
                self._stream_idx -= 1
        else:
            self._thought_texts[tid] = self._thought_buf
            collapsed = self._thought_collapsed.get(tid, False)
            self._blocks[idx] = self._thought_block(self._thought_buf, tid, collapsed)
        self._thought_idx = None
        self._thought_buf = ""
        self._thought_id = None
        self._render()

    # -------------------------------------------------------------- streaming
    #: минимальный интервал полной перерисовки чата при стриминге, сек.
    #: на каждый токен setHtml(весь чат) даёт O(n²) и «заморозку» окна
    #: на длинных ответах (презентации, скрипты) — держим ~10 fps.
    STREAM_INTERVAL = 0.1

    def stream_start(self, name: str = "Немуся") -> None:
        """Начинает потоковую передачу сообщения."""
        if self._stream_idx is not None:
            return
        self.thought_end()  # мысли всегда предшествуют ответу
        self._stream_name = name
        self._stream_buf = ""
        self._stream_dirty = False
        self._last_stream_render = 0.0
        self._stream_idx = len(self._blocks)
        self._blocks.append(self._assistant_block(name, ""))
        self._render()

    def stream_delta(self, text: str) -> None:
        """Добавляет текст в потоковое сообщение (перерисовка не чаще 10 раз/с)."""
        if not text:
            return
        if self._stream_idx is None:
            self.stream_start()
        assert self._stream_idx is not None
        self._stream_buf += text
        now = time.monotonic()
        if now - self._last_stream_render < self.STREAM_INTERVAL:
            # копим буфер дёшево, отрисовку догонит таймер
            if not self._stream_dirty:
                self._stream_dirty = True
                QTimer.singleShot(
                    int(self.STREAM_INTERVAL * 1000), self._flush_stream
                )
            return
        self._stream_dirty = False
        self._last_stream_render = now
        self._blocks[self._stream_idx] = self._assistant_block(
            self._stream_name, self._stream_buf
        )
        self._render()

    def _flush_stream(self) -> None:
        """Дорисовывает накопленный буфер (таймер после пропущенной отрисовки)."""
        if self._stream_idx is None or not self._stream_dirty:
            return
        self._stream_dirty = False
        self._last_stream_render = time.monotonic()
        self._blocks[self._stream_idx] = self._assistant_block(
            self._stream_name, self._stream_buf
        )
        self._render()

    def stream_end(self, final_text: str = "") -> None:
        """Заканчивает потоковую передачу (при пустом потоке — вставляет текст).

        Непустой ``final_text`` всегда заменяет потоковый буфер: так в чате
        остаётся чистый финальный ответ без черновиков и рассуждений.
        """
        if self._stream_idx is None:
            # поток не открывался (например, сразу пришёл финальный текст)
            if final_text.strip():
                self.thought_end()
                self._push_assistant(final_text, self._stream_name, indent=True)
            return
        if final_text.strip():
            self._stream_buf = final_text
        self._stream_dirty = False
        text = self._stream_buf
        idx = self._stream_idx
        self._stream_idx = None
        self._stream_buf = ""
        if not text.strip():
            # пустой блок не показываем
            self._blocks.pop(idx)
            self._render()
            return
        questions, body = parse_brainstorm(text)
        self._last_assistant_text = text.strip()
        if questions:
            # варианты A/B/C/D уходят в кликабельную панель под ответом
            self._blocks[idx] = self._assistant_block(
                self._stream_name, body or text, indent=True
            )
            self._render()
            self._add_brainstorm(questions)
            return
        self._blocks[idx] = self._assistant_block(self._stream_name, text)
        self._render()


__all__ = [
    "ChatView",
    "parse_choice_answer",
    "parse_brainstorm",
    "parse_freeform_answer",
    "parse_semantic_answer",
    "render_markdown",
]
