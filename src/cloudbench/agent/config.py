from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VibratorConfig(_Strict):
    # "sounddevice" plays through an audio output (e.g. into an amplifier driving the
    # exciter); "mock" only simulates playback, for development without hardware.
    driver: Literal["sounddevice", "mock"] = "mock"
    # sounddevice output device name or index; None uses the system default.
    device: str | int | None = None
    max_duration_s: float = 180
    max_gain: float = Field(default=1.0, gt=0, le=1)
    # Frequency range the exciter may be driven at.
    min_frequency_hz: float = Field(default=1, gt=0)
    max_frequency_hz: float = Field(default=2000, gt=0)
    sample_rates_hz: list[int] = Field(default_factory=lambda: [44_100, 48_000])


class CameraConfig(_Strict):
    # "opencv" reads any camera the OS exposes (USB webcam, iPhone via Continuity
    # Camera, capture card); "ffmpeg" (macOS) does the same through ffmpeg/AVFoundation and
    # also accepts a device name such as "iPhone Camera"; "mock" draws synthetic frames.
    driver: Literal["opencv", "ffmpeg", "mock"] = "mock"
    # opencv: device index. ffmpeg: AVFoundation index or a name substring.
    device: int | str = 0
    width: int = 1280
    height: int = 720
    fps: float = 30
    jpeg_quality: int = Field(default=80, ge=10, le=100)


class AgentConfig(_Strict):
    gateway_url: str = "http://localhost:8000"
    workbench_id: str = "wb_faraday"
    # Prefer the CLOUDBENCH_AGENT_SECRET environment variable over putting this in a file.
    secret: str | None = None
    name: str = "TODO:text - workbench name"
    description: str = "TODO:text - one-line workbench description"
    vibrator: VibratorConfig = Field(default_factory=VibratorConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)

    @classmethod
    def load(cls, path: Path | None) -> AgentConfig:
        if path is None:
            return cls()
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
