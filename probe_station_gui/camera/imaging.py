"""Microscope scan geometry, scale calibration, and composition."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from PySide6.QtGui import QImage

from probe_station_gui.camera import flat_field_processing as _flat_field_processing


Point2D = tuple[float, float]


@dataclass(frozen=True)
class MicroscopeScaleCalibration:
    """Physical pixel size for one captured microscope frame."""

    pixel_size_x_um: float
    pixel_size_y_um: float
    source: str = ""
    pixels_to_mm: tuple[tuple[float, float], tuple[float, float]] | None = None

    @property
    def pixel_size_x_mm(self) -> float:
        return float(self.pixel_size_x_um) / 1000.0

    @property
    def pixel_size_y_mm(self) -> float:
        return float(self.pixel_size_y_um) / 1000.0

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "PixelSize": [float(self.pixel_size_x_um), float(self.pixel_size_y_um)],
            "PixelSizeUnits": "um",
            "PhysicalSizeX": float(self.pixel_size_x_um),
            "PhysicalSizeXUnit": "um",
            "PhysicalSizeY": float(self.pixel_size_y_um),
            "PhysicalSizeYUnit": "um",
            "source": self.source,
        }
        if self.pixels_to_mm is not None:
            data["PixelToStageMatrix"] = [
                [float(value) for value in row] for row in self.pixels_to_mm
            ]
            data["PixelToStageMatrixUnits"] = "mm/px"
        return data

    def pixel_delta_to_stage_mm(self, dx_px: float, dy_px: float) -> Point2D:
        matrix = self.pixels_to_mm
        if matrix is None:
            return (
                float(dx_px) * self.pixel_size_x_mm,
                -float(dy_px) * self.pixel_size_y_mm,
            )
        return (
            float(matrix[0][0]) * float(dx_px) + float(matrix[0][1]) * float(dy_px),
            float(matrix[1][0]) * float(dx_px) + float(matrix[1][1]) * float(dy_px),
        )

    def stage_delta_to_pixel(self, dx_mm: float, dy_mm: float) -> Point2D:
        matrix = self.pixels_to_mm
        if matrix is None:
            return (
                float(dx_mm) / self.pixel_size_x_mm,
                -float(dy_mm) / self.pixel_size_y_mm,
            )
        determinant = float(matrix[0][0]) * float(matrix[1][1]) - float(
            matrix[0][1]
        ) * float(matrix[1][0])
        if abs(determinant) < 1e-18:
            raise ValueError("Pixel-to-stage matrix is singular.")
        return (
            (float(matrix[1][1]) * float(dx_mm) - float(matrix[0][1]) * float(dy_mm))
            / determinant,
            (-float(matrix[1][0]) * float(dx_mm) + float(matrix[0][0]) * float(dy_mm))
            / determinant,
        )


@dataclass(frozen=True)
class MicroscopeScanTile:
    """One stage position in a design scan plan."""

    index: int
    row: int
    column: int
    stage_xy: Point2D
    label: str = ""


@dataclass(frozen=True)
class MicroscopeScanPlan:
    """Tile grid that covers an axis-aligned stage-space design bounds."""

    tiles: tuple[MicroscopeScanTile, ...]
    stage_bounds: tuple[float, float, float, float]
    covered_stage_bounds: tuple[float, float, float, float]
    fov_size_mm: Point2D
    overlap_fraction: float
    row_count: int
    column_count: int


def utc_timestamp() -> str:
    """Return an ISO timestamp suitable for metadata sidecars."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def objective_scale_calibration(objective: object) -> MicroscopeScaleCalibration | None:
    """Build pixel-size metadata from an objective profile's pixels_to_mm matrix."""

    if not bool(getattr(objective, "xy_calibration_configured", False)):
        return None
    matrix = getattr(objective, "pixels_to_mm", None)
    try:
        xx = float(matrix[0][0])
        xy = float(matrix[1][0])
        yx = float(matrix[0][1])
        yy = float(matrix[1][1])
    except (TypeError, ValueError, IndexError):
        return None
    pixel_x_mm = math.hypot(xx, xy)
    pixel_y_mm = math.hypot(yx, yy)
    if not (
        math.isfinite(pixel_x_mm)
        and math.isfinite(pixel_y_mm)
        and pixel_x_mm > 0.0
        and pixel_y_mm > 0.0
    ):
        return None
    name = str(getattr(objective, "name", "") or "")
    return MicroscopeScaleCalibration(
        pixel_size_x_um=pixel_x_mm * 1000.0,
        pixel_size_y_um=pixel_y_mm * 1000.0,
        source=f"objective:{name}" if name else "objective",
        pixels_to_mm=((xx, yx), (xy, yy)),
    )


