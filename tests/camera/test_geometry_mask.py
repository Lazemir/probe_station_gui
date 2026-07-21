from __future__ import annotations

import cv2
import numpy as np
import pytest
from PySide6.QtGui import QImage

import probe_station_gui.camera.geometry_mask as geometry_mask
from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    fit_stage_geometry_from_observations,
)
from probe_station_gui.camera.geometry_mask import (
    GeometryMaskComponent,
    GeometryMaskFeature,
    GeometryMaskFrame,
    GeometryAlignmentPreviewError,
    build_geometry_alignment_previews,
    segment_metal_geometry,
)


def synthetic_metal_pattern() -> tuple[np.ndarray, np.ndarray]:
    """Return a grid with two disconnected edge combs and its exact support."""
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


def illumination_variants(reference: np.ndarray) -> list[np.ndarray]:
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


def mask_iou(first: np.ndarray, second: np.ndarray) -> float:
    union = np.count_nonzero(first | second)
    return float(np.count_nonzero(first & second)) / float(union)


def as_qimage(rgb: np.ndarray) -> QImage:
    contiguous = np.ascontiguousarray(rgb)
    height, width, _channels = contiguous.shape
    return QImage(
        contiguous.data,
        width,
        height,
        width * 3,
        QImage.Format_RGB888,
    ).copy()


