"""Camera drivers. Each produces JPEG frames on a background thread; the agent
forwards the most recent one, so a slow uplink drops frames instead of lagging."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import cv2
import numpy as np

from cloudbench.agent.audio import SimulatedSurface
from cloudbench.agent.config import CameraConfig

log = logging.getLogger(__name__)

FrameSource = Callable[[], np.ndarray | None]


class Camera:
    """Runs a frame source on a thread and exposes JPEG frames as an async iterator."""

    def __init__(self, config: CameraConfig, open_source: Callable[[], FrameSource]) -> None:
        self._config = config
        self._open_source = open_source
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: bytes | None = None
        self._seq = 0
        self._cond = threading.Condition()
        self.fault: str | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self._config.jpeg_quality]
        period = 1.0 / self._config.fps
        while not self._stop.is_set():
            read = None
            try:
                read = self._open_source()
                self.fault = None
                next_at = time.monotonic()
                while not self._stop.is_set():
                    frame = read()
                    if frame is None:
                        raise RuntimeError("camera returned no frame")
                    ok, jpeg = cv2.imencode(".jpg", frame, params)
                    if ok:
                        with self._cond:
                            self._latest = jpeg.tobytes()
                            self._seq += 1
                            self._cond.notify_all()
                    next_at += period
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_at = time.monotonic()
            except Exception as exc:
                self.fault = str(exc)
                log.warning("Camera error: %s; retrying in 2 s", exc)
                self._stop.wait(2)
            finally:
                # Sources that own a subprocess expose close() so it never outlives the loop.
                closer = getattr(read, "close", None)
                if closer is not None:
                    closer()

    def _wait_frame(self, after: int, timeout: float) -> tuple[int, bytes | None]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after or self._stop.is_set(), timeout)
            return self._seq, self._latest

    async def frames(self) -> AsyncIterator[bytes]:
        seq = 0
        while not self._stop.is_set():
            new_seq, frame = await asyncio.to_thread(self._wait_frame, seq, 1.0)
            if new_seq != seq and frame is not None:
                seq = new_seq
                yield frame


def opencv_source(config: CameraConfig) -> Callable[[], FrameSource]:
    def open_source() -> FrameSource:
        cap = cv2.VideoCapture(config.device)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open camera {config.device!r}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.height)
        cap.set(cv2.CAP_PROP_FPS, config.fps)
        log.info(
            "Camera %r opened at %dx%d @ %.0f fps",
            config.device,
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            cap.get(cv2.CAP_PROP_FPS),
        )

        def read() -> np.ndarray | None:
            ok, frame = cap.read()
            if not ok:
                cap.release()
                return None
            return frame

        return read

    return open_source


def _list_avfoundation_video_devices() -> list[tuple[str, str]]:
    """(index, name) of macOS video devices, as ffmpeg numbers them."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
        timeout=15,
    ).stderr
    if "AVFoundation video devices:" not in out:
        raise RuntimeError("ffmpeg could not list AVFoundation devices (is ffmpeg installed?)")
    video = out.split("AVFoundation video devices:")[1].split("AVFoundation audio devices:")[0]
    return re.findall(r"\[(\d+)\] (.+)", video)


def pick_avfoundation_device(device: int | str, devices: list[tuple[str, str]]) -> str:
    """An int is an ffmpeg device index. A string is matched against names, case-insensitively;
    "Desk View" cameras are skipped because they share a name prefix with the real camera."""
    if isinstance(device, int):
        return str(device)
    wanted = device.lower()
    for index, name in devices:
        if wanted in name.lower() and "desk view" not in name.lower():
            return index
    raise RuntimeError(f"no video device matching {device!r}; found {[n for _, n in devices]}")


def ffmpeg_source(config: CameraConfig) -> Callable[[], FrameSource]:
    """macOS camera via ffmpeg/AVFoundation. Unlike OpenCV it selects the device by name
    (so a Continuity Camera iPhone keeps working when device indexes shift) and honors the
    requested resolution and frame rate."""
    w, h = config.width, config.height
    if w % 2 or h % 2:
        raise ValueError("ffmpeg camera width and height must be even")
    frame_bytes = w * h * 3 // 2  # NV12

    def open_source() -> FrameSource:
        index = pick_avfoundation_device(config.device, _list_avfoundation_video_devices())
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-framerate", f"{config.fps:g}",
            "-video_size", f"{w}x{h}", "-pixel_format", "nv12",
            "-fflags", "nobuffer", "-i", index,
            "-f", "rawvideo", "-pix_fmt", "nv12", "-fps_mode", "passthrough", "-",
        ]  # fmt: skip
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 2
        )
        log.info("Camera %r (avfoundation device %s) opened at %dx%d @ %g fps",
                 config.device, index, w, h, config.fps)  # fmt: skip

        def read() -> np.ndarray | None:
            assert proc.stdout is not None
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                reason = ""
                if proc.stderr is not None:
                    proc.kill()
                    reason = proc.stderr.read().decode(errors="replace").strip()[-300:]
                log.warning("ffmpeg camera stopped: %s", reason or "no output")
                return None
            yuv = np.frombuffer(buf, np.uint8).reshape(h * 3 // 2, w)
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)

        def close() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()

        read.close = close  # type: ignore[attr-defined]
        return read

    return open_source


def mock_source(config: CameraConfig, surface: SimulatedSurface) -> Callable[[], FrameSource]:
    """A top-down view of a petri dish whose surface ripples while the mock vibrator plays."""
    w, h = config.width, config.height
    cx, cy, radius = w / 2, h / 2, min(w, h) * 0.4
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = xs - cx, ys - cy
    r = np.sqrt(dx * dx + dy * dy)
    theta = np.arctan2(dy, dx)
    inside = r < radius
    started = time.monotonic()

    def open_source() -> FrameSource:
        def read() -> np.ndarray:
            t = time.monotonic() - started
            frame = np.full((h, w, 3), 24, np.uint8)
            freq = surface.frequency_hz
            if freq:
                # Faraday waves respond at half the drive frequency; draw a standing
                # square-ish lattice whose spatial scale shrinks with frequency.
                k = 2 * math.pi / max(radius / 4, radius * 12 / freq)
                phase = math.cos(math.pi * freq / 2 * t / 10)
                height = (np.cos(k * r * np.cos(theta)) + np.cos(k * r * np.sin(theta))) * phase
                level = 120 + 90 * surface.amplitude * height / 2
            else:
                level = np.full_like(r, 120)
            shade = np.clip(level, 0, 255).astype(np.uint8)
            frame[inside] = np.stack([shade, shade * 0.85, shade * 0.6], axis=-1)[inside].astype(
                np.uint8
            )
            cv2.circle(frame, (int(cx), int(cy)), int(radius), (200, 200, 200), 2, cv2.LINE_AA)
            label = datetime.now(UTC).strftime("MOCK CAMERA  %H:%M:%S.%f")[:-3]
            if freq:
                label += f"  drive {freq:.0f} Hz"
            # Bottom-left, clear of the badges the web app overlays at the top.
            cv2.putText(
                frame, label, (12, h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1
            )
            return frame

        return read

    return open_source
