"""Hardware-facing limits that do not need a gateway: the audio ceiling and camera selection."""

from __future__ import annotations

import asyncio
import io
import wave

import numpy as np
import pytest

from cloudbench.agent.audio import Audio
from cloudbench.agent.camera import pick_avfoundation_device
from cloudbench.agent.config import VibratorConfig
from cloudbench.agent.tools import ActionError, VibratorTool


class RecordingOutput:
    def __init__(self) -> None:
        self.played: list[Audio] = []

    async def play(self, audio: Audio, cancel: asyncio.Event) -> float:
        self.played.append(audio)
        return audio.duration_s

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