def as_padded_qimage(rgb: np.ndarray) -> QImage:
    """Build RGB888 input with QImage's alignment padding retained."""
    height, width, _channels = rgb.shape
    stride = ((width * 3 + 3) // 4) * 4
    assert stride > width * 3
    rows = np.full((height, stride), 17, dtype=np.uint8)
    rows[:, : width * 3] = np.ascontiguousarray(rgb).reshape(height, width * 3)
    return QImage(rows.data, width, height, stride, QImage.Format_RGB888).copy()


def synthetic_topology_pattern() -> tuple[np.ndarray, dict[str, tuple[float, float]]]:
    """Return isolated L, T, and line components with known centerline points."""
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
    reference, expected = synthetic_metal_pattern()

    masks = [segment_metal_geometry(frame).mask for frame in illumination_variants(reference)]

    assert all(mask_iou(mask, expected) > 0.90 for mask in masks)
    assert all(mask[expected].mean() > 0.95 for mask in masks)
    assert all(mask[~expected].mean() < 0.02 for mask in masks)
    assert all(np.count_nonzero(mask[:, :100]) > 300 for mask in masks)
    assert all(np.count_nonzero(mask[:, 260:]) > 300 for mask in masks)


def test_segmentation_accepts_qimage_input_without_changing_the_mask() -> None:
    reference, _expected = synthetic_metal_pattern()

    array_result = segment_metal_geometry(reference)
    image_result = segment_metal_geometry(as_qimage(reference))

    assert image_result.frame_size == (reference.shape[1], reference.shape[0])
    assert mask_iou(array_result.mask, image_result.mask) > 0.99


def test_segmentation_reads_padded_rgb888_qimage_rows() -> None:
    reference, _expected = synthetic_metal_pattern()
    padded_reference = reference[:, :355]

    array_result = segment_metal_geometry(padded_reference)
    image_result = segment_metal_geometry(as_padded_qimage(padded_reference))

    assert image_result.frame_size == (355, reference.shape[0])
    assert mask_iou(array_result.mask, image_result.mask) > 0.99


def test_segmentation_rejects_tiny_and_border_artifacts() -> None:
    reference, expected = synthetic_metal_pattern()
    artifacted = reference.copy()
    artifacted[:2, 250:310] = (208, 176, 112)
    artifacted[12:14, 12:14] = (208, 176, 112)

    result = segment_metal_geometry(artifacted)

    assert not result.mask[:3].any()
    assert not result.mask[10:16, 10:16].any()
    assert mask_iou(result.mask, expected) > 0.90


def test_features_are_real_mask_corners_or_endpoints_without_cartesian_crossings() -> None:
    reference, _expected = synthetic_metal_pattern()

    result = segment_metal_geometry(reference)
    feature_points = np.asarray([feature.point_px for feature in result.features])

    assert len(result.features) >= 30
    assert any(np.linalg.norm(point - (87.0, 70.0)) <= 5.0 for point in feature_points)
    assert any(np.linalg.norm(point - (273.0, 70.0)) <= 5.0 for point in feature_points)
    assert any(125.0 <= point[0] <= 210.0 and 85.0 <= point[1] <= 170.0 for point in feature_points)
    assert all(
        result.mask[round(point[1]), round(point[0])]
        for point in feature_points
    )

    # The grid's vertical line projects over the comb's y=70 tooth, but the
    # shapes are separate. Cartesian line products would invent this feature.
    projected_only_crossing = np.array((130.0, 70.0))
    assert not result.mask[70, 130]
    assert not any(
        np.linalg.norm(point - projected_only_crossing) <= 5.0
        for point in feature_points
    )


def test_each_feature_is_anchored_to_pattern_topology() -> None:
    reference, _expected = synthetic_metal_pattern()

    result = segment_metal_geometry(reference)
    topology_points = [
        *( (float(x), float(y)) for x in (130, 155, 180, 205) for y in (90, 115, 140, 165) ),
        (44.0, 52.0),
        (44.0, 208.0),
        (316.0, 52.0),
        (316.0, 208.0),
        *( (float(x), float(y)) for x in (44, 87, 273, 316) for y in (70, 95, 120, 145, 170, 195) ),
    ]

    assert all(
        min(np.linalg.norm(np.asarray(feature.point_px) - point) for point in topology_points) <= 5.0
        for feature in result.features
    )


def test_features_record_component_geometry_and_local_orientation() -> None:
    image, points = synthetic_topology_pattern()

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


def test_feature_extraction_skeletonizes_padded_component_regions(monkeypatch) -> None:
    reference, _expected = synthetic_metal_pattern()
    observed_shapes: list[tuple[int, int]] = []
    original = geometry_mask._morphological_skeleton

    def record_shape(component: np.ndarray) -> np.ndarray:
        observed_shapes.append(component.shape)
        return original(component)

    monkeypatch.setattr(geometry_mask, "_morphological_skeleton", record_shape)

    segment_metal_geometry(reference)

    assert observed_shapes
    assert all(shape != reference.shape[:2] for shape in observed_shapes)
    assert max(height * width for height, width in observed_shapes) < reference.shape[0] * reference.shape[1] // 2


def _synthetic_geometry_mask_frame(
    features: list[tuple[str, tuple[float, float], str, float]],
    *,
    frame_size: tuple[int, int],
) -> GeometryMaskFrame:
    """Build a small, topology-compatible feature frame without segmentation."""
    width, height = frame_size
    mask = np.zeros((height, width), dtype=bool)
    components: list[GeometryMaskComponent] = []
    mask_features: list[GeometryMaskFeature] = []
    for component_id, (label, point, feature_type, orientation) in enumerate(features, start=1):
        x, y = point
        mask[round(y), round(x)] = True
        components.append(
            GeometryMaskComponent(
                component_id=component_id,
                bounding_box_px=(round(x) - 5, round(y) - 5, 11, 11),
                area_px=36 if label.startswith("edge") else 64,
                position_px=(x, y),
                orientation_rad=orientation,
                length_px=44.0 if label.startswith("edge") else 68.0,
                line_center_samples_px=((x - 2.0, y), (x, y), (x + 2.0, y)),
            )
        )
        branch_count = {"endpoint": 1, "corner": 2, "junction": 4}[feature_type]
        branch_orientations = tuple(
            (orientation + index * np.pi / branch_count) % np.pi
            for index in range(branch_count)
        )
        descriptor = tuple(float(value) for value in np.eye(3, dtype=int).ravel())
        mask_features.append(
            GeometryMaskFeature(
                point_px=(x, y),
                descriptor=descriptor,
                response=float(branch_count),
                component_id=component_id,
                feature_type=feature_type,
                orientation_rad=orientation,
                branch_orientations_rad=branch_orientations,
            )
        )
    return GeometryMaskFrame(
        mask=mask,
        features=tuple(mask_features),
        frame_size=frame_size,
        components=tuple(components),
    )


def _translated_synthetic_features(
    features: list[tuple[str, tuple[float, float], str, float]],
    shift_px: tuple[float, float],
    *,
    missing: set[str] = frozenset(),
    distractors: list[tuple[str, tuple[float, float], str, float]] | None = None,
) -> list[tuple[str, tuple[float, float], str, float]]:
    shifted = [
        (label, (point[0] + shift_px[0], point[1] + shift_px[1]), feature_type, orientation)
        for label, point, feature_type, orientation in features
        if label not in missing
    ]
    return shifted + list(distractors or ())


def _assert_unique_frame_per_track(observations: tuple[object, ...]) -> None:
    by_track: dict[object, set[int]] = {}
    for observation in observations:
        frame_indexes = by_track.setdefault(observation.feature_id, set())
        assert observation.frame_index not in frame_indexes
        frame_indexes.add(observation.frame_index)


def test_geometry_tracking_uses_unique_stage_predictions_for_grid_and_edge_combs() -> None:
    frame_size = (640, 400)
    pixels_to_mm = np.array(((-0.001, 0.0), (0.0, -0.001)), dtype=float)
    base_features = [
        ("edge-left", (54.0, 80.0), "endpoint", 0.0),
        ("comb-left-a", (88.0, 120.0), "corner", 0.0),
        ("comb-left-b", (88.0, 156.0), "corner", 0.0),
        ("grid-a", (280.0, 160.0), "junction", 0.0),
        ("grid-b", (352.0, 228.0), "junction", np.pi / 2.0),
        ("comb-right-a", (550.0, 120.0), "corner", np.pi),
        ("edge-right", (586.0, 280.0), "endpoint", np.pi),
    ]
    stage_offsets = ((0.0, 0.0), (0.020, -0.010), (-0.015, 0.012))
    shifts = tuple(
        tuple(np.linalg.inv(pixels_to_mm) @ np.asarray(offset, dtype=float))
        for offset in stage_offsets
    )
    frames = tuple(GridCalibrationFrame(frame=None, stage_offset_mm=offset) for offset in stage_offsets)
    masks = (
        _synthetic_geometry_mask_frame(base_features, frame_size=frame_size),
        _synthetic_geometry_mask_frame(
            _translated_synthetic_features(
                base_features,
                shifts[1],
                missing={"comb-left-b"},
                # This is inside the old 30 px radius, but outside the hard 12 px gate.
                distractors=[("distractor", (base_features[3][1][0] + shifts[1][0] + 22.0, base_features[3][1][1] + shifts[1][1]), "junction", 0.0)],
            ),
            frame_size=frame_size,
        ),
        _synthetic_geometry_mask_frame(
            _translated_synthetic_features(base_features, shifts[2]),
            frame_size=frame_size,
        ),
    )

    observations = geometry_mask.build_geometry_feature_observations(
        frames,
        masks,
        frame_size=frame_size,
        image_pixels_to_mm=pixels_to_mm,
        match_gate_px=12.0,
    )

    _assert_unique_frame_per_track(observations)
    assert len({observation.feature_id for observation in observations}) == len(base_features)
    assert any(observation.pixel_xy[0] < 80.0 for observation in observations)
    assert any(observation.pixel_xy[0] > 560.0 for observation in observations)
    assert any(240.0 < observation.pixel_xy[0] < 380.0 for observation in observations)
    assert not any(
        observation.frame_index == 1
        and abs(observation.pixel_xy[0] - (base_features[3][1][0] + shifts[1][0] + 22.0)) < 1e-6
        for observation in observations
    )


def test_geometry_tracking_rejects_cumulative_track_spread_beyond_quality_gate() -> None:
    frame_size = (640, 400)
    pixels_to_mm = np.array(((-0.001, 0.0), (0.0, -0.001)), dtype=float)
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=(0.0, 0.0))
        for _ in range(3)
    )
    masks = tuple(
        _synthetic_geometry_mask_frame(
            [("grid", (x, 160.0), "junction", 0.0)],
            frame_size=frame_size,
        )
        for x in (300.0, 304.0, 311.0)
    )

    observations = geometry_mask.build_geometry_feature_observations(
        frames,
        masks,
        frame_size=frame_size,
        image_pixels_to_mm=pixels_to_mm,
        match_gate_px=12.0,
    )

    assert len(observations) == 2
    assert {observation.frame_index for observation in observations} == {0, 1}
    assert {observation.pixel_xy for observation in observations} == {
        (300.0, 160.0),
        (304.0, 160.0),
    }

    wider_quality_gate = geometry_mask.build_geometry_feature_observations(
        frames,
        masks,
        frame_size=frame_size,
        image_pixels_to_mm=pixels_to_mm,
        match_gate_px=12.0,
        max_track_spread_px=6.0,
    )

    assert len(wider_quality_gate) == 3


