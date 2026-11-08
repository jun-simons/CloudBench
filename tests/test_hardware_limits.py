"""Hardware-facing limits that do not need a gateway: the audio ceiling and camera selection."""

from __future__ import annotations

import asyncio
import io
import wave

import numpy as np
import pytest

from cloudbench.agent.audio import Audio, SoundDeviceOutput
from cloudbench.agent.camera import pick_avfoundation_device
from cloudbench.agent.config import VibratorConfig
from cloudbench.agent.tools import ActionError, VibratorTool


class RecordingOutput:
    def __init__(self) -> None:
        self.played: list[Audio] = []

    async def play(self, audio: Audio, cancel: asyncio.Event) -> float:
        self.played.append(audio)
        return audio.duration_s

    async def play_tone(self, synth: object, done: asyncio.Event, max_duration_s: float) -> float:
        return 0.0

    def stop(self) -> None:
        pass


def full_scale_wav(rate: int = 48_000, seconds: float = 0.1) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    pcm = (np.sign(np.sin(2 * np.pi * 40 * t)) * 32767).astype("<i2")  # square wave, full scale
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


async def play(tool: VibratorTool, gain: float) -> None:
    async def fetch(_: str) -> bytes:
        return full_scale_wav()

    await tool.invoke("play_waveform", {"audio": "f", "gain": gain}, fetch, asyncio.Event())


async def test_output_never_exceeds_max_gain() -> None:
    out = RecordingOutput()
    tool = VibratorTool(VibratorConfig(max_gain=0.05, sample_rates_hz=[48_000]), out)
    await play(tool, 0.05)
    assert 0 < np.max(np.abs(out.played[0].samples)) <= 0.05 + 1e-6


async def test_gain_above_max_is_rejected_not_clamped() -> None:
    out = RecordingOutput()
    tool = VibratorTool(VibratorConfig(max_gain=0.05, sample_rates_hz=[48_000]), out)
    with pytest.raises(ActionError):
        await play(tool, 0.4)
    assert not out.played


DEVICES = [
    ("0", "MacBook Pro Camera"),
    ("1", "Jun's iPhone Camera"),
    ("2", "Jun's iPhone Desk View Camera"),
]


def test_camera_selected_by_name_skips_desk_view() -> None:
    assert pick_avfoundation_device("iPhone Camera", DEVICES) == "1"
    assert pick_avfoundation_device("iphone", DEVICES) == "1"


def test_camera_selected_by_index_and_missing_name() -> None:
    assert pick_avfoundation_device(3, DEVICES) == "3"
    with pytest.raises(RuntimeError):
        pick_avfoundation_device("webcam", DEVICES)


class FakeSD:
    """Stands in for the sounddevice module so the real output driver can be tested."""

    def __init__(self) -> None:
        self.played: np.ndarray | None = None
        self.callback = None

    def query_devices(self, device, kind=None):
        return {"max_output_channels": 2}

    def play(self, samples, rate, device=None):
        self.played = samples

    def get_stream(self):
        return type("S", (), {"active": False})()

    def stop(self):
        pass

    def OutputStream(self, **kw):
        self.channels = kw["channels"]
        self.callback = kw["callback"]
        return type(
            "St",
            (),
            {
                "start": lambda s: None,
                "stop": lambda s: None,
                "close": lambda s: None,
                "abort": lambda s: None,
            },
        )()


class LoudSynth:
    sample_rate = 48_000
    finished = True

    def render(self, frames: int) -> np.ndarray:
        return np.ones(frames, np.float32)  # full scale, as if every limit upstream failed

    def release(self) -> None:
        pass


def make_output(
    monkeypatch: pytest.MonkeyPatch, max_gain: float
) -> tuple[SoundDeviceOutput, FakeSD]:
    import sys

    fake = FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return SoundDeviceOutput("External Headphones", max_gain), fake


async def test_driver_clips_clips_and_drives_both_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    output, fake = make_output(monkeypatch, 0.25)
    loud = Audio(np.ones(480, np.float32), 48_000)
    await output.play(loud, asyncio.Event())
    assert fake.played is not None and fake.played.shape == (480, 2)
    assert np.max(np.abs(fake.played)) == pytest.approx(0.25)


async def test_driver_clips_live_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    output, fake = make_output(monkeypatch, 0.25)
    done = asyncio.Event()
    done.set()
    await output.play_tone(LoudSynth(), done, 1.0)  # type: ignore[arg-type]
    assert fake.channels == 2
    buf = np.zeros((256, 2), np.float32)
    assert fake.callback is not None
    fake.callback(buf, 256, None, None)
    assert np.max(np.abs(buf)) == pytest.approx(0.25)
