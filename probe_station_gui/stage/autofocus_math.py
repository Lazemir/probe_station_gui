"""Image and numeric helpers used by stage autofocus/calibration."""

from __future__ import annotations

import math


def frame_rate_from_timestamps(timestamps: list[float]) -> float | None:
    if len(timestamps) < 2:
        return None
    duration = float(timestamps[-1]) - float(timestamps[0])
    if duration <= 0.0:
        return None
    frame_rate = (len(timestamps) - 1) / duration
    if not math.isfinite(frame_rate) or frame_rate <= 0.0:
        return None
    return float(frame_rate)


def autofocus_sweep_feedrate_mm_min(
    fine_step_mm: float,
    frame_rate_hz: float | None,
    *,
    min_feedrate_mm_min: float,
    error_factory: type[Exception] = ValueError,
) -> float:
    if frame_rate_hz is None:
        raise error_factory(
            "Camera did not provide enough frames to estimate autofocus speed."
        )
    feedrate = abs(float(fine_step_mm)) * float(frame_rate_hz) * 60.0
    if not math.isfinite(feedrate) or feedrate <= 0.0:
        raise error_factory("Autofocus speed estimate is invalid.")
    feedrate = max(float(min_feedrate_mm_min), feedrate)
    return float(feedrate)


def static_focus_candidates(
    center_z: float,
    *,
    min_z: float,
    max_z: float,
    step_mm: float,
    max_points: int,
) -> list[float]:
    step = abs(float(step_mm))
    if step <= 0.0 or not math.isfinite(step):
        return []
    lower_limit = float(min_z)
    upper_limit = float(max_z)
    if upper_limit <= lower_limit:
        return []
    count = max(3, int(max_points))
    if count % 2 == 0:
        count += 1
    radius = count // 2
    center = max(lower_limit, min(upper_limit, float(center_z)))
    candidates: list[float] = []
    for offset in range(-radius, radius + 1):
        candidate = max(lower_limit, min(upper_limit, center + offset * step))
        if not candidates or abs(candidate - candidates[-1]) >= step * 0.25:
            candidates.append(candidate)
    if len(candidates) >= 3:
        return candidates

    lower = max(lower_limit, center - step)
    upper = min(upper_limit, center + step)
    fallback = sorted({lower, center, upper})
    if len(fallback) < 3 or fallback[-1] <= fallback[0]:
        return []
    return fallback


def parabolic_focus_peak(
    scored: list[tuple[float, float]],
    best_index: int,
) -> float | None:
    if best_index <= 0 or best_index >= len(scored) - 1:
        return None
    import numpy as np

    xs = np.asarray(
        [scored[best_index - 1][0], scored[best_index][0], scored[best_index + 1][0]],
        dtype=float,
    )
    ys = np.asarray(
        [scored[best_index - 1][1], scored[best_index][1], scored[best_index + 1][1]],
        dtype=float,
    )
    try:
        a, b, _c = np.polyfit(xs, ys, 2)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(a) or not math.isfinite(b) or a >= 0.0:
        return None
    peak = -b / (2.0 * a)
    if not math.isfinite(peak):
        return None
    return float(peak)


def estimate_shift(frame_a: object, frame_b: object) -> tuple[float, float]:
    shift_x, shift_y, _response = estimate_shift_with_response(frame_a, frame_b)
    return shift_x, shift_y


def estimate_shift_with_response(
    frame_a: object,
    frame_b: object,
) -> tuple[float, float, float]:
    import cv2
    import numpy as np

    a = frame_a.astype(np.float32)
    b = frame_b.astype(np.float32)
    window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (shift_x, shift_y), response = cv2.phaseCorrelate(a, b, window)
    return float(shift_x), float(-shift_y), float(response)


def focus_metric(frame: object) -> float:
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    crop_factor = 0.55
    crop_w = max(16, int(width * crop_factor))
    crop_h = max(16, int(height * crop_factor))
    left = max(0, (width - crop_w) // 2)
    top = max(0, (height - crop_h) // 2)
    roi = frame[top : top + crop_h, left : left + crop_w]
    if roi.size == 0:
        roi = frame
    filtered = cv2.medianBlur(roi, 3)
    normalized = filtered.astype(np.float32)
    mean = float(normalized.mean())
    if mean > 1e-6:
        normalized = normalized / mean
    grad_x = cv2.Sobel(normalized, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(normalized, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(grad_x * grad_x + grad_y * grad_y))


def qimage_to_gray(image: object, rgb888_format: object) -> object:
    import cv2
    import numpy as np

    converted = image.convertToFormat(rgb888_format)
    width = converted.width()
    height = converted.height()
    ptr = converted.constBits()
    array = np.frombuffer(
        ptr, np.uint8, count=converted.sizeInBytes()
    ).reshape((height, converted.bytesPerLine()))
    array = array[:, : width * 3].reshape((height, width, 3))
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    return gray