def test_geometry_tracking_assigns_one_candidate_to_only_one_competing_track() -> None:
    frame_size = (640, 400)
    pixels_to_mm = np.array(((-0.001, 0.0), (0.0, -0.001)), dtype=float)
    frames = (
        GridCalibrationFrame(frame=None, stage_offset_mm=(0.0, 0.0)),
        GridCalibrationFrame(frame=None, stage_offset_mm=(0.0, 0.0)),
    )
    masks = (
        _synthetic_geometry_mask_frame(
            [
                ("track-a", (300.0, 160.0), "junction", 0.0),
                ("track-b", (304.0, 160.0), "junction", 0.0),
            ],
            frame_size=frame_size,
        ),
        _synthetic_geometry_mask_frame(
            [("shared-candidate", (302.0, 160.0), "junction", 0.0)],
            frame_size=frame_size,
        ),
    )

    observations = geometry_mask.build_geometry_feature_observations(
        frames,
        masks,
        frame_size=frame_size,
        image_pixels_to_mm=pixels_to_mm,
        match_gate_px=12.0,
    )

    candidate_tracks = {
        observation.feature_id
        for observation in observations
        if observation.frame_index == 1 and observation.pixel_xy == (302.0, 160.0)
    }
    assert len(candidate_tracks) == 1
    assert len(observations) == 2
    continuing_track = next(iter(candidate_tracks))
    assert {
        observation.frame_index
        for observation in observations
        if observation.feature_id == continuing_track
    } == {0, 1}


