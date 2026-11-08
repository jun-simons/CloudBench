"""Tools exposed by this workbench: their descriptors and action implementations.

Validation here is the last line of defense for the hardware: the gateway
already checked the input against the schema, but the agent re-checks limits
because it is the only component that knows the device's real constraints.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from cloudbench.agent.audio import Audio, AudioOutput, WavError, decode_wav
from cloudbench.agent.camera import Camera
from cloudbench.agent.config import CameraConfig, VibratorConfig
from cloudbench.protocol import (
    FILE_ID_FORMAT,
    ActionDescriptor,
    StreamAccess,
    StreamDescriptor,
    ToolDescriptor,
)

log = logging.getLogger(__name__)


class ActionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


FileFetcher = Callable[[str], Awaitable[bytes]]


class Tool(ABC):
    descriptor: ToolDescriptor

    @abstractmethod
    async def invoke(
        self,
        action: str,
        input: dict[str, Any],
        fetch_file: FileFetcher,
        cancel: asyncio.Event,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def reset(self) -> None:
        """Put the device into its safe state. Must be fast and must not raise."""


class VibratorTool(Tool):
    def __init__(self, config: VibratorConfig, output: AudioOutput) -> None:
        self._config = config
        self._output = output
        self.descriptor = ToolDescriptor(
            name="vibrator",
            type="vibration-exciter",
            title="TODO:text - vibrator title",
            description="TODO:text - what the vibrator is and what it shakes",
            actions=[
                ActionDescriptor(
                    name="play_waveform",
                    title="Play waveform",
                    description=(
                        "Drive the vibration exciter with a WAV file. "
                        "Completes when playback ends or is stopped."
                    ),
                    input_schema={
                        "type": "object",
                        "required": ["audio"],
                        "additionalProperties": False,
                        "properties": {
                            "audio": {
                                "type": "string",
                                "format": FILE_ID_FORMAT,
                                "contentMediaType": "audio/wav",
                                "description": "ID of an uploaded WAV File",
                            },
                            "gain": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": config.max_gain,
                                "default": min(0.5, config.max_gain),
                            },
                            "loop": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
                        },
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"played_s": {"type": "number"}},
                    },
                    limits={
                        "max_duration_s": config.max_duration_s,
                        "sample_rates_hz": config.sample_rates_hz,
                    },
                    safety={"interruptible": True, "safe_state_action": "stop"},
                ),
                ActionDescriptor(
                    name="stop",
                    title="Stop",
                    description="Stop any playback immediately.",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
            ],
        )

    async def invoke(
        self,
        action: str,
        input: dict[str, Any],
        fetch_file: FileFetcher,
        cancel: asyncio.Event,
    ) -> dict[str, Any]:
        if action == "stop":
            self.reset()
            return {}
        if action != "play_waveform":
            raise ActionError("unknown_action", f"Unknown action {action!r}")

        gain = float(input.get("gain", 0.5))
        loop = int(input.get("loop", 1))
        if not 0 <= gain <= self._config.max_gain:
            raise ActionError("invalid_input", f"gain must be within [0, {self._config.max_gain}]")
        if not 1 <= loop <= 10:
            raise ActionError("invalid_input", "loop must be within [1, 10]")

        data = await fetch_file(input["audio"])
        try:
            audio = await asyncio.to_thread(decode_wav, data)
        except WavError as exc:
            raise ActionError("invalid_input", str(exc)) from exc
        if audio.sample_rate not in self._config.sample_rates_hz:
            raise ActionError(
                "invalid_input",
                f"sample rate {audio.sample_rate} Hz not in {self._config.sample_rates_hz}",
            )
        total = audio.duration_s * loop
        if total > self._config.max_duration_s:
            raise ActionError(
                "invalid_input",
                f"playback of {total:.1f} s exceeds the {self._config.max_duration_s:g} s limit",
            )

        # Hard ceiling regardless of gain or file content: nothing leaves above max_gain.
        samples = np.clip(
            np.tile(audio.samples, loop) * gain, -self._config.max_gain, self._config.max_gain
        ).astype(np.float32)
        played = await self._output.play(Audio(samples, audio.sample_rate), cancel)
        if cancel.is_set():
            raise asyncio.CancelledError
        return {"played_s": round(played, 3)}

    def reset(self) -> None:
        try:
            self._output.stop()
        except Exception:
            log.exception("Failed to stop audio output")


class CameraTool(Tool):
    def __init__(self, config: CameraConfig, camera: Camera) -> None:
        self.camera = camera
        self.descriptor = ToolDescriptor(
            name="camera",
            type="camera",
            title="TODO:text - camera title",
            description="TODO:text - what the camera looks at",
            streams=[
                StreamDescriptor(
                    name="video",
                    access=StreamAccess.PUBLIC,
                    width=config.width,
                    height=config.height,
                    fps=config.fps,
                )
            ],
        )

    async def invoke(
        self,
        action: str,
        input: dict[str, Any],
        fetch_file: FileFetcher,
        cancel: asyncio.Event,
    ) -> dict[str, Any]:
        raise ActionError("unknown_action", "The camera has no actions")

    def reset(self) -> None:
        pass
