"""Image analysis of the dish, on synthetic images: a red dish with a known wave pattern."""

from __future__ import annotations

import asyncio
from typing import Any

import cv2
import numpy as np
import pytest

from cloudbench.agent.camera import Camera
from cloudbench.agent.config import CameraConfig, VisionConfig
from cloudbench.agent.tools import ActionError, CameraTool
from cloudbench.agent.vision import (
    Analyzer,
    OverlayStream,
    VisionError,
    lattice_stats,
    triangulate,
)

W, H, DIAMETER, WAVELENGTH_PX = 800, 600, 400, 16.0


def dish_image(
    pattern: str = "hex", amplitude: float = 35, tilt: float = 1.0, seed: int = 0
) -> np.ndarray:
    """A grey table with a red dish. Inside, brightness follows ``pattern``: 'flat', 'stripes',
    'square' or 'hex' (plane waves of WAVELENGTH_PX image pixels). ``tilt`` squashes the dish."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:H, :W].astype(np.float32)
    x, y = xx - W / 2, yy - H / 2
    angles = {"flat": [], "stripes": [0], "square": [0, 90], "hex": [0, 60, 120]}[pattern]
    wave = np.zeros_like(x)
    for angle in angles:
        direction = np.cos(np.radians(angle)) * x + np.sin(np.radians(angle)) * y
        wave += np.cos(2 * np.pi * direction / WAVELENGTH_PX)
    brightness = 150 + amplitude * wave / max(len(angles), 1) * 2
    brightness = brightness + rng.normal(0, 1, brightness.shape)
    image = np.full((H, W, 3), 90, np.uint8)
    inside = (x / (DIAMETER / 2)) ** 2 + (y / (DIAMETER / 2 * tilt)) ** 2 <= 1
    shade = np.clip(brightness, 0, 255)
    red = np.stack([shade * 0.2, shade * 0.2, shade], axis=-1).astype(np.uint8)  # BGR: strongly red
    image[inside] = red[inside]
    return image


def test_fourier_measures_wavelength_and_symmetry() -> None:
    analyzer = Analyzer(cup_diameter_mm=68)
    # The dish is 400 px wide and 68 mm across, and 82% of it is analysed.
    expected_mm = WAVELENGTH_PX * 68 / DIAMETER
    hexagonal = analyzer.fourier(dish_image("hex"), 6)
    assert hexagonal["wavelength_mm"] == pytest.approx(expected_mm, rel=0.12)
    assert hexagonal["n_directions"] == 3
    assert hexagonal["symmetry"]["6"] > 2 * hexagonal["symmetry"]["4"]
    square = analyzer.fourier(dish_image("square"), 6)
    assert square["symmetry"]["4"] > 2 * square["symmetry"]["6"]
    stripes = analyzer.fourier(dish_image("stripes"), 6)
    assert stripes["n_directions"] == 1
    assert stripes["spottiness"] > hexagonal["spottiness"]


def test_dish_tilt_does_not_change_the_measurement() -> None:
    analyzer = Analyzer(cup_diameter_mm=68)
    flat = analyzer.fourier(dish_image("hex"), 3)["wavelength_mm"]
    # Squashed along y the dish is an ellipse; the pattern is squashed too, so only check that
    # the dish is found and the wavelength stays in the same range.
    squashed = analyzer.analyze(dish_image("hex", tilt=0.8))
    assert squashed["dish"]["axis_ratio"] == pytest.approx(0.8, abs=0.03)
    assert squashed["fourier"]["wavelength_mm"] == pytest.approx(flat, rel=0.3)


def test_points_and_delaunay_on_a_hexagonal_pattern() -> None:
    analyzer = Analyzer(cup_diameter_mm=68)
    frame = dish_image("hex")
    result = analyzer.points(frame, "crests", max_points=50)
    # Hexagonal crests: one per 2 / (sqrt(3) * (WAVELENGTH_PX / canonical_scale ...)); just
    # require a sensible number, truncation and coordinates inside the dish.
    assert result["count"] > 60
    assert result["truncated"] is True and len(result["points_mm"]) == 50
    radius = result["dish_radius_mm"]
    assert all(np.hypot(x, y) <= radius * 1.05 for x, y in result["points_mm"])

    lattice = analyzer.delaunay(frame, "crests")
    assert lattice["coordination_mean"] == pytest.approx(6, abs=0.7)
    assert lattice["psi6_local"] > lattice["psi4_local"]
    assert lattice["psi6_local"] > 0.6


def test_square_pattern_is_more_square_than_hexagonal() -> None:
    lattice = Analyzer(68).delaunay(dish_image("square"), "crests")
    assert lattice["psi4_local"] > lattice["psi6_local"]


def test_triangulation_of_a_known_grid() -> None:
    grid = np.array([[x, y] for y in range(100, 400, 50) for x in range(100, 400, 50)], float)
    edges = triangulate(grid)
    # 6x6 grid: 60 axis-aligned edges plus one diagonal per cell (25 cells).
    assert len(edges) == 60 + 25
    stats = lattice_stats(grid, mm_per_px=0.1)
    assert stats["bond_length_mm"]["median"] == pytest.approx(5.0, rel=0.01)
    assert lattice_stats(grid[:5], 0.1)["note"].startswith("too few")


def test_highlights_find_bright_glints() -> None:
    frame = dish_image("flat")
    for cx, cy in [(300, 250), (400, 300), (500, 350), (380, 420)]:
        cv2.circle(frame, (cx, cy), 4, (255, 255, 255), -1)
    result = Analyzer(68).points(frame, "highlights", max_points=10)
    assert result["count"] == 4


def test_activity_against_a_still_reference() -> None:
    analyzer = Analyzer(68)
    with pytest.raises(VisionError) as error:
        analyzer.activity(dish_image("flat"))
    assert error.value.code == "no_reference"

    analyzer.capture_reference(dish_image("flat", seed=1))
    still = analyzer.activity(dish_image("flat", seed=2))["activity"]
    waves = analyzer.activity(dish_image("hex", seed=3))["activity"]
    assert still < 1.5
    assert waves > 10 * still


def test_no_dish_is_a_clear_error() -> None:
    blank = np.full((H, W, 3), 90, np.uint8)
    with pytest.raises(VisionError) as error:
        Analyzer(68).fourier(blank, 3)
    assert error.value.code == "no_dish"


class _StaticCamera(Camera):
    """A camera holding one frame, without a thread."""

    def __init__(self, frame: np.ndarray | None) -> None:
        super().__init__(CameraConfig(driver="mock", width=W, height=H), lambda: lambda: None)
        if frame is not None:
            ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            assert ok
            self._latest, self._seq = jpeg.tobytes(), 1


async def invoke(tool: CameraTool, action: str, **input: Any) -> dict[str, Any]:
    return await tool.invoke(action, input, _unused_fetch, asyncio.Event())


async def _unused_fetch(file_id: str) -> bytes:
    raise AssertionError("the camera never fetches files")


async def test_camera_tool_actions_run_on_the_latest_frame() -> None:
    tool = CameraTool(CameraConfig(driver="mock"), _StaticCamera(dish_image("hex")), VisionConfig())
    assert {a.name for a in tool.descriptor.actions} == {
        "detect_points", "fourier", "delaunay", "analyze", "capture_reference", "activity",
    }  # fmt: skip
    assert all(a.concurrent for a in tool.descriptor.actions)
    assert [s.name for s in tool.descriptor.streams] == ["video"]  # overlays are off by default
    assert tool.overlay_streams() == {}

    fourier = await invoke(tool, "fourier", peaks=2)
    assert len(fourier["peaks"]) <= 2 and fourier["wavelength_mm"] > 0
    points = await invoke(tool, "detect_points", kind="highlights", max_points=5)
    assert points["kind"] == "highlights"
    with pytest.raises(ActionError) as error:
        await invoke(tool, "activity")
    assert error.value.code == "no_reference"
    await invoke(tool, "capture_reference")
    assert "activity" in await invoke(tool, "analyze")
    with pytest.raises(ActionError) as unknown:
        await invoke(tool, "explode")
    assert unknown.value.code == "unknown_action"


async def test_camera_tool_without_vision_is_unchanged() -> None:
    for vision in (None, VisionConfig(enabled=False)):
        tool = CameraTool(CameraConfig(driver="mock"), _StaticCamera(dish_image("hex")), vision)
        assert tool.descriptor.actions == []
        assert [s.name for s in tool.descriptor.streams] == ["video"]
        with pytest.raises(ActionError) as error:
            await invoke(tool, "analyze")
        assert error.value.code == "unknown_action"


async def test_missing_frame_and_missing_dish_report_errors() -> None:
    no_frame = CameraTool(CameraConfig(driver="mock"), _StaticCamera(None), VisionConfig())
    with pytest.raises(ActionError) as error:
        await invoke(no_frame, "analyze")
    assert error.value.code == "no_frame"
    blank = CameraTool(
        CameraConfig(driver="mock"), _StaticCamera(np.full((H, W, 3), 90, np.uint8)), VisionConfig()
    )
    with pytest.raises(ActionError) as error:
        await invoke(blank, "analyze")
    assert error.value.code == "no_dish"


async def test_overlay_streams_are_advertised_and_produce_images() -> None:
    camera = _StaticCamera(dish_image("hex"))
    tool = CameraTool(CameraConfig(driver="mock"), camera, VisionConfig(stream_fps=10))
    assert [s.name for s in tool.descriptor.streams] == ["video", "points", "delaunay", "spectrum"]
    streams = tool.overlay_streams()
    assert set(streams) == {("camera", "points"), ("camera", "delaunay"), ("camera", "spectrum")}
    for stream in streams.values():
        assert isinstance(stream, OverlayStream)
        jpeg = await asyncio.wait_for(anext(aiter(stream.frames())), 30)
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert image is not None and image.shape[:2] == (480, 480)
