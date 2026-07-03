"""Lens distortion correction helpers for microscope frames."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from PySide6.QtGui import QImage


Point2D = tuple[float, float]
MODEL_VERSION = 1


@dataclass(frozen=True)
class DistortionCorrection:
    """Runtime correction model built from a persisted objective payload."""

    frame_size: tuple[int, int]
    source_points: tuple[Point2D, ...]
    target_points: tuple[Point2D, ...]
    homography: object
    residual_mean_px: float = 0.0
    residual_max_px: float = 0.0


@dataclass(frozen=True)
class GridDetection:
    """Detected bright grid line centers and their intersections in one frame."""

    vertical_lines_px: tuple[float, ...]
    horizontal_lines_px: tuple[float, ...]
    intersections_px: tuple[Point2D, ...]


@dataclass(frozen=True)
class GridCalibrationFrame:
    """One captured calibration frame with its stage offset from the origin."""

    frame: object
    stage_offset_mm: Point2D


def distortion_payload_from_points(
    *,
    frame_size: tuple[int, int],
    source_points: Sequence[Sequence[float]],
    target_points: Sequence[Sequence[float]],
    grid_spacing_um: float,
    residual_mean_px: float = 0.0,
    residual_max_px: float = 0.0,
) -> dict[str, object]:
    """Build a persisted correction payload from corresponding image points."""

    width, height = _valid_frame_size(frame_size)
    sources = _valid_points(source_points, "source_points")
    targets = _valid_points(target_points, "target_points")
    if len(sources) != len(targets):
        raise ValueError("source_points and target_points must have the same length.")
    if len(sources) < 4:
        raise ValueError("At least four point pairs are required.")
    spacing = _positive_float(grid_spacing_um, "grid_spacing_um")
    mean_error = _nonnegative_float(residual_mean_px, "residual_mean_px")
    max_error = _nonnegative_float(residual_max_px, "residual_max_px")
    return {
        "model_version": MODEL_VERSION,
        "frame_size": [width, height],
        "grid_spacing_um": spacing,
        "source_points": [[x, y] for x, y in sources],
        "target_points": [[x, y] for x, y in targets],
        "residual_mean_px": mean_error,
        "residual_max_px": max_error,
    }


def correction_from_payload(payload: object) -> DistortionCorrection:
    """Build a runtime correction model from a persisted payload."""

    if not isinstance(payload, dict):
        raise ValueError("Distortion correction payload must be an object.")
    version = int(payload.get("model_version", 0))
    if version != MODEL_VERSION:
        raise ValueError("Unsupported distortion correction model version.")
    frame_size = _valid_frame_size(payload.get("frame_size"))
    sources = _valid_points(payload.get("source_points"), "source_points")
    targets = _valid_points(payload.get("target_points"), "target_points")
    if len(sources) != len(targets):
        raise ValueError("source_points and target_points must have the same length.")
    if len(sources) < 4:
        raise ValueError("At least four point pairs are required.")

    import cv2
    import numpy as np

    source_array = np.asarray(sources, dtype=np.float32)
    target_array = np.asarray(targets, dtype=np.float32)
    homography, _mask = cv2.findHomography(source_array, target_array, 0)
    if homography is None or not np.isfinite(homography).all():
        raise ValueError("Distortion correction point pairs are degenerate.")
    return DistortionCorrection(
        frame_size=frame_size,
        source_points=sources,
        target_points=targets,
        homography=homography,
        residual_mean_px=_optional_nonnegative(payload.get("residual_mean_px")),
        residual_max_px=_optional_nonnegative(payload.get("residual_max_px")),
    )


def apply_distortion_correction(
    frame: QImage,
    correction: DistortionCorrection,
) -> QImage:
    """Return a corrected copy of a microscope frame."""

    if frame.isNull():
        return frame.copy()
    expected_width, expected_height = correction.frame_size
    if frame.width() != expected_width or frame.height() != expected_height:
        raise ValueError("Distortion correction frame size does not match image frame size.")

    import cv2

    image = frame.convertToFormat(QImage.Format_RGB32)
    array = _qimage_rgb32_array(image)
    corrected = cv2.warpPerspective(
        array,
        correction.homography,
        (expected_width, expected_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    result = QImage(
        corrected.data,
        expected_width,
        expected_height,
        int(corrected.strides[0]),
        QImage.Format_RGB32,
    )
    return result.copy()


def detect_bright_grid(frame: object) -> GridDetection:
    """Detect visible bright grid lines in a microscope calibration frame."""

    import cv2
    import numpy as np

    gray = _gray_array(frame)
    if gray.size == 0:
        return GridDetection((), (), ())
    blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=9.0, sigmaY=9.0)
    enhanced = cv2.addWeighted(gray, 1.6, blurred, -0.6, 0.0)
    threshold = max(float(np.percentile(enhanced, 92.0)), float(enhanced.max()) * 0.55)
    mask = enhanced >= threshold
    vertical = _projection_line_centers(mask.mean(axis=0))
    horizontal = _projection_line_centers(mask.mean(axis=1))
    intersections = tuple((x_pos, y_pos) for y_pos in horizontal for x_pos in vertical)
    return GridDetection(
        vertical_lines_px=vertical,
        horizontal_lines_px=horizontal,
        intersections_px=intersections,
    )


def fit_distortion_from_grid_frames(
    frames: Sequence[GridCalibrationFrame],
    *,
    frame_size: tuple[int, int],
    grid_spacing_um: float = 50.0,
) -> dict[str, object]:
    """Fit a correction payload from partial bright-grid calibration frames."""

    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    capture_offsets: list[list[float]] = []
    frame_candidates: list[dict[str, object]] = []
    for item in frames:
        detection = detect_bright_grid(item.frame)
        if len(detection.vertical_lines_px) < 2 or len(detection.horizontal_lines_px) < 2:
            continue
        frame_source, frame_target = _grid_points_from_detection(detection)
        if len(frame_source) < 8:
            continue
        offset = [float(item.stage_offset_mm[0]), float(item.stage_offset_mm[1])]
        candidate = _distortion_payload_with_residuals(
            frame_size=frame_size,
            source_points=frame_source,
            target_points=frame_target,
            grid_spacing_um=grid_spacing_um,
        )
        candidate["capture_offsets_mm"] = [offset]
        candidate["fit_frame_count"] = 1
        frame_candidates.append(candidate)
        capture_offsets.append(offset)
        source_points.extend(frame_source)
        target_points.extend(frame_target)

    if len(source_points) < 8:
        raise ValueError("Grid coverage is too small.")
    payload = _distortion_payload_with_residuals(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    payload["capture_offsets_mm"] = capture_offsets
    payload["fit_frame_count"] = len(capture_offsets)
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


def _grid_points_from_detection(
    detection: GridDetection,
) -> tuple[list[Point2D], list[Point2D]]:
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    target_x = _regularized_positions(detection.vertical_lines_px)
    target_y = _regularized_positions(detection.horizontal_lines_px)
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


def _qimage_rgb32_array(image: QImage):
    import numpy as np

    width = image.width()
    height = image.height()
    ptr = image.constBits()
    array = np.frombuffer(ptr, np.uint8, count=image.sizeInBytes()).reshape(
        (height, image.bytesPerLine())
    )
    return array[:, : width * 4].reshape((height, width, 4))


def _gray_array(frame: object):
    import cv2
    import numpy as np

    if isinstance(frame, QImage):
        converted = frame.convertToFormat(QImage.Format_RGB888)
        width = converted.width()
        height = converted.height()
        ptr = converted.constBits()
        array = np.frombuffer(
            ptr,
            np.uint8,
            count=converted.sizeInBytes(),
        ).reshape((height, converted.bytesPerLine()))
        rgb = array[:, : width * 3].reshape((height, width, 3))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    array = np.asarray(frame)
    if array.ndim == 2:
        return array.astype(np.uint8, copy=False)
    if array.ndim == 3 and array.shape[2] >= 3:
        return cv2.cvtColor(array[:, :, :3].astype(np.uint8, copy=False), cv2.COLOR_RGB2GRAY)
    return np.asarray([], dtype=np.uint8)


def _projection_line_centers(projection: object) -> tuple[float, ...]:
    import numpy as np

    values = np.asarray(projection, dtype=float)
    if values.size == 0:
        return ()
    maximum = float(values.max())
    if maximum <= 0.0 or not math.isfinite(maximum):
        return ()
    active = values >= max(maximum * 0.35, float(values.mean() + values.std()))
    components: list[tuple[float, float]] = []
    index = 0
    while index < active.size:
        if not active[index]:
            index += 1
            continue
        start = index
        while index < active.size and active[index]:
            index += 1
        stop = index
        weights = values[start:stop]
        positions = np.arange(start, stop, dtype=float)
        weight_sum = float(weights.sum())
        if weight_sum <= 0.0:
            center = float((start + stop - 1) * 0.5)
        else:
            center = float((positions * weights).sum() / weight_sum)
        components.append((center, weight_sum))
    min_gap_px = max(12.0, float(values.size) / 50.0)
    components = _merge_close_components(components, min_gap_px=min_gap_px)
    components = _strongest_components(components, max_count=5)
    return tuple(center for center, _score in components)


def _merge_close_components(
    components: Sequence[tuple[float, float]],
    *,
    min_gap_px: float,
) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for center, score in sorted(
        ((float(item[0]), max(0.0, float(item[1]))) for item in components),
        key=lambda item: item[0],
    ):
        if not merged or abs(center - merged[-1][0]) >= min_gap_px:
            merged.append((center, score))
        else:
            old_center, old_score = merged[-1]
            total_score = old_score + score
            if total_score <= 0.0:
                merged[-1] = ((old_center + center) * 0.5, 0.0)
            else:
                merged[-1] = (
                    (old_center * old_score + center * score) / total_score,
                    total_score,
                )
    return merged


def _strongest_components(
    components: Sequence[tuple[float, float]],
    *,
    max_count: int,
) -> list[tuple[float, float]]:
    values = list(components)
    if not values:
        return []
    max_score = max(score for _center, score in values)
    if max_score > 0.0:
        strong = [item for item in values if item[1] >= max_score * 0.2]
        if len(strong) >= 2:
            values = strong
    if len(values) <= max_count:
        return sorted(values, key=lambda item: item[0])
    strongest = sorted(values, key=lambda item: item[1], reverse=True)[:max_count]
    return sorted(strongest, key=lambda item: item[0])


def _regularized_positions(positions: Sequence[float]) -> tuple[float, ...]:
    values = [float(value) for value in positions]
    if len(values) <= 2:
        return tuple(values)
    first = values[0]
    last = values[-1]
    step = (last - first) / float(len(values) - 1)
    return tuple(first + step * index for index in range(len(values)))


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


def _valid_frame_size(raw_size: object) -> tuple[int, int]:
    if isinstance(raw_size, (str, bytes)):
        raise ValueError("frame_size must contain width and height.")
    try:
        values = list(raw_size)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("frame_size must contain width and height.") from exc
    if len(values) != 2:
        raise ValueError("frame_size must contain width and height.")
    try:
        width = int(values[0])
        height = int(values[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("frame_size must contain positive integers.") from exc
    if width <= 0 or height <= 0:
        raise ValueError("frame_size must contain positive integers.")
    return width, height


def _valid_points(raw_points: object, label: str) -> tuple[Point2D, ...]:
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
        raise ValueError(f"{label} must be a point sequence.")
    points: list[Point2D] = []
    for raw_point in raw_points:
        if not isinstance(raw_point, Sequence) or isinstance(raw_point, (str, bytes)):
            raise ValueError(f"{label} must contain 2D points.")
        if len(raw_point) != 2:
            raise ValueError(f"{label} must contain 2D points.")
        x = _finite_float(raw_point[0], label)
        y = _finite_float(raw_point[1], label)
        points.append((x, y))
    return tuple(points)


def _finite_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} contains a non-numeric value.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} contains a non-finite value.")
    return number


def _positive_float(value: object, label: str) -> float:
    number = _finite_float(value, label)
    if number <= 0.0:
        raise ValueError(f"{label} must be positive.")
    return number


def _nonnegative_float(value: object, label: str) -> float:
    number = _finite_float(value, label)
    if number < 0.0:
        raise ValueError(f"{label} must be non-negative.")
    return number


def _optional_nonnegative(value: object) -> float:
    try:
        return _nonnegative_float(value, "residual")
    except ValueError:
        return 0.0


__all__ = [
    "DistortionCorrection",
    "GridCalibrationFrame",
    "GridDetection",
    "MODEL_VERSION",
    "apply_distortion_correction",
    "correction_from_payload",
    "detect_bright_grid",
    "distortion_payload_from_points",
    "fit_distortion_from_grid_frames",
]
