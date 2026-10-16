"""Wake word детектор на базе Vosk (русская модель, офлайн)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from urllib.request import urlopen

from nemus.utils.logger import logger

DEFAULT_MODEL = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{DEFAULT_MODEL}.zip"


class VoskWakeWord:
    """Детектор ключевого слова на русском (Vosk KWS)."""

    def __init__(
        self, model_path: str | Path, words: list[str], sample_rate: int = 16000
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
        grammar = json.dumps(self.words)
        self.rec = vosk.KaldiRecognizer(self.model, sample_rate, grammar)

    def feed(self, block: bytes) -> bool:
        """Прогоняет блок PCM (int16). True — если обнаружено слово."""
        if self.rec.AcceptWaveform(block):
            try:
                text = json.loads(self.rec.Result()).get("text", "").lower()
            except json.JSONDecodeError:
                text = ""
            return any(w in text for w in self.words)
        return False

    def reset(self) -> None:
        try:
            self.rec.Reset()
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