def build_design_scan_plan(
    *,
    stage_bounds: tuple[float, float, float, float],
    fov_size_mm: Point2D,
    overlap_fraction: float,
) -> MicroscopeScanPlan:
    """Build a serpentine tile plan covering stage-space design bounds."""

    left, bottom, right, top = _normalized_bounds(stage_bounds)
    fov_w = _flat_field_processing._positive_float(fov_size_mm[0], "FOV width")
    fov_h = _flat_field_processing._positive_float(fov_size_mm[1], "FOV height")
    overlap = min(max(float(overlap_fraction), 0.0), 0.95)
    step_x = fov_w * (1.0 - overlap)
    step_y = fov_h * (1.0 - overlap)
    x_centers = _axis_centers(left, right, fov_w, step_x)
    y_centers_bottom_to_top = _axis_centers(bottom, top, fov_h, step_y)
    y_centers_top_to_bottom = list(reversed(y_centers_bottom_to_top))

    tiles: list[MicroscopeScanTile] = []
    for row, y_value in enumerate(y_centers_top_to_bottom):
        row_columns = list(enumerate(x_centers))
        if row % 2 == 1:
            row_columns.reverse()
        for column, x_value in row_columns:
            tiles.append(
                MicroscopeScanTile(
                    index=len(tiles) + 1,
                    row=row,
                    column=column,
                    stage_xy=(float(x_value), float(y_value)),
                )
            )
    covered_left = min(x_centers) - fov_w * 0.5
    covered_right = max(x_centers) + fov_w * 0.5
    covered_bottom = min(y_centers_bottom_to_top) - fov_h * 0.5
    covered_top = max(y_centers_bottom_to_top) + fov_h * 0.5
    return MicroscopeScanPlan(
        tiles=tuple(tiles),
        stage_bounds=(left, bottom, right, top),
        covered_stage_bounds=(
            float(covered_left),
            float(covered_bottom),
            float(covered_right),
            float(covered_top),
        ),
        fov_size_mm=(fov_w, fov_h),
        overlap_fraction=overlap,
        row_count=len(y_centers_bottom_to_top),
        column_count=len(x_centers),
    )


def _scan_tile_pixel_placements(
    tile_images: Sequence[tuple[MicroscopeScanTile, QImage]],
    scale: MicroscopeScaleCalibration,
) -> list[tuple[MicroscopeScanTile, float, float, Any]]:
    origin_tile = tile_images[0][0]
    origin_stage = origin_tile.stage_xy
    placements: list[tuple[MicroscopeScanTile, float, float, Any]] = []
    for tile, image in tile_images:
        raw = _flat_field_processing.qimage_to_rgb_array(image)
        center_dx, center_dy = scale.stage_delta_to_pixel(
            float(tile.stage_xy[0]) - float(origin_stage[0]),
            float(tile.stage_xy[1]) - float(origin_stage[1]),
        )
        # X stage motion moves the sample opposite to image coordinates.
        left_px = -float(center_dx) - raw.shape[1] * 0.5
        top_px = float(center_dy) - raw.shape[0] * 0.5
        placements.append((tile, left_px, top_px, raw))
    return placements


