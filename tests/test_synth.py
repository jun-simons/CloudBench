"""The live tone synth must never click: continuous phase, smoothed changes, fades."""

from __future__ import annotations

import numpy as np
import pytest

from cloudbench.agent.synth import SAMPLE_RATE_HZ, ToneParams, ToneSynth
from cloudbench.signals import Shape, waveform

BLOCK = 480  # 10 ms


def play(synth: ToneSynth, seconds: float) -> np.ndarray:
    blocks = round(seconds * SAMPLE_RATE_HZ / BLOCK)
    return np.concatenate([synth.render(BLOCK) for _ in range(blocks)])


def max_jump(signal: np.ndarray) -> float:
    return float(np.max(np.abs(np.diff(signal))))


@pytest.mark.parametrize("shape", list(Shape))
def test_waveforms_are_unit_amplitude_and_start_at_zero(shape: Shape) -> None:
    values = waveform(shape, np.linspace(0, 1, 1000, endpoint=False))
    assert values.max() == pytest.approx(1, abs=0.01)
    assert values.min() == pytest.approx(-1, abs=0.01)
    if shape != Shape.SQUARE:
        assert waveform(shape, np.array([0.0]))[0] == pytest.approx(0, abs=1e-9)


def test_tone_fades_in_and_reaches_target_amplitude() -> None:
    synth = ToneSynth(ToneParams(Shape.SINE, 50, 0.5))
    out = play(synth, 0.3)
    assert abs(out[0]) < 0.01
    assert np.max(np.abs(out[-SAMPLE_RATE_HZ // 50 :])) == pytest.approx(0.5, abs=0.01)


def test_frequency_and_shape_changes_are_click_free() -> None:
    synth = ToneSynth(ToneParams(Shape.SINE, 40, 0.5))
    before = play(synth, 0.2)
    synth.update(frequency_hz=110, shape=Shape.TRIANGLE)
    after = play(synth, 0.3)
    signal = np.concatenate([before, after])
    # A 110 Hz wave at 0.5 amplitude moves at most ~0.0072 per sample; a click
    # (phase reset or hard switch) would jump far more.
    assert max_jump(signal) < 0.02
    assert synth.params.frequency_hz == 110


def test_release_fades_to_silence_and_finishes() -> None:
    synth = ToneSynth(ToneParams(Shape.SQUARE, 60, 0.8))
    play(synth, 0.2)
    synth.release()
    tail = play(synth, 0.1)
    assert synth.finished
    assert tail[-1] == 0
    assert np.max(np.abs(tail[-BLOCK:])) == 0
