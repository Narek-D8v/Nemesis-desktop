"""Проверка, не растёт ли задержка RNNoise-шумодава по ходу сессии."""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from nemus.voice.noise import RNNoiseReducer
    from nemus.voice.params import SAMPLE_RATE
    from nemus.voice.rnnoise_native import available

    print("rnnoise available:", available())
    if not available():
        return 1

    block_ms = 60
    block = int(SAMPLE_RATE * block_ms / 1000)
    reducer = RNNoiseReducer(SAMPLE_RATE)

    # Тональные посылки: в каждом 10-м блоке — 40 мс синуса 440 Гц.
    # RNNoise сохраняет голосовой диапазон, поэтому можно измерить задержку.
    total_blocks = 100
    positions: list[int] = []
    out_signals: list[np.ndarray] = []
    tone = 440.0
    for index in range(total_blocks):
        data = np.zeros(block, dtype=np.int16)
        if index % 10 == 0:
            t = np.arange(block) / SAMPLE_RATE
            data[: int(SAMPLE_RATE * 0.04)] = (
                8000 * np.sin(2 * np.pi * tone * t[: int(SAMPLE_RATE * 0.04)])
            ).astype(np.int16)
            positions.append(index * block)
        cleaned = reducer.clean(data)
        arr = np.asarray(cleaned, dtype=np.int16)
        out_signals.append(arr.copy())

    out = np.concatenate(out_signals).astype(np.int32)
    print("вход: блоков", total_blocks, "| выход:", len(out), "сэмплов")

    # огибающая: RMS по окну 10 мс, порог — половина амплитуды
    window = int(SAMPLE_RATE * 0.01)
    env = np.sqrt(
        np.convolve(out.astype(np.float64) ** 2, np.ones(window) / window, mode="same")
    )
    found: list[int] = []
    i = 0
    while i < len(env):
        if env[i] > 2000:
            found.append(i)
            i += window * 5
        else:
            i += 1
    print("посылок на входе:", len(positions), "| найдено на выходе:", len(found))
    if len(found) >= 2:
        delays = [
            (out_pos - in_pos) / SAMPLE_RATE * 1000
            for out_pos, in_pos in zip(found[: len(positions)], positions, strict=False)
            if 0 <= out_pos - in_pos < SAMPLE_RATE
        ]
        print("задержка по импульсам, мс:", [round(d, 1) for d in delays])
        if len(delays) >= 2:
            drift = delays[-1] - delays[0]
            print(f"дрейф от первого к последнему: {drift:+.1f} мс")
            if abs(drift) > 100:
                print("ПРОБЛЕМА: задержка растёт/уменьшается")
                return 1
    reducer.close()
    print("OK: задержка стабильна")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
