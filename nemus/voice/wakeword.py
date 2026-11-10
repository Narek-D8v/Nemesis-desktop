"""Wake word детектор на базе Vosk (русская модель, офлайн)."""

from __future__ import annotations

import itertools
import json
import re
import zipfile
from pathlib import Path
from urllib.request import urlopen

from nemus.utils.logger import logger
from nemus.voice.params import SAMPLE_RATE

DEFAULT_MODEL = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{DEFAULT_MODEL}.zip"
#: Слова по умолчанию: используются, если в конфиге пусто или битая кодировка.
DEFAULT_WAKE_WORDS = ("немуся", "немесис", "немо")

#: Латиница → кириллица. Русская модель Vosk понимает только кириллицу,
#: поэтому введённое латиницей имя переводим в кириллицу ещё до грамматики.
_LATIN_TO_CYRILLIC: tuple[tuple[str, str], ...] = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("tch", "ч"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
    ("ye", "е"),
    ("yo", "е"),
    ("yi", "йи"),
    ("iy", "ий"),
    ("oy", "ой"),
    ("ay", "ай"),
    ("ey", "ей"),
    ("a", "а"),
    ("b", "б"),
    ("c", "к"),
    ("d", "д"),
    ("e", "е"),
    ("f", "ф"),
    ("g", "г"),
    ("h", "х"),
    ("i", "и"),
    ("j", "й"),
    ("k", "к"),
    ("l", "л"),
    ("m", "м"),
    ("n", "н"),
    ("o", "о"),
    ("p", "п"),
    ("q", "к"),
    ("r", "р"),
    ("s", "с"),
    ("t", "т"),
    ("u", "у"),
    ("v", "в"),
    ("w", "в"),
    ("x", "кс"),
    ("y", "и"),
    ("z", "з"),
)

#: Короткие служебные слова, из которых Vosk строит фразу (`не муся`).
#: Остальные куски короче трёх букв в грамматику не пускаем: иначе `немуся`
#: распадается на `не му ся`, а такие обрывки ловят постороннюю речь.
_SHORT_PIECES = frozenset(
    {"не", "ни", "ну", "эй", "хей", "ок", "ты", "вы", "он", "мы", "я"}
)

_FRONT_VOWELS = frozenset("еиыэёюя")
_BACK_VOWELS = frozenset("аоу")

#: Похожие гласные: имя может прозвучать чуть иначе, а словарь — знать
#: только один из вариантов (`немесис` → `не мисис`).
_VOWEL_VARIANTS: dict[str, tuple[str, ...]] = {
    "а": ("а", "о", "я"),
    "о": ("о", "а"),
    "у": ("у", "ю"),
    "ы": ("ы", "и"),
    "и": ("и", "е", "ы"),
    "е": ("е", "и", "э", "я"),
    "э": ("э", "е"),
    "ю": ("ю", "у"),
    "я": ("я", "а", "е"),
}


def transliterate_to_cyrillic(text: str) -> str:
    """Переводит латиницу в кириллицу, не трогая уже кириллические буквы.

    Латиница нужна не для Vosk (он ждёт кириллицу), а как удобный ввод:
    ``nemusya`` → ``немуся``. Понимает частые сочетания ``sh/ch/zh/ya``.
    """
    lowered = (text or "").lower().replace("ё", "е")
    result: list[str] = []
    index = 0
    while index < len(lowered):
        char = lowered[index]
        if "а" <= char <= "я" or char in " -":
            result.append(char)
            index += 1
            continue
        for latin, cyrillic in _LATIN_TO_CYRILLIC:
            if lowered.startswith(latin, index):
                result.append(cyrillic)
                index += len(latin)
                break
        else:
            result.append(char)
            index += 1
    return "".join(result)


