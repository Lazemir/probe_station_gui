from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtGui import QImage

import probe_station_gui.camera.geometry_segmentation as geometry_segmentation
from probe_station_gui.camera.geometry_segmentation import segment_metal_geometry


def _synthetic_metal_pattern() -> tuple[np.ndarray, np.ndarray]:
    height, width = 260, 360
    y, x = np.mgrid[:height, :width]
    background = np.empty((height, width, 3), dtype=np.float32)
    background[:, :, 0] = 32.0 + 24.0 * x / width + 8.0 * y / height
    background[:, :, 1] = 46.0 + 10.0 * x / width + 18.0 * y / height
    background[:, :, 2] = 62.0 + 17.0 * x / width + 7.0 * y / height
    expected = np.zeros((height, width), dtype=np.uint8)

    for coordinate in (130, 155, 180, 205):
        cv2.line(expected, (coordinate, 90), (coordinate, 170), 1, 5)
    for coordinate in (90, 115, 140, 165):
        cv2.line(expected, (130, coordinate), (205, coordinate), 1, 5)
    for spine_x, tooth_end_x in ((44, 87), (316, 273)):
        cv2.line(expected, (spine_x, 52), (spine_x, 208), 1, 5)
        for tooth_y in (70, 95, 120, 145, 170, 195):
            cv2.line(expected, (spine_x, tooth_y), (tooth_end_x, tooth_y), 1, 5)

    image = background
    image[expected.astype(bool)] = (208.0, 176.0, 112.0)
    return np.clip(image, 0, 255).astype(np.uint8), expected.astype(bool)


def _illumination_variants(reference: np.ndarray) -> list[np.ndarray]:
    height, width = reference.shape[:2]
    y, x = np.mgrid[:height, :width]
    gradient = 0.58 + 0.78 * x / max(width - 1, 1) + 0.12 * y / max(height - 1, 1)
    channel_scale = np.array((1.22, 0.79, 1.12), dtype=np.float32)
    variants = [
        reference,
        np.clip(reference.astype(np.float32) * gradient[:, :, None], 0, 255),
        np.clip(reference.astype(np.float32) * channel_scale, 0, 255),
        np.clip(reference.astype(np.float32) * gradient[:, :, None] * channel_scale, 0, 255),
        np.clip(reference.astype(np.float32) * 0.67, 0, 255),
        np.clip(reference.astype(np.float32) * 1.16, 0, 255),
    ]
    return [variant.astype(np.uint8) for variant in variants]


def _mask_iou(first: np.ndarray, second: np.ndarray) -> float:
    union = np.count_nonzero(first | second)
    return float(np.count_nonzero(first & second)) / float(union)


def _as_qimage(rgb: np.ndarray) -> QImage:
    contiguous = np.ascontiguousarray(rgb)
    height, width, _channels = contiguous.shape
    return QImage(
        contiguous.data,
        width,
        height,
        width * 3,
        QImage.Format_RGB888,
    ).copy()


