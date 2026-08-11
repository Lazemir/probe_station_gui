"""Runtime lens-distortion payloads and correction application."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

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
    axis_source_x: tuple[float, ...] = ()
    axis_target_x: tuple[float, ...] = ()
    axis_source_y: tuple[float, ...] = ()
    axis_target_y: tuple[float, ...] = ()
    axis_map_x: object | None = field(default=None, repr=False, compare=False)
    axis_map_y: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class GridCalibrationFrame:
    """One captured calibration frame with its stage offset from the origin."""

    frame: object
    stage_offset_mm: Point2D


@dataclass(frozen=True)
class StageFeatureObservation:
    """One matched image feature observation at a known stage position."""

    frame_index: int
    feature_id: object
    stage_xy: Point2D
    pixel_xy: Point2D


@dataclass(frozen=True)
class StageGeometryCorrection:
    """Stage-consistent affine plus low-order lens geometry correction."""

    frame_size: tuple[int, int]
    pixels_to_mm: tuple[tuple[float, float], tuple[float, float]]
    center_px: Point2D
    k1: float = 0.0
    k2: float = 0.0
    p1: float = 0.0
    p2: float = 0.0
    residual_mean_px: float = 0.0
    residual_max_px: float = 0.0
    residual_mean_mm: float = 0.0
    residual_max_mm: float = 0.0
    feature_count: int = 0
    observation_count: int = 0
    map_x: object | None = field(default=None, repr=False, compare=False)
    map_y: object | None = field(default=None, repr=False, compare=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "model_version": MODEL_VERSION,
            "model_type": "stage_geometry",
            "frame_size": [int(self.frame_size[0]), int(self.frame_size[1])],
            "pixels_to_mm": [
                [float(value) for value in row] for row in self.pixels_to_mm
            ],
            "calibrated_pixels_to_mm": [
                [float(value) for value in row] for row in self.pixels_to_mm
            ],
            "center_px": [float(self.center_px[0]), float(self.center_px[1])],
            "k1": float(self.k1),
            "k2": float(self.k2),
            "p1": float(self.p1),
            "p2": float(self.p2),
            "residual_mean_px": float(self.residual_mean_px),
            "residual_max_px": float(self.residual_max_px),
            "residual_mean_mm": float(self.residual_mean_mm),
            "residual_max_mm": float(self.residual_max_mm),
            "feature_count": int(self.feature_count),
            "observation_count": int(self.observation_count),
        }


@dataclass(frozen=True)
class RadialDistortionModel:
    """Radial correction model used for seam-fit experiments."""

    frame_size: tuple[int, int]
    center_px: Point2D
    k1: float = 0.0
    k2: float = 0.0

    def scaled(self, scale: float) -> "RadialDistortionModel":
        return RadialDistortionModel(
            frame_size=(
                max(1, int(round(float(self.frame_size[0]) * float(scale)))),
                max(1, int(round(float(self.frame_size[1]) * float(scale)))),
            ),
            center_px=(
                float(self.center_px[0]) * float(scale),
                float(self.center_px[1]) * float(scale),
            ),
            k1=float(self.k1),
            k2=float(self.k2),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "radial",
            "frame_size": [int(self.frame_size[0]), int(self.frame_size[1])],
            "center_px": [float(self.center_px[0]), float(self.center_px[1])],
            "k1": float(self.k1),
            "k2": float(self.k2),
        }


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


def correction_from_payload(
    payload: object,
) -> DistortionCorrection | StageGeometryCorrection:
    """Build a runtime correction model from a persisted payload."""

    if not isinstance(payload, dict):
        raise ValueError("Distortion correction payload must be an object.")
    version = int(payload.get("model_version", 0))
    if version != MODEL_VERSION:
        raise ValueError("Unsupported distortion correction model version.")
    model_type = str(payload.get("model_type", "") or "").strip().lower()
    if model_type == "stage_geometry":
        return _stage_geometry_correction_from_payload(payload)
    frame_size = _valid_frame_size(payload.get("frame_size"))
    sources = _valid_points(payload.get("source_points"), "source_points")
    targets = _valid_points(payload.get("target_points"), "target_points")
    if len(sources) != len(targets):
        raise ValueError("source_points and target_points must have the same length.")
    if len(sources) < 4:
        raise ValueError("At least four point pairs are required.")
    axis_source_x, axis_target_x = _optional_axis_pair(
        payload,
        "axis_source_x",
        "axis_target_x",
    )
    axis_source_y, axis_target_y = _optional_axis_pair(
        payload,
        "axis_source_y",
        "axis_target_y",
    )
    if bool(axis_source_x) != bool(axis_source_y):
        raise ValueError("Distortion correction axis mapping is incomplete.")

    import cv2
    import numpy as np

    source_array = np.asarray(sources, dtype=np.float32)
    target_array = np.asarray(targets, dtype=np.float32)
    homography, _mask = cv2.findHomography(source_array, target_array, 0)
    if homography is None or not np.isfinite(homography).all():
        raise ValueError("Distortion correction point pairs are degenerate.")
    axis_map_x = None
    axis_map_y = None
    if axis_source_x and axis_source_y:
        axis_map_x, axis_map_y = _axis_interpolation_maps(
            width=frame_size[0],
            height=frame_size[1],
            axis_source_x=axis_source_x,
            axis_target_x=axis_target_x,
            axis_source_y=axis_source_y,
            axis_target_y=axis_target_y,
        )
    return DistortionCorrection(
        frame_size=frame_size,
        source_points=sources,
        target_points=targets,
        homography=homography,
        residual_mean_px=_optional_nonnegative(payload.get("residual_mean_px")),
        residual_max_px=_optional_nonnegative(payload.get("residual_max_px")),
        axis_source_x=axis_source_x,
        axis_target_x=axis_target_x,
        axis_source_y=axis_source_y,
        axis_target_y=axis_target_y,
        axis_map_x=axis_map_x,
        axis_map_y=axis_map_y,
    )


def apply_distortion_correction(
    frame: QImage,
    correction: DistortionCorrection | StageGeometryCorrection,
) -> QImage:
    """Return a corrected copy of a microscope frame."""

    if frame.isNull():
        return frame.copy()
    expected_width, expected_height = correction.frame_size
    if frame.width() != expected_width or frame.height() != expected_height:
        raise ValueError(
            "Distortion correction frame size does not match image frame size."
        )

    import cv2

    _source_image, array, result_format = _qimage_array_for_correction(frame)
    if isinstance(correction, StageGeometryCorrection):
        corrected = _apply_stage_geometry_correction_array(array, correction)
    elif correction.axis_source_x and correction.axis_source_y:
        corrected = _apply_axis_interpolation_correction(
            array,
            correction,
            width=expected_width,
            height=expected_height,
        )
    else:
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
        result_format,
    )
    return result.copy()


def apply_radial_distortion_correction(
    frame: object,
    model: RadialDistortionModel,
) -> object:
    """Apply a radial lens correction to a QImage or numpy image array."""

    width, height = _valid_frame_size(model.frame_size)
    if isinstance(frame, QImage):
        if frame.width() != width or frame.height() != height:
            raise ValueError(
                "Radial distortion frame size does not match image frame size."
            )
        _source_image, array, result_format = _qimage_array_for_correction(frame)
        corrected = _apply_radial_distortion_array(array, model)
        result = QImage(
            corrected.data,
            width,
            height,
            int(corrected.strides[0]),
            result_format,
        )
        return result.copy()
    array = _image_array(frame)
    if array.shape[1] != width or array.shape[0] != height:
        raise ValueError(
            "Radial distortion frame size does not match image frame size."
        )
    return _apply_radial_distortion_array(array, model)


def _valid_pixels_to_mm_matrix(
    pixels_to_mm: Sequence[Sequence[float]] | None,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
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
    determinant = rows[0][0] * rows[1][1] - rows[0][1] * rows[1][0]
    if abs(determinant) < 1e-18:
        return None
    return ((rows[0][0], rows[0][1]), (rows[1][0], rows[1][1]))


def _valid_point(raw_point: object, label: str) -> Point2D:
    if not isinstance(raw_point, Iterable) or isinstance(raw_point, (str, bytes)):
        raise ValueError(f"{label} must contain two coordinates.")
    values: list[float] = []
    for raw_value in raw_point:
        values.append(_finite_float(raw_value, label))
    if len(values) != 2:
        raise ValueError(f"{label} must contain two coordinates.")
    return (float(values[0]), float(values[1]))


def _optional_nonnegative_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _stage_geometry_correction_from_payload(
    payload: dict[str, object],
) -> StageGeometryCorrection:
    frame_size = _valid_frame_size(payload.get("frame_size"))
    matrix = _valid_pixels_to_mm_matrix(payload.get("pixels_to_mm"))
    if matrix is None:
        raise ValueError(
            "Stage-geometry correction requires a valid pixels_to_mm matrix."
        )
    center = _valid_point(
        payload.get(
            "center_px",
            (float(frame_size[0]) * 0.5, float(frame_size[1]) * 0.5),
        ),
        "center_px",
    )
    return StageGeometryCorrection(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=center,
        k1=_finite_float(payload.get("k1", 0.0), "k1"),
        k2=_finite_float(payload.get("k2", 0.0), "k2"),
        p1=_finite_float(payload.get("p1", 0.0), "p1"),
        p2=_finite_float(payload.get("p2", 0.0), "p2"),
        residual_mean_px=_optional_nonnegative(payload.get("residual_mean_px")),
        residual_max_px=_optional_nonnegative(payload.get("residual_max_px")),
        residual_mean_mm=_optional_nonnegative(payload.get("residual_mean_mm")),
        residual_max_mm=_optional_nonnegative(payload.get("residual_max_mm")),
        feature_count=_optional_nonnegative_int(payload.get("feature_count")),
        observation_count=_optional_nonnegative_int(payload.get("observation_count")),
    )


def _apply_stage_geometry_correction_array(
    array: object,
    correction: StageGeometryCorrection,
):
    import cv2
    import numpy as np

    image = np.asarray(array)
    width, height = _valid_frame_size(correction.frame_size)
    if image.shape[0] != height or image.shape[1] != width:
        raise ValueError(
            "Distortion correction frame size does not match image frame size."
        )
    if (
        abs(float(correction.k1)) <= 1e-15
        and abs(float(correction.k2)) <= 1e-15
        and abs(float(correction.p1)) <= 1e-15
        and abs(float(correction.p2)) <= 1e-15
    ):
        return image.copy()
    map_x = correction.map_x
    map_y = correction.map_y
    if map_x is None or map_y is None:
        map_x, map_y = _stage_geometry_inverse_maps(correction)
        object.__setattr__(correction, "map_x", map_x)
        object.__setattr__(correction, "map_y", map_y)
    return cv2.remap(
        np.ascontiguousarray(image),
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _stage_geometry_inverse_maps(
    correction: StageGeometryCorrection,
) -> tuple[object, object]:
    import numpy as np

    width, height = _valid_frame_size(correction.frame_size)
    x_values = np.arange(width, dtype=np.float32)
    y_values = np.arange(height, dtype=np.float32)
    target_x, target_y = np.meshgrid(x_values, y_values)
    observed_x = target_x.astype(np.float32, copy=True)
    observed_y = target_y.astype(np.float32, copy=True)
    for _ in range(8):
        mapped_x, mapped_y = _stage_geometry_correct_points(
            observed_x,
            observed_y,
            frame_size=(width, height),
            center_px=correction.center_px,
            k1=correction.k1,
            k2=correction.k2,
            p1=correction.p1,
            p2=correction.p2,
        )
        observed_x += target_x - mapped_x
        observed_y += target_y - mapped_y
    return observed_x.astype(np.float32, copy=False), observed_y.astype(
        np.float32, copy=False
    )


def _stage_geometry_correct_points(
    x: object,
    y: object,
    *,
    frame_size: tuple[int, int],
    center_px: Point2D,
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> tuple[object, object]:
    width, height = _valid_frame_size(frame_size)
    radius = max(float(width), float(height)) * 0.5
    cx = float(center_px[0])
    cy = float(center_px[1])
    xn = (x - cx) / radius
    yn = (y - cy) / radius
    r2 = xn * xn + yn * yn
    radial = 1.0 + float(k1) * r2 + float(k2) * r2 * r2
    x_corr = xn * radial + 2.0 * float(p1) * xn * yn + float(p2) * (r2 + 2.0 * xn * xn)
    y_corr = yn * radial + float(p1) * (r2 + 2.0 * yn * yn) + 2.0 * float(p2) * xn * yn
    return cx + x_corr * radius, cy + y_corr * radius


def _stage_geometry_correct_point(
    pixel_xy: Point2D,
    *,
    frame_size: tuple[int, int],
    center_px: Point2D,
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> Point2D:
    x, y = _stage_geometry_correct_points(
        float(pixel_xy[0]),
        float(pixel_xy[1]),
        frame_size=frame_size,
        center_px=center_px,
        k1=k1,
        k2=k2,
        p1=p1,
        p2=p2,
    )
    return (float(x), float(y))


def _apply_radial_distortion_array(array: object, model: RadialDistortionModel):
    import cv2
    import numpy as np

    image = np.asarray(array)
    width, height = _valid_frame_size(model.frame_size)
    if image.shape[0] != height or image.shape[1] != width:
        raise ValueError(
            "Radial distortion frame size does not match image frame size."
        )
    if abs(float(model.k1)) <= 1e-15 and abs(float(model.k2)) <= 1e-15:
        return image.copy()
    map_x, map_y = _radial_distortion_maps(model)
    return cv2.remap(
        np.ascontiguousarray(image),
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _radial_distortion_maps(model: RadialDistortionModel) -> tuple[object, object]:
    import numpy as np

    width, height = _valid_frame_size(model.frame_size)
    cx = _finite_float(model.center_px[0], "center_px")
    cy = _finite_float(model.center_px[1], "center_px")
    k1 = _finite_float(model.k1, "k1")
    k2 = _finite_float(model.k2, "k2")
    x_values = np.arange(width, dtype=np.float32)
    y_values = np.arange(height, dtype=np.float32)
    grid_x, grid_y = np.meshgrid(x_values, y_values)
    radius_norm = max(float(width), float(height)) * 0.5
    dx = grid_x - float(cx)
    dy = grid_y - float(cy)
    norm_x = dx / radius_norm
    norm_y = dy / radius_norm
    r2 = norm_x * norm_x + norm_y * norm_y
    radial = 1.0 + float(k1) * r2 + float(k2) * r2 * r2
    map_x = (float(cx) + dx * radial).astype(np.float32, copy=False)
    map_y = (float(cy) + dy * radial).astype(np.float32, copy=False)
    return map_x, map_y


def _image_array(frame: object):
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
        return array[:, : width * 3].reshape((height, width, 3)).copy()
    array = np.asarray(frame)
    if array.ndim not in (2, 3):
        raise ValueError("Image frame must be a 2D or 3D array.")
    if array.dtype == np.uint8:
        return array.copy()
    return np.clip(array, 0, 255).astype(np.uint8)


def _apply_axis_interpolation_correction(
    array: object,
    correction: DistortionCorrection,
    *,
    width: int,
    height: int,
):
    import cv2

    if correction.axis_map_x is None or correction.axis_map_y is None:
        map_x, map_y = _axis_interpolation_maps(
            width=width,
            height=height,
            axis_source_x=correction.axis_source_x,
            axis_target_x=correction.axis_target_x,
            axis_source_y=correction.axis_source_y,
            axis_target_y=correction.axis_target_y,
        )
    else:
        map_x = correction.axis_map_x
        map_y = correction.axis_map_y
    return cv2.remap(
        array,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _axis_interpolation_maps(
    *,
    width: int,
    height: int,
    axis_source_x: Sequence[float],
    axis_target_x: Sequence[float],
    axis_source_y: Sequence[float],
    axis_target_y: Sequence[float],
) -> tuple[object, object]:
    import numpy as np

    target_x = np.arange(width, dtype=np.float32)
    target_y = np.arange(height, dtype=np.float32)
    source_x = _interp_with_extrapolation(
        target_x,
        axis_target_x,
        axis_source_x,
    ).astype(np.float32, copy=False)
    source_y = _interp_with_extrapolation(
        target_y,
        axis_target_y,
        axis_source_y,
    ).astype(np.float32, copy=False)
    map_x = np.tile(source_x.reshape(1, width), (height, 1))
    map_y = np.tile(source_y.reshape(height, 1), (1, width))
    return map_x, map_y


def _qimage_rgb32_array(image: QImage):
    import numpy as np

    width = image.width()
    height = image.height()
    ptr = image.constBits()
    array = np.frombuffer(ptr, np.uint8, count=image.sizeInBytes()).reshape(
        (height, image.bytesPerLine())
    )
    return array[:, : width * 4].reshape((height, width, 4))


def _qimage_rgb888_array(image: QImage):
    import numpy as np

    width = image.width()
    height = image.height()
    ptr = image.constBits()
    array = np.frombuffer(ptr, np.uint8, count=image.sizeInBytes()).reshape(
        (height, image.bytesPerLine())
    )
    return array[:, : width * 3].reshape((height, width, 3))


def _qimage_array_for_correction(frame: QImage):
    if frame.format() == QImage.Format_RGB888:
        return frame, _qimage_rgb888_array(frame), QImage.Format_RGB888
    image = frame.convertToFormat(QImage.Format_RGB32)
    return image, _qimage_rgb32_array(image), QImage.Format_RGB32


def _optional_axis_pair(
    payload: dict[str, object],
    source_key: str,
    target_key: str,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    has_source = source_key in payload
    has_target = target_key in payload
    if not has_source and not has_target:
        return (), ()
    if not has_source or not has_target:
        raise ValueError("Distortion correction axis mapping is incomplete.")
    source = _valid_axis_points(payload.get(source_key), source_key)
    target = _valid_axis_points(payload.get(target_key), target_key)
    if len(source) != len(target):
        raise ValueError("Distortion correction axis mapping lengths do not match.")
    if len(source) < 2:
        raise ValueError("Distortion correction axis mapping needs two points.")
    return source, target


def _valid_axis_points(raw_points: object, label: str) -> tuple[float, ...]:
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
        raise ValueError(f"{label} must be a numeric sequence.")
    points = tuple(_finite_float(value, label) for value in raw_points)
    if len(points) < 2:
        raise ValueError(f"{label} needs at least two values.")
    if any(next_value <= value for value, next_value in zip(points, points[1:])):
        raise ValueError(f"{label} must be strictly increasing.")
    return points


def _interp_with_extrapolation(
    values: object,
    source_points: Sequence[float],
    target_points: Sequence[float],
):
    import numpy as np

    x_values = np.asarray(values, dtype=np.float64)
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    mapped = np.interp(x_values, source, target)
    left = x_values < source[0]
    if np.any(left):
        mapped[left] = target[0] + (x_values[left] - source[0]) * (
            (target[1] - target[0]) / (source[1] - source[0])
        )
    right = x_values > source[-1]
    if np.any(right):
        mapped[right] = target[-1] + (x_values[right] - source[-1]) * (
            (target[-1] - target[-2]) / (source[-1] - source[-2])
        )
    return mapped


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
    "MODEL_VERSION",
    "RadialDistortionModel",
    "StageFeatureObservation",
    "StageGeometryCorrection",
    "apply_distortion_correction",
    "apply_radial_distortion_correction",
    "correction_from_payload",
    "distortion_payload_from_points",
]
