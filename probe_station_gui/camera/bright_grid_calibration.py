"""Axis/grid distortion calibration fitting from bright-grid detections."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import probe_station_gui.camera.bright_grid_detection as bright_grid_detection
from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    _interp_with_extrapolation,
    _positive_float,
    _valid_pixels_to_mm_matrix,
    correction_from_payload,
    distortion_payload_from_points,
)


Point2D = tuple[float, float]


def fit_distortion_from_grid_frames(
    frames: Sequence[GridCalibrationFrame],
    *,
    frame_size: tuple[int, int],
    grid_spacing_um: float = 50.0,
    pixels_to_mm: Sequence[Sequence[float]] | None = None,
) -> dict[str, object]:
    """Fit a correction payload from partial bright-grid calibration frames."""

    expected_spacing_px = _expected_axis_spacing_px(
        pixels_to_mm,
        grid_spacing_um=grid_spacing_um,
    )
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    capture_offsets: list[list[float]] = []
    frame_candidates: list[dict[str, object]] = []
    detections: list[bright_grid_detection.GridDetection] = []
    for item in frames:
        detection = bright_grid_detection.detect_bright_grid(
            item.frame,
            expected_spacing_px=expected_spacing_px,
        )
        if (
            len(detection.vertical_lines_px) < 2
            or len(detection.horizontal_lines_px) < 2
        ):
            continue
        frame_source, frame_target = _grid_points_from_detection(detection)
        if len(frame_source) < 8:
            continue
        offset = [float(item.stage_offset_mm[0]), float(item.stage_offset_mm[1])]
        candidate = _axis_distortion_payload_from_detection(
            frame_size=frame_size,
            detection=detection,
            grid_spacing_um=grid_spacing_um,
            expected_spacing_px=expected_spacing_px,
        )
        candidate["capture_offsets_mm"] = [offset]
        candidate["fit_frame_count"] = 1
        frame_candidates.append(candidate)
        detections.append(detection)
        capture_offsets.append(offset)
        source_points.extend(frame_source)
        target_points.extend(frame_target)

    if len(source_points) < 8:
        raise ValueError("Grid coverage is too small.")
    scale_payload = _grid_scale_payload_from_detections(
        detections,
        stage_offsets_mm=capture_offsets,
        pixels_to_mm=pixels_to_mm,
        grid_spacing_um=grid_spacing_um,
    )
    payload = _distortion_payload_with_residuals(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    payload["capture_offsets_mm"] = capture_offsets
    payload["fit_frame_count"] = len(capture_offsets)
    payload.update(scale_payload)
    if scale_payload:
        for candidate in frame_candidates:
            candidate.update(scale_payload)
    if len(frame_candidates) == 1:
        return frame_candidates[0]
    axis_payload = _axis_distortion_payload_from_detections(
        frame_size=frame_size,
        detections=detections,
        grid_spacing_um=grid_spacing_um,
        scale_payload=scale_payload,
    )
    if axis_payload is not None:
        axis_mean = float(axis_payload.get("residual_mean_px", math.inf))
        combined_mean = float(payload.get("residual_mean_px", math.inf))
        if axis_mean <= max(2.0, combined_mean * 2.0):
            return axis_payload
    if frame_candidates:
        best_payload = min(
            frame_candidates,
            key=lambda item: float(item.get("residual_mean_px", math.inf)),
        )
        combined_mean = float(payload.get("residual_mean_px", math.inf))
        best_mean = float(best_payload.get("residual_mean_px", math.inf))
        if combined_mean > max(8.0, best_mean * 4.0):
            return best_payload
    return payload


def _axis_distortion_payload_from_detections(
    *,
    frame_size: tuple[int, int],
    detections: Sequence[bright_grid_detection.GridDetection],
    grid_spacing_um: float,
    scale_payload: dict[str, object],
) -> dict[str, object] | None:
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    x_pairs: list[tuple[float, float]] = []
    y_pairs: list[tuple[float, float]] = []
    for detection in detections:
        frame_source, frame_target = _grid_points_from_detection(detection)
        source_points.extend(frame_source)
        target_points.extend(frame_target)
        x_pairs.extend(
            zip(
                (float(value) for value in detection.vertical_lines_px),
                bright_grid_detection._regularized_positions(
                    detection.vertical_lines_px
                ),
            )
        )
        y_pairs.extend(
            zip(
                (float(value) for value in detection.horizontal_lines_px),
                bright_grid_detection._regularized_positions(
                    detection.horizontal_lines_px
                ),
            )
        )
    axis_x = _consolidated_axis_pairs(x_pairs)
    axis_y = _consolidated_axis_pairs(y_pairs)
    if axis_x is None or axis_y is None or len(source_points) < 8:
        return None
    axis_source_x, axis_target_x = axis_x
    axis_source_y, axis_target_y = axis_y
    payload = distortion_payload_from_points(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    residuals = _axis_interpolation_residuals(
        axis_source_x=axis_source_x,
        axis_target_x=axis_target_x,
        axis_source_y=axis_source_y,
        axis_target_y=axis_target_y,
        source_points=source_points,
        target_points=target_points,
    )
    payload["residual_mean_px"] = (
        float(sum(residuals) / len(residuals)) if residuals else 0.0
    )
    payload["residual_max_px"] = float(max(residuals)) if residuals else 0.0
    payload["axis_source_x"] = [float(value) for value in axis_source_x]
    payload["axis_target_x"] = [float(value) for value in axis_target_x]
    payload["axis_source_y"] = [float(value) for value in axis_source_y]
    payload["axis_target_y"] = [float(value) for value in axis_target_y]
    payload["fit_frame_count"] = len(detections)
    payload.update(scale_payload)
    return payload


def _consolidated_axis_pairs(
    pairs: Sequence[tuple[float, float]],
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    values = sorted(
        (
            (float(source), float(target))
            for source, target in pairs
            if math.isfinite(float(source)) and math.isfinite(float(target))
        ),
        key=lambda item: item[0],
    )
    if len(values) < 2:
        return None
    merged: list[tuple[float, float, int]] = []
    for source, target in values:
        if not merged or abs(source - merged[-1][0]) >= 3.0:
            merged.append((source, target, 1))
            continue
        old_source, old_target, count = merged[-1]
        next_count = count + 1
        merged[-1] = (
            (old_source * count + source) / next_count,
            (old_target * count + target) / next_count,
            next_count,
        )
    source_axis: list[float] = []
    target_axis: list[float] = []
    for source, target, _count in merged:
        if source_axis and (
            source <= source_axis[-1] + 1e-6 or target <= target_axis[-1] + 1e-6
        ):
            continue
        source_axis.append(source)
        target_axis.append(target)
    if len(source_axis) < 2:
        return None
    return (tuple(source_axis), tuple(target_axis))


def _grid_scale_payload_from_detections(
    detections: Sequence[bright_grid_detection.GridDetection],
    *,
    stage_offsets_mm: Sequence[Sequence[float]] = (),
    pixels_to_mm: Sequence[Sequence[float]] | None,
    grid_spacing_um: float,
) -> dict[str, object]:
    matrix = _valid_pixels_to_mm_matrix(pixels_to_mm)
    if matrix is None:
        return {}
    spacing_mm = _positive_float(grid_spacing_um, "grid_spacing_um") / 1000.0
    x_spacing_px = _median_grid_line_spacing_px(
        detection.vertical_lines_px for detection in detections
    )
    y_spacing_px = _median_grid_line_spacing_px(
        detection.horizontal_lines_px for detection in detections
    )
    if x_spacing_px is None or y_spacing_px is None:
        return {}
    calibrated = _rescale_pixel_matrix_axes(
        matrix,
        x_mm_per_px=spacing_mm / x_spacing_px,
        y_mm_per_px=spacing_mm / y_spacing_px,
    )
    payload: dict[str, object] = {
        "grid_line_spacing_px": [float(x_spacing_px), float(y_spacing_px)],
        "grid_pixel_size_um": [
            float(spacing_mm / x_spacing_px * 1000.0),
            float(spacing_mm / y_spacing_px * 1000.0),
        ],
    }
    if calibrated is not None:
        payload["grid_pixels_to_mm_estimate"] = [
            [float(calibrated[0][0]), float(calibrated[0][1])],
            [float(calibrated[1][0]), float(calibrated[1][1])],
        ]
    stage_x_spacing_mm = _median_nonzero_stage_offset_mm(stage_offsets_mm, 0)
    stage_y_spacing_mm = _median_nonzero_stage_offset_mm(stage_offsets_mm, 1)
    if stage_x_spacing_mm is None or stage_y_spacing_mm is None:
        return payload
    if not _stage_offsets_match_grid_spacing(
        stage_x_spacing_mm,
        stage_y_spacing_mm,
        expected_spacing_mm=spacing_mm,
    ):
        return payload
    stage_calibrated = _rescale_pixel_matrix_axes(
        matrix,
        x_mm_per_px=stage_x_spacing_mm / x_spacing_px,
        y_mm_per_px=stage_y_spacing_mm / y_spacing_px,
    )
    if stage_calibrated is None:
        return payload
    payload["calibrated_pixels_to_mm"] = [
        [float(stage_calibrated[0][0]), float(stage_calibrated[0][1])],
        [float(stage_calibrated[1][0]), float(stage_calibrated[1][1])],
    ]
    payload["calibrated_pixel_size_um"] = [
        float(stage_x_spacing_mm / x_spacing_px * 1000.0),
        float(stage_y_spacing_mm / y_spacing_px * 1000.0),
    ]
    payload["calibrated_stage_spacing_mm"] = [
        float(stage_x_spacing_mm),
        float(stage_y_spacing_mm),
    ]
    return payload


def _stage_offsets_match_grid_spacing(
    x_spacing_mm: float,
    y_spacing_mm: float,
    *,
    expected_spacing_mm: float,
) -> bool:
    if (
        not math.isfinite(x_spacing_mm)
        or not math.isfinite(y_spacing_mm)
        or not math.isfinite(expected_spacing_mm)
        or expected_spacing_mm <= 0.0
    ):
        return False
    tolerance = max(expected_spacing_mm * 0.25, 1e-6)
    return (
        abs(float(x_spacing_mm) - expected_spacing_mm) <= tolerance
        and abs(float(y_spacing_mm) - expected_spacing_mm) <= tolerance
    )


def _median_nonzero_stage_offset_mm(
    offsets: Sequence[Sequence[float]],
    axis_index: int,
) -> float | None:
    values: list[float] = []
    for offset in offsets:
        try:
            value = abs(float(offset[axis_index]))
        except (IndexError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 1e-9:
            values.append(value)
    if not values:
        return None
    values.sort()
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) * 0.5


def _median_grid_line_spacing_px(
    line_groups: Iterable[Sequence[float]],
) -> float | None:
    samples: list[float] = []
    for lines in line_groups:
        regularized = bright_grid_detection._regularized_positions(lines)
        for left, right in zip(regularized, regularized[1:]):
            spacing = float(right) - float(left)
            if math.isfinite(spacing) and spacing > 0.0:
                samples.append(spacing)
    if not samples:
        return None
    values = sorted(samples)
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) * 0.5


def _rescale_pixel_matrix_axes(
    matrix: tuple[tuple[float, float], tuple[float, float]],
    *,
    x_mm_per_px: float,
    y_mm_per_px: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if (
        not math.isfinite(x_mm_per_px)
        or not math.isfinite(y_mm_per_px)
        or x_mm_per_px <= 0.0
        or y_mm_per_px <= 0.0
    ):
        return None
    x_norm = math.hypot(matrix[0][0], matrix[1][0])
    y_norm = math.hypot(matrix[0][1], matrix[1][1])
    if x_norm <= 0.0 or y_norm <= 0.0:
        return None
    x_scale = x_mm_per_px / x_norm
    y_scale = y_mm_per_px / y_norm
    calibrated = (
        (matrix[0][0] * x_scale, matrix[0][1] * y_scale),
        (matrix[1][0] * x_scale, matrix[1][1] * y_scale),
    )
    determinant = (
        calibrated[0][0] * calibrated[1][1] - calibrated[0][1] * calibrated[1][0]
    )
    if not math.isfinite(determinant) or abs(determinant) < 1e-18:
        return None
    return calibrated


def _grid_points_from_detection(
    detection: bright_grid_detection.GridDetection,
    *,
    expected_spacing_px: Point2D | None = None,
) -> tuple[list[Point2D], list[Point2D]]:
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    target_x = bright_grid_detection._regularized_positions(detection.vertical_lines_px)
    target_y = bright_grid_detection._regularized_positions(
        detection.horizontal_lines_px
    )
    for row, source_y in enumerate(detection.horizontal_lines_px):
        for column, source_x in enumerate(detection.vertical_lines_px):
            source_points.append((float(source_x), float(source_y)))
            target_points.append((float(target_x[column]), float(target_y[row])))
    return source_points, target_points


def _distortion_payload_with_residuals(
    *,
    frame_size: tuple[int, int],
    source_points: Sequence[Sequence[float]],
    target_points: Sequence[Sequence[float]],
    grid_spacing_um: float,
) -> dict[str, object]:
    payload = distortion_payload_from_points(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    correction = correction_from_payload(payload)
    residuals = _homography_residuals(
        correction.homography,
        correction.source_points,
        correction.target_points,
    )
    payload["residual_mean_px"] = (
        float(sum(residuals) / len(residuals)) if residuals else 0.0
    )
    payload["residual_max_px"] = float(max(residuals)) if residuals else 0.0
    return payload


def _axis_distortion_payload_from_detection(
    *,
    frame_size: tuple[int, int],
    detection: bright_grid_detection.GridDetection,
    grid_spacing_um: float,
    expected_spacing_px: Point2D | None = None,
) -> dict[str, object]:
    source_points, target_points = _grid_points_from_detection(
        detection,
    )
    axis_source_x = tuple(float(value) for value in detection.vertical_lines_px)
    expected_x = bright_grid_detection._positive_optional_spacing(
        expected_spacing_px, 0
    )
    expected_y = bright_grid_detection._positive_optional_spacing(
        expected_spacing_px, 1
    )
    axis_target_x = bright_grid_detection._regularized_positions(axis_source_x)
    axis_source_y = tuple(float(value) for value in detection.horizontal_lines_px)
    axis_target_y = bright_grid_detection._regularized_positions(axis_source_y)
    payload = distortion_payload_from_points(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    residuals = _axis_interpolation_residuals(
        axis_source_x=axis_source_x,
        axis_target_x=axis_target_x,
        axis_source_y=axis_source_y,
        axis_target_y=axis_target_y,
        source_points=source_points,
        target_points=target_points,
    )
    payload["residual_mean_px"] = (
        float(sum(residuals) / len(residuals)) if residuals else 0.0
    )
    payload["residual_max_px"] = float(max(residuals)) if residuals else 0.0
    payload["axis_source_x"] = [float(value) for value in axis_source_x]
    payload["axis_target_x"] = [float(value) for value in axis_target_x]
    payload["axis_source_y"] = [float(value) for value in axis_source_y]
    payload["axis_target_y"] = [float(value) for value in axis_target_y]
    if expected_x is not None or expected_y is not None:
        payload["axis_expected_spacing_px"] = [
            float(expected_x) if expected_x is not None else None,
            float(expected_y) if expected_y is not None else None,
        ]
    return payload


def _axis_interpolation_residuals(
    *,
    axis_source_x: Sequence[float],
    axis_target_x: Sequence[float],
    axis_source_y: Sequence[float],
    axis_target_y: Sequence[float],
    source_points: Sequence[Point2D],
    target_points: Sequence[Point2D],
) -> list[float]:
    import numpy as np

    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    if source.size == 0 or target.size == 0:
        return []
    mapped_x = _interp_with_extrapolation(
        source[:, 0],
        axis_source_x,
        axis_target_x,
    )
    mapped_y = _interp_with_extrapolation(
        source[:, 1],
        axis_source_y,
        axis_target_y,
    )
    mapped = np.column_stack((mapped_x, mapped_y))
    errors = np.linalg.norm(mapped - target, axis=1)
    return [float(value) for value in errors if math.isfinite(float(value))]


def _expected_axis_spacing_px(
    pixels_to_mm: Sequence[Sequence[float]] | None,
    *,
    grid_spacing_um: float,
) -> Point2D | None:
    if pixels_to_mm is None:
        return None
    try:
        rows = [[float(value) for value in row] for row in pixels_to_mm]
    except (TypeError, ValueError):
        return None
    if len(rows) != 2 or any(len(row) != 2 for row in rows):
        return None
    if any(not math.isfinite(value) for row in rows for value in row):
        return None
    spacing_mm = _positive_float(grid_spacing_um, "grid_spacing_um") / 1000.0
    x_mm_per_px = math.hypot(rows[0][0], rows[1][0])
    y_mm_per_px = math.hypot(rows[0][1], rows[1][1])
    if x_mm_per_px <= 0.0 or y_mm_per_px <= 0.0:
        return None
    return (spacing_mm / x_mm_per_px, spacing_mm / y_mm_per_px)


def _homography_residuals(
    homography: object,
    source_points: Sequence[Point2D],
    target_points: Sequence[Point2D],
) -> list[float]:
    import cv2
    import numpy as np

    source = np.asarray(source_points, dtype=np.float32).reshape((-1, 1, 2))
    projected = cv2.perspectiveTransform(source, homography).reshape((-1, 2))
    target = np.asarray(target_points, dtype=np.float32)
    errors = np.linalg.norm(projected - target, axis=1)
    return [float(value) for value in errors if math.isfinite(float(value))]


__all__ = [
    "fit_distortion_from_grid_frames",
]
