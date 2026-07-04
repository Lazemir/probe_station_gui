"""Microscope image annotation, metadata, route capture, and scan planning."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen


Point2D = tuple[float, float]
STITCH_MAX_REGISTRATION_SHIFT_PX = 32.0
STITCH_MAX_REGISTRATION_OVERLAP_FRACTION = 0.25
STITCH_PHOTOMETRY_MIN_SAMPLES = 16
STITCH_PHOTOMETRY_GAIN_MIN = 0.65
STITCH_PHOTOMETRY_GAIN_MAX = 1.55
STITCH_PHOTOMETRY_OFFSET_LIMIT = 28.0


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
            float(matrix[0][0]) * float(dx_px)
            + float(matrix[0][1]) * float(dy_px),
            float(matrix[1][0]) * float(dx_px)
            + float(matrix[1][1]) * float(dy_px),
        )

    def stage_delta_to_pixel(self, dx_mm: float, dy_mm: float) -> Point2D:
        matrix = self.pixels_to_mm
        if matrix is None:
            return (
                float(dx_mm) / self.pixel_size_x_mm,
                -float(dy_mm) / self.pixel_size_y_mm,
            )
        determinant = (
            float(matrix[0][0]) * float(matrix[1][1])
            - float(matrix[0][1]) * float(matrix[1][0])
        )
        if abs(determinant) < 1e-18:
            raise ValueError("Pixel-to-stage matrix is singular.")
        return (
            (float(matrix[1][1]) * float(dx_mm) - float(matrix[0][1]) * float(dy_mm))
            / determinant,
            (-float(matrix[1][0]) * float(dx_mm) + float(matrix[0][0]) * float(dy_mm))
            / determinant,
        )


@dataclass(frozen=True)
class MicroscopeImageMetadata:
    """Metadata written into the image overlay and a JSON sidecar."""

    title: str
    mode: str
    captured_at: str
    objective_name: str = ""
    magnification: float | None = None
    route_name: str = ""
    route_point_index: int | None = None
    route_point_label: str = ""
    route_position: int | None = None
    route_total: int | None = None
    scan_tile_index: int | None = None
    scan_tile_total: int | None = None
    scan_row: int | None = None
    scan_column: int | None = None
    design_xy: Point2D | None = None
    stage_position: tuple[float, ...] | None = None
    stage_xy: Point2D | None = None
    image_size_px: tuple[int, int] | None = None
    fov_um: Point2D | None = None
    notes: tuple[str, ...] = ()
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["standard"] = {
            "metadata_sidecar": "BIDS microscopy style PixelSize/PixelSizeUnits",
            "pixel_size_reference": "OME PhysicalSizeX/PhysicalSizeY",
        }
        return data


@dataclass(frozen=True)
class MicroscopeCaptureResult:
    """Saved microscope image plus the raw source frame used for mosaics."""

    image_path: Path
    metadata_path: Path
    raw_image: QImage
    metadata: dict[str, object]
    raw_image_path: Path | None = None


@dataclass(frozen=True)
class FlatFieldProfile:
    """Low-frequency illumination model used to normalize microscope frames."""

    image_size_px: tuple[int, int]
    source: str
    blur_radius_px: int
    max_gain: float
    mean_rgb: tuple[float, float, float]
    illumination_rgb: Any = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": True,
            "source": self.source,
            "image_size_px": list(self.image_size_px),
            "blur_radius_px": int(self.blur_radius_px),
            "max_gain": float(self.max_gain),
            "mean_rgb": [float(value) for value in self.mean_rgb],
        }


@dataclass(frozen=True)
class MicroscopeScanTile:
    """One stage position in a design scan plan."""

    index: int
    row: int
    column: int
    stage_xy: Point2D


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


def save_microscope_image(
    *,
    frame: QImage,
    output_dir: str | Path,
    filename_stem: str,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
    save_raw: bool = False,
) -> MicroscopeCaptureResult:
    """Save an annotated PNG and a JSON sidecar with calibrated pixel metadata."""

    if frame.isNull():
        raise ValueError("Cannot save an empty microscope frame.")
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = safe_filename_component(filename_stem) or "microscope_image"
    image_path = _deduplicated_path(directory / f"{safe_stem}.png")
    sidecar_path = image_path.with_suffix(".json")

    raw = frame.convertToFormat(QImage.Format_RGB888).copy()
    metadata = _metadata_with_image_size(metadata, raw, scale)
    annotated = render_microscope_overlay(raw, metadata, scale)
    sidecar = _sidecar_payload(metadata, scale, image_path.name)
    annotated.setText("ProbeStationGUI.Metadata", json.dumps(sidecar, ensure_ascii=True))
    if not annotated.save(str(image_path), "PNG"):
        raise OSError(f"Unable to save microscope image to {image_path}.")
    raw_image_path: Path | None = None
    if save_raw:
        raw_image_path = _deduplicated_path(
            image_path.with_name(f"{image_path.stem}_raw{image_path.suffix}")
        )
        if not raw.save(str(raw_image_path), "PNG"):
            raise OSError(f"Unable to save raw microscope image to {raw_image_path}.")
    with sidecar_path.open("w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2, ensure_ascii=False)
    return MicroscopeCaptureResult(
        image_path=image_path,
        metadata_path=sidecar_path,
        raw_image=raw,
        metadata=sidecar,
        raw_image_path=raw_image_path,
    )


def render_microscope_overlay(
    frame: QImage,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> QImage:
    """Return a copy of frame with a SEM-style data band and scale bar."""

    image = frame.convertToFormat(QImage.Format_RGB32).copy()
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        _draw_data_band(painter, image, metadata, scale)
    finally:
        painter.end()
    return image


def build_flat_field_profile(
    reference_frame: QImage,
    *,
    blur_radius_px: int = 401,
    max_gain: float = 4.0,
    source: str = "reference",
) -> FlatFieldProfile:
    """Build a low-frequency illumination profile from a reference frame."""

    if reference_frame.isNull():
        raise ValueError("Cannot build a flat-field profile from an empty frame.")
    radius = _flat_field_radius(blur_radius_px)
    gain_limit = _positive_float(max_gain, "max_gain")
    rgb = _qimage_to_rgb_array(reference_frame)
    illumination = _flat_field_illumination(rgb, radius)
    mean_rgb = tuple(
        float(max(1.0, illumination[:, :, channel].mean())) for channel in range(3)
    )
    return FlatFieldProfile(
        image_size_px=(int(reference_frame.width()), int(reference_frame.height())),
        source=str(source or "reference"),
        blur_radius_px=radius,
        max_gain=gain_limit,
        mean_rgb=mean_rgb,
        illumination_rgb=illumination,
    )


def build_median_flat_field_profile(
    frames: Sequence[QImage],
    *,
    blur_radius_px: int = 401,
    max_gain: float = 4.0,
    source: str = "scan_median",
) -> FlatFieldProfile:
    """Build a flat-field profile from the median of several shifted frames."""

    if not frames:
        raise ValueError("Cannot build a median flat-field profile without frames.")
    first = frames[0]
    if first.isNull():
        raise ValueError("Cannot build a flat-field profile from an empty frame.")
    frame_size = (int(first.width()), int(first.height()))
    radius = _flat_field_radius(blur_radius_px)
    gain_limit = _positive_float(max_gain, "max_gain")
    median_rgb = _median_rgb_array(frames, frame_size)
    illumination = _flat_field_illumination(median_rgb, radius)
    mean_rgb = tuple(
        float(max(1.0, illumination[:, :, channel].mean())) for channel in range(3)
    )
    return FlatFieldProfile(
        image_size_px=frame_size,
        source=str(source or "scan_median"),
        blur_radius_px=radius,
        max_gain=gain_limit,
        mean_rgb=mean_rgb,
        illumination_rgb=illumination,
    )


def apply_flat_field_correction(frame: QImage, profile: FlatFieldProfile) -> QImage:
    """Normalize frame brightness/color using a precomputed flat-field profile."""

    if frame.isNull():
        raise ValueError("Cannot flat-field an empty frame.")
    expected_size = tuple(profile.image_size_px)
    frame_size = (int(frame.width()), int(frame.height()))
    if frame_size != expected_size:
        raise ValueError(
            "Flat-field profile frame size "
            f"{expected_size[0]}x{expected_size[1]} does not match frame size "
            f"{frame_size[0]}x{frame_size[1]}."
        )
    rgb = _qimage_to_rgb_array(frame)
    corrected = _apply_flat_field_array(rgb, profile)
    return _rgb_array_to_qimage(corrected)


def apply_self_flat_field_correction(
    frame: QImage,
    *,
    blur_radius_px: int = 401,
    max_gain: float = 4.0,
) -> QImage:
    """Estimate the illumination field from frame itself and normalize it."""

    profile = build_flat_field_profile(
        frame,
        blur_radius_px=blur_radius_px,
        max_gain=max_gain,
        source="self",
    )
    return apply_flat_field_correction(frame, profile)


def build_design_scan_plan(
    *,
    stage_bounds: tuple[float, float, float, float],
    fov_size_mm: Point2D,
    overlap_fraction: float,
) -> MicroscopeScanPlan:
    """Build a serpentine tile plan covering stage-space design bounds."""

    left, bottom, right, top = _normalized_bounds(stage_bounds)
    fov_w = _positive_float(fov_size_mm[0], "FOV width")
    fov_h = _positive_float(fov_size_mm[1], "FOV height")
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


def stitch_scan_tiles(
    *,
    plan: MicroscopeScanPlan,
    tile_images: Sequence[tuple[MicroscopeScanTile, QImage]],
    scale: MicroscopeScaleCalibration,
) -> QImage:
    """Place scan tiles into a single stage-coordinate mosaic."""

    import numpy as np

    if not tile_images:
        raise ValueError("No scan tiles were captured.")
    placements = _normalize_scan_tile_photometry(
        _refine_scan_tile_pixel_placements(
            _scan_tile_pixel_placements(tile_images, scale)
        )
    )
    min_left = min(placement[1] for placement in placements)
    min_top = min(placement[2] for placement in placements)
    max_right = max(placement[1] + placement[3].shape[1] for placement in placements)
    max_bottom = max(placement[2] + placement[3].shape[0] for placement in placements)
    width_px = max(1, int(math.ceil(max_right - min_left)))
    height_px = max(1, int(math.ceil(max_bottom - min_top)))
    output = np.zeros((height_px, width_px, 3), dtype=np.uint8)
    scores = np.zeros((height_px, width_px), dtype=np.float32)
    for _tile, left_px, top_px, raw in placements:
        x_px = int(round(left_px - min_left))
        y_px = int(round(top_px - min_top))
        raw_h, raw_w, _channels = raw.shape
        dst_x0 = max(0, x_px)
        dst_y0 = max(0, y_px)
        dst_x1 = min(width_px, x_px + raw_w)
        dst_y1 = min(height_px, y_px + raw_h)
        if dst_x0 >= dst_x1 or dst_y0 >= dst_y1:
            continue
        src_x0 = dst_x0 - x_px
        src_y0 = dst_y0 - y_px
        src_x1 = src_x0 + (dst_x1 - dst_x0)
        src_y1 = src_y0 + (dst_y1 - dst_y0)
        blend_weights = _tile_blend_weights(
            raw_w,
            raw_h,
            plan.overlap_fraction,
        )[src_y0:src_y1, src_x0:src_x1]
        score_view = scores[dst_y0:dst_y1, dst_x0:dst_x1]
        replace = blend_weights >= score_view
        output_view = output[dst_y0:dst_y1, dst_x0:dst_x1, :]
        source_view = raw[src_y0:src_y1, src_x0:src_x1, :]
        output_view[replace] = source_view[replace]
        score_view[replace] = blend_weights[replace]
    return _rgb_array_to_qimage(output)


def _scan_tile_pixel_placements(
    tile_images: Sequence[tuple[MicroscopeScanTile, QImage]],
    scale: MicroscopeScaleCalibration,
) -> list[tuple[MicroscopeScanTile, float, float, Any]]:
    origin_tile = tile_images[0][0]
    origin_stage = origin_tile.stage_xy
    placements: list[tuple[MicroscopeScanTile, float, float, Any]] = []
    for tile, image in tile_images:
        raw = _qimage_to_rgb_array(image)
        center_dx, center_dy = scale.stage_delta_to_pixel(
            float(tile.stage_xy[0]) - float(origin_stage[0]),
            float(tile.stage_xy[1]) - float(origin_stage[1]),
        )
        # X stage motion moves the sample opposite to image coordinates.
        left_px = -float(center_dx) - raw.shape[1] * 0.5
        top_px = float(center_dy) - raw.shape[0] * 0.5
        placements.append((tile, left_px, top_px, raw))
    return placements


def _refine_scan_tile_pixel_placements(
    placements: list[tuple[MicroscopeScanTile, float, float, Any]],
) -> list[tuple[MicroscopeScanTile, float, float, Any]]:
    refined: list[tuple[MicroscopeScanTile, float, float, Any]] = []
    for placement in placements:
        tile, left_px, top_px, raw = placement
        corrections: list[tuple[float, float, float]] = []
        for existing in refined:
            if not _scan_tiles_are_axis_neighbors(existing[0], tile):
                continue
            estimate = _estimate_overlap_registration(existing, placement)
            if estimate is None:
                continue
            shift_x, shift_y, weight = estimate
            if not _registration_shift_is_plausible(shift_x, shift_y):
                continue
            corrections.append((-shift_x, -shift_y, weight))
        if corrections:
            total_weight = sum(item[2] for item in corrections)
            if total_weight > 0.0:
                left_px += sum(item[0] * item[2] for item in corrections) / total_weight
                top_px += sum(item[1] * item[2] for item in corrections) / total_weight
        refined.append((tile, left_px, top_px, raw))
    return refined


def _estimate_overlap_registration(
    existing: tuple[MicroscopeScanTile, float, float, Any],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> tuple[float, float, float] | None:
    overlap = _overlap_patches(existing, current)
    if overlap is None:
        return None
    existing_patch, current_patch = overlap
    overlap_h, overlap_w = existing_patch.shape[:2]
    shift = _phase_overlap_shift(existing_patch, current_patch)
    if shift is None:
        return None
    shift_x, shift_y, response = shift
    max_shift = _overlap_registration_shift_limit(overlap_w, overlap_h)
    if not _registration_shift_is_plausible(shift_x, shift_y, max_shift):
        return None
    weight = max(float(response), 1e-4) * float(overlap_w * overlap_h)
    return float(shift_x), float(shift_y), weight


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
    x1 = x0 + overlap_w
    y1 = y0 + overlap_h
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


def _overlap_registration_shift_limit(overlap_w: int, overlap_h: int) -> float:
    narrow_overlap = float(max(0, min(int(overlap_w), int(overlap_h))))
    overlap_limit = max(3.0, narrow_overlap * STITCH_MAX_REGISTRATION_OVERLAP_FRACTION)
    return min(STITCH_MAX_REGISTRATION_SHIFT_PX, overlap_limit)


def _registration_shift_is_plausible(
    shift_x: float,
    shift_y: float,
    max_shift_px: float = STITCH_MAX_REGISTRATION_SHIFT_PX,
) -> bool:
    return abs(float(shift_x)) <= max_shift_px and abs(float(shift_y)) <= max_shift_px


def _normalize_scan_tile_photometry(
    placements: list[tuple[MicroscopeScanTile, float, float, Any]],
) -> list[tuple[MicroscopeScanTile, float, float, Any]]:
    import numpy as np

    normalized: list[tuple[MicroscopeScanTile, float, float, Any]] = []
    for placement in placements:
        tile, left_px, top_px, raw = placement
        adjustments: list[tuple[Any, Any, float]] = []
        for existing in normalized:
            if not _scan_tiles_are_axis_neighbors(existing[0], tile):
                continue
            adjustment = _estimate_overlap_photometry_adjustment(existing, placement)
            if adjustment is not None:
                gain, offset, weight = adjustment
                adjustments.append((gain, offset, weight))
        if adjustments:
            total_weight = sum(item[2] for item in adjustments)
            if total_weight > 0.0:
                gain = sum(item[0] * item[2] for item in adjustments) / total_weight
                offset = sum(item[1] * item[2] for item in adjustments) / total_weight
                raw = np.clip(
                    raw.astype(np.float32, copy=False) * gain.reshape((1, 1, 3))
                    + offset.reshape((1, 1, 3)),
                    0.0,
                    255.0,
                ).astype(np.uint8)
        normalized.append((tile, left_px, top_px, raw))
    return normalized


def _estimate_overlap_photometry_adjustment(
    existing: tuple[MicroscopeScanTile, float, float, Any],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> tuple[Any, Any, float] | None:
    import numpy as np

    overlap = _overlap_patches(existing, current)
    if overlap is None:
        return None
    existing_patch, current_patch = overlap
    mask = _photometry_overlap_mask(existing_patch, current_patch)
    sample_count = int(mask.sum())
    if sample_count < STITCH_PHOTOMETRY_MIN_SAMPLES:
        return None
    existing_values = existing_patch[mask].astype(np.float32, copy=False)
    current_values = current_patch[mask].astype(np.float32, copy=False)
    existing_median = np.median(existing_values, axis=0).astype(np.float32)
    current_median = np.median(current_values, axis=0).astype(np.float32)
    gain = existing_median / np.maximum(current_median, 1.0)
    gain = np.clip(gain, STITCH_PHOTOMETRY_GAIN_MIN, STITCH_PHOTOMETRY_GAIN_MAX)
    residual = existing_values - current_values * gain.reshape((1, 3))
    offset = np.median(residual, axis=0).astype(np.float32)
    offset = np.clip(
        offset,
        -STITCH_PHOTOMETRY_OFFSET_LIMIT,
        STITCH_PHOTOMETRY_OFFSET_LIMIT,
    )
    return gain, offset, float(sample_count)


def _photometry_overlap_mask(existing_patch: Any, current_patch: Any) -> Any:
    import numpy as np

    existing_max = existing_patch.max(axis=2)
    current_max = current_patch.max(axis=2)
    existing_min = existing_patch.min(axis=2)
    current_min = current_patch.min(axis=2)
    mask = (
        (existing_max < 220)
        & (current_max < 220)
        & (existing_min > 5)
        & (current_min > 5)
    )
    if int(mask.sum()) < STITCH_PHOTOMETRY_MIN_SAMPLES:
        return mask
    existing_gray = existing_patch.mean(axis=2)
    current_gray = current_patch.mean(axis=2)
    combined = (existing_gray + current_gray) * 0.5
    values = combined[mask]
    low, high = np.percentile(values, (8.0, 85.0))
    filtered = mask & (combined >= low) & (combined <= high)
    if int(filtered.sum()) >= STITCH_PHOTOMETRY_MIN_SAMPLES:
        return filtered
    return mask


def _phase_overlap_shift(existing_patch: Any, current_patch: Any) -> tuple[float, float, float] | None:
    import cv2
    import numpy as np

    existing_gray = _registration_gray(existing_patch)
    current_gray = _registration_gray(current_patch)
    if existing_gray.shape != current_gray.shape:
        return None
    if float(existing_gray.std()) < 1e-3 or float(current_gray.std()) < 1e-3:
        return None
    window = cv2.createHanningWindow(
        (existing_gray.shape[1], existing_gray.shape[0]),
        cv2.CV_32F,
    )
    (shift_x, shift_y), response = cv2.phaseCorrelate(
        existing_gray.astype(np.float32, copy=False),
        current_gray.astype(np.float32, copy=False),
        window,
    )
    if not all(math.isfinite(value) for value in (shift_x, shift_y, response)):
        return None
    if response < 0.001:
        return None
    refined_x, refined_y = _refine_integer_overlap_shift(
        existing_gray,
        current_gray,
        shift_x,
        shift_y,
    )
    return float(refined_x), float(refined_y), float(response)


def _refine_integer_overlap_shift(
    existing_gray: Any,
    current_gray: Any,
    shift_x: float,
    shift_y: float,
) -> tuple[int, int]:
    import numpy as np

    base_x = int(round(float(shift_x)))
    base_y = int(round(float(shift_y)))
    best_score = -math.inf
    best_shift = (base_x, base_y)
    for candidate_y in range(base_y - 3, base_y + 4):
        for candidate_x in range(base_x - 3, base_x + 4):
            score = _integer_shift_score(
                existing_gray,
                current_gray,
                candidate_x,
                candidate_y,
            )
            if score > best_score:
                best_score = score
                best_shift = (candidate_x, candidate_y)
    return best_shift


def _integer_shift_score(
    existing_gray: Any,
    current_gray: Any,
    shift_x: int,
    shift_y: int,
) -> float:
    import numpy as np

    height, width = existing_gray.shape[:2]
    existing_x0 = max(0, -int(shift_x))
    current_x0 = max(0, int(shift_x))
    existing_y0 = max(0, -int(shift_y))
    current_y0 = max(0, int(shift_y))
    overlap_w = min(width - existing_x0, width - current_x0)
    overlap_h = min(height - existing_y0, height - current_y0)
    if overlap_w < 3 or overlap_h < 3:
        return -math.inf
    a = existing_gray[
        existing_y0 : existing_y0 + overlap_h,
        existing_x0 : existing_x0 + overlap_w,
    ]
    b = current_gray[
        current_y0 : current_y0 + overlap_h,
        current_x0 : current_x0 + overlap_w,
    ]
    a_flat = a.reshape(-1).astype(np.float32, copy=False)
    b_flat = b.reshape(-1).astype(np.float32, copy=False)
    a_std = float(a_flat.std())
    b_std = float(b_flat.std())
    if a_std < 1e-6 or b_std < 1e-6:
        return -math.inf
    return float(np.mean(((a_flat - float(a_flat.mean())) / a_std) * ((b_flat - float(b_flat.mean())) / b_std)))


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


def safe_filename_component(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("._")[:120]


def route_photo_filename(
    *,
    route_name: str,
    point_index: int,
    point_label: str,
    captured_at: str,
) -> str:
    stamp = _timestamp_for_filename(captured_at)
    label = safe_filename_component(point_label) or f"P{int(point_index):03d}"
    route = safe_filename_component(route_name) or "route"
    return f"{route}_point_{int(point_index):03d}_{label}_{stamp}"


def scan_tile_filename(
    *,
    scan_name: str,
    tile: MicroscopeScanTile,
    captured_at: str,
) -> str:
    stamp = _timestamp_for_filename(captured_at)
    name = safe_filename_component(scan_name) or "design_scan"
    return (
        f"{name}_tile_{tile.index:04d}_"
        f"r{tile.row + 1:03d}_c{tile.column + 1:03d}_{stamp}"
    )


def _draw_data_band(
    painter: QPainter,
    image: QImage,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> None:
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return
    font_size = max(8, min(13, width // 150))
    base_font = QFont()
    base_font.setPointSize(font_size)
    painter.setFont(base_font)
    line_height = float(font_size + 8)
    band_height = max(74, int(line_height * 3 + 24))
    band_top = max(0, height - band_height)
    band = QRectF(0.0, float(band_top), float(width), float(height - band_top))
    painter.fillRect(band, QColor(0, 0, 0, 220))
    painter.setPen(QPen(QColor(255, 255, 255, 210), 1))
    painter.drawLine(QPointF(0.0, float(band_top)), QPointF(float(width), float(band_top)))

    margin = 14.0
    scale_bar_width = min(max(width * 0.30, 170.0), 360.0)
    scale_rect = QRectF(
        margin,
        float(band_top) + 10.0,
        scale_bar_width,
        max(42.0, band.height() - 18.0),
    )
    _draw_scale_bar(painter, scale_rect, scale)

    text_left = scale_rect.right() + 18.0
    text_rect_width = max(40.0, float(width) - text_left - margin)
    text_top = float(band_top) + 8.0
    lines = _metadata_overlay_lines(metadata, scale)
    painter.setPen(QPen(QColor(255, 255, 255), 1))
    for index, line in enumerate(lines[:3]):
        rect = QRectF(
            text_left,
            text_top + index * line_height,
            text_rect_width,
            line_height,
        )
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, line)


def _draw_scale_bar(
    painter: QPainter,
    rect: QRectF,
    scale: MicroscopeScaleCalibration,
) -> None:
    pixel_size_um = float(scale.pixel_size_x_um)
    if pixel_size_um <= 0.0 or not math.isfinite(pixel_size_um):
        return
    target_px = max(70.0, min(rect.width() * 0.72, 220.0))
    length_um = _nice_length_um(target_px * pixel_size_um)
    bar_px = length_um / pixel_size_um
    if bar_px <= 0.0 or bar_px > rect.width() - 18.0:
        return
    bar_x = rect.left() + 4.0
    bar_y = rect.bottom() - 17.0
    tick = 8.0
    label = _format_um(length_um)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, False)
    painter.setPen(QPen(QColor(255, 255, 255), 5.0))
    painter.drawLine(QPointF(bar_x, bar_y), QPointF(bar_x + bar_px, bar_y))
    painter.setPen(QPen(QColor(255, 255, 255), 2.0))
    painter.drawLine(QPointF(bar_x, bar_y - tick), QPointF(bar_x, bar_y + tick))
    painter.drawLine(
        QPointF(bar_x + bar_px, bar_y - tick),
        QPointF(bar_x + bar_px, bar_y + tick),
    )
    font = painter.font()
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(
        QRectF(bar_x, rect.top(), max(bar_px, 72.0), 24.0),
        Qt.AlignLeft | Qt.AlignVCenter,
        label,
    )
    painter.restore()


def _metadata_overlay_lines(
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> list[str]:
    objective = metadata.objective_name or "objective unknown"
    if metadata.magnification is not None and math.isfinite(float(metadata.magnification)):
        objective = f"{objective} {float(metadata.magnification):g}x"
    point = ""
    if metadata.route_point_index is not None:
        label = metadata.route_point_label or f"P{metadata.route_point_index}"
        point = f" | point {metadata.route_point_index} {label}"
    if metadata.scan_tile_index is not None and metadata.scan_tile_total is not None:
        point = f" | tile {metadata.scan_tile_index}/{metadata.scan_tile_total}"
    fov = ""
    if metadata.fov_um is not None:
        fov = f" | FoV {metadata.fov_um[0]:.1f} x {metadata.fov_um[1]:.1f} um"
    stage = _format_stage_position(metadata.stage_position, metadata.stage_xy)
    design = ""
    if metadata.design_xy is not None:
        design = f" | design X={metadata.design_xy[0]:.3f} Y={metadata.design_xy[1]:.3f}"
    return [
        f"{metadata.title}{point}",
        (
            f"{metadata.captured_at} | {metadata.mode} | {objective} | "
            f"pixel {scale.pixel_size_x_um:.4g} x {scale.pixel_size_y_um:.4g} um"
            f"{fov}"
        ),
        f"{stage}{design}".strip(" |") or "stage position unavailable",
    ]


def _format_stage_position(
    stage_position: tuple[float, ...] | None,
    stage_xy: Point2D | None,
) -> str:
    values: list[float] = []
    if stage_position is not None:
        values = [float(value) for value in stage_position]
    elif stage_xy is not None:
        values = [float(stage_xy[0]), float(stage_xy[1])]
    labels = ("X", "Y", "Z", "A", "B", "C")
    parts: list[str] = []
    for index, value in enumerate(values[: len(labels)]):
        if math.isfinite(value):
            parts.append(f"{labels[index]}={value:.4f}")
    return "stage " + " ".join(parts) if parts else ""


def _metadata_with_image_size(
    metadata: MicroscopeImageMetadata,
    image: QImage,
    scale: MicroscopeScaleCalibration,
) -> MicroscopeImageMetadata:
    fov_um = (
        float(image.width()) * scale.pixel_size_x_um,
        float(image.height()) * scale.pixel_size_y_um,
    )
    return MicroscopeImageMetadata(
        **{
            **asdict(metadata),
            "image_size_px": (int(image.width()), int(image.height())),
            "fov_um": fov_um,
        }
    )


def _sidecar_payload(
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
    image_name: str,
) -> dict[str, object]:
    return {
        "FileName": image_name,
        "MicroscopeImage": metadata.to_dict(),
        "Microscopy": scale.to_dict(),
    }


def _nice_length_um(target_um: float) -> float:
    if target_um <= 0.0 or not math.isfinite(target_um):
        return 1.0
    exponent = math.floor(math.log10(target_um))
    base = 10.0 ** exponent
    candidates = [1.0, 2.0, 5.0, 10.0]
    best = base
    for multiplier in candidates:
        value = multiplier * base
        if value <= target_um:
            best = value
    return float(best)


def _format_um(value_um: float) -> str:
    if value_um >= 1000.0:
        return f"{value_um / 1000.0:g} mm"
    if value_um >= 10.0:
        return f"{value_um:g} um"
    return f"{value_um:.3g} um"


def _axis_centers(minimum: float, maximum: float, fov: float, step: float) -> list[float]:
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


def _positive_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number.") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive number.")
    return number


def _flat_field_radius(value: object) -> int:
    try:
        radius = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("blur_radius_px must be a positive integer.") from exc
    if radius <= 0:
        raise ValueError("blur_radius_px must be a positive integer.")
    if radius % 2 == 0:
        radius += 1
    return radius


def _qimage_to_rgb_array(image: QImage) -> Any:
    import numpy as np

    rgb_image = image.convertToFormat(QImage.Format_RGB888)
    width = int(rgb_image.width())
    height = int(rgb_image.height())
    bytes_per_line = int(rgb_image.bytesPerLine())
    bits = rgb_image.bits()
    array = np.frombuffer(bits, dtype=np.uint8, count=height * bytes_per_line)
    rows = array.reshape((height, bytes_per_line))
    return rows[:, : width * 3].reshape((height, width, 3)).copy()


def _rgb_array_to_qimage(array: Any) -> QImage:
    import numpy as np

    rgb = np.ascontiguousarray(array.astype(np.uint8, copy=False))
    height, width, _channels = rgb.shape
    image = QImage(
        rgb.data,
        int(width),
        int(height),
        int(width) * 3,
        QImage.Format_RGB888,
    )
    return image.copy().convertToFormat(QImage.Format_RGB32)


def _flat_field_illumination(rgb: Any, blur_radius_px: int) -> Any:
    import cv2
    import numpy as np

    rgb_float = rgb.astype(np.float32, copy=False)
    sigma = max(1.0, float(blur_radius_px) / 3.0)
    return cv2.GaussianBlur(
        rgb_float,
        (0, 0),
        sigmaX=sigma,
        sigmaY=sigma,
        borderType=cv2.BORDER_REPLICATE,
    )


def _median_rgb_array(frames: Sequence[QImage], frame_size: tuple[int, int]) -> Any:
    import numpy as np

    arrays = []
    for frame in frames:
        if frame.isNull():
            raise ValueError("Cannot use an empty frame for median flat-field.")
        size = (int(frame.width()), int(frame.height()))
        if size != frame_size:
            raise ValueError(
                "Median flat-field frame size "
                f"{size[0]}x{size[1]} does not match "
                f"{frame_size[0]}x{frame_size[1]}."
            )
        arrays.append(_qimage_to_rgb_array(frame))
    return np.median(np.stack(arrays, axis=0), axis=0).astype(np.uint8)


def _tile_blend_weights(width: int, height: int, overlap_fraction: float) -> Any:
    import numpy as np

    overlap = min(max(float(overlap_fraction), 0.0), 0.95)
    if overlap <= 0.0:
        return np.ones((int(height), int(width)), dtype=np.float32)
    feather_x = min(int(width) // 2, max(1, int(round(float(width) * overlap * 0.5))))
    feather_y = min(int(height) // 2, max(1, int(round(float(height) * overlap * 0.5))))
    wx = np.ones(int(width), dtype=np.float32)
    wy = np.ones(int(height), dtype=np.float32)
    if feather_x > 0:
        ramp = np.linspace(0.05, 1.0, feather_x, dtype=np.float32)
        wx[:feather_x] = np.minimum(wx[:feather_x], ramp)
        wx[-feather_x:] = np.minimum(wx[-feather_x:], ramp[::-1])
    if feather_y > 0:
        ramp = np.linspace(0.05, 1.0, feather_y, dtype=np.float32)
        wy[:feather_y] = np.minimum(wy[:feather_y], ramp)
        wy[-feather_y:] = np.minimum(wy[-feather_y:], ramp[::-1])
    return wy[:, None] * wx[None, :]


def _apply_flat_field_array(rgb: Any, profile: FlatFieldProfile) -> Any:
    import numpy as np

    rgb_float = rgb.astype(np.float32, copy=False)
    illumination = profile.illumination_rgb.astype(np.float32, copy=False)
    mean = np.asarray(profile.mean_rgb, dtype=np.float32).reshape((1, 1, 3))
    denominator_floor = np.maximum(mean / float(profile.max_gain), 1.0)
    denominator = np.maximum(illumination, denominator_floor)
    corrected = rgb_float * (mean / denominator)
    return np.clip(corrected, 0.0, 255.0).astype(np.uint8)


def _deduplicated_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}_{index:03d}{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Unable to choose a unique file name near {path}.")


def _timestamp_for_filename(captured_at: str) -> str:
    text = str(captured_at or "").strip()
    text = text.replace(":", "").replace("-", "").replace("+", "_")
    text = text.replace("T", "_").replace(" ", "_")
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", text)
    return text.strip("_") or datetime.now().strftime("%Y%m%d_%H%M%S")


__all__ = [
    "FlatFieldProfile",
    "MicroscopeCaptureResult",
    "MicroscopeImageMetadata",
    "MicroscopeScaleCalibration",
    "MicroscopeScanPlan",
    "MicroscopeScanTile",
    "apply_flat_field_correction",
    "apply_self_flat_field_correction",
    "build_flat_field_profile",
    "build_median_flat_field_profile",
    "build_design_scan_plan",
    "objective_scale_calibration",
    "render_microscope_overlay",
    "route_photo_filename",
    "save_microscope_image",
    "scan_tile_filename",
    "stage_bounds_from_design_bounds",
    "stitch_scan_tiles",
    "utc_timestamp",
]
