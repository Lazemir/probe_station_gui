"""Capture geometry and fitting for optical calibration."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtGui import QImage

from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    StageGeometryCorrection,
    correction_from_payload,
    fit_stage_geometry_from_observations,
)
from probe_station_gui.settings.objective_config import parse_pixels_to_mm_matrix


PixelMatrix = tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True)
class LensFitLimits:
    cluster_tolerance_px: float = 12.0
    min_feature_count: int = 4
    min_observation_count: int = 12
    max_residual_mean_px: float = 3.0
    max_residual_max_px: float = 12.0


@dataclass(frozen=True)
class LensFitArtifact:
    payload: dict[str, object]
    before_preview: QImage
    after_preview: QImage


@dataclass(frozen=True)
class LensPreviewMetrics:
    without_calibration: tuple[float, float]
    with_calibration: tuple[float, float]


def frame_size(frame: QImage) -> tuple[int, int]:
    width = int(frame.width())
    height = int(frame.height())
    if width <= 0 or height <= 0:
        raise RuntimeError("Camera frame size is unavailable.")
    return width, height


def flat_field_capture_offsets_mm(
    size_px: tuple[int, int],
    pixel_size_mm: tuple[float, float],
    overlap_fraction: float,
) -> tuple[tuple[float, float], ...]:
    step_x = abs(float(size_px[0]) * float(pixel_size_mm[0])) * (
        1.0 - float(overlap_fraction)
    )
    step_y = abs(float(size_px[1]) * float(pixel_size_mm[1])) * (
        1.0 - float(overlap_fraction)
    )
    if not all(math.isfinite(value) and value > 0.0 for value in (step_x, step_y)):
        raise RuntimeError("Flat-field capture spacing is invalid.")
    return _serpentine_offsets((-step_x, 0.0, step_x), (-step_y, 0.0, step_y))


def lens_capture_offsets_mm(
    size_px: tuple[int, int],
    pixels_to_mm: PixelMatrix,
    pixel_size_mm: tuple[float, float],
    *,
    grid_size: int,
    fov_fraction: float,
) -> tuple[tuple[float, float], ...]:
    width, height = size_px
    if width <= 0 or height <= 0:
        raise RuntimeError("Camera frame size is unavailable.")
    x_offsets = _axis_offsets(float(width) * float(fov_fraction), grid_size)
    y_offsets = _axis_offsets(float(height) * float(fov_fraction), grid_size)
    pixel_offsets = _serpentine_offsets(x_offsets, y_offsets)
    return tuple(
        _pixel_shift_to_stage(pixels_to_mm, pixel_size_mm, x_px, y_px)
        for x_px, y_px in pixel_offsets
    )


def fit_lens_artifact(
    frames: Sequence[GridCalibrationFrame],
    *,
    size_px: tuple[int, int],
    pixels_to_mm: PixelMatrix,
    limits: LensFitLimits,
) -> LensFitArtifact:
    from probe_station_gui.camera.geometry_alignment_preview import (
        build_geometry_alignment_previews,
    )
    from probe_station_gui.camera.geometry_feature_tracking import (
        build_geometry_feature_observations,
    )
    from probe_station_gui.camera.geometry_segmentation import segment_metal_geometry

    persisted_matrix = _required_matrix(pixels_to_mm)
    image_matrix = flip_pixel_matrix_y(persisted_matrix)
    masks = tuple(segment_metal_geometry(item.frame) for item in frames)
    observations = build_geometry_feature_observations(
        frames,
        masks,
        frame_size=size_px,
        image_pixels_to_mm=image_matrix,
        match_gate_px=float(limits.cluster_tolerance_px),
    )
    fit = fit_stage_geometry_from_observations(
        observations,
        frame_size=size_px,
        initial_pixels_to_mm=image_matrix,
    )
    payload = fit.to_payload()
    if not isinstance(payload, dict):
        raise RuntimeError("Lens distortion fit returned an invalid payload.")
    _restore_payload_y_convention(payload)
    validate_lens_payload(payload, limits)
    before, after = build_geometry_alignment_previews(
        frames,
        masks,
        persisted_matrix,
        payload,
    )
    return LensFitArtifact(payload, before, after)


def validate_lens_payload(payload: object, limits: LensFitLimits) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError("Lens distortion fit returned an invalid payload.")
    if payload.get("model_type") != "stage_geometry":
        raise RuntimeError("Lens distortion calibration model_type must be stage_geometry.")
    residuals = _validated_residuals(payload)
    if (
        residuals["residual_mean_px"] > limits.max_residual_mean_px
        or residuals["residual_max_px"] > limits.max_residual_max_px
    ):
        raise RuntimeError(
            "Lens distortion calibration residual is too high "
            f"({residuals['residual_mean_px']:.2f} px mean, "
            f"{residuals['residual_max_px']:.2f} px max)."
        )
    _validate_required_payload(payload, limits)
    try:
        correction = correction_from_payload(payload)
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Lens distortion calibration payload cannot be applied.") from exc
    if not isinstance(correction, StageGeometryCorrection):
        raise RuntimeError("Lens distortion calibration payload is not stage geometry.")


def lens_success_message(payload: dict[str, object], limits: LensFitLimits) -> str:
    validate_lens_payload(payload, limits)
    return (
        "Lens distortion calibration saved "
        f"(raw geometry, {float(payload['residual_mean_px']):.2f} px mean, "
        f"{float(payload['residual_max_px']):.2f} px max)."
    )


def validate_lens_previews(
    payload: dict[str, object],
    before_preview: QImage,
    after_preview: QImage,
    limits: LensFitLimits,
) -> LensPreviewMetrics:
    if before_preview.isNull() or after_preview.isNull():
        raise RuntimeError("Invalid lens calibration previews.")
    validate_lens_payload(payload, limits)
    return LensPreviewMetrics(
        without_calibration=(
            float(payload["baseline_residual_mean_px"]),
            float(payload["baseline_residual_max_px"]),
        ),
        with_calibration=(
            float(payload["residual_mean_px"]),
            float(payload["residual_max_px"]),
        ),
    )


def flip_pixel_matrix_y(matrix: PixelMatrix) -> PixelMatrix:
    return (
        (float(matrix[0][0]), -float(matrix[0][1])),
        (float(matrix[1][0]), -float(matrix[1][1])),
    )


def _serpentine_offsets(
    x_offsets: Sequence[float],
    y_offsets: Sequence[float],
) -> tuple[tuple[float, float], ...]:
    offsets: list[tuple[float, float]] = [(0.0, 0.0)]
    for row, y_value in enumerate(y_offsets):
        row_x = x_offsets if row % 2 == 0 else tuple(reversed(x_offsets))
        for x_value in row_x:
            if abs(x_value) <= 1e-15 and abs(y_value) <= 1e-15:
                continue
            offsets.append((float(x_value), float(y_value)))
    return tuple(offsets)


def _axis_offsets(extent_value: float, grid_size: int) -> tuple[float, ...]:
    count = max(3, int(grid_size))
    count += int(count % 2 == 0)
    extent = abs(float(extent_value))
    if not math.isfinite(extent) or extent <= 0.0:
        return (0.0,)
    midpoint = count // 2
    step = extent / float(midpoint)
    return tuple((index - midpoint) * step for index in range(count))


def _pixel_shift_to_stage(
    matrix: PixelMatrix,
    pixel_size_mm: tuple[float, float],
    shift_x_px: float,
    shift_y_px: float,
) -> tuple[float, float]:
    x_delta = float(shift_x_px)
    y_delta = -float(shift_y_px)
    parsed = parse_pixels_to_mm_matrix(matrix)
    if parsed:
        return (
            float(parsed[0][0]) * x_delta + float(parsed[0][1]) * y_delta,
            float(parsed[1][0]) * x_delta + float(parsed[1][1]) * y_delta,
        )
    return (x_delta * pixel_size_mm[0], -y_delta * pixel_size_mm[1])


def _required_matrix(matrix: PixelMatrix) -> PixelMatrix:
    parsed = parse_pixels_to_mm_matrix(matrix)
    if not parsed:
        raise RuntimeError("Lens distortion calibration requires click-to-move calibration.")
    return tuple(tuple(float(value) for value in row) for row in parsed)  # type: ignore[return-value]


def _restore_payload_y_convention(payload: dict[str, object]) -> None:
    for key in ("pixels_to_mm", "calibrated_pixels_to_mm"):
        parsed = parse_pixels_to_mm_matrix(payload.get(key))
        if parsed:
            payload[key] = [list(row) for row in flip_pixel_matrix_y(parsed)]


def _validated_residuals(payload: dict[str, object]) -> dict[str, float]:
    result: dict[str, float] = {}
    for field in (
        "baseline_residual_mean_px",
        "baseline_residual_max_px",
        "residual_mean_px",
        "residual_max_px",
    ):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"Lens distortion calibration {field} is not numeric.")
        result[field] = float(value)
        if not math.isfinite(result[field]):
            raise RuntimeError(f"Lens distortion calibration {field} is not finite.")
        if result[field] < 0.0:
            raise RuntimeError(f"Lens distortion calibration {field} is negative.")
    return result


def _validate_required_payload(payload: dict[str, object], limits: LensFitLimits) -> None:
    required = (
        "model_version", "frame_size", "pixels_to_mm", "calibrated_pixels_to_mm",
        "center_px", "k1", "k2", "p1", "p2", "feature_count",
        "observation_count", "optimizer_success",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise RuntimeError(
            "Lens distortion calibration payload is incomplete: " + ", ".join(missing) + "."
        )
    if payload.get("model_version") != 1 or isinstance(payload.get("model_version"), bool):
        raise RuntimeError("Lens distortion calibration model_version is invalid.")
    if not parse_pixels_to_mm_matrix(payload.get("pixels_to_mm")) or not parse_pixels_to_mm_matrix(
        payload.get("calibrated_pixels_to_mm")
    ):
        raise RuntimeError("Lens distortion calibration pixel matrices are invalid.")
    _validate_count(payload, "feature_count", limits.min_feature_count)
    _validate_count(payload, "observation_count", limits.min_observation_count)
    if payload.get("optimizer_success") is not True:
        raise RuntimeError("Lens distortion calibration optimizer did not converge.")


def _validate_count(payload: dict[str, object], field: str, minimum: int) -> None:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"Lens distortion calibration {field} is insufficient.")


__all__ = [
    "LensFitArtifact",
    "LensFitLimits",
    "LensPreviewMetrics",
    "PixelMatrix",
    "fit_lens_artifact",
    "flat_field_capture_offsets_mm",
    "flip_pixel_matrix_y",
    "frame_size",
    "lens_capture_offsets_mm",
    "lens_success_message",
    "validate_lens_payload",
    "validate_lens_previews",
]