def _inverse_synthetic_geometry_point(
    corrected_px: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    """Invert the fitter's normalized radial/tangential correction numerically."""
    from probe_station_gui.camera.distortion import _stage_geometry_correct_point

    estimate = corrected_px.copy()
    for _ in range(30):
        mapped = np.asarray(
            _stage_geometry_correct_point(
                (float(estimate[0]), float(estimate[1])),
                frame_size=frame_size,
                center_px=center_px,
                k1=k1,
                k2=k2,
                p1=p1,
                p2=p2,
            ),
            dtype=float,
        )
        estimate += corrected_px - mapped
    return estimate


def test_geometry_tracking_supplies_fit_with_recoverable_affine_and_distortion_data() -> None:
    frame_size = (640, 400)
    true_matrix = np.array(((-0.00082, 0.00003), (-0.00002, -0.00079)), dtype=float)
    initial_matrix = np.array(((-0.00084, 0.0), (0.0, -0.00081)), dtype=float)
    center_px = (326.0, 186.0)
    distortion = {"k1": 0.018, "k2": -0.006, "p1": 0.0018, "p2": -0.0012}
    stage_offsets = tuple(
        (x, y) for y in (-0.050, 0.0, 0.050) for x in (-0.060, 0.0, 0.060)
    )
    world_features = [
        ("edge-left", (-0.130, -0.085), "endpoint", 0.0),
        ("edge-right", (0.128, 0.083), "endpoint", np.pi),
        ("comb-left-a", (-0.096, -0.048), "corner", 0.0),
        ("comb-left-b", (-0.096, 0.018), "corner", 0.0),
        ("grid-a", (-0.038, -0.036), "junction", 0.0),
        ("grid-b", (0.024, -0.012), "junction", np.pi / 2.0),
        ("grid-c", (0.064, 0.044), "junction", 0.0),
        ("comb-right-a", (0.098, -0.048), "corner", np.pi),
        ("comb-right-b", (0.098, 0.020), "corner", np.pi),
    ]
    frame_center = np.asarray((frame_size[0] * 0.5, frame_size[1] * 0.5), dtype=float)
    masks: list[GeometryMaskFrame] = []
    frames: list[GridCalibrationFrame] = []
    for frame_index, stage_offset in enumerate(stage_offsets):
        frame_features: list[tuple[str, tuple[float, float], str, float]] = []
        for label, world_xy, feature_type, orientation in world_features:
            corrected_px = frame_center + np.linalg.inv(true_matrix) @ (
                np.asarray(stage_offset) - np.asarray(world_xy)
            )
            if not (18.0 < corrected_px[0] < frame_size[0] - 18.0 and 18.0 < corrected_px[1] < frame_size[1] - 18.0):
                continue
            observed_px = _inverse_synthetic_geometry_point(
                corrected_px,
                frame_size=frame_size,
                center_px=center_px,
                **distortion,
            )
            frame_features.append((label, tuple(observed_px), feature_type, orientation))
        masks.append(_synthetic_geometry_mask_frame(frame_features, frame_size=frame_size))
        frames.append(GridCalibrationFrame(frame=None, stage_offset_mm=stage_offset))

    observations = geometry_mask.build_geometry_feature_observations(
        frames,
        masks,
        frame_size=frame_size,
        image_pixels_to_mm=initial_matrix,
        match_gate_px=12.0,
    )
    fit = fit_stage_geometry_from_observations(
        observations,
        frame_size=frame_size,
        initial_pixels_to_mm=initial_matrix,
        max_nfev=500,
    )

    assert len(observations) >= 20
    assert fit.baseline_residual_mean_px > fit.residual_mean_px * 2.0
    assert fit.residual_mean_px < 0.01
    assert fit.residual_max_px < 0.25
    assert fit.baseline_residual_max_px > fit.residual_max_px + 3.5
    assert fit.pixels_to_mm[0][0] == pytest.approx(true_matrix[0, 0], rel=0.06)
    assert fit.pixels_to_mm[0][1] == pytest.approx(true_matrix[0, 1], rel=0.08)
    assert fit.pixels_to_mm[1][0] == pytest.approx(true_matrix[1, 0], rel=0.08)
    assert fit.pixels_to_mm[1][1] == pytest.approx(true_matrix[1, 1], rel=0.06)
    assert fit.model.k1 == pytest.approx(distortion["k1"], abs=0.002)
    assert fit.model.k2 == pytest.approx(distortion["k2"], abs=0.002)
    assert fit.model.p1 == pytest.approx(distortion["p1"], abs=0.0005)
    assert fit.model.p2 == pytest.approx(distortion["p2"], abs=0.0005)
    assert fit.center_px == pytest.approx(center_px, abs=2.0)


def _preview_mask_pattern(frame_size: tuple[int, int]) -> np.ndarray:
    """Return a central grid with visible left and right comb structures."""
    width, height = frame_size
    mask = np.zeros((height, width), dtype=np.uint8)
    for x in (width // 2 - 16, width // 2, width // 2 + 16):
        cv2.line(mask, (x, height // 2 - 20), (x, height // 2 + 20), 1, 3)
    for y in (height // 2 - 20, height // 2, height // 2 + 20):
        cv2.line(mask, (width // 2 - 16, y), (width // 2 + 16, y), 1, 3)
    for spine_x, tooth_end_x in ((12, 34), (width - 13, width - 35)):
        cv2.line(mask, (spine_x, 18), (spine_x, height - 19), 1, 3)
        for y in (28, 44, 60, height - 61, height - 45, height - 29):
            cv2.line(mask, (spine_x, y), (tooth_end_x, y), 1, 3)
    return mask.astype(bool)


def _inverse_preview_geometry_point(
    corrected_px: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    from probe_station_gui.camera.distortion import _stage_geometry_correct_point

    estimate = corrected_px.astype(float, copy=True)
    for _ in range(30):
        mapped = np.asarray(
            _stage_geometry_correct_point(
                (float(estimate[0]), float(estimate[1])),
                frame_size=frame_size,
                center_px=center_px,
                k1=k1,
                k2=k2,
                p1=p1,
                p2=p2,
            ),
            dtype=float,
        )
        estimate += corrected_px - mapped
    return estimate


def _distorted_preview_mask(
    corrected_mask: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    observed = np.zeros_like(corrected_mask, dtype=bool)
    ys, xs = np.nonzero(corrected_mask)
    for x, y in zip(xs, ys):
        raw = _inverse_preview_geometry_point(
            np.asarray((x, y), dtype=float),
            frame_size=frame_size,
            center_px=center_px,
            k1=k1,
            k2=k2,
            p1=p1,
            p2=p2,
        )
        raw_x, raw_y = np.rint(raw).astype(int)
        if 0 <= raw_x < frame_size[0] and 0 <= raw_y < frame_size[1]:
            observed[raw_y, raw_x] = True
    return observed


def _translated_preview_mask(mask: np.ndarray, shift_px: np.ndarray) -> np.ndarray:
    height, width = mask.shape
    return cv2.warpAffine(
        mask.astype(np.uint8),
        np.asarray(((1.0, 0.0, shift_px[0]), (0.0, 1.0, shift_px[1]))),
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)


def _preview_rgb_array(image: QImage) -> np.ndarray:
    assert image.format() == QImage.Format_RGB888
    rows = np.frombuffer(
        image.constBits(), dtype=np.uint8, count=image.sizeInBytes()
    ).reshape((image.height(), image.bytesPerLine()))
    return rows[:, : image.width() * 3].reshape(
        (image.height(), image.width(), 3)
    ).copy()


def _nine_preview_frames(step_px: int = 4) -> tuple[GridCalibrationFrame, ...]:
    offsets = [(0.0, 0.0)]
    offsets.extend(
        (float(x), float(y))
        for y in (-step_px, 0, step_px)
        for x in (-step_px, 0, step_px)
        if (x, y) != (0, 0)
    )
    return tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in offsets
    )


def _preview_landmark_masks(
    frames: tuple[GridCalibrationFrame, ...],
    *,
    frame_size: tuple[int, int] = (64, 48),
    adjustments: dict[int, tuple[float, float]] | None = None,
    image_matrix: np.ndarray | None = None,
) -> tuple[np.ndarray, ...]:
    width, height = frame_size
    adjustments = adjustments or {}
    masks: list[np.ndarray] = []
    for index, frame in enumerate(frames):
        point = np.asarray((30.0, 22.0))
        if image_matrix is None:
            point += np.asarray(frame.stage_offset_mm)
        else:
            point += np.linalg.inv(image_matrix) @ np.asarray(frame.stage_offset_mm)
        point += np.asarray(adjustments.get(index, (0.0, 0.0)))
        x, y = np.rint(point).astype(int)
        mask = np.zeros((height, width), dtype=bool)
        mask[y, x] = True
        masks.append(mask)
    return tuple(masks)


def _identity_preview_args(
    frames: tuple[GridCalibrationFrame, ...],
    masks: tuple[np.ndarray, ...],
    *,
    matrix: np.ndarray | None = None,
) -> tuple[tuple[GridCalibrationFrame, ...], tuple[np.ndarray, ...], np.ndarray, dict[str, object]]:
    frame_size = (masks[0].shape[1], masks[0].shape[0])
    matrix = matrix if matrix is not None else np.array(((1.0, 0.0), (0.0, -1.0)))
    return (
        frames,
        masks,
        matrix,
        _preview_payload(
            frame_size=frame_size,
            pixels_to_mm=matrix,
            center_px=(frame_size[0] / 2.0, frame_size[1] / 2.0),
        ),
    )


def _render_preview_rgb(
    frames: tuple[GridCalibrationFrame, ...],
    masks: tuple[np.ndarray, ...],
    *,
    matrix: np.ndarray | None = None,
) -> np.ndarray:
    before, _after = build_geometry_alignment_previews(
        *_identity_preview_args(frames, masks, matrix=matrix)
    )
    return _preview_rgb_array(before)


def _preview_spread_metric(image: QImage) -> float:
    values = _preview_rgb_array(image).max(axis=2)
    return float(np.count_nonzero(values)) / float(values.sum())


def _assert_preview_occupancy_levels(
    image: QImage,
) -> np.ndarray:
    rgb = _preview_rgb_array(image)
    values = rgb[:, :, 0]
    neutral = np.all(rgb == rgb[:, :, :1], axis=2)
    nonzero_levels = np.unique(values[neutral & (values > 0)])

    assert np.any(values == 0)
    assert nonzero_levels.size >= 1
    return values


def _preview_payload(
    *,
    frame_size: tuple[int, int],
    pixels_to_mm: np.ndarray,
    center_px: tuple[float, float],
    k1: float = 0.0,
    k2: float = 0.0,
    p1: float = 0.0,
    p2: float = 0.0,
) -> dict[str, object]:
    return {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": list(frame_size),
        "pixels_to_mm": pixels_to_mm.tolist(),
        "calibrated_pixels_to_mm": pixels_to_mm.tolist(),
        "center_px": list(center_px),
        "k1": k1,
        "k2": k2,
        "p1": p1,
        "p2": p2,
    }


def test_geometry_alignment_previews_keep_combs_and_reduce_mask_spread() -> None:
    frame_size = (160, 112)
    center_px = (81.0, 53.0)
    initial_gui_matrix = np.array(((-0.00122, 0.0), (0.0, -0.00093)))
    fitted_gui_matrix = np.array(((-0.001, 0.00004), (0.00002, -0.001)))
    fitted_image_matrix = fitted_gui_matrix @ np.diag((1.0, -1.0))
    distortion = {"k1": 0.042, "k2": -0.009, "p1": 0.002, "p2": -0.0015}
    pixel_offsets = [
        (0.0, 0.0),
        (-12.0, -8.0),
        (0.0, -8.0),
        (12.0, -8.0),
        (-12.0, 0.0),
        (12.0, 0.0),
        (-12.0, 8.0),
        (0.0, 8.0),
        (12.0, 8.0),
    ]
    stage_offsets = tuple(
        tuple(fitted_image_matrix @ np.asarray(offset))
        for offset in pixel_offsets
    )
    base_mask = _preview_mask_pattern(frame_size)
    masks = tuple(
        _distorted_preview_mask(
            _translated_preview_mask(
                base_mask,
                np.asarray(offset),
            ),
            frame_size=frame_size,
            center_px=center_px,
            **distortion,
        )
        for offset in pixel_offsets
    )
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in stage_offsets
    )

    before, after = build_geometry_alignment_previews(
        frames,
        masks,
        initial_gui_matrix,
        _preview_payload(
            frame_size=frame_size,
            pixels_to_mm=fitted_gui_matrix,
            center_px=center_px,
            **distortion,
        ),
    )

    assert not before.isNull()
    assert not after.isNull()
    assert before.size() == after.size()
    assert before.format() == QImage.Format_RGB888
    assert after.format() == QImage.Format_RGB888
    _assert_preview_occupancy_levels(before)
    after_values = _assert_preview_occupancy_levels(after)

    assert np.count_nonzero(after_values) > 2500

    component_count, _labels, component_stats, _centroids = cv2.connectedComponentsWithStats(
        (after_values > 0).astype(np.uint8),
        connectivity=8,
    )
    components = [tuple(int(value) for value in row) for row in component_stats[1:]]
    assert component_count - 1 >= 2
    assert sum(area > 500 for _x, _y, _width, _height, area in components) >= 2
    after_rgb = _preview_rgb_array(after)
    neutral = np.all(after_rgb == after_rgb[:, :, :1], axis=2)
    assert all(np.count_nonzero(after_rgb[:, :, channel][neutral]) > 500 for channel in range(3))
    assert _preview_spread_metric(after) < _preview_spread_metric(before)
    assert int((after_rgb.max(axis=2) - after_rgb.min(axis=2)).sum()) < int(
        (_preview_rgb_array(before).max(axis=2) - _preview_rgb_array(before).min(axis=2)).sum()
    )


def test_geometry_alignment_previews_align_asymmetric_landmark_with_signed_axes() -> None:
    frame_size = (96, 72)
    gui_matrix = np.array(((-0.5, 0.0), (0.0, -0.25)))
    image_matrix = gui_matrix @ np.diag((1.0, -1.0))
    landmark_px = np.asarray((40.0, 30.0))
    pixel_offsets = (
        (0.0, 0.0),
        (-8.0, -6.0),
        (0.0, -6.0),
        (8.0, -6.0),
        (-8.0, 0.0),
        (8.0, 0.0),
        (-8.0, 6.0),
        (0.0, 6.0),
        (8.0, 6.0),
    )
    stage_offsets = tuple(
        tuple(image_matrix @ np.asarray(offset)) for offset in pixel_offsets
    )
    assert stage_offsets[1][0] > 0.0 and stage_offsets[1][1] < 0.0
    assert stage_offsets[3][0] < 0.0 and stage_offsets[3][1] < 0.0
    masks: list[np.ndarray] = []
    for pixel_offset in pixel_offsets:
        source_px = landmark_px + np.asarray(pixel_offset)
        mask = np.zeros((frame_size[1], frame_size[0]), dtype=bool)
        source_x, source_y = np.rint(source_px).astype(int)
        mask[source_y, source_x] = True
        masks.append(mask)

    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in stage_offsets
    )
    payload = _preview_payload(
        frame_size=frame_size,
        pixels_to_mm=gui_matrix,
        center_px=(48.0, 36.0),
    )

    _before, after = build_geometry_alignment_previews(
        frames,
        tuple(masks),
        gui_matrix,
        payload,
    )

    after_values = _preview_rgb_array(after)
    occupied = np.any(after_values > 0, axis=2)
    assert np.count_nonzero(occupied) == 1
    assert np.array_equal(after_values[occupied][0], np.array((253, 253, 253)))


def test_geometry_alignment_previews_use_an_exact_common_crop() -> None:
    frame_size = (120, 84)
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))
    mask = _preview_mask_pattern(frame_size)
    frames = _nine_preview_frames(step_px=6)
    masks = tuple(
        _translated_preview_mask(mask, np.asarray(frame.stage_offset_mm))
        for frame in frames
    )
    payload = _preview_payload(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=(60.0, 42.0),
    )

    before, after = build_geometry_alignment_previews(frames, masks, matrix, payload)

    assert before.size() == after.size()
    assert np.array_equal(_preview_rgb_array(before), _preview_rgb_array(after))


def test_geometry_alignment_previews_build_fitted_maps_once(monkeypatch) -> None:
    frame_size = (80, 60)
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))
    frames = _nine_preview_frames()
    masks = tuple(np.ones((60, 80), dtype=bool) for _ in frames)
    payload = _preview_payload(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=(40.0, 30.0),
        k1=0.01,
    )
    apply_calls: list[object] = []
    apply_correction = geometry_mask.apply_distortion_correction

    def count_apply(frame, correction):
        apply_calls.append(correction)
        return apply_correction(frame, correction)

    monkeypatch.setattr(
        geometry_mask,
        "apply_distortion_correction",
        count_apply,
    )

    build_geometry_alignment_previews(frames, masks, matrix, payload)

    assert len(apply_calls) == 1


@pytest.mark.parametrize(
    ("frames", "masks", "matrix", "payload", "message"),
    [
        (
            (GridCalibrationFrame(frame=None, stage_offset_mm=(0.0, 0.0)),),
            (),
            ((-0.001, 0.0), (0.0, -0.001)),
            _preview_payload(
                frame_size=(80, 60),
                pixels_to_mm=np.array(((-0.001, 0.0), (0.0, -0.001))),
                center_px=(40.0, 30.0),
            ),
            "same length",
        ),
        (
            _nine_preview_frames(),
            tuple(np.zeros((60, 80), dtype=bool) for _ in range(9)),
            np.array(((1.0, 0.0), (0.0, -1.0))),
            _preview_payload(
                frame_size=(80, 60),
                pixels_to_mm=np.array(((1.0, 0.0), (0.0, -1.0))),
                center_px=(40.0, 30.0),
            ),
            "empty",
        ),
        (
            _nine_preview_frames(),
            tuple(np.ones((60, 80), dtype=bool) for _ in range(9)),
            np.array(((1.0, 0.0), (0.0, -1.0))),
            {"model_version": 1, "model_type": "stage_geometry", "frame_size": [80, 60], "pixels_to_mm": [[1.0, 2.0], [2.0, 4.0]]},
            "fitted",
        ),
    ],
)
def test_geometry_alignment_previews_reject_invalid_inputs(
    frames: tuple[GridCalibrationFrame, ...],
    masks: tuple[np.ndarray, ...],
    matrix: object,
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_geometry_alignment_previews(frames, masks, matrix, payload)


def test_geometry_alignment_previews_reject_size_mismatch_and_correction_errors(monkeypatch) -> None:
    frame_size = (80, 60)
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))
    frames = _nine_preview_frames()
    payload = _preview_payload(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=(40.0, 30.0),
        k1=0.01,
    )

    with pytest.raises(ValueError, match="same size"):
        build_geometry_alignment_previews(
            frames,
            tuple(
                np.ones((59, 80), dtype=bool) if index == 1 else np.ones((60, 80), dtype=bool)
                for index in range(9)
            ),
            matrix,
            payload,
        )

    monkeypatch.setattr(
        geometry_mask,
        "apply_distortion_correction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("map unavailable")),
        raising=False,
    )
    with pytest.raises(ValueError, match="correction"):
        build_geometry_alignment_previews(
            frames,
            tuple(np.ones((60, 80), dtype=bool) for _ in frames),
            matrix,
            payload,
        )


def test_geometry_alignment_previews_render_one_horizontal_disagreement_at_half_red() -> None:
    frames = _nine_preview_frames()
    masks = _preview_landmark_masks(frames, adjustments={4: (1.0, 0.0)})

    values = _render_preview_rgb(frames, masks)
    red_chroma = values[:, :, 0].astype(int) - values[:, :, 1].astype(int)
    blue_chroma = values[:, :, 2].astype(int) - values[:, :, 1].astype(int)

    assert red_chroma.max() == 128
    assert blue_chroma.max() <= 0


def test_geometry_alignment_previews_render_vertical_and_corner_channels() -> None:
    frames = _nine_preview_frames()

    vertical_values = _render_preview_rgb(
        frames,
        _preview_landmark_masks(frames, adjustments={2: (0.0, 1.0)}),
    )
    vertical_chroma = (
        vertical_values[:, :, 1].astype(int) - vertical_values[:, :, 0].astype(int)
    )
    assert vertical_chroma.max() == 128

    corner_values = _render_preview_rgb(
        frames,
        _preview_landmark_masks(frames, adjustments={1: (1.0, 0.0)}),
    )
    blue_chroma = corner_values[:, :, 2].astype(int) - corner_values[:, :, 1].astype(int)
    assert blue_chroma.max() == 64


def test_geometry_alignment_previews_render_full_red_for_two_horizontal_disagreements() -> None:
    frames = _nine_preview_frames()
    masks = _preview_landmark_masks(
        frames,
        adjustments={4: (1.0, 0.0), 5: (-1.0, 0.0)},
    )

    values = _render_preview_rgb(frames, masks)
    red_chroma = values[:, :, 0].astype(int) - values[:, :, 1].astype(int)

    assert red_chroma.max() == 255


def test_preview_grid_roles_are_stable_for_shuffled_affine_grid() -> None:
    persisted_gui_matrix = np.array(((1.0, 0.2), (0.1, -1.0)))
    image_matrix = geometry_mask._image_matrix_from_persisted_gui(
        persisted_gui_matrix,
        "initial_pixels_to_mm",
    )
    pixel_offsets = [
        (0.0, 0.0),
        (-4.0, -4.0),
        (0.0, -4.0),
        (4.0, -4.0),
        (-4.0, 0.0),
        (4.0, 0.0),
        (-4.0, 4.0),
        (0.0, 4.0),
        (4.0, 4.0),
    ]
    stage_offsets = tuple(
        tuple(image_matrix @ np.asarray(offset)) for offset in pixel_offsets
    )

    center_index, roles = geometry_mask._preview_grid_roles(
        stage_offsets,
        image_matrix,
    )

    assert center_index == 0
    assert roles.count("horizontal") == 2
    assert roles.count("vertical") == 2
    assert roles.count("diagonal") == 4
    assert roles.count(None) == 1

    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in stage_offsets
    )
    masks = _preview_landmark_masks(frames, image_matrix=image_matrix)
    order = (8, 2, 5, 0, 7, 1, 4, 6, 3)
    original = _render_preview_rgb(frames, masks, matrix=persisted_gui_matrix)
    shuffled = _render_preview_rgb(
        tuple(frames[index] for index in order),
        tuple(masks[index] for index in order),
        matrix=persisted_gui_matrix,
    )
    assert np.array_equal(original, shuffled)


