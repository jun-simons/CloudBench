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
    # Crop each frame onto the dish (found by colour). The stream keeps width x height.
    auto_crop: bool = False
    # Border above and below the dish, as a fraction of the dish diameter.
    crop_margin: float = Field(default=0.08, ge=0, le=1)
    # Look for the dish every N frames.
    crop_detect_every: int = Field(default=15, ge=1)


class VisionConfig(_Strict):
    # Image-analysis actions on the camera tool (detect_points, fourier, delaunay, analyze,
    # capture_reference, activity). They need the dish to be visible and red; see the README.
    enabled: bool = True
    # Inner diameter of the dish, used to convert pixels to millimetres.
    cup_diameter_mm: float = Field(default=68.0, gt=0)
    # Overlay streams on the camera tool (points, delaunay, spectrum), each analysed at most this
    # many times a second. 0 disables them; they cost CPU on the workbench even with no viewers.
    stream_fps: float = Field(default=0, ge=0, le=10)


class AgentConfig(_Strict):
    gateway_url: str = "http://localhost:8000"
    workbench_id: str = "wb_faraday"
    # Prefer the CLOUDBENCH_AGENT_SECRET environment variable over putting this in a file.
    secret: str | None = None
    name: str = "Faraday Waves Bench"
    description: str = (
        "A dish of water on a subwoofer, filmed from above, for making Faraday waves."
    )
    vibrator: VibratorConfig = Field(default_factory=VibratorConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)

    @classmethod
    def load(cls, path: Path | None) -> AgentConfig:
        if path is None:
            return cls()
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
