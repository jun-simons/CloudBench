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

import cv2
import numpy as np

from cloudbench.agent.audio import Audio, AudioOutput, WavError, decode_wav
from cloudbench.agent.camera import Camera, FrameStream
from cloudbench.agent.config import CameraConfig, VibratorConfig, VisionConfig
from cloudbench.agent.synth import ToneParams, ToneSynth
from cloudbench.agent.vision import CANON, Analyzer, OverlayStream, PointKind, View, VisionError
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
_VIEWS: tuple[View, ...] = ("points", "delaunay", "spectrum")


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
            title="Vibrator",
            description=(
                "The speaker underneath the dish. Plays low frequency sounds to physically "
                "shake the dish up and down."
            ),
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


_POINT_KINDS = ["crests", "highlights"]
_KIND_PROPERTY = {
    "kind": {
        "type": "string",
        "enum": _POINT_KINDS,
        "default": "crests",
        "description": "crests: one point per wave crest (needs a wave pattern); "
        "highlights: bright specular glints",
    }
}


def _vision_actions() -> list[ActionDescriptor]:
    """Image analysis of the latest camera frame. Quick and read-only, so they may run while the
    vibrator is playing. All of them need the red dish to be visible."""

    def action(
        name: str, title: str, description: str, properties: dict[str, Any]
    ) -> ActionDescriptor:
        return ActionDescriptor(
            name=name,
            title=title,
            description=description,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
            },
            concurrent=True,
        )

    return [
        action(
            "detect_points",
            "Detect points",
            "Find crests or specular highlights in the dish. Returns count, density_per_cm2 and "
            "points_mm as [x, y] in mm from the dish centre.",
            {
                **_KIND_PROPERTY,
                "max_points": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 300},
            },
        ),
        action(
            "fourier",
            "Fourier analysis",
            "2D FFT of the dish pattern. Returns wavelength_mm, k_rad_per_m, contrast, "
            "ring_strength (compare with a still frame), spectral peaks (wavelength_mm, angle_deg, "
            "relative_power) and "
            "symmetry: angular harmonics m=2..12 of the spectral ring (large m=4: square, "
            "m=6: hexagonal, all small: labyrinth or disorder).",
            {"peaks": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6}},
        ),
        action(
            "delaunay",
            "Delaunay lattice analysis",
            "Triangulate the detected points. Returns coordination_histogram (neighbours per "
            "interior point), bond_length_mm, nearest_neighbour_mm and the bond-orientational "
            "order "
            "psi4 (square) and psi6 (hexagonal), global and per-point (_local).",
            _KIND_PROPERTY,
        ),
        action(
            "analyze",
            "Analyze pattern",
            "One call with the dish geometry, fourier summary, crest count and delaunay statistics "
            "(plus activity if a reference was captured).",
            {},
        ),
        action(
            "capture_reference",
            "Capture still reference",
            "Remember the current frame as the still surface. Call it with the vibrator off; "
            "activity is measured against it.",
            {},
        ),
        action(
            "activity",
            "Measure activity",
            "Wave activity: std-dev of the band-passed dish image minus the reference (about 0.6 "
            "is noise, 10 or more is strong waves). Needs capture_reference first, and an "
            "unmoved camera.",
            {},
        ),
    ]


class CameraTool(Tool):
    def __init__(
        self, config: CameraConfig, camera: Camera, vision: VisionConfig | None = None
    ) -> None:
        self.camera = camera
        self._config = config
        self._vision = vision if vision is not None and vision.enabled else None
        self._analyzer = Analyzer(self._vision.cup_diameter_mm) if self._vision else None
        streams = [
            StreamDescriptor(
                name="video",
                access=StreamAccess.PUBLIC,
                width=config.width,
                height=config.height,
                fps=config.fps,
            )
        ]
        description = "View the live camera stream of the faraday dish."
        if self._vision is not None:
            description += (
                " Analysis actions measure the pattern in the latest frame (wavelength, "
                "crests, lattice order, activity)."
            )
            if self._vision.stream_fps > 0:
                description += (
                    " The points, delaunay and spectrum streams show what the analysis sees."
                )
                streams += [
                    StreamDescriptor(
                        name=view,
                        access=StreamAccess.PUBLIC,
                        width=CANON,
                        height=CANON,
                        fps=self._vision.stream_fps,
                    )
                    for view in _VIEWS
                ]
        self.descriptor = ToolDescriptor(
            name="camera",
            type="camera",
            title="Camera",
            description=description,
            actions=_vision_actions() if self._vision is not None else [],
            streams=streams,
        )

    def overlay_streams(self) -> dict[tuple[str, str], FrameStream]:
        """The derived streams to push to the gateway (empty unless vision streams are enabled)."""
        if self._vision is None or self._analyzer is None or self._vision.stream_fps <= 0:
            return {}
        return {
            ("camera", view): OverlayStream(
                self.camera,
                self._analyzer,
                view,
                self._vision.stream_fps,
                self._config.jpeg_quality,
            )
            for view in _VIEWS
        }

    def _latest_frame(self) -> np.ndarray:
        _, jpeg = self.camera.latest()
        frame = (
            None if jpeg is None else cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        )
        if frame is None:
            raise ActionError("no_frame", "No camera frame is available yet")
        return frame

    async def invoke(
        self,
        action: str,
        input: dict[str, Any],
        fetch_file: FileFetcher,
        cancel: asyncio.Event,
    ) -> dict[str, Any]:
        analyzer = self._analyzer
        if analyzer is None or self.descriptor.action(action) is None:
            raise ActionError("unknown_action", f"The camera has no action {action!r}")
        frame = self._latest_frame()
        kind: PointKind = "highlights" if input.get("kind") == "highlights" else "crests"
        calls: dict[str, Callable[[], dict[str, Any]]] = {
            "detect_points": lambda: analyzer.points(
                frame, kind, int(input.get("max_points", 300))
            ),
            "fourier": lambda: analyzer.fourier(frame, int(input.get("peaks", 6))),
            "delaunay": lambda: analyzer.delaunay(frame, kind),
            "analyze": lambda: analyzer.analyze(frame),
            "capture_reference": lambda: analyzer.capture_reference(frame),
            "activity": lambda: analyzer.activity(frame),
        }
        try:
            return await asyncio.to_thread(calls[action])
        except VisionError as exc:
            raise ActionError(exc.code, exc.message) from exc

    def reset(self) -> None:
        pass
