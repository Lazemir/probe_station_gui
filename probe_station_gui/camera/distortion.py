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


def _qimage_rgb32_array(image: QImage):
    import numpy as np

    width = image.width()
    height = image.height()
    ptr = image.constBits()
    array = np.frombuffer(ptr, np.uint8, count=image.sizeInBytes()).reshape(
        (height, image.bytesPerLine())
    )
    return array[:, : width * 4].reshape((height, width, 4))


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
    "MODEL_VERSION",
    "apply_distortion_correction",
    "correction_from_payload",
    "distortion_payload_from_points",
]
