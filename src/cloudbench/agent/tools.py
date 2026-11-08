"""Tools exposed by this workbench: their descriptors and action implementations.

Validation here is the last line of defense for the hardware: the gateway
already checked the input against the schema, but the agent re-checks limits
because it is the only component that knows the device's real constraints.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import numpy as np

from cloudbench.agent.audio import Audio, AudioOutput, WavError, decode_wav
from cloudbench.agent.camera import Camera
from cloudbench.agent.config import CameraConfig, VibratorConfig
from cloudbench.agent.synth import ToneParams, ToneSynth
from cloudbench.protocol import (
    FILE_ID_FORMAT,
    ActionDescriptor,
    StreamAccess,
    StreamDescriptor,
    ToolDescriptor,
)
from cloudbench.signals import Shape

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


_SHAPES = [shape.value for shape in Shape]


class VibratorTool(Tool):
    """The vibration exciter, driven through an audio output.

    Two ways to drive it:

    * ``play_tone`` starts a continuous tone that ``set_tone`` adjusts live and
      ``stop`` ends: what an interactive UI uses.
    * ``play_waveform`` plays an uploaded WAV clip: for arbitrary signals via the API.
    """

    def __init__(self, config: VibratorConfig, output: AudioOutput) -> None:
        self._config = config
        self._output = output
        self._tone: ToneSynth | None = None
        self._stop = asyncio.Event()
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        tone_properties: dict[str, Any] = {
            "shape": {"type": "string", "enum": _SHAPES, "default": Shape.SINE.value},
            "frequency_hz": {
                "type": "number",
                "minimum": config.min_frequency_hz,
                "maximum": config.max_frequency_hz,
            },
            "amplitude": {
                "type": "number",
                "minimum": 0,
                "maximum": config.max_gain,
                "description": "Fraction of full scale",
            },
        }
        self.descriptor = ToolDescriptor(
            name="vibrator",
            type="vibration-exciter",
            title="TODO:text - vibrator title",
            description="TODO:text - what the vibrator is and what it shakes",
            actions=[
                ActionDescriptor(
                    name="play_tone",
                    title="Play tone",
                    description=(
                        "Start a continuous tone. Adjust it live with set_tone and end it "
                        "with stop; it also ends after duration_s. Completes when it ends."
                    ),
                    input_schema={
                        "type": "object",
                        "required": ["frequency_hz", "amplitude"],
                        "additionalProperties": False,
                        "properties": {
                            **tone_properties,
                            "duration_s": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "maximum": config.max_duration_s,
                                "default": config.max_duration_s,
                            },
                        },
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"played_s": {"type": "number"}},
                    },
                    limits={"max_duration_s": config.max_duration_s},
                    safety={"interruptible": True, "safe_state_action": "stop"},
                ),
                ActionDescriptor(
                    name="set_tone",
                    title="Adjust tone",
                    description=(
                        "Change the playing tone's shape, frequency or amplitude. Changes are "
                        "smoothed. Fails with not_playing if no tone is playing."
                    ),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "minProperties": 1,
                        "properties": tone_properties,
                    },
                    concurrent=True,
                ),
                ActionDescriptor(
                    name="stop",
                    title="Stop",
                    description="Stop the tone or clip that is playing (fades out quickly).",
                    input_schema=empty,
                    concurrent=True,
                ),
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
            ],
        )

    async def invoke(
        self,
        action: str,
        input: dict[str, Any],
        fetch_file: FileFetcher,
        cancel: asyncio.Event,
    ) -> dict[str, Any]:
        match action:
            case "play_tone":
                return await self._play_tone(input, cancel)
            case "set_tone":
                return self._set_tone(input)
            case "stop":
                # Ends whatever is playing; tones fade out, clips stop.
                self._stop.set()
                return {}
            case "play_waveform":
                return await self._play_waveform(input, fetch_file, cancel)
            case _:
                raise ActionError("unknown_action", f"Unknown action {action!r}")

    # ----------------------------------------------------------------- tone

    def _tone_params(self, input: dict[str, Any], base: ToneParams | None) -> ToneParams:
        cfg = self._config
        try:
            shape = Shape(input["shape"]) if "shape" in input else None
        except ValueError as exc:
            raise ActionError("invalid_input", f"shape must be one of {_SHAPES}") from exc
        frequency = input.get("frequency_hz")
        amplitude = input.get("amplitude")
        in_range = cfg.min_frequency_hz <= float(frequency or 0) <= cfg.max_frequency_hz
        if frequency is not None and not in_range:
            raise ActionError(
                "invalid_input",
                f"frequency_hz must be within [{cfg.min_frequency_hz}, {cfg.max_frequency_hz}]",
            )
        if amplitude is not None and not 0 <= float(amplitude) <= cfg.max_gain:
            raise ActionError("invalid_input", f"amplitude must be within [0, {cfg.max_gain}]")
        if base is None:
            if frequency is None or amplitude is None:
                raise ActionError("invalid_input", "frequency_hz and amplitude are required")
            return ToneParams(shape or Shape.SINE, float(frequency), float(amplitude))
        return ToneParams(
            shape or base.shape,
            base.frequency_hz if frequency is None else float(frequency),
            base.amplitude if amplitude is None else float(amplitude),
        )

    async def _play_tone(self, input: dict[str, Any], cancel: asyncio.Event) -> dict[str, Any]:
        if self._tone is not None:
            raise ActionError("busy", "A tone is already playing; use set_tone or stop")
        duration = float(input.get("duration_s", self._config.max_duration_s))
        if not 0 < duration <= self._config.max_duration_s:
            raise ActionError(
                "invalid_input", f"duration_s must be within (0, {self._config.max_duration_s}]"
            )
        synth = ToneSynth(self._tone_params(input, None))
        self._tone = synth
        self._stop = asyncio.Event()
        stop = self._stop

        # Ends on stop, on cancel, or when the duration is up.
        try:
            async with either(stop, cancel) as done:
                played = await self._output.play_tone(synth, done, duration)
        finally:
            if self._tone is synth:
                self._tone = None
        if cancel.is_set():
            raise asyncio.CancelledError
        return {"played_s": round(played, 3), **_params_out(synth.params)}

    def _set_tone(self, input: dict[str, Any]) -> dict[str, Any]:
        synth = self._tone
        if synth is None:
            raise ActionError("not_playing", "No tone is playing; start one with play_tone")
        params = self._tone_params(input, synth.params)
        synth.update(params.shape, params.frequency_hz, params.amplitude)
        return _params_out(params)

    # ----------------------------------------------------------------- clip

    async def _play_waveform(
        self, input: dict[str, Any], fetch_file: FileFetcher, cancel: asyncio.Event
    ) -> dict[str, Any]:
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

        samples = (np.tile(audio.samples, loop) * gain).astype(np.float32)
        self._stop = asyncio.Event()
        async with either(self._stop, cancel) as done:
            played = await self._output.play(Audio(samples, audio.sample_rate), done)
        if cancel.is_set():
            raise asyncio.CancelledError
        return {"played_s": round(played, 3)}

    def reset(self) -> None:
        self._stop.set()
        synth = self._tone
        if synth is not None:
            synth.release()
        try:
            self._output.stop()
        except Exception:
            log.exception("Failed to stop audio output")


@contextlib.asynccontextmanager
async def either(*events: asyncio.Event) -> AsyncIterator[asyncio.Event]:
    """An event that is set as soon as any of ``events`` is."""
    combined = asyncio.Event()

    async def watch(event: asyncio.Event) -> None:
        await event.wait()
        combined.set()

    watchers = [asyncio.create_task(watch(e)) for e in events]
    try:
        yield combined
    finally:
        for task in watchers:
            task.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


def _params_out(params: ToneParams) -> dict[str, Any]:
    return {
        "shape": params.shape.value,
        "frequency_hz": params.frequency_hz,
        "amplitude": params.amplitude,
    }


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