def _overlap_patches(
    existing: tuple[MicroscopeScanTile, float, float, Any],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> tuple[Any, Any] | None:
    _existing_tile, existing_left, existing_top, existing_raw = existing
    _current_tile, current_left, current_top, current_raw = current
    existing_h, existing_w = existing_raw.shape[:2]
    current_h, current_w = current_raw.shape[:2]
    left = max(existing_left, current_left)
    top = max(existing_top, current_top)
    right = min(existing_left + existing_w, current_left + current_w)
    bottom = min(existing_top + existing_h, current_top + current_h)
    overlap_w = int(round(right - left))
    overlap_h = int(round(bottom - top))
    if overlap_w < 4 or overlap_h < 4:
        return None
    x0 = int(round(left))
    y0 = int(round(top))
    existing_x = x0 - int(round(existing_left))
    existing_y = y0 - int(round(existing_top))
    current_x = x0 - int(round(current_left))
    current_y = y0 - int(round(current_top))
    existing_patch = existing_raw[
        existing_y : existing_y + overlap_h,
        existing_x : existing_x + overlap_w,
    ]
    current_patch = current_raw[
        current_y : current_y + overlap_h,
        current_x : current_x + overlap_w,
    ]
    if existing_patch.shape[:2] != current_patch.shape[:2]:
        return None
    return existing_patch, current_patch


def _scan_tiles_are_axis_neighbors(
    first: MicroscopeScanTile,
    second: MicroscopeScanTile,
) -> bool:
    row_delta = abs(int(first.row) - int(second.row))
    column_delta = abs(int(first.column) - int(second.column))
    return row_delta + column_delta == 1


def _registration_gray(rgb: Any) -> Any:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32, copy=False)
    if min(gray.shape[:2]) >= 9:
        blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.0, sigmaY=1.0)
        gray = cv2.Laplacian(blurred, cv2.CV_32F)
    mean = float(gray.mean())
    std = float(gray.std())
    if std > 1e-6:
        gray = (gray - mean) / std
    return gray.astype(np.float32, copy=False)


def _sobel_magnitude(gray: Any) -> Any:
    import cv2

    return cv2.magnitude(
        cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3),
    )


def stage_bounds_from_design_bounds(
    design_bounds: tuple[float, float, float, float],
    transform: Callable[[Point2D], Point2D | None],
) -> tuple[float, float, float, float]:
    """Project design bounds corners through a registration into stage bounds."""

    left, bottom, right, top = design_bounds
    corners = (
        (float(left), float(bottom)),
        (float(left), float(top)),
        (float(right), float(bottom)),
        (float(right), float(top)),
    )
    stage_points: list[Point2D] = []
    for corner in corners:
        point = transform(corner)
        if point is None:
            raise ValueError("Design registration is required for microscope scan.")
        stage_points.append((float(point[0]), float(point[1])))
    xs = [point[0] for point in stage_points]
    ys = [point[1] for point in stage_points]
    return (min(xs), min(ys), max(xs), max(ys))


def _axis_centers(
    minimum: float, maximum: float, fov: float, step: float
) -> list[float]:
    span = max(0.0, float(maximum) - float(minimum))
    if span <= fov:
        return [(float(minimum) + float(maximum)) * 0.5]
    count = int(math.ceil((span - fov) / step)) + 1
    count = max(2, count)
    travel = span - fov
    return [
        float(minimum) + fov * 0.5 + travel * index / (count - 1)
        for index in range(count)
    ]


def _normalized_bounds(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if len(bounds) != 4:
        raise ValueError("Bounds must contain left, bottom, right, top.")
    left = float(bounds[0])
    bottom = float(bounds[1])
    right = float(bounds[2])
    top = float(bounds[3])
    if not all(math.isfinite(value) for value in (left, bottom, right, top)):
        raise ValueError("Bounds contain non-finite values.")
    return (min(left, right), min(bottom, top), max(left, right), max(bottom, top))


__all__ = [
    "MicroscopeScaleCalibration",
    "MicroscopeScanPlan",
    "MicroscopeScanTile",
    "build_design_scan_plan",
    "objective_scale_calibration",
    "stage_bounds_from_design_bounds",
    "utc_timestamp",
]