def _as_padded_qimage(rgb: np.ndarray) -> QImage:
    height, width, _channels = rgb.shape
    stride = ((width * 3 + 3) // 4) * 4
    assert stride > width * 3
    rows = np.full((height, stride), 17, dtype=np.uint8)
    rows[:, : width * 3] = np.ascontiguousarray(rgb).reshape(height, width * 3)
    return QImage(rows.data, width, height, stride, QImage.Format_RGB888).copy()


def _synthetic_topology_pattern() -> tuple[np.ndarray, dict[str, tuple[float, float]]]:
    height, width = 180, 280
    image = np.full((height, width, 3), (38, 52, 64), dtype=np.uint8)
    metal = np.zeros((height, width), dtype=np.uint8)
    cv2.line(metal, (42, 34), (42, 124), 1, 5)
    cv2.line(metal, (42, 124), (118, 124), 1, 5)
    cv2.line(metal, (166, 34), (166, 124), 1, 5)
    cv2.line(metal, (134, 78), (198, 78), 1, 5)
    cv2.line(metal, (238, 34), (238, 124), 1, 5)
    image[metal.astype(bool)] = (208, 176, 112)
    return image, {
        "corner": (42.0, 124.0),
        "junction": (166.0, 78.0),
        "endpoint": (42.0, 34.0),
    }


def test_segmentation_is_stable_under_illumination_and_color_changes() -> None:
    reference, expected = _synthetic_metal_pattern()

    masks = [segment_metal_geometry(frame).mask for frame in _illumination_variants(reference)]

    assert all(_mask_iou(mask, expected) > 0.90 for mask in masks)
    assert all(mask[expected].mean() > 0.95 for mask in masks)
    assert all(mask[~expected].mean() < 0.02 for mask in masks)
    assert all(np.count_nonzero(mask[:, :100]) > 300 for mask in masks)
    assert all(np.count_nonzero(mask[:, 260:]) > 300 for mask in masks)


def test_segmentation_accepts_qimage_input_without_changing_the_mask() -> None:
    reference, _expected = _synthetic_metal_pattern()
    source_pixels = reference.copy()

    array_result = segment_metal_geometry(reference)
    image_result = segment_metal_geometry(_as_qimage(reference))

    assert np.array_equal(reference, source_pixels)
    assert image_result.frame_size == (reference.shape[1], reference.shape[0])
    assert _mask_iou(array_result.mask, image_result.mask) > 0.99


def test_segmentation_reads_only_rgb_pixels_from_padded_qimage_rows() -> None:
    reference, _expected = _synthetic_metal_pattern()
    padded_reference = reference[:, :355]

    array_result = segment_metal_geometry(padded_reference)
    image_result = segment_metal_geometry(_as_padded_qimage(padded_reference))

    assert image_result.frame_size == (355, reference.shape[0])
    assert _mask_iou(array_result.mask, image_result.mask) > 0.99


def test_segmentation_rejects_tiny_and_border_artifacts() -> None:
    reference, expected = _synthetic_metal_pattern()
    artifacted = reference.copy()
    artifacted[:2, 250:310] = (208, 176, 112)
    artifacted[12:14, 12:14] = (208, 176, 112)

    result = segment_metal_geometry(artifacted)

    assert not result.mask[:3].any()
    assert not result.mask[10:16, 10:16].any()
    assert _mask_iou(result.mask, expected) > 0.90


def test_features_are_real_mask_topology_without_cartesian_crossings() -> None:
    reference, _expected = _synthetic_metal_pattern()

    result = segment_metal_geometry(reference)
    feature_points = np.asarray([feature.point_px for feature in result.features])

    assert len(result.features) >= 30
    assert any(np.linalg.norm(point - (87.0, 70.0)) <= 5.0 for point in feature_points)
    assert any(np.linalg.norm(point - (273.0, 70.0)) <= 5.0 for point in feature_points)
    assert any(125.0 <= point[0] <= 210.0 and 85.0 <= point[1] <= 170.0 for point in feature_points)
    assert all(result.mask[round(point[1]), round(point[0])] for point in feature_points)
    projected_only_crossing = np.array((130.0, 70.0))
    assert not result.mask[70, 130]
    assert not any(
        np.linalg.norm(point - projected_only_crossing) <= 5.0
        for point in feature_points
    )


def test_each_feature_is_anchored_to_pattern_topology() -> None:
    reference, _expected = _synthetic_metal_pattern()
    result = segment_metal_geometry(reference)
    topology_points = [
        *((float(x), float(y)) for x in (130, 155, 180, 205) for y in (90, 115, 140, 165)),
        (44.0, 52.0),
        (44.0, 208.0),
        (316.0, 52.0),
        (316.0, 208.0),
        *((float(x), float(y)) for x in (44, 87, 273, 316) for y in (70, 95, 120, 145, 170, 195)),
    ]

    assert all(
        min(np.linalg.norm(np.asarray(feature.point_px) - point) for point in topology_points) <= 5.0
        for feature in result.features
    )


def test_features_record_component_geometry_and_local_orientation() -> None:
    image, points = _synthetic_topology_pattern()

    result = segment_metal_geometry(image)
    corner = next(
        feature
        for feature in result.features
        if feature.feature_type == "corner"
        and np.linalg.norm(np.asarray(feature.point_px) - points["corner"]) <= 5.0
    )

    assert any(
        feature.feature_type == "junction"
        and np.linalg.norm(np.asarray(feature.point_px) - points["junction"]) <= 5.0
        for feature in result.features
    )
    assert any(
        feature.feature_type == "endpoint"
        and np.linalg.norm(np.asarray(feature.point_px) - points["endpoint"]) <= 5.0
        for feature in result.features
    )
    assert len(corner.branch_orientations_rad) == 2
    assert corner.orientation_rad is not None

    component = next(item for item in result.components if item.component_id == corner.component_id)
    assert component.bounding_box_px[2] > 0
    assert component.area_px > 0
    assert component.length_px > 0.0
    assert component.orientation_rad is not None
    assert component.line_center_samples_px
    assert all(
        result.mask[round(point[1]), round(point[0])]
        for point in component.line_center_samples_px
    )


def test_feature_extraction_skeletonizes_local_component_regions(monkeypatch) -> None:
    reference, _expected = _synthetic_metal_pattern()
    observed_shapes: list[tuple[int, int]] = []
    original = geometry_segmentation._morphological_skeleton

    def record_shape(component: np.ndarray) -> np.ndarray:
        observed_shapes.append(component.shape)
        return original(component)

    monkeypatch.setattr(
        geometry_segmentation,
        "_morphological_skeleton",
        record_shape,
    )

    segment_metal_geometry(reference)

    assert observed_shapes
    assert all(shape != reference.shape[:2] for shape in observed_shapes)
    assert (
        max(height * width for height, width in observed_shapes)
        < reference.shape[0] * reference.shape[1] // 2
    )
