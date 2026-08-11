"""Flat-field profile compilation and QImage correction."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtGui import QImage


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
class CompiledFlatFieldCorrection:
    """Precomputed per-pixel RGB gain for repeated flat-field correction."""

    image_size_px: tuple[int, int]
    source: str
    gain_rgb: Any = field(repr=False, compare=False)


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
    rgb = qimage_to_rgb_array(reference_frame)
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

    return build_flat_field_profile(
        median_flat_field_reference(frames),
        blur_radius_px=blur_radius_px,
        max_gain=max_gain,
        source=source or "scan_median",
    )


def median_flat_field_reference(frames: Sequence[QImage]) -> QImage:
    """Return the per-pixel RGB median of equally sized flat-field frames."""

    if not frames:
        raise ValueError("Cannot build a median flat-field reference without frames.")
    first = frames[0]
    if first.isNull():
        raise ValueError(
            "Cannot build a median flat-field reference from an empty frame."
        )
    frame_size = (int(first.width()), int(first.height()))
    return rgb_array_to_qimage(_median_rgb_array(frames, frame_size))


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
    rgb = qimage_to_rgb_array(frame)
    corrected = _apply_flat_field_array(rgb, profile)
    return rgb_array_to_qimage(corrected)


def compile_flat_field_correction(
    profile: FlatFieldProfile,
) -> CompiledFlatFieldCorrection:
    """Precompute the gain map used to correct every frame of a live stream."""

    import numpy as np

    illumination = profile.illumination_rgb.astype(np.float32, copy=False)
    expected_shape = (
        int(profile.image_size_px[1]),
        int(profile.image_size_px[0]),
        3,
    )
    if tuple(illumination.shape) != expected_shape:
        raise ValueError("Flat-field illumination data does not match profile size.")
    mean = np.asarray(profile.mean_rgb, dtype=np.float32).reshape((1, 1, 3))
    denominator_floor = np.maximum(mean / float(profile.max_gain), 1.0)
    denominator = np.maximum(illumination, denominator_floor)
    gain = np.ascontiguousarray(mean / denominator, dtype=np.float32)
    return CompiledFlatFieldCorrection(
        image_size_px=tuple(profile.image_size_px),
        source=str(profile.source),
        gain_rgb=gain,
    )


def apply_compiled_flat_field_correction(
    frame: QImage,
    correction: CompiledFlatFieldCorrection,
) -> QImage:
    """Apply a cached flat-field gain map to one frame."""

    import numpy as np

    if frame.isNull():
        raise ValueError("Cannot flat-field an empty frame.")
    frame_size = (int(frame.width()), int(frame.height()))
    if frame_size != tuple(correction.image_size_px):
        raise ValueError(
            "Flat-field correction frame size "
            f"{correction.image_size_px[0]}x{correction.image_size_px[1]} "
            f"does not match frame size {frame_size[0]}x{frame_size[1]}."
        )
    rgb = qimage_to_rgb_array(frame).astype(np.float32, copy=False)
    corrected = rgb * correction.gain_rgb
    return rgb_array_to_qimage(np.clip(corrected, 0.0, 255.0).astype(np.uint8))


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


def qimage_to_rgb_array(image: QImage) -> Any:
    """Copy a QImage into a packed RGB array while honoring row stride."""

    import numpy as np

    rgb_image = image.convertToFormat(QImage.Format_RGB888)
    width = int(rgb_image.width())
    height = int(rgb_image.height())
    bytes_per_line = int(rgb_image.bytesPerLine())
    bits = rgb_image.bits()
    array = np.frombuffer(bits, dtype=np.uint8, count=height * bytes_per_line)
    rows = array.reshape((height, bytes_per_line))
    return rows[:, : width * 3].reshape((height, width, 3)).copy()


def rgb_array_to_qimage(array: Any) -> QImage:
    """Return a detached RGB32 QImage from an RGB array."""

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
        arrays.append(qimage_to_rgb_array(frame))
    return np.median(np.stack(arrays, axis=0), axis=0).astype(np.uint8)


def _apply_flat_field_array(rgb: Any, profile: FlatFieldProfile) -> Any:
    import numpy as np

    rgb_float = rgb.astype(np.float32, copy=False)
    illumination = profile.illumination_rgb.astype(np.float32, copy=False)
    mean = np.asarray(profile.mean_rgb, dtype=np.float32).reshape((1, 1, 3))
    denominator_floor = np.maximum(mean / float(profile.max_gain), 1.0)
    denominator = np.maximum(illumination, denominator_floor)
    corrected = rgb_float * (mean / denominator)
    return np.clip(corrected, 0.0, 255.0).astype(np.uint8)


__all__ = [
    "CompiledFlatFieldCorrection",
    "FlatFieldProfile",
    "apply_compiled_flat_field_correction",
    "apply_flat_field_correction",
    "apply_self_flat_field_correction",
    "build_flat_field_profile",
    "build_median_flat_field_profile",
    "compile_flat_field_correction",
    "median_flat_field_reference",
    "qimage_to_rgb_array",
    "rgb_array_to_qimage",
]
