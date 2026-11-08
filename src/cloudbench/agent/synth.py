"""A continuous tone generator whose parameters can change while it plays.

Used for live control: the vibrator plays a tone and the user drags the
frequency or amplitude, or switches wave shape, without stopping. To avoid
audible clicks (and mechanical jolts on the exciter) every change is smoothed:
the phase is continuous, frequency glides, amplitude slews, and a shape change
crossfades over one block. Start and stop fade in and out the same way.

``render`` is called from the audio thread; ``update`` and ``release`` from the
event loop, so shared state is guarded by a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from cloudbench.signals import Shape, waveform

SAMPLE_RATE_HZ = 48_000
# Time constants for smoothing parameter changes.
FREQUENCY_GLIDE_S = 0.08
AMPLITUDE_SLEW_S = 0.05


@dataclass(frozen=True)
class ToneParams:
    shape: Shape
    frequency_hz: float
    amplitude: float  # 0..1 of full scale


class ToneSynth:
    def __init__(self, params: ToneParams, sample_rate: int = SAMPLE_RATE_HZ) -> None:
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._target = params
        self._shape = params.shape
        self._frequency = params.frequency_hz
        self._amplitude = 0.0  # fades in from silence
        self._phase = 0.0
        self._released = False

    @property
    def params(self) -> ToneParams:
        """The parameters the tone is heading towards (what the user asked for)."""
        with self._lock:
            return self._target

    @property
    def current_amplitude(self) -> float:
        with self._lock:
            return self._amplitude

    @property
    def finished(self) -> bool:
        """True once released and faded out completely."""
        with self._lock:
            return self._released and self._amplitude == 0.0

    def update(
        self,
        shape: Shape | None = None,
        frequency_hz: float | None = None,
        amplitude: float | None = None,
    ) -> ToneParams:
        with self._lock:
            t = self._target
            self._target = ToneParams(
                shape=t.shape if shape is None else shape,
                frequency_hz=t.frequency_hz if frequency_hz is None else frequency_hz,
                amplitude=t.amplitude if amplitude is None else amplitude,
            )
            return self._target

    def release(self) -> None:
        """Fade out; ``finished`` turns True when silent."""
        with self._lock:
            self._released = True

    def render(self, frames: int) -> np.ndarray:
        """Produce the next ``frames`` samples as float32 in [-1, 1]."""
        with self._lock:
            target = self._target
            block_s = frames / self.sample_rate
            target_amp = 0.0 if self._released else target.amplitude

            # Frequency glides exponentially towards the target; amplitude slews linearly.
            f0 = self._frequency
            f1 = f0 + (target.frequency_hz - f0) * min(1.0, block_s / FREQUENCY_GLIDE_S)
            a0 = self._amplitude
            max_step = block_s / AMPLITUDE_SLEW_S
            a1 = a0 + float(np.clip(target_amp - a0, -max_step, max_step))

            freqs = np.linspace(f0, f1, frames, endpoint=False)
            phases = self._phase + np.cumsum(freqs) / self.sample_rate
            amps = np.linspace(a0, a1, frames, endpoint=False)

            signal = waveform(self._shape, phases)
            if target.shape != self._shape:
                fade = np.linspace(0.0, 1.0, frames, endpoint=False)
                signal = signal * (1 - fade) + waveform(target.shape, phases) * fade
                self._shape = target.shape

            self._phase = float(phases[-1] % 1.0) if frames else self._phase
            self._frequency = f1
            self._amplitude = a1 if abs(a1 - target_amp) > 1e-6 else target_amp
            return (signal * amps).astype(np.float32)
