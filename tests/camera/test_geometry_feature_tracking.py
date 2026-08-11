from __future__ import annotations

import numpy as np
import pytest

from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    fit_stage_geometry_from_observations,
)
from probe_station_gui.camera.geometry_feature_tracking import (
    build_geometry_feature_observations,
)
from probe_station_gui.camera.geometry_segmentation import (
    GeometryMaskComponent,
    GeometryMaskFeature,
    GeometryMaskFrame,
)


def _synthetic_geometry_mask_frame(
    features: list[tuple[str, tuple[float, float], str, float]],
    *,
    frame_size: tuple[int, int],
) -> GeometryMaskFrame:
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


def test_tracking_uses_unique_stage_predictions_for_grid_and_edge_combs() -> None:
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
                distractors=[
                    (
                        "distractor",
                        (
                            base_features[3][1][0] + shifts[1][0] + 22.0,
                            base_features[3][1][1] + shifts[1][1],
                        ),
                        "junction",
                        0.0,
                    )
                ],
            ),
            frame_size=frame_size,
        ),
        _synthetic_geometry_mask_frame(
            _translated_synthetic_features(base_features, shifts[2]),
            frame_size=frame_size,
        ),
    )

    observations = build_geometry_feature_observations(
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
        and abs(
            observation.pixel_xy[0]
            - (base_features[3][1][0] + shifts[1][0] + 22.0)
        )
        < 1e-6
        for observation in observations
    )


def test_tracking_rejects_cumulative_spread_beyond_quality_gate() -> None:
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

    observations = build_geometry_feature_observations(
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

    wider_quality_gate = build_geometry_feature_observations(
        frames,
        masks,
        frame_size=frame_size,
        image_pixels_to_mm=pixels_to_mm,
        match_gate_px=12.0,
        max_track_spread_px=6.0,
    )

    assert len(wider_quality_gate) == 3


def test_tracking_assigns_one_candidate_to_only_one_competing_track() -> None:
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

    observations = build_geometry_feature_observations(
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


def test_tracking_supplies_fit_with_recoverable_affine_and_distortion_data() -> None:
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
    for stage_offset in stage_offsets:
        frame_features: list[tuple[str, tuple[float, float], str, float]] = []
        for label, world_xy, feature_type, orientation in world_features:
            corrected_px = frame_center + np.linalg.inv(true_matrix) @ (
                np.asarray(stage_offset) - np.asarray(world_xy)
            )
            if not (
                18.0 < corrected_px[0] < frame_size[0] - 18.0
                and 18.0 < corrected_px[1] < frame_size[1] - 18.0
            ):
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

    observations = build_geometry_feature_observations(
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
