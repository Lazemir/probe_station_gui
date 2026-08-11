"""Bright-grid morphology, line detection, and regular subset selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from PySide6.QtGui import QImage


Point2D = tuple[float, float]


@dataclass(frozen=True)
class GridDetection:
    """Detected bright grid line centers and their intersections in one frame."""

    vertical_lines_px: tuple[float, ...]
    horizontal_lines_px: tuple[float, ...]
    intersections_px: tuple[Point2D, ...]


@dataclass(frozen=True)
class BrightFeatureBounds:
    """Bounding box of the central bright calibration structure in one frame."""

    left: float
    top: float
    right: float
    bottom: float


def detect_bright_grid(
    frame: object,
    *,
    expected_spacing_px: Point2D | None = None,
) -> GridDetection:
    """Detect visible bright grid lines in a microscope calibration frame."""

    import numpy as np

    gray = _gray_array(frame)
    if gray.size == 0:
        return GridDetection((), (), ())
    threshold = max(float(np.percentile(gray, 88.0)), float(gray.max()) * 0.55, 120.0)
    mask = gray >= threshold
    expected_x = _positive_optional_spacing(expected_spacing_px, 0)
    expected_y = _positive_optional_spacing(expected_spacing_px, 1)
    vertical_projection = _morphological_axis_projection(mask, axis="vertical")
    horizontal_projection = _morphological_axis_projection(mask, axis="horizontal")
    vertical = _projection_line_centers(
        vertical_projection,
        expected_spacing_px=expected_x,
    )
    horizontal = _projection_line_centers(
        horizontal_projection,
        expected_spacing_px=expected_y,
    )
    if len(vertical) < 2:
        vertical = _projection_line_centers(
            mask.mean(axis=0),
            expected_spacing_px=expected_x,
        )
    if len(horizontal) < 2:
        horizontal = _projection_line_centers(
            mask.mean(axis=1),
            expected_spacing_px=expected_y,
        )
    intersections = tuple((x_pos, y_pos) for y_pos in horizontal for x_pos in vertical)
    return GridDetection(
        vertical_lines_px=vertical,
        horizontal_lines_px=horizontal,
        intersections_px=intersections,
    )


def detect_bright_feature_bounds(frame: object) -> BrightFeatureBounds | None:
    """Detect the central bright calibration structure bounds in a frame."""

    import cv2
    import numpy as np

    gray = _gray_array(frame)
    if gray.size == 0:
        return None
    height, width = gray.shape[:2]
    if width < 16 or height < 16:
        return None
    kernel_size = _odd_kernel_size(
        max(31, min(width, height) // 24),
        limit=max(3, min(width, height) - 1),
    )
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (kernel_size, kernel_size),
    )
    top_hat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    top_hat = cv2.GaussianBlur(top_hat, (3, 3), 0.0)
    maximum = float(top_hat.max())
    if maximum <= 0.0 or not math.isfinite(maximum):
        return None
    threshold = max(
        12.0,
        float(np.percentile(top_hat, 99.0)) * 0.55,
        maximum * 0.18,
    )
    mask = top_hat >= threshold
    border_x = max(2, int(round(width * 0.01)))
    border_y = max(2, int(round(height * 0.01)))
    mask[:border_y, :] = False
    mask[height - border_y :, :] = False
    mask[:, :border_x] = False
    mask[:, width - border_x :] = False
    if int(np.count_nonzero(mask)) < max(16, int(width * height * 1e-5)):
        return None

    link_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (
            _odd_kernel_size(max(9, width // 160), limit=max(3, width - 1)),
            _odd_kernel_size(max(9, height // 160), limit=max(3, height - 1)),
        ),
    )
    linked = cv2.dilate(mask.astype(np.uint8), link_kernel, iterations=1)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        linked,
        connectivity=8,
    )
    if count <= 1:
        return None
    frame_center = np.array([width * 0.5, height * 0.5], dtype=float)
    frame_diag = math.hypot(width, height)
    best_label = 0
    best_score = -1.0
    for label in range(1, count):
        area = float(stats[label, cv2.CC_STAT_AREA])
        if area < max(16.0, float(width * height) * 2e-5):
            continue
        centroid = np.asarray(centroids[label], dtype=float)
        distance = float(np.linalg.norm(centroid - frame_center))
        distance_weight = 1.0 / (1.0 + 4.0 * distance / max(frame_diag, 1.0))
        score = area * distance_weight
        if score > best_score:
            best_score = score
            best_label = label
    if best_label <= 0:
        return None
    component = labels == best_label
    ys, xs = np.where(mask & component)
    if xs.size < 8 or ys.size < 8:
        ys, xs = np.where(component)
    if xs.size < 8 or ys.size < 8:
        return None
    return BrightFeatureBounds(
        left=float(xs.min()),
        top=float(ys.min()),
        right=float(xs.max() + 1),
        bottom=float(ys.max() + 1),
    )


def _odd_kernel_size(value: object, *, limit: int) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = 3
    size = max(3, size)
    if size % 2 == 0:
        size += 1
    max_size = max(3, int(limit))
    if max_size % 2 == 0:
        max_size -= 1
    return max(3, min(size, max_size))


def _morphological_axis_projection(mask: object, *, axis: str):
    import cv2
    import numpy as np

    values = np.asarray(mask, dtype=np.uint8) * 255
    if values.ndim != 2 or values.size == 0:
        return np.asarray([], dtype=float)
    height, width = values.shape
    if axis == "vertical":
        kernel_width = max(3, int(round(width / 240.0)))
        kernel_height = max(31, int(round(height / 8.0)))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (kernel_width, kernel_height),
        )
        opened = cv2.morphologyEx(values, cv2.MORPH_OPEN, kernel)
        return opened.mean(axis=0)
    kernel_width = max(31, int(round(width / 12.0)))
    kernel_height = max(3, int(round(height / 240.0)))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (kernel_width, kernel_height),
    )
    opened = cv2.morphologyEx(values, cv2.MORPH_OPEN, kernel)
    return opened.mean(axis=1)


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
        return cv2.cvtColor(
            array[:, :, :3].astype(np.uint8, copy=False), cv2.COLOR_RGB2GRAY
        )
    return np.asarray([], dtype=np.uint8)


def _projection_line_centers(
    projection: object,
    *,
    expected_spacing_px: float | None = None,
) -> tuple[float, ...]:
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
    if expected_spacing_px is not None and expected_spacing_px > 0.0:
        return _regular_grid_subset(
            tuple(center for center, _score in components),
            expected_spacing_px=expected_spacing_px,
        )
    components = _strongest_components(components, max_count=5)
    return _regular_grid_subset(tuple(center for center, _score in components))


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


def _regularized_positions(
    positions: Sequence[float],
    *,
    target_step_px: float | None = None,
) -> tuple[float, ...]:
    values = [float(value) for value in positions]
    if not values:
        return ()
    if target_step_px is not None and target_step_px > 0.0 and len(values) >= 2:
        center = sum(values) / float(len(values))
        mid = float(len(values) - 1) * 0.5
        return tuple(
            center + (float(index) - mid) * target_step_px
            for index in range(len(values))
        )
    if len(values) <= 2:
        return tuple(values)
    first = values[0]
    last = values[-1]
    step = (last - first) / float(len(values) - 1)
    return tuple(first + step * index for index in range(len(values)))


def _regular_grid_subset(
    positions: Sequence[float],
    *,
    expected_spacing_px: float | None = None,
) -> tuple[float, ...]:
    values = tuple(sorted(float(value) for value in positions))
    if len(values) <= 3:
        if expected_spacing_px is None or len(values) <= 1:
            return values
        if _regular_subset_spacing_is_expected(values, expected_spacing_px):
            return values
        return ()

    from itertools import combinations

    largest_count = min(
        len(values), 5 if expected_spacing_px is not None else len(values)
    )
    smallest_count = 2 if expected_spacing_px is not None else 3
    for count in range(largest_count, smallest_count - 1, -1):
        candidates: list[tuple[float, float, float, tuple[float, ...]]] = []
        for subset in combinations(values, count):
            step = (subset[-1] - subset[0]) / float(count - 1)
            if step <= 0.0:
                continue
            if (
                expected_spacing_px is not None
                and not _regular_subset_spacing_is_expected(
                    subset,
                    expected_spacing_px,
                )
            ):
                continue
            target = _regularized_positions(subset)
            errors = [abs(value - expected) for value, expected in zip(subset, target)]
            max_error = max(errors)
            tolerance = max(12.0, abs(step) * 0.12)
            if max_error <= tolerance:
                spacing_error = 0.0
                if expected_spacing_px is not None:
                    spacing_error = (
                        abs(step - expected_spacing_px) / expected_spacing_px
                    )
                candidates.append(
                    (
                        spacing_error,
                        max_error / abs(step),
                        -abs(subset[-1] - subset[0]),
                        subset,
                    )
                )
        if candidates:
            return min(candidates, key=lambda item: (item[0], item[1]))[3]
    if expected_spacing_px is not None:
        return ()
    return values


def _regular_subset_spacing_is_expected(
    positions: Sequence[float],
    expected_spacing_px: float,
) -> bool:
    if expected_spacing_px <= 0.0 or len(positions) < 2:
        return False
    step = (float(positions[-1]) - float(positions[0])) / float(len(positions) - 1)
    if step <= 0.0:
        return False
    return abs(step - expected_spacing_px) <= expected_spacing_px * 0.45


def _positive_optional_spacing(
    expected_spacing_px: Point2D | None,
    index: int,
) -> float | None:
    if expected_spacing_px is None:
        return None
    try:
        value = float(expected_spacing_px[index])
    except (IndexError, TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


__all__ = [
    "BrightFeatureBounds",
    "GridDetection",
    "detect_bright_feature_bounds",
    "detect_bright_grid",
]