def normalize_words(words: list[str]) -> list[str]:
    """Чистит слова активации и подставляет дефолтные при пустом/битом списке.

    Принимает кириллицу и латиницу (``nemusya`` → ``немуся``), убирает
    пунктуацию и следы битой кодировки. Внутри одного элемента можно писать
    фразу через пробел — она попадёт в грамматику как есть, если Vosk знает
    все её слова.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in words:
        word = (raw or "").strip().lower().replace("ё", "е")
        if not word:
            continue
        # U+FFFD — следы битой кодировки: такое слово не произнести.
        if "\ufffd" in word:
            continue
        word = transliterate_to_cyrillic(word)
        word = re.sub(r"[^а-я\s-]", " ", word)
        word = re.sub(r"[\s-]+", " ", word).strip()
        if not word or not any("а" <= char <= "я" for char in word):
            continue
        if word in seen:
            continue
        seen.add(word)
        cleaned.append(word)
    return cleaned or list(DEFAULT_WAKE_WORDS)


def phonetic_key(text: str) -> str:
    """Фонетический ключ для сравнения кириллицы и латиницы.

    Гласные сводятся к двум классам, ``й/ь/ъ`` и двойные буквы убираются:
    ``Немуся`` и распознанное «не муся» дают один ключ. Согласные не
    склеиваются, поэтому «небо» не превращается в «немо».
    """
    lowered = transliterate_to_cyrillic((text or "").lower().replace("ё", "е"))
    letters = re.sub(r"[^а-я]", "", lowered)
    letters = letters.replace("й", "и").replace("ъ", "").replace("ь", "")
    folded: list[str] = []
    for char in letters:
        if char in _FRONT_VOWELS:
            folded.append("и")
        elif char in _BACK_VOWELS:
            folded.append("а")
        else:
            folded.append(char)
    key = "".join(folded)
    return re.sub(r"(.)\1+", r"\1", key)


def _lookup_word(lookup, word: str) -> bool:
    """Есть ли слово в словаре Vosk (у тестового lookup может не быть метода)."""
    try:
        return int(lookup(word)) >= 0
    except Exception:  # noqa: BLE001
        return False


def _piece_ok(piece: str) -> bool:
    return len(piece) >= 3 or piece in _SHORT_PIECES


def _segments(word: str, lookup) -> list[list[str]]:
    """Все разбиения слова на 1–3 слова из словаря Vosk."""
    if " " in word:
        parts = word.split()
        if (
            1 <= len(parts) <= 3
            and all(_piece_ok(part) and _lookup_word(lookup, part) for part in parts)
        ):
            return [parts]
        return []

    length = len(word)
    found: list[list[str]] = []
    for parts_count in range(1, min(3, length) + 1):
        for cuts in itertools.combinations(range(1, length), parts_count - 1):
            pieces: list[str] = []
            start = 0
            for cut in cuts:
                pieces.append(word[start:cut])
                start = cut
            pieces.append(word[start:])
            if all(
                _piece_ok(piece) and _lookup_word(lookup, piece) for piece in pieces
            ):
                found.append(pieces)
    return found


def _segment_score(pieces: list[str]) -> tuple[int, int, int, int]:
    """Лучше длинный хвост и длинные куски: `не муся`, а не `нем уся`."""
    return (
        len(pieces[-1]),
        min(len(piece) for piece in pieces),
        -len(pieces),
        len(pieces[0]),
    )


def _word_variants(word: str, limit: int = 24) -> list[str]:
    """Варианты имени с похожими гласными (для слов вне словаря Vosk)."""
    variants = [word]
    for index, char in enumerate(word):
        options = _VOWEL_VARIANTS.get(char)
        if not options:
            continue
        expanded: list[str] = []
        for current in variants:
            for option in options:
                candidate = f"{current[:index]}{option}{current[index + 1:]}"
                if candidate not in expanded:
                    expanded.append(candidate)
        variants = expanded[:limit]
    return variants


def build_wake_phrases(word: str, lookup) -> list[str]:
    """Фразы для грамматики Vosk или пустой список, если слова нет в словаре.

    ``немуся`` → ``["не муся"]``, ``немесис`` → ``["не мисис"]``,
    ``немо`` → ``["немо"]``. Фразы, а не отдельные слова, важны: иначе
    грамматика ловила бы «муся» в любой посторонней фразе.
    """
    if _lookup_word(lookup, word) and _piece_ok(word):
        return [word]

    exact_variants = [
        candidate
        for candidate in _word_variants(word)
        if candidate != word
        and _piece_ok(candidate)
        and _lookup_word(lookup, candidate)
    ]
    if exact_variants:
        return list(dict.fromkeys(exact_variants))[:3]

    best_phrase = ""
    best_score: tuple[int, int, int, int] | None = None
    for candidate in _word_variants(word):
        variants = _segments(candidate, lookup)
        if not variants:
            continue
        best = max(variants, key=_segment_score)
        score = _segment_score(best)
        if best_score is None or score > best_score:
            best_score = score
            best_phrase = " ".join(best)
    return [best_phrase] if best_phrase else []


class VoskWakeWord:
    """Детектор ключевого слова на русском (Vosk KWS).

    Если слово есть в словаре модели (или раскладывается на слова из него),
    используется быстрая грамматика. Для незнакомого имени включается
    open-vocabulary распознавание только для него — точность выше, но CPU
    заметно дороже. Смешанный список поддерживается: два распознавателя
    получают один и тот же аудиоблок.
    """

    def __init__(
        self, model_path: str | Path, words: list[str], sample_rate: int = SAMPLE_RATE
    ) -> None:
        import vosk  # noqa: PLC0415 — локальный импорт для скорости старта

        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Модель Vosk не найдена: {self.model_path}. "
                f"Скачайте {DEFAULT_MODEL} или вызовите download_model()."
            )
        self.model = vosk.Model(str(self.model_path))
        self.words = [w.lower().strip() for w in words if w.strip()]
        self.sample_rate = sample_rate

        lookup = getattr(self.model, "vosk_model_find_word", lambda _word: -1)
        grammar_entries: list[str] = []
        resolved_words: list[str] = []
        open_words: list[str] = []
        for word in self.words:
            phrases = build_wake_phrases(word, lookup)
            if phrases:
                grammar_entries.extend(phrases)
                resolved_words.append(word)
            else:
                open_words.append(word)

        self.grammar_entries = list(dict.fromkeys(grammar_entries))
        self.open_words = list(dict.fromkeys(open_words))
        self._grammar_keys = {key for word in resolved_words if (key := phonetic_key(word))}
        self._open_keys = {key for word in open_words if (key := phonetic_key(word))}

        self.rec = None
        self.open_rec = None
        if self.grammar_entries:
            grammar = json.dumps(self.grammar_entries, ensure_ascii=False)
            self.rec = vosk.KaldiRecognizer(self.model, sample_rate, grammar)
        if self.open_words:
            self.open_rec = vosk.KaldiRecognizer(self.model, sample_rate)
            logger.warning(
                "Слова нет в словаре Vosk: "
                f"{', '.join(self.open_words)} — включено open-vocabulary "
                "распознавание (выше нагрузка на CPU)."
            )
        if self.rec is None and self.open_rec is None:
            raise ValueError("Не удалось построить wake-word распознаватель")

        open_note = (
            f"; open-vocabulary: {', '.join(self.open_words)}"
            if self.open_words
            else ""
        )
        logger.info(
            f"Wake word грамматика: {', '.join(self.grammar_entries) or '—'}{open_note}"
        )

    @property
    def mode(self) -> str:
        """Режим детектора: grammar, open или mixed (для логов и тестов)."""
        if self.rec is not None and self.open_rec is not None:
            return "mixed"
        return "grammar" if self.rec is not None else "open"

    @staticmethod
    def _result_text(result: str) -> str:
        try:
            return json.loads(result).get("text", "").lower()
        except (json.JSONDecodeError, AttributeError):
            return ""

    @staticmethod
    def _matches(text: str, keys: set[str]) -> bool:
        key = phonetic_key(text)
        return bool(key) and any(wake_key and wake_key in key for wake_key in keys)

    def feed(self, block: bytes) -> bool:
        """Прогоняет блок PCM (int16). True — если обнаружено слово."""
        hit = False
        if self.rec is not None and self.rec.AcceptWaveform(block):
            hit = self._matches(self._result_text(self.rec.Result()), self._grammar_keys)
        if self.open_rec is not None and self.open_rec.AcceptWaveform(block):
            hit = (
                self._matches(
                    self._result_text(self.open_rec.Result()), self._open_keys
                )
                or hit
            )
        return hit

    def reset(self) -> None:
        for recognizer in (self.rec, self.open_rec):
            if recognizer is None:
                continue
            try:
                recognizer.Reset()
            except Exception:  # noqa: BLE001
                pass


def model_available(model_path: str | Path) -> bool:
    path = Path(model_path)
    if not path.exists():
        return False
    return (path / "am" / "final.mdl").exists() or any(path.iterdir())


def download_model(dest_dir: str | Path, model_name: str = DEFAULT_MODEL) -> Path:
    """Скачивает и распаковывает модель Vosk в dest_dir/model_name."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / model_name
    if model_available(target):
        return target

    url = f"https://alphacephei.com/vosk/models/{model_name}.zip"
    zip_path = dest_dir / f"{model_name}.zip"
    logger.info(f"Скачиваю модель Vosk: {url}")
    with urlopen(url) as resp, zip_path.open("wb") as out:  # noqa: S310
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    logger.info("Распаковываю…")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    zip_path.unlink(missing_ok=True)
    logger.info(f"Модель готова: {target}")
    return target
