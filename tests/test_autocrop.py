"""Auto-crop onto the dish, using a synthetic red cup on a grey table."""

from __future__ import annotations

import cv2
import numpy as np

from cloudbench.agent.autocrop import CropTracker, find_cup_ellipse, with_autocrop
from cloudbench.agent.config import CameraConfig

W, H = 640, 360


def scene(cx: int = 400, cy: int = 150, r: int = 70) -> np.ndarray:
    frame = np.full((H, W, 3), 120, np.uint8)
    cv2.circle(frame, (cx, cy), r, (30, 30, 200), -1)  # BGR red
    return frame


def test_finds_the_cup() -> None:
    ellipse = find_cup_ellipse(scene())
    assert ellipse is not None
    (cx, cy), (a, b), _ = ellipse
    assert abs(cx - 400) < 3 and abs(cy - 150) < 3 and abs(max(a, b) - 140) < 6


def test_no_cup_returns_none() -> None:
    assert find_cup_ellipse(np.full((H, W, 3), 120, np.uint8)) is None


def test_crop_fills_frame_with_margin_and_keeps_aspect() -> None:
    tracker = CropTracker(W, H, margin=0.1)
    tracker.update(find_cup_ellipse(scene()))
    x, y, w, h = tracker.rect()
    assert abs(h - 140 * 1.2) <= 3  # diameter plus 10 % above and below
    assert abs(w / h - W / H) < 0.02
    assert x <= 400 - 70 and x + w >= 400 + 70 and y <= 150 - 70 and y + h >= 150 + 70


def test_crop_stays_inside_frame_near_edges() -> None:
    tracker = CropTracker(W, H, margin=0.1)
    tracker.update(find_cup_ellipse(scene(cx=590, cy=300, r=60)))
    x, y, w, h = tracker.rect()
    assert x >= 0 and y >= 0 and x + w <= W and y + h <= H


def test_keeps_last_crop_when_cup_lost_and_ignores_jitter() -> None:
    tracker = CropTracker(W, H, margin=0.1)
    tracker.update(find_cup_ellipse(scene()))
    first = tracker.rect()
    tracker.update(None)
    tracker.update(find_cup_ellipse(scene(cx=402)))
    assert tracker.rect() == first


def test_wrapper_output_size_is_unchanged_and_close_is_forwarded() -> None:
    closed = []

    def open_source():
        def read() -> np.ndarray:
            return scene()

        read.close = lambda: closed.append(True)  # type: ignore[attr-defined]
        return read

    config = CameraConfig(driver="mock", width=W, height=H, auto_crop=True, crop_detect_every=1)
    read = with_autocrop(open_source, config)()
    frame = read()
    assert frame is not None and frame.shape == (H, W, 3)
    # The cup now fills much more of the frame than the 1/9 it did uncropped.
    red = (frame[..., 2] > 150) & (frame[..., 0] < 80)
    assert red.mean() > 0.3
    read.close()  # type: ignore[attr-defined]
    assert closed
