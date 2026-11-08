"""Automatic crop onto the dish.

Ported from the CameraTest rig's ``find_cup_ellipse``: the cup is found by colour (a red/orange
mask, closed and reduced to its largest contour, then ``fitEllipse``). The crop keeps the stream's
aspect ratio, is sized so the cup fills the frame height with a small border above and below, and
is resized back to the configured size, so the stream dimensions never change.

Detection runs every few frames, the crop moves only when the cup has really moved (deadband)
and then eases toward the new position, and when the cup is lost the last crop is kept.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import cv2
import numpy as np

from cloudbench.agent.config import CameraConfig

log = logging.getLogger(__name__)

# Ellipse = ((cx, cy), (axis1, axis2), angle_deg), as returned by cv2.fitEllipse.
Ellipse = tuple[Sequence[float], Sequence[float], float]

MIN_CUP_FRACTION = 0.15  # cup diameter must be at least this fraction of the short frame side
MIN_AXIS_RATIO = 0.5  # reject very elongated blobs: not a cup seen from above


def find_cup_ellipse(bgr: np.ndarray) -> Ellipse | None:
    """The cup as a fitted ellipse, or None if no plausible cup is visible."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = ((hue < 15) | (hue > 170)) & (sat > 90) & (val > 50)
    mask = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((15, 15)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    biggest = max(contours, key=cv2.contourArea)
    if len(biggest) < 5:  # fitEllipse needs at least 5 points
        return None
    ellipse = cv2.fitEllipse(biggest)
    (_, _), (a, b), _ = ellipse
    if max(a, b) < MIN_CUP_FRACTION * min(bgr.shape[:2]) or min(a, b) < MIN_AXIS_RATIO * max(a, b):
        return None
    return ellipse


class CropTracker:
    """Turns occasional cup detections into a stable crop rectangle."""

    def __init__(self, width: int, height: int, margin: float, smoothing: float = 0.3) -> None:
        self._w = width
        self._h = height
        self._margin = margin
        self._alpha = smoothing
        self._state: np.ndarray | None = None  # (cx, cy, diameter)

    def update(self, ellipse: Ellipse | None) -> None:
        if ellipse is None:
            return  # keep the last crop
        (cx, cy), (a, b), _ = ellipse
        target = np.array([cx, cy, max(a, b)])
        if self._state is None:
            self._state = target
        elif np.any(np.abs(target - self._state) > 0.03 * self._state[2]):  # deadband
            self._state = self._state + self._alpha * (target - self._state)

    def rect(self) -> tuple[int, int, int, int]:
        """(x, y, w, h) of the crop; the whole frame until a cup has been seen."""
        if self._state is None:
            return 0, 0, self._w, self._h
        cx, cy, d = self._state
        crop_h = min(d * (1 + 2 * self._margin), self._h)
        crop_w = crop_h * self._w / self._h  # same aspect as the frame, so never wider than it
        x = float(np.clip(cx - crop_w / 2, 0, self._w - crop_w))
        y = float(np.clip(cy - crop_h / 2, 0, self._h - crop_h))
        return int(x), int(y), max(2, int(crop_w)), max(2, int(crop_h))


def with_autocrop(
    open_source: Callable[[], Callable[[], np.ndarray | None]], config: CameraConfig
) -> Callable[[], Callable[[], np.ndarray | None]]:
    """Wrap a frame source so each frame is cropped onto the dish."""

    def open_cropped() -> Callable[[], np.ndarray | None]:
        read = open_source()
        tracker: CropTracker | None = None
        count = 0

        def read_cropped() -> np.ndarray | None:
            nonlocal count, tracker
            frame = read()
            if frame is None:
                return None
            h, w = frame.shape[:2]  # the camera may not have honored the requested size
            if tracker is None:
                tracker = CropTracker(w, h, config.crop_margin)
            if count % config.crop_detect_every == 0:
                try:
                    tracker.update(find_cup_ellipse(frame))
                except cv2.error:
                    log.exception("Cup detection failed")
            count += 1
            x, y, cw, ch = tracker.rect()
            if (cw, ch) == (w, h):
                return frame
            return cv2.resize(frame[y : y + ch, x : x + cw], (w, h), interpolation=cv2.INTER_AREA)

        close = getattr(read, "close", None)
        if close is not None:
            read_cropped.close = close  # type: ignore[attr-defined]
        return read_cropped

    return open_cropped
