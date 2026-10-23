"""Офлайн-тесты подавления шума: встроенный редюсер, off и fallback Krisp."""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import NoiseConfig  # noqa: E402
from nemus.voice.noise import (  # noqa: E402
    BuiltinNoiseReducer,
    KrispNoiseReducer,
    OffNoiseReducer,
    RNNoiseReducer,
    create_noise_reducer,
)

SR = 16000
BLOCK_MS = 30
N = int(SR * BLOCK_MS / 1000)
RNG = np.random.default_rng(99)


def _noise(rms: float) -> np.ndarray:
    x = RNG.standard_normal(N).astype(np.float32)
    x /= max(1e-6, np.sqrt(np.mean(x * x)))
    return (x * rms * 32768.0).astype(np.int16)


def _rms(block: np.ndarray) -> float:
    x = block.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def test_builtin_suppresses_noise() -> None:
    r = BuiltinNoiseReducer(sample_rate=SR, block_ms=BLOCK_MS)
    assert r.name == "builtin"
    outs = [_rms(r.clean(_noise(0.01))) for _ in range(80)]
    assert max(outs[20:]) < 0.004, f"шум не подавлен: {max(outs[20:]):.4f}"
    print("ok  builtin подавляет постоянный шум")


def test_builtin_preserves_length_and_dtype() -> None:
    r = BuiltinNoiseReducer(sample_rate=SR, block_ms=BLOCK_MS)
    out = r.clean(_noise(0.1))
    assert out.shape == (N,) and out.dtype == np.int16, (out.shape, out.dtype)
    print("ok  builtin сохраняет длину и тип int16")


def test_off_passthrough() -> None:
    r = OffNoiseReducer()
    x = _noise(0.1)
    out = r.clean(x)
    assert out is x or np.array_equal(out, x)
    print("ok  off пропускает сигнал без изменений")


def test_factory_builtin_and_off() -> None:
    assert isinstance(create_noise_reducer(NoiseConfig(provider="builtin")), BuiltinNoiseReducer)
    assert isinstance(create_noise_reducer(NoiseConfig(provider="off")), OffNoiseReducer)
    print("ok  фабрика: builtin/off")


def test_factory_krisp_fallback() -> None:
    # krisp_audio не установлен в этой среде -> мягкий откат на builtin
    r = create_noise_reducer(NoiseConfig(provider="krisp"))
    assert isinstance(r, BuiltinNoiseReducer), type(r)
    print("ok  фабрика: Krisp недоступен -> fallback на builtin")


def test_factory_rnnoise() -> None:
    r = create_noise_reducer(NoiseConfig(provider="rnnoise"))
    # rnnoise.dll вендорена -> должен активироваться RNNoise, иначе fallback
    assert isinstance(r, (RNNoiseReducer, BuiltinNoiseReducer)), type(r)
    print(f"ok  фабрика rnnoise -> {r.name}")


def test_rnnoise_preserves_length() -> None:
    import nemus.voice.rnnoise_native as rn

    if not rn.available():
        print("skip rnnoise.dll не найден")
        return
    r = RNNoiseReducer(sample_rate=SR)
    for _ in range(5):
        out = r.clean(_noise(0.05))
        assert out.shape == (N,) and out.dtype == np.int16, (out.shape, out.dtype)
    print("ok  RNNoise сохраняет длину блока и тип int16")


def test_rnnoise_reduces_noise() -> None:
    import nemus.voice.rnnoise_native as rn

    if not rn.available():
        print("skip rnnoise.dll не найден")
        return
    r = RNNoiseReducer(sample_rate=SR)
    inputs = [_noise(0.05) for _ in range(40)]
    outs = [r.clean(b) for b in inputs]
    late = outs[20:]  # после прогрева ресемплера
    before = np.mean([_rms(b) for b in inputs[20:]])
    after = np.mean([_rms(b) for b in late])
    assert after < before, f"RNNoise не снизил шум: {before:.4f} -> {after:.4f}"
    print(f"ok  RNNoise снижает шум ({before:.4f} -> {after:.4f})")


def test_krisp_requires_sdk() -> None:
    try:
        import krisp_audio  # type: ignore  # noqa: F401
    except Exception:
        pass
    else:
        print("skip KrispNoiseReducer требует SDK (krisp_audio установлен)")
        return
    try:
        KrispNoiseReducer(sample_rate=SR, api_key="x", model_path="y.kef")
    except RuntimeError as exc:
        assert "krisp" in str(exc).lower() or "sdk" in str(exc).lower()
        print("ok  KrispNoiseReducer без SDK -> RuntimeError")
    else:  # pragma: no cover
        raise AssertionError("ожидался RuntimeError без SDK")


def test_rnnoise_close_idempotent_concurrent() -> None:
    """Регресс 0xC0000374: stop() (GUI) и finally цикла одновременно зовут
    close() -> двойной rnnoise_destroy портит кучу нативной DLL."""
    import threading

    import nemus.voice.rnnoise_native as rn

    if not rn.available():
        print("skip rnnoise.dll не найден")
        return

    r = RNNoiseReducer(sample_rate=SR)
    r.clean(_noise(0.05))

    barrier = threading.Barrier(8)
    errors: list[Exception] = []

    def _close_once() -> None:
        try:
            barrier.wait()
            r.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_close_once) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert r._den._state is None, "состояние RNNoise не освобождено"
    r.close()  # ещё один вызов — no-op
    print("ok  RNNoise: close() идемпотентен при гонке из 8 потоков")


if __name__ == "__main__":
    test_builtin_suppresses_noise()
    test_builtin_preserves_length_and_dtype()
    test_off_passthrough()
    test_factory_builtin_and_off()
    test_factory_krisp_fallback()
    test_factory_rnnoise()
    test_rnnoise_preserves_length()
    test_rnnoise_reduces_noise()
    test_rnnoise_close_idempotent_concurrent()
    test_krisp_requires_sdk()
    print("\nNOISE TESTS PASSED")
