"""Server-side waveform generation, so clients can pick a frequency instead of uploading audio."""

from __future__ import annotations

import io
import wave
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, Field

SAMPLE_RATE_HZ = 48_000
# Headroom below full scale so DAC/amplifier never clips.
PEAK = 0.9
FADE_S = 0.05


class SineSpec(BaseModel):
    kind: Literal["sine"] = "sine"
    frequency_hz: Annotated[float, Field(gt=0, le=20_000)]
    duration_s: Annotated[float, Field(gt=0, le=600)]
    amplitude: Annotated[float, Field(ge=0, le=1)] = 1.0


def sine_wav(spec: SineSpec, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    """A mono 16-bit PCM sine wave with short fades to avoid clicks."""
    n = round(spec.duration_s * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    signal = np.sin(2 * np.pi * spec.frequency_hz * t) * (PEAK * spec.amplitude)

    fade = min(round(FADE_S * sample_rate), n // 2)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade)
        signal[:fade] *= ramp
        signal[-fade:] *= ramp[::-1]

    pcm = (signal * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
