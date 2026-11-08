"""Computer vision for the dish, ported from the CameraTest toolkit.

Every measurement starts the same way: find the dish (colour mask, ``fitEllipse``), warp the ellipse
into a fixed-size circle (so tilt and camera distance do not matter), keep the inner part of the
radius (drops the sloped wall) and subtract a heavy blur (drops lighting gradients). One canonical
pixel is then ``cup_diameter_mm * FRAC / CANON`` millimetres.

* ``fourier``: wavelength, spectral peaks and the angular harmonics of the spectral ring.
* ``points``: crest positions (difference of Gaussians) or specular highlights.
* ``delaunay``: neighbours per point, bond lengths and bond-orientational order (triangulation).
* ``capture_reference`` / ``activity``: how much the surface differs from a still reference.

Only OpenCV and numpy are used. The same code renders the optional overlay streams.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy as np

from cloudbench.agent.autocrop import Ellipse, find_cup_ellipse
from cloudbench.agent.camera import Camera

log = logging.getLogger(__name__)

CANON = 480  # side of the canonical dish image, in pixels
FRAC = 0.82  # fraction of the dish radius that is analysed
_FFT_N = 2 * CANON  # zero-padded FFT size
_K_MIN, _K_MAX = 1 / 80, 1 / 8  # spectral band: wavelengths of 8 to 80 canonical pixels
_INTERIOR = 0.85  # points within this fraction of the canonical radius count as interior

PointKind = Literal["crests", "highlights"]
View = Literal["points", "delaunay", "spectrum"]


class VisionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Dish geometry
# --------------------------------------------------------------------------- #


@dataclass(eq=False)
class Dish:
    ellipse: Ellipse
    image: np.ndarray  # canonical BGR image, CANON x CANON
    mask: np.ndarray  # boolean disc inside the canonical image
    mm_per_px: float


@functools.cache
def _disc_mask() -> np.ndarray:
    yy, xx = np.mgrid[:CANON, :CANON]
    return (xx - CANON / 2) ** 2 + (yy - CANON / 2) ** 2 <= (CANON / 2) ** 2


def warp(bgr: np.ndarray, ellipse: Ellipse) -> np.ndarray:
    """The dish ellipse mapped onto a CANON x CANON circle, keeping FRAC of the radius."""
    (cx, cy), (a, b), angle = ellipse
    t = np.radians(angle)
    c, s = np.cos(t), np.sin(t)
    rotate = np.array([[c, s], [-s, c]])
    scale = np.diag([(CANON / 2) / (a / 2 * FRAC), (CANON / 2) / (b / 2 * FRAC)])
    m = scale @ rotate
    offset = np.array([CANON / 2, CANON / 2]) - m @ np.array([cx, cy])
    matrix = np.hstack([m, offset[:, None]])
    return cv2.warpAffine(bgr, matrix, (CANON, CANON), flags=cv2.INTER_AREA)


def locate(bgr: np.ndarray, cup_mm: float, ellipse: Ellipse | None = None) -> Dish:
    ellipse = ellipse or find_cup_ellipse(bgr)
    if ellipse is None:
        raise VisionError("no_dish", "The dish was not found in the camera image")
    return Dish(ellipse, warp(bgr, ellipse), _disc_mask(), cup_mm * FRAC / CANON)


def band_pass(dish_image: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Luminance with slow illumination removed; zero outside the dish."""
    g = cv2.cvtColor(dish_image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g -= cv2.GaussianBlur(g, (0, 0), CANON / 12)
    g[~mask] = 0
    return g


# --------------------------------------------------------------------------- #
# Fourier
# --------------------------------------------------------------------------- #


@functools.cache
def _hann() -> np.ndarray:
    return np.outer(np.hanning(CANON), np.hanning(CANON)).astype(np.float32)


@functools.cache
def _k_grid() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(kr, kx, ky) of the fftshifted spectrum, in cycles per canonical pixel."""
    ky, kx = np.mgrid[-_FFT_N // 2 : _FFT_N // 2, -_FFT_N // 2 : _FFT_N // 2] / _FFT_N
    return np.hypot(kx, ky), kx, ky


def power_spectrum(g: np.ndarray) -> np.ndarray:
    smooth = cv2.GaussianBlur(g, (0, 0), 1.0) * _hann()
    return np.abs(np.fft.fftshift(np.fft.fft2(smooth, (_FFT_N, _FFT_N)))) ** 2


def fourier(g: np.ndarray, mask: np.ndarray, mm_per_px: float, n_peaks: int = 6) -> dict[str, Any]:
    """Wavelength of the strongest spectral ring, the peaks on it, and its angular harmonics.

    ``symmetry`` holds s_m = |sum P(theta) e^(-i m theta)| / sum P(theta) over the ring.
    Stripes: all even m are large. Squares: m = 4, 8, ... Hexagons: m = 6, 12, ...
    A labyrinth or disordered pattern: all small. ``spottiness`` is the share of ring power in the
    strongest 10 degree window (1 = a single pair of spots, about 0.1 = isotropic).
    """
    contrast = float(np.percentile(g[mask], 99) - np.percentile(g[mask], 1))
    p = power_spectrum(g)
    kr, kx, ky = _k_grid()
    band = (kr > _K_MIN) & (kr < _K_MAX)
    edges = np.linspace(_K_MIN, _K_MAX, 120)
    profile = np.array([p[(kr >= lo) & (kr < hi)].sum() for lo, hi in itertools.pairwise(edges)])
    if profile.max() <= 0:
        return {"wavelength_mm": None, "contrast": round(contrast, 2), "peaks": []}
    k0 = float((edges[profile.argmax()] + edges[profile.argmax() + 1]) / 2)

    pb = np.where(band, p, 0).astype(np.float32)
    top = float(pb.max())
    local_max = cv2.dilate(pb, np.ones((9, 9), np.uint8))
    candidates = np.argwhere((pb == local_max) & (pb > 0.35 * top))
    found = sorted(
        (
            (
                float(pb[y, x] / top),
                float(kr[y, x]),
                float(np.degrees(np.arctan2(ky[y, x], kx[y, x])) % 180),
            )
            for y, x in candidates
        ),
        reverse=True,
    )
    directions: list[tuple[float, float, float]] = []
    for power, k, angle in found:  # +k and -k are the same wave: keep distinct directions
        if all(min(abs(angle - d[2]), 180 - abs(angle - d[2])) > 12 for d in directions):
            directions.append((power, k, angle))

    ring = (kr > 0.85 * k0) & (kr < 1.15 * k0)
    weights, theta = p[ring], np.arctan2(ky[ring], kx[ring])
    total = float(weights.sum())
    symmetry = {
        str(m): round(float(abs((weights * np.exp(-1j * m * theta)).sum()) / total), 3)
        for m in (2, 4, 6, 8, 10, 12)
    }
    hist, _ = np.histogram(np.degrees(theta) % 180, bins=18, weights=weights)
    wavelength_mm = mm_per_px / k0
    return {
        "wavelength_mm": round(wavelength_mm, 3),
        "wavelength_px": round(1 / k0, 2),
        "k_rad_per_m": round(2 * np.pi / (wavelength_mm * 1e-3), 1),
        "contrast": round(contrast, 2),
        # Peak of the ring profile over its mean. Static texture also gives a ring (a still dish
        # reads about 8), so compare with a still frame; `activity` is the better wave detector.
        "ring_strength": round(float(profile.max() / profile.mean()), 2),
        "n_directions": len(directions),
        "peaks": [
            {
                "relative_power": round(power, 3),
                "wavelength_mm": round(mm_per_px / k, 3),
                "angle_deg": round(angle, 1),
            }
            for power, k, angle in directions[:n_peaks]
        ],
        "symmetry": symmetry,
        "spottiness": round(float(hist.max() / hist.sum()), 3),
    }


def spectrum_image(g: np.ndarray) -> np.ndarray:
    """The central part of the log power spectrum as a colour image."""
    p = np.log1p(power_spectrum(g))
    c = _FFT_N // 2
    crop = p[c - 120 : c + 120, c - 120 : c + 120]
    scaled = (255 * crop / max(float(crop.max()), 1e-9)).astype(np.uint8)
    return cv2.applyColorMap(cv2.resize(scaled, (CANON, CANON)), cv2.COLORMAP_INFERNO)


# --------------------------------------------------------------------------- #
# Points and triangulation
# --------------------------------------------------------------------------- #


def _centroids(binary: np.ndarray) -> np.ndarray:
    n, _, _, centroids = cv2.connectedComponentsWithStats(binary.astype(np.uint8))
    return centroids[1:n].astype(np.float64).reshape(-1, 2)


def find_crests(dish: Dish, spacing_px: float, min_frac: float = 0.35) -> np.ndarray:
    """One point per crest: difference of Gaussians at the lattice scale, then non-maximum
    suppression at about 0.45 of the spacing. Points: canonical pixels, shape (n, 2), (x, y)."""
    gray = cv2.cvtColor(dish.image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sigma = max(2.0, spacing_px / 7)
    dog = cv2.GaussianBlur(gray, (0, 0), sigma) - cv2.GaussianBlur(gray, (0, 0), sigma * 2)
    margin = int(0.5 * spacing_px) | 1  # edge margin scales with the spacing
    inside = cv2.erode(dish.mask.astype(np.uint8), np.ones((margin, margin), np.uint8)).astype(bool)
    dog[~inside] = 0
    threshold = max(1.5, min_frac * float(np.percentile(dog[dish.mask], 99.5)))
    distance = max(3, int(0.45 * spacing_px))
    local_max = cv2.dilate(dog, np.ones((2 * distance + 1, 2 * distance + 1), np.uint8))
    return _centroids((dog == local_max) & (dog > threshold) & inside)


def find_highlights(dish: Dish, min_area: int = 6, max_area: int = 900) -> np.ndarray:
    """Specular glints: bright blobs against the local background."""
    gray = cv2.cvtColor(dish.image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    local = gray - cv2.GaussianBlur(gray, (0, 0), 15)
    threshold = max(25.0, float(local[dish.mask].mean() + 3 * local[dish.mask].std()))
    binary = ((local > threshold) & dish.mask).astype(np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, _, stats, centroids = cv2.connectedComponentsWithStats(binary)
    keep = [i for i in range(1, n) if min_area <= stats[i, cv2.CC_STAT_AREA] <= max_area]
    return centroids[keep].astype(np.float64).reshape(-1, 2)


def triangulate(points: np.ndarray) -> np.ndarray:
    """Delaunay edges as an (m, 2) array of indices into ``points`` (OpenCV Subdiv2D)."""
    pts = np.clip(points, 1, CANON - 2).astype(np.float32)
    subdiv = cv2.Subdiv2D((0, 0, CANON, CANON))
    index: dict[tuple[float, float], int] = {}
    for i, (x, y) in enumerate(pts):
        key = (float(x), float(y))
        if key not in index:
            index[key] = i
            subdiv.insert(key)
    edges = set()
    for x1, y1, x2, y2 in subdiv.getEdgeList():
        a, b = index.get((float(x1), float(y1))), index.get((float(x2), float(y2)))
        if a is not None and b is not None and a != b:
            edges.add((min(a, b), max(a, b)))
    return np.array(sorted(edges), dtype=np.int64).reshape(-1, 2)


def lattice_stats(points: np.ndarray, mm_per_px: float) -> dict[str, Any]:
    """Neighbours, bond lengths and bond-orientational order of a point set.

    Edges longer than 1.6x the median are dropped (they span the convex hull). ``psi4`` and ``psi6``
    are |mean exp(i m theta)| over all bonds (the lattice orientation matters); ``psi4_local`` and
    ``psi6_local`` average the same quantity per point over interior points, so they do not depend
    on a common orientation. 1 = perfect m-fold order, near 0 = disordered.
    """
    if len(points) < 8:
        return {"n_points": len(points), "note": "too few points (need at least 8)"}
    edges = triangulate(points)
    if len(edges) < 8:
        return {"n_points": len(points), "note": "triangulation failed"}
    vec = points[edges[:, 1]] - points[edges[:, 0]]
    length = np.hypot(vec[:, 0], vec[:, 1])
    keep = length < 1.6 * np.median(length)
    edges, vec, length = edges[keep], vec[keep], length[keep]
    theta = np.arctan2(vec[:, 1], vec[:, 0])

    n = len(points)
    interior = np.hypot(points[:, 0] - CANON / 2, points[:, 1] - CANON / 2) < _INTERIOR * CANON / 2
    neighbour_angles: list[list[float]] = [[] for _ in range(n)]
    for (a, b), t in zip(edges, theta, strict=True):
        neighbour_angles[a].append(float(t))
        neighbour_angles[b].append(float(t + np.pi))
    counts = np.array([len(neighbour_angles[i]) for i in np.flatnonzero(interior)])

    def local(m: int) -> float:
        values = [
            abs(np.mean(np.exp(1j * m * np.array(neighbour_angles[i]))))
            for i in np.flatnonzero(interior)
            if neighbour_angles[i]
        ]
        return float(np.mean(values)) if values else 0.0

    diff = points[:, None, :] - points[None, :, :]
    dist = np.hypot(diff[..., 0], diff[..., 1])
    np.fill_diagonal(dist, np.inf)
    nearest = dist.min(axis=1)
    histogram = {str(k): int((counts == k).sum()) for k in range(1, 10)} if len(counts) else {}
    return {
        "n_points": n,
        "n_interior": int(interior.sum()),
        "coordination_histogram": {k: v for k, v in histogram.items() if v},
        "coordination_mean": round(float(counts.mean()), 2) if len(counts) else None,
        "bond_length_mm": {
            "mean": round(float(length.mean() * mm_per_px), 3),
            "median": round(float(np.median(length) * mm_per_px), 3),
            "cv": round(float(length.std() / length.mean()), 3),
        },
        "nearest_neighbour_mm": round(float(np.median(nearest) * mm_per_px), 3),
        "psi4": round(float(abs(np.mean(np.exp(4j * theta)))), 3),
        "psi6": round(float(abs(np.mean(np.exp(6j * theta)))), 3),
        "psi4_local": round(local(4), 3),
        "psi6_local": round(local(6), 3),
    }


# --------------------------------------------------------------------------- #
# The analyzer
# --------------------------------------------------------------------------- #


class Analyzer:
    """All measurements on a BGR camera frame. Thread-safe; call from a worker thread."""

    def __init__(self, cup_diameter_mm: float) -> None:
        self._cup_mm = cup_diameter_mm
        self._lock = threading.Lock()
        self._reference: tuple[Ellipse, np.ndarray, float] | None = (
            None  # ellipse, band-passed, time
        )

    def _prepare(self, bgr: np.ndarray) -> tuple[Dish, np.ndarray]:
        dish = locate(bgr, self._cup_mm)
        return dish, band_pass(dish.image, dish.mask)

    def _points(
        self, dish: Dish, g: np.ndarray, kind: PointKind
    ) -> tuple[np.ndarray, dict[str, Any]]:
        if kind == "highlights":
            return find_highlights(dish), {}
        info = fourier(g, dish.mask, dish.mm_per_px, n_peaks=1)
        if info.get("wavelength_px") is None:
            return np.empty((0, 2)), {"note": "no wave pattern found, so no crests"}
        return find_crests(dish, info["wavelength_px"] / 1.4), {}

    def _mm(self, dish: Dish, points: np.ndarray) -> np.ndarray:
        """Canonical pixels to mm from the dish centre (x right, y down, in the dish frame)."""
        return (points - CANON / 2) * dish.mm_per_px

    def points(self, bgr: np.ndarray, kind: PointKind, max_points: int) -> dict[str, Any]:
        dish, g = self._prepare(bgr)
        pts, extra = self._points(dish, g, kind)
        area_cm2 = np.pi * (self._cup_mm * FRAC / 2) ** 2 / 100
        shown = self._mm(dish, pts)[:max_points]
        return {
            "kind": kind,
            "count": len(pts),
            "density_per_cm2": round(len(pts) / area_cm2, 2),
            "points_mm": [[round(float(x), 2), round(float(y), 2)] for x, y in shown],
            "truncated": len(pts) > max_points,
            "dish_radius_mm": round(self._cup_mm * FRAC / 2, 2),
            **extra,
        }

    def fourier(self, bgr: np.ndarray, n_peaks: int) -> dict[str, Any]:
        dish, g = self._prepare(bgr)
        return fourier(g, dish.mask, dish.mm_per_px, n_peaks)

    def delaunay(self, bgr: np.ndarray, kind: PointKind) -> dict[str, Any]:
        dish, g = self._prepare(bgr)
        pts, extra = self._points(dish, g, kind)
        return {"kind": kind, **lattice_stats(pts, dish.mm_per_px), **extra}

    def analyze(self, bgr: np.ndarray) -> dict[str, Any]:
        dish, g = self._prepare(bgr)
        spectrum = fourier(g, dish.mask, dish.mm_per_px, n_peaks=4)
        pts, extra = self._points(dish, g, "crests")
        (_, _), (a, b), angle = dish.ellipse
        return {
            "dish": {
                "diameter_px": round((a + b) / 2, 1),
                "axis_ratio": round(min(a, b) / max(a, b), 3),
                "angle_deg": round(angle, 1),
                "mm_per_canonical_px": round(dish.mm_per_px, 4),
            },
            "fourier": spectrum,
            "crests": {"count": len(pts), **extra},
            "delaunay": lattice_stats(pts, dish.mm_per_px),
            **({"activity": self._activity(bgr)} if self._reference else {}),
        }

    def capture_reference(self, bgr: np.ndarray) -> dict[str, Any]:
        dish, g = self._prepare(bgr)
        with self._lock:
            self._reference = (dish.ellipse, g, time.monotonic())
        return {"captured": True}

    def activity(self, bgr: np.ndarray) -> dict[str, Any]:
        return self._activity(bgr)

    def _activity(self, bgr: np.ndarray) -> dict[str, Any]:
        """Std-dev of the band-passed dish image minus the still reference (about 0.6 is the
        noise floor, 10 or more is strong waves). The dish is warped with the reference's ellipse,
        so the camera and dish must not have moved since the reference was captured."""
        with self._lock:
            reference = self._reference
        if reference is None:
            raise VisionError(
                "no_reference", "Capture a reference of the still surface with capture_reference"
            )
        ellipse, g_ref, taken = reference
        dish = locate(bgr, self._cup_mm, ellipse)
        diff = band_pass(dish.image, dish.mask) - g_ref
        diff[~dish.mask] = 0
        value = float(cv2.GaussianBlur(diff, (0, 0), 1.5)[dish.mask].std())
        return {"activity": round(value, 2), "reference_age_s": round(time.monotonic() - taken, 1)}

    # ------------------------------------------------------------ rendering

    def render(self, bgr: np.ndarray, view: View) -> np.ndarray:
        """An annotated image for an overlay stream. Falls back to the frame with a message."""
        try:
            dish, g = self._prepare(bgr)
            if view == "spectrum":
                info = fourier(g, dish.mask, dish.mm_per_px, n_peaks=1)
                image = spectrum_image(g)
                label = (
                    f"wavelength {info['wavelength_mm']} mm" if info.get("wavelength_mm") else ""
                )
                return _label(image, label)
            pts, _ = self._points(dish, g, "crests")
            image = dish.image.copy()
            if view == "delaunay" and len(pts) >= 8:
                edges = triangulate(pts)
                for a, b in edges:
                    cv2.line(
                        image, tuple(map(int, pts[a])), tuple(map(int, pts[b])), (0, 255, 255), 1
                    )
            for x, y in pts:
                cv2.circle(image, (int(x), int(y)), 3, (0, 255, 0), -1, cv2.LINE_AA)
            return _label(image, f"{len(pts)} crests")
        except VisionError as exc:
            return _label(bgr.copy(), exc.message)


def _label(image: np.ndarray, text: str) -> np.ndarray:
    if text:
        cv2.putText(
            image, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 4, cv2.LINE_AA
        )
        cv2.putText(
            image, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA
        )
    return image


# --------------------------------------------------------------------------- #
# Overlay streams
# --------------------------------------------------------------------------- #


class OverlayStream:
    """A stream derived from the camera: each new frame is analysed and drawn on, at most ``fps``
    times a second. It never owns the camera, so closing it leaves the camera running."""

    fault: str | None = None

    def __init__(
        self, camera: Camera, analyzer: Analyzer, view: View, fps: float, jpeg_quality: int
    ) -> None:
        self._camera = camera
        self._analyzer = analyzer
        self._view: View = view
        self._period = 1.0 / fps
        self._quality = jpeg_quality

    def start(self) -> None:
        self._camera.start()

    def close(self) -> None:
        pass

    def _render(self, jpeg: bytes) -> bytes | None:
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return None
        image = self._analyzer.render(frame, self._view)
        ok, out = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality])
        return out.tobytes() if ok else None

    async def frames(self) -> AsyncIterator[bytes]:
        seq = 0
        while True:
            new_seq, jpeg = self._camera.latest()
            if jpeg is not None and new_seq != seq:
                seq = new_seq
                try:
                    out = await asyncio.to_thread(self._render, jpeg)
                except Exception:
                    log.exception("Overlay %s failed", self._view)
                    out = None
                if out is not None:
                    yield out
            await asyncio.sleep(self._period)