def test_geometry_alignment_previews_require_a_complete_3x3_grid() -> None:
    frames = _nine_preview_frames()[:-1]
    masks = _preview_landmark_masks(frames)

    with pytest.raises(GeometryAlignmentPreviewError, match="3x3"):
        build_geometry_alignment_previews(*_identity_preview_args(frames, masks))


def test_geometry_alignment_previews_reject_role_counts_without_signed_grid() -> None:
    malformed_offsets = (
        (0.0, 0.0),
        (4.0, 0.0),
        (8.0, 0.0),
        (0.0, 4.0),
        (0.0, 8.0),
        (4.0, 4.0),
        (4.0, 8.0),
        (8.0, 4.0),
        (8.0, 8.0),
    )
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in malformed_offsets
    )
    masks = _preview_landmark_masks(frames)

    with pytest.raises(GeometryAlignmentPreviewError, match="complete 3x3"):
        build_geometry_alignment_previews(*_identity_preview_args(frames, masks))


def test_geometry_alignment_previews_ignore_disagreement_outside_shared_footprint() -> None:
    frames = _nine_preview_frames()
    masks = list(_preview_landmark_masks(frames, frame_size=(40, 32)))
    masks[5][16, 0] = True
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))

    before, after = build_geometry_alignment_previews(
        frames,
        tuple(masks),
        matrix,
        _preview_payload(
            frame_size=(40, 32),
            pixels_to_mm=matrix,
            center_px=(20.0, 16.0),
            k1=-0.05,
        ),
    )
    values = _preview_rgb_array(before)
    row = int(np.rint(16.0 + 16.0))
    column = 12

    assert int(values[row, column].max() - values[row, column].min()) == 0
    fitted_values = _preview_rgb_array(after)
    transformed_column = 13
    assert fitted_values[row, transformed_column].max() > 0
    assert (
        int(
            fitted_values[row, transformed_column].max()
            - fitted_values[row, transformed_column].min()
        )
        == 0
    )
