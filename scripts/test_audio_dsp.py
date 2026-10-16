"""Офлайн-тесты очистки микрофона (ВЧ-фильтр + шумовые ворота)."""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.voice.audio_dsp import AudioCleaner  # noqa: E402

SR = 16000
BLOCK_MS = 30
N = int(SR * BLOCK_MS / 1000)
RNG = np.random.default_rng(1234)


def _noise(rms: float) -> np.ndarray:
    x = RNG.standard_normal(N).astype(np.float32)
    x /= max(1e-6, np.sqrt(np.mean(x * x)))
    return (x * rms * 32768.0).astype(np.int16)


def _rms(block: np.ndarray) -> float:
    x = block.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def test_gate_suppresses_noise() -> None:
    c = AudioCleaner(sample_rate=SR, block_ms=BLOCK_MS)
    outs = [_rms(c.clean(_noise(0.01))) for _ in range(80)]
    late = outs[20:]  # после прогрева ворот
    assert max(late) < 0.004, f"шум не подавлен: max={max(late):.4f}"
    print(f"ok  постоянный шум подавлен (max после прогрева={max(late):.4f})")


def test_speech_passes() -> None:
    c = AudioCleaner(sample_rate=SR, block_ms=BLOCK_MS)
    for _ in range(40):
        c.clean(_noise(0.01))  # фон
    outs = [_rms(c.clean(_noise(0.15))) for _ in range(40)]  # «речь»
    late = outs[5:]
    assert min(late) > 0.1, f"речь глушится: min={min(late):.4f}"
    print(f"ok  речь проходит (min={min(late):.4f})")


def test_highpass_removes_dc() -> None:
    c = AudioCleaner(sample_rate=SR, highpass_hz=80.0, block_ms=BLOCK_MS)
    dc = np.full(N, 3000, dtype=np.int16)  # постоянная составляющая
    out = c.clean(dc)
    assert _rms(out) < 0.01, f"ВЧ-фильтр не убрал DC: rms={_rms(out):.4f}"
    print(f"ok  ВЧ-фильтр убирает DC (rms={_rms(out):.5f})")


def test_no_gate_when_disabled() -> None:
    # порог 0 -> после прогрева сигнал проходит почти без изменений
    c = AudioCleaner(
        sample_rate=SR, min_rms=0.0, gate_ratio=1.0, hangover_ms=0, block_ms=BLOCK_MS
    )
    x = _noise(0.2)
    out = None
    for _ in range(5):
        out = c.clean(x)
    assert abs(_rms(out) - _rms(x)) < 0.02, (_rms(out), _rms(x))
    print("ok  без гейта сигнал проходит как есть")


def test_speaker_prebuffer_and_playing() -> None:
    from nemus.voice.realtime.audio_io import SpeakerPlayer

    sr = 24000
    sp = SpeakerPlayer(sr, prebuffer_ms=50, block_ms=20)
    sp._running = True  # без реального устройства
    frames = int(sr * 20 / 1000)
    out = np.zeros((frames, 1), dtype=np.int16)

    # мало данных (< пре-буфера) -> тишина, не играет
    sp.play((10000).to_bytes(2, "little", signed=True) * 100)
    sp._callback(out, frames, None, None)
    assert out.max() == 0 and not sp.is_playing, "не должен играть до пре-буфера"

    # доливаем до пре-буфера (50мс = 1200 сэмплов) -> начинает играть
    sp.play((10000).to_bytes(2, "little", signed=True) * 1200)
    out.fill(0)
    sp._callback(out, frames, None, None)
    assert out.max() == 10000, f"нет звука после пре-буфера: {out.max()}"
    assert sp.is_playing

    # clear (барж-ин) -> стоп и сброс пре-буфера
    sp.clear()
    assert not sp.is_playing
    out.fill(999)
    sp._callback(out, frames, None, None)
    assert out.max() == 0, "после clear должен быть только нуль"
    print("ok  SpeakerPlayer: пре-буфер, воспроизведение, clear")


if __name__ == "__main__":
    test_gate_suppresses_noise()
    test_speech_passes()
    test_highpass_removes_dc()
    test_no_gate_when_disabled()
    test_speaker_prebuffer_and_playing()
    print("\nAUDIO DSP TESTS PASSED")
