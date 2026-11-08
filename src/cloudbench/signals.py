"""Periodic waveforms shared by the agent's live synth and the gateway's WAV generator."""

from __future__ import annotations

from enum import StrEnum

import numpy as np


class Shape(StrEnum):
    SINE = "sine"
    SQUARE = "square"
    TRIANGLE = "triangle"
    SAWTOOTH = "sawtooth"


def waveform(shape: Shape, phase: np.ndarray) -> np.ndarray:
    """Evaluate a unit-amplitude waveform at phases in cycles (any real numbers)."""
    p = phase % 1.0
    match shape:
        case Shape.SINE:
            return np.sin(2 * np.pi * p)
        case Shape.SQUARE:
            return np.where(p < 0.5, 1.0, -1.0)
        case Shape.TRIANGLE:
            # 0 at p=0, +1 at 0.25, -1 at 0.75: in phase with the sine.
            return 1.0 - 4.0 * np.abs(((p + 0.25) % 1.0) - 0.5)
        case Shape.SAWTOOTH:
            return 2.0 * ((p + 0.5) % 1.0) - 1.0
