"""Audio output drivers for the vibration exciter, plus WAV decoding."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
import wave
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from cloudbench.agent.synth import ToneSynth

log = logging.getLogger(__name__)


class WavError(ValueError):
    pass


@dataclass(frozen=True)
class Audio:
    samples: np.ndarray  # float32, shape (frames,), range [-1, 1]
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate

    def dominant_frequency_hz(self) -> float | None:
        """Rough peak frequency, used by the mock driver to animate the simulated surface."""
        if len(self.samples) < 64:
            return None
        window = self.samples[: min(len(self.samples), self.sample_rate)]
        spectrum = np.abs(np.fft.rfft(window))
        spectrum[0] = 0
        peak = int(np.argmax(spectrum))
        return peak * self.sample_rate / len(window) if spectrum[peak] > 0 else None


def decode_wav(data: bytes) -> Audio:
    """Decode 8/16/24/32-bit PCM WAV to mono float32 (multi-channel input is averaged)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise WavError(f"not a PCM WAV file: {exc}") from exc

    if width == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        ints = (
            b[:, 0].astype(np.int32)
            | (b[:, 1].astype(np.int32) << 8)
            | (b[:, 2].astype(np.int32) << 16)
        )
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        pcm = ints.astype(np.float32) / 8388608
    elif width == 4:
        pcm = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    else:
        raise WavError(f"unsupported sample width {width}")

    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return Audio(samples=np.clip(pcm, -1, 1).astype(np.float32), sample_rate=rate)


class AudioOutput(Protocol):
    async def play(self, audio: Audio, cancel: asyncio.Event) -> float:
        """Play to completion or until ``cancel`` is set. Returns seconds played."""
        ...

    async def play_tone(
        self, synth: ToneSynth, done: asyncio.Event, max_duration_s: float
    ) -> float:
        """Play a live tone until ``done`` is set or ``max_duration_s`` passes, then fade
        out. Returns seconds played."""
        ...

    def stop(self) -> None:
        """Silence the output immediately. Must be safe to call at any time."""
        ...


class SoundDeviceOutput:
    def __init__(self, device: str | int | None) -> None:
        import sounddevice  # imported lazily: needs PortAudio, absent on CI and servers

        self._sd = sounddevice
        self._device = device
        self._tone_stream: Any = None

    async def play(self, audio: Audio, cancel: asyncio.Event) -> float:
        started = time.monotonic()
        self._sd.play(audio.samples, audio.sample_rate, device=self._device)
        try:
            deadline = started + audio.duration_s + 0.5
            while time.monotonic() < deadline and not cancel.is_set():
                stream = self._sd.get_stream()
                if not stream.active:
                    break
                await asyncio.sleep(0.02)
        finally:
            self._sd.stop()
        return min(time.monotonic() - started, audio.duration_s)

    async def play_tone(
        self, synth: ToneSynth, done: asyncio.Event, max_duration_s: float
    ) -> float:
        def callback(outdata: np.ndarray, frames: int, _time: object, _status: object) -> None:
            outdata[:, 0] = synth.render(frames)

        stream = self._sd.OutputStream(
            samplerate=synth.sample_rate,
            channels=1,
            dtype="float32",
            device=self._device,
            callback=callback,
        )
        self._tone_stream = stream
        started = time.monotonic()
        stream.start()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), max_duration_s)
            synth.release()
            # Let the fade-out play (a few blocks, at most 0.5 s) before closing the stream.
            for _ in range(50):
                if synth.finished:
                    break
                await asyncio.sleep(0.01)
        finally:
            stream.stop()
            stream.close()
            self._tone_stream = None
        return time.monotonic() - started

    def stop(self) -> None:
        self._sd.stop()
        stream = self._tone_stream
        if stream is not None:
            stream.abort()


@dataclass
class SimulatedSurface:
    """Shared state between the mock vibrator and the mock camera."""

    frequency_hz: float | None = None
    amplitude: float = 0.0
    # While a live tone plays, the camera reads its parameters directly.
    synth: ToneSynth | None = None

    def drive(self) -> tuple[float | None, float]:
        """Current drive frequency and amplitude, from a live tone or a clip."""
        synth = self.synth
        if synth is not None:
            params = synth.params
            return params.frequency_hz, synth.current_amplitude
        return self.frequency_hz, self.amplitude


class MockOutput:
    """Pretends to play audio. Drives ``SimulatedSurface`` so the mock camera shows waves."""

    def __init__(self, surface: SimulatedSurface) -> None:
        self._surface = surface

    async def play(self, audio: Audio, cancel: asyncio.Event) -> float:
        started = time.monotonic()
        self._surface.frequency_hz = audio.dominant_frequency_hz()
        self._surface.amplitude = float(np.max(np.abs(audio.samples))) if len(audio.samples) else 0
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(cancel.wait(), audio.duration_s)
        finally:
            self.stop()
        return min(time.monotonic() - started, audio.duration_s)

    async def play_tone(
        self, synth: ToneSynth, done: asyncio.Event, max_duration_s: float
    ) -> float:
        started = time.monotonic()
        self._surface.synth = synth
        try:
            # Nothing renders audio here, so advance the synth's smoothing ourselves.
            while not done.is_set() and time.monotonic() - started < max_duration_s:
                synth.render(480)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(done.wait(), 0.01)
            synth.release()
            while not synth.finished:
                synth.render(480)
                await asyncio.sleep(0.01)
        finally:
            self._surface.synth = None
        return time.monotonic() - started

    def stop(self) -> None:
        self._surface.frequency_hz = None
        self._surface.amplitude = 0.0
        self._surface.synth = None
