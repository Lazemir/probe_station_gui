"""Illumination-invariant metal geometry masks for lens calibration."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PySide6.QtGui import QImage


MIN_GEOMETRY_FEATURES = 8
_MIN_COMPONENT_AREA = 16
_MIN_BACKGROUND_SIGMA_PX = 5.0
_MAX_BACKGROUND_SIGMA_PX = 41.0
_MIN_CLEANUP_KERNEL_PX = 3
_MAX_CLEANUP_KERNEL_PX = 5
_FEATURE_PATCH_RADIUS_PX = 4
_COMPONENT_PADDING_PX = 1
_LINE_CENTER_SAMPLE_SPACING_PX = 8
_CORNER_MAX_COLLINEAR_COSINE = float(np.cos(np.deg2rad(45.0)))


@dataclass(frozen=True)
class GeometryMaskFeature:
    """A corner, junction, or endpoint measured directly from a mask component."""

    point_px: tuple[float, float]
    descriptor: tuple[float, ...]
    response: float
    component_id: int
    feature_type: str = "endpoint"
    orientation_rad: float | None = None
    branch_orientations_rad: tuple[float, ...] = ()


@dataclass(frozen=True)
class GeometryMaskComponent:
    """Centerline geometry measured from one connected mask component."""

    component_id: int
    bounding_box_px: tuple[int, int, int, int]
    area_px: int
    position_px: tuple[float, float]
    orientation_rad: float | None
    length_px: float
    line_center_samples_px: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class GeometryMaskFrame:
    """Binary metal support and its directly observed geometric features."""

    mask: np.ndarray
    features: tuple[GeometryMaskFeature, ...]
    frame_size: tuple[int, int]
    components: tuple[GeometryMaskComponent, ...] = ()


def segment_metal_geometry(frame: object) -> GeometryMaskFrame:
    """Segment visible metal geometry from an RGB ndarray or ``QImage`` frame."""
    rgb = _rgb_array(frame)
    mask = _adaptive_metal_mask(rgb)
    components, features = _actual_mask_geometry(mask)
    if len(features) < MIN_GEOMETRY_FEATURES:
        raise ValueError("Insufficient metal geometry was detected.")
    height, width = rgb.shape[:2]
    return GeometryMaskFrame(
        mask=mask,
        features=features,
        frame_size=(width, height),
        components=components,
    )


def _rgb_array(frame: object) -> np.ndarray:
    if isinstance(frame, QImage):
        if frame.isNull():
            raise ValueError("Image frame must not be null.")
        image = frame.convertToFormat(QImage.Format_RGB888)
        width = image.width()
        height = image.height()
        rows = np.frombuffer(
            image.constBits(),
            dtype=np.uint8,
            count=image.sizeInBytes(),
        ).reshape((height, image.bytesPerLine()))
        return rows[:, : width * 3].reshape((height, width, 3)).copy()

    array = np.asarray(frame)
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError("Image frame must be a 2D array or an RGB array.")
    if array.shape[0] == 0 or array.shape[1] == 0:
        raise ValueError("Image frame must not be empty.")
    rgb = array[:, :, :3]
    if rgb.dtype != np.uint8:
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb)


def _adaptive_metal_mask(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    if min(height, width) < 12:
        return np.zeros((height, width), dtype=bool)

    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    sigma = float(
        np.clip(
            min(height, width) / 18.0,
            _MIN_BACKGROUND_SIGMA_PX,
            _MAX_BACKGROUND_SIGMA_PX,
        )
    )
    background = cv2.GaussianBlur(
        lab,
        (0, 0),
        sigmaX=sigma,
        sigmaY=sigma,
        borderType=cv2.BORDER_REPLICATE,
    )
    delta = lab - background
    lightness = np.maximum(delta[:, :, 0], 0.0)
    chroma = np.hypot(delta[:, :, 1], delta[:, :, 2])
    score = lightness + 0.30 * chroma
    mask = _otsu_mask(score)

    kernel_side = _cleanup_kernel_side(min(height, width), mask)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_side, kernel_side))
    cleaned = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel)

    border = max(kernel_side, min(height, width) // 200)
    cleaned[:border, :] = 0
    cleaned[height - border :, :] = 0
    cleaned[:, :border] = 0
    cleaned[:, width - border :] = 0
    return _retain_geometric_components(cleaned.astype(bool))


def _otsu_mask(score: np.ndarray) -> np.ndarray:
    maximum = float(score.max(initial=0.0))
    if maximum <= 0.0 or not np.isfinite(maximum):
        return np.zeros(score.shape, dtype=bool)
    scaled = np.clip(score * (255.0 / maximum), 0, 255).astype(np.uint8)
    _threshold, binary = cv2.threshold(
        scaled,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    return binary.astype(bool)


def _cleanup_kernel_side(short_side: int, preliminary_mask: np.ndarray) -> int:
    support = _detected_line_support_px(preliminary_mask)
    side = int(round(max(short_side / 220.0, support / 2.0)))
    side = max(_MIN_CLEANUP_KERNEL_PX, min(_MAX_CLEANUP_KERNEL_PX, side))
    return side if side % 2 else side + 1


def _detected_line_support_px(mask: np.ndarray) -> float:
    distance = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    values = distance[distance > 0]
    if values.size == 0:
        return 1.0
    return max(1.0, 2.0 * float(np.percentile(values, 90.0)) - 1.0)


def _retain_geometric_components(mask: np.ndarray) -> np.ndarray:
    height, width = mask.shape
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8),
        connectivity=8,
    )
    minimum_area = max(_MIN_COMPONENT_AREA, int(round(height * width * 0.0001)))
    retained = np.zeros(mask.shape, dtype=bool)
    for component_id in range(1, count):
        area = int(stats[component_id, cv2.CC_STAT_AREA])
        if area >= minimum_area:
            retained[labels == component_id] = True
    return retained


def _actual_mask_geometry(
    mask: np.ndarray,
) -> tuple[tuple[GeometryMaskComponent, ...], tuple[GeometryMaskFeature, ...]]:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8),
        connectivity=8,
    )
    components: list[GeometryMaskComponent] = []
    features: list[GeometryMaskFeature] = []
    for component_id in range(1, count):
        component, offset, bounding_box, area, position = _component_region(
            labels,
            component_id,
            stats[component_id],
            centroids[component_id],
        )
        skeleton = _morphological_skeleton(component)
        geometry, component_features = _component_geometry_and_features(
            skeleton,
            component_id,
            offset=offset,
            bounding_box=bounding_box,
            area=area,
            position=position,
        )
        components.append(geometry)
        features.extend(component_features)
    return tuple(components), tuple(features)


def _component_region(
    labels: np.ndarray,
    component_id: int,
    statistics: np.ndarray,
    centroid: np.ndarray,
) -> tuple[np.ndarray, tuple[int, int], tuple[int, int, int, int], int, tuple[float, float]]:
    x, y, width, height, area = (int(value) for value in statistics)
    x0 = max(0, x - _COMPONENT_PADDING_PX)
    y0 = max(0, y - _COMPONENT_PADDING_PX)
    x1 = min(labels.shape[1], x + width + _COMPONENT_PADDING_PX)
    y1 = min(labels.shape[0], y + height + _COMPONENT_PADDING_PX)
    component = labels[y0:y1, x0:x1] == component_id
    return component, (x0, y0), (x, y, width, height), area, tuple(map(float, centroid))


def _component_geometry_and_features(
    skeleton: np.ndarray,
    component_id: int,
    *,
    offset: tuple[int, int],
    bounding_box: tuple[int, int, int, int],
    area: int,
    position: tuple[float, float],
) -> tuple[GeometryMaskComponent, tuple[GeometryMaskFeature, ...]]:
    if not skeleton.any():
        return (
            GeometryMaskComponent(
                component_id=component_id,
                bounding_box_px=bounding_box,
                area_px=area,
                position_px=position,
                orientation_rad=None,
                length_px=0.0,
                line_center_samples_px=(),
            ),
            (),
        )

    degrees = _topology_degrees(skeleton)
    components = GeometryMaskComponent(
        component_id=component_id,
        bounding_box_px=bounding_box,
        area_px=area,
        position_px=position,
        orientation_rad=_centerline_orientation(skeleton),
        length_px=_centerline_length_px(skeleton),
        line_center_samples_px=_line_center_samples(skeleton, offset),
    )
    feature_sets = (
        ("endpoint", skeleton & (degrees == 1)),
        ("corner", _corner_candidates(skeleton, degrees)),
        ("junction", skeleton & (degrees >= 3)),
    )
    features: list[GeometryMaskFeature] = []
    for feature_type, candidates in feature_sets:
        features.extend(
            _features_from_candidates(
                skeleton,
                degrees,
                candidates,
                component_id,
                feature_type=feature_type,
                offset=offset,
            )
        )
    return components, tuple(features)


def _features_from_candidates(
    skeleton: np.ndarray,
    degrees: np.ndarray,
    candidates: np.ndarray,
    component_id: int,
    *,
    feature_type: str,
    offset: tuple[int, int],
) -> tuple[GeometryMaskFeature, ...]:
    if not candidates.any():
        return ()
    linked = cv2.dilate(candidates.astype(np.uint8), np.ones((3, 3), dtype=np.uint8))
    feature_count, feature_labels, _stats, _centroids = cv2.connectedComponentsWithStats(
        linked,
        connectivity=8,
    )
    features: list[GeometryMaskFeature] = []
    for feature_id in range(1, feature_count):
        ys, xs = np.where(candidates & (feature_labels == feature_id))
        if xs.size == 0:
            continue
        center_x = float(xs.mean())
        center_y = float(ys.mean())
        nearest = int(np.argmin((xs - center_x) ** 2 + (ys - center_y) ** 2))
        x = int(xs[nearest])
        y = int(ys[nearest])
        descriptor = _binary_patch_descriptor(skeleton, x, y)
        response = float(degrees[ys, xs].max(initial=0))
        branch_orientations = _branch_orientations(skeleton, x, y)
        features.append(
            GeometryMaskFeature(
                point_px=(float(x + offset[0]), float(y + offset[1])),
                descriptor=descriptor,
                response=response,
                component_id=component_id,
                feature_type=feature_type,
                orientation_rad=_mean_undirected_orientation(branch_orientations),
                branch_orientations_rad=branch_orientations,
            )
        )
    return tuple(features)


def _topology_degrees(skeleton: np.ndarray) -> np.ndarray:
    degrees = np.zeros(skeleton.shape, dtype=np.int16)
    for y, x in zip(*np.where(skeleton)):
        degrees[y, x] = len(_skeleton_neighbour_offsets(skeleton, int(x), int(y)))
    return degrees


def _skeleton_neighbour_offsets(skeleton: np.ndarray, x: int, y: int) -> tuple[tuple[int, int], ...]:
    offsets: list[tuple[int, int]] = []
    height, width = skeleton.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neighbour_x = x + dx
            neighbour_y = y + dy
            if not (0 <= neighbour_x < width and 0 <= neighbour_y < height):
                continue
            if not skeleton[neighbour_y, neighbour_x]:
                continue
            if dx and dy and (skeleton[y, neighbour_x] or skeleton[neighbour_y, x]):
                continue
            offsets.append((dx, dy))
    return tuple(offsets)


def _corner_candidates(skeleton: np.ndarray, degrees: np.ndarray) -> np.ndarray:
    corners = np.zeros(skeleton.shape, dtype=bool)
    for y, x in zip(*np.where(skeleton & (degrees == 2))):
        first, second = _skeleton_neighbour_offsets(skeleton, int(x), int(y))
        dot = first[0] * second[0] + first[1] * second[1]
        norm = float(np.hypot(*first) * np.hypot(*second))
        if norm and abs(dot / norm) <= _CORNER_MAX_COLLINEAR_COSINE:
            corners[y, x] = True
    return corners


def _branch_orientations(skeleton: np.ndarray, x: int, y: int) -> tuple[float, ...]:
    orientations = [
        float(np.arctan2(dy, dx) % np.pi)
        for dx, dy in _skeleton_neighbour_offsets(skeleton, x, y)
    ]
    return tuple(sorted(orientations))


def _mean_undirected_orientation(orientations: tuple[float, ...]) -> float | None:
    if not orientations:
        return None
    doubled = np.exp(2j * np.asarray(orientations, dtype=float))
    return float(np.angle(doubled.mean()) % np.pi)


def _centerline_orientation(skeleton: np.ndarray) -> float | None:
    ys, xs = np.where(skeleton)
    if xs.size < 2:
        return None
    points = np.column_stack((xs, ys)).astype(float)
    _values, vectors = np.linalg.eigh(np.cov(points, rowvar=False))
    direction = vectors[:, -1]
    return float(np.arctan2(direction[1], direction[0]) % np.pi)


def _centerline_length_px(skeleton: np.ndarray) -> float:
    length = 0.0
    for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
        y_start = max(0, -dy)
        y_stop = min(skeleton.shape[0], skeleton.shape[0] - dy)
        x_start = max(0, -dx)
        x_stop = min(skeleton.shape[1], skeleton.shape[1] - dx)
        source = skeleton[y_start:y_stop, x_start:x_stop]
        target = skeleton[y_start + dy : y_stop + dy, x_start + dx : x_stop + dx]
        length += float(np.count_nonzero(source & target)) * float(np.hypot(dx, dy))
    return length


def _line_center_samples(
    skeleton: np.ndarray,
    offset: tuple[int, int],
) -> tuple[tuple[float, float], ...]:
    ys, xs = np.where(skeleton)
    return tuple(
        (float(x + offset[0]), float(y + offset[1]))
        for x, y in zip(xs[::_LINE_CENTER_SAMPLE_SPACING_PX], ys[::_LINE_CENTER_SAMPLE_SPACING_PX])
    )


def _morphological_skeleton(component: np.ndarray) -> np.ndarray:
    """Thin a component while preserving connected line topology."""
    working = np.pad(component.astype(np.uint8), 1, mode="constant")
    changed = True
    while changed:
        changed = False
        for first_step in (True, False):
            delete = _zhang_suen_delete_mask(working, first_step=first_step)
            if delete.any():
                working[1:-1, 1:-1][delete] = 0
                changed = True
    return working[1:-1, 1:-1].astype(bool)


def _zhang_suen_delete_mask(working: np.ndarray, *, first_step: bool) -> np.ndarray:
    north = working[:-2, 1:-1]
    north_east = working[:-2, 2:]
    east = working[1:-1, 2:]
    south_east = working[2:, 2:]
    south = working[2:, 1:-1]
    south_west = working[2:, :-2]
    west = working[1:-1, :-2]
    north_west = working[:-2, :-2]
    neighbours = (
        north,
        north_east,
        east,
        south_east,
        south,
        south_west,
        west,
        north_west,
    )
    neighbour_count = sum(neighbours)
    transitions = sum(
        (current == 0) & (following == 1)
        for current, following in zip(neighbours, neighbours[1:] + neighbours[:1])
    )
    candidate = (
        (working[1:-1, 1:-1] == 1)
        & (neighbour_count >= 2)
        & (neighbour_count <= 6)
        & (transitions == 1)
    )
    if first_step:
        return candidate & ((north * east * south) == 0) & ((east * south * west) == 0)
    return candidate & ((north * east * west) == 0) & ((north * south * west) == 0)


def _binary_patch_descriptor(skeleton: np.ndarray, x: int, y: int) -> tuple[float, ...]:
    radius = _FEATURE_PATCH_RADIUS_PX
    padded = np.pad(skeleton, radius, mode="constant")
    patch = padded[y : y + 2 * radius + 1, x : x + 2 * radius + 1]
    return tuple(float(value) for value in patch.ravel())
