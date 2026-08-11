"""Stage-predicted assignment of observed metal-geometry features."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree

from probe_station_gui.camera.distortion import StageFeatureObservation
from probe_station_gui.camera.geometry_segmentation import (
    GeometryMaskComponent,
    GeometryMaskFeature,
    GeometryMaskFrame,
)


_DEFAULT_GEOMETRY_MATCH_GATE_PX = 12.0
_DEFAULT_GEOMETRY_MAX_TRACK_SPREAD_PX = 5.0
_MAX_ORIENTATION_DELTA_RAD = float(np.deg2rad(30.0))
_MAX_DESCRIPTOR_DISTANCE = 0.50
_DESCRIPTOR_COST_CAP = 0.35
_DESCRIPTOR_COST_WEIGHT_PX = 2.0
_MIN_COMPONENT_LENGTH_TOLERANCE_PX = 10.0
_MAX_COMPONENT_LENGTH_RELATIVE_DELTA = 0.55
_ASSIGNMENT_BLOCKED_COST = 1e9


@dataclass(frozen=True)
class _GeometryFeatureRecord:
    feature: GeometryMaskFeature
    component: GeometryMaskComponent
    frame_index: int
    stage_xy: tuple[float, float]
    pixel_xy: np.ndarray
    world_xy: np.ndarray


@dataclass
class _GeometryFeatureTrack:
    track_id: int
    records: list[_GeometryFeatureRecord] = field(default_factory=list)
    world_centroid: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=float))

    def add(self, record: _GeometryFeatureRecord) -> None:
        if any(item.frame_index == record.frame_index for item in self.records):
            raise ValueError("A geometry track cannot contain two features from one frame.")
        self.records.append(record)
        self.world_centroid = np.mean(
            np.asarray([item.world_xy for item in self.records], dtype=float),
            axis=0,
        )


def build_geometry_feature_observations(
    frames: Sequence[object],
    masks: Sequence[GeometryMaskFrame],
    *,
    frame_size: tuple[int, int],
    image_pixels_to_mm: Sequence[Sequence[float]],
    match_gate_px: float = _DEFAULT_GEOMETRY_MATCH_GATE_PX,
    max_track_spread_px: float = _DEFAULT_GEOMETRY_MAX_TRACK_SPREAD_PX,
) -> tuple[StageFeatureObservation, ...]:
    """Build unique feature tracks predicted from known Stage offsets."""
    width, height = _valid_geometry_frame_size(frame_size)
    if len(frames) != len(masks):
        raise ValueError("frames and masks must have the same length.")
    if not frames:
        return ()
    matrix = _valid_image_pixels_to_mm(image_pixels_to_mm)
    inverse_matrix = np.linalg.inv(matrix)
    gate_px = _positive_match_gate_px(match_gate_px)
    spread_px = _valid_max_track_spread_px(max_track_spread_px, gate_px)
    frame_center = np.asarray((width * 0.5, height * 0.5), dtype=float)

    tracks: list[_GeometryFeatureTrack] = []
    next_track_id = 0
    for frame_index, (frame, mask_frame) in enumerate(zip(frames, masks)):
        if mask_frame.frame_size != (width, height):
            raise ValueError("Every geometry mask must match frame_size.")
        stage_xy = _stage_offset_mm(frame)
        records = _geometry_feature_records(
            mask_frame,
            frame_index=frame_index,
            stage_xy=stage_xy,
            matrix=matrix,
            frame_center=frame_center,
        )
        matched_record_indexes: set[int] = set()
        if tracks and records:
            costs = _geometry_assignment_costs(
                tracks,
                records,
                stage_xy=stage_xy,
                inverse_matrix=inverse_matrix,
                frame_center=frame_center,
                match_gate_px=gate_px,
                max_track_spread_px=spread_px,
            )
            viable_rows = np.flatnonzero(
                np.any(costs < _ASSIGNMENT_BLOCKED_COST, axis=1)
            )
            viable_columns = np.flatnonzero(
                np.any(costs < _ASSIGNMENT_BLOCKED_COST, axis=0)
            )
            if viable_rows.size and viable_columns.size:
                compact_costs = costs[np.ix_(viable_rows, viable_columns)]
                row_indexes, column_indexes = linear_sum_assignment(compact_costs)
            else:
                row_indexes = column_indexes = ()
            for compact_row, compact_column in zip(row_indexes, column_indexes):
                row_index = int(viable_rows[compact_row])
                column_index = int(viable_columns[compact_column])
                if costs[row_index, column_index] >= _ASSIGNMENT_BLOCKED_COST:
                    continue
                tracks[row_index].add(records[column_index])
                matched_record_indexes.add(column_index)

        for record_index, record in enumerate(records):
            if record_index in matched_record_indexes:
                continue
            track = _GeometryFeatureTrack(track_id=next_track_id)
            track.add(record)
            tracks.append(track)
            next_track_id += 1

    observations: list[StageFeatureObservation] = []
    for track in tracks:
        if len(track.records) < 2:
            continue
        for record in track.records:
            observations.append(
                StageFeatureObservation(
                    frame_index=record.frame_index,
                    feature_id=f"geometry_{track.track_id}",
                    stage_xy=record.stage_xy,
                    pixel_xy=(float(record.pixel_xy[0]), float(record.pixel_xy[1])),
                )
            )
    return tuple(observations)


def _valid_geometry_frame_size(frame_size: object) -> tuple[int, int]:
    try:
        width, height = (int(value) for value in frame_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("frame_size must contain positive width and height.") from exc
    if width <= 0 or height <= 0:
        raise ValueError("frame_size must contain positive width and height.")
    return (width, height)


def _valid_image_pixels_to_mm(matrix: Sequence[Sequence[float]]) -> np.ndarray:
    values = np.asarray(matrix, dtype=float)
    if values.shape != (2, 2) or not np.isfinite(values).all():
        raise ValueError("image_pixels_to_mm must be a finite 2x2 matrix.")
    if abs(float(np.linalg.det(values))) < 1e-18:
        raise ValueError("image_pixels_to_mm must be non-singular.")
    return values


def _positive_match_gate_px(match_gate_px: object) -> float:
    try:
        gate_px = float(match_gate_px)
    except (TypeError, ValueError) as exc:
        raise ValueError("match_gate_px must be a positive finite value.") from exc
    if not np.isfinite(gate_px) or gate_px <= 0.0:
        raise ValueError("match_gate_px must be a positive finite value.")
    return gate_px


def _valid_max_track_spread_px(value: object, match_gate_px: float) -> float:
    try:
        spread_px = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "max_track_spread_px must be positive and no larger than match_gate_px."
        ) from exc
    if (
        not np.isfinite(spread_px)
        or spread_px <= 0.0
        or spread_px > match_gate_px
    ):
        raise ValueError(
            "max_track_spread_px must be positive and no larger than match_gate_px."
        )
    return spread_px


def _stage_offset_mm(frame: object) -> tuple[float, float]:
    try:
        raw_offset = getattr(frame, "stage_offset_mm")
        stage = np.asarray(raw_offset, dtype=float)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Each frame must provide a finite stage_offset_mm pair.") from exc
    if stage.shape != (2,) or not np.isfinite(stage).all():
        raise ValueError("Each frame must provide a finite stage_offset_mm pair.")
    return (float(stage[0]), float(stage[1]))


def _geometry_feature_records(
    mask_frame: GeometryMaskFrame,
    *,
    frame_index: int,
    stage_xy: tuple[float, float],
    matrix: np.ndarray,
    frame_center: np.ndarray,
) -> list[_GeometryFeatureRecord]:
    components = {
        component.component_id: component for component in mask_frame.components
    }
    stage = np.asarray(stage_xy, dtype=float)
    records: list[_GeometryFeatureRecord] = []
    for feature in mask_frame.features:
        component = components.get(feature.component_id)
        if component is None:
            continue
        pixel = np.asarray(feature.point_px, dtype=float)
        if pixel.shape != (2,) or not np.isfinite(pixel).all():
            continue
        records.append(
            _GeometryFeatureRecord(
                feature=feature,
                component=component,
                frame_index=frame_index,
                stage_xy=stage_xy,
                pixel_xy=pixel,
                world_xy=stage - matrix @ (pixel - frame_center),
            )
        )
    return records


def _geometry_assignment_costs(
    tracks: Sequence[_GeometryFeatureTrack],
    records: Sequence[_GeometryFeatureRecord],
    *,
    stage_xy: tuple[float, float],
    inverse_matrix: np.ndarray,
    frame_center: np.ndarray,
    match_gate_px: float,
    max_track_spread_px: float,
) -> np.ndarray:
    costs = np.full((len(tracks), len(records)), _ASSIGNMENT_BLOCKED_COST, dtype=float)
    if not tracks or not records:
        return costs
    stage = np.asarray(stage_xy, dtype=float)
    pixel_scale_mm = float(
        np.mean(np.linalg.svd(np.linalg.inv(inverse_matrix), compute_uv=False))
    )
    record_tree = cKDTree(
        np.asarray([record.pixel_xy for record in records], dtype=float)
    )
    predictions = np.asarray(
        [
            frame_center + inverse_matrix @ (stage - track.world_centroid)
            for track in tracks
        ],
        dtype=float,
    )
    nearby_record_indexes = record_tree.query_ball_point(predictions, r=match_gate_px)
    for track_index, record_indexes in enumerate(nearby_record_indexes):
        if not record_indexes:
            continue
        track = tracks[track_index]
        reference = track.records[-1]
        predicted_px = predictions[track_index]
        for record_index in record_indexes:
            record = records[record_index]
            if not _geometry_features_compatible(reference, record):
                continue
            pixel_distance = float(np.linalg.norm(record.pixel_xy - predicted_px))
            if pixel_distance > match_gate_px:
                continue
            if (
                _prospective_track_spread_px(track, record, inverse_matrix)
                > max_track_spread_px
            ):
                continue
            world_distance = float(
                np.linalg.norm(record.world_xy - track.world_centroid)
            )
            descriptor_distance = _descriptor_distance(reference.feature, record.feature)
            costs[track_index, record_index] = (
                world_distance / max(pixel_scale_mm, 1e-12)
                + min(descriptor_distance, _DESCRIPTOR_COST_CAP)
                * _DESCRIPTOR_COST_WEIGHT_PX
            )
    return costs


def _prospective_track_spread_px(
    track: _GeometryFeatureTrack,
    candidate: _GeometryFeatureRecord,
    inverse_matrix: np.ndarray,
) -> float:
    world_points = np.asarray(
        [*(record.world_xy for record in track.records), candidate.world_xy],
        dtype=float,
    )
    centered_world = world_points - np.mean(world_points, axis=0)
    pixel_offsets = (inverse_matrix @ centered_world.T).T
    return float(np.max(np.linalg.norm(pixel_offsets, axis=1)))


def _geometry_features_compatible(
    reference: _GeometryFeatureRecord,
    candidate: _GeometryFeatureRecord,
) -> bool:
    if reference.feature.feature_type != candidate.feature.feature_type:
        return False
    if len(reference.feature.branch_orientations_rad) != len(
        candidate.feature.branch_orientations_rad
    ):
        return False
    if not _orientations_compatible(
        reference.feature.orientation_rad,
        candidate.feature.orientation_rad,
    ):
        return False
    if not _orientations_compatible(
        reference.component.orientation_rad,
        candidate.component.orientation_rad,
    ):
        return False
    longest = max(reference.component.length_px, candidate.component.length_px, 1.0)
    length_delta = abs(reference.component.length_px - candidate.component.length_px)
    if length_delta > max(
        _MIN_COMPONENT_LENGTH_TOLERANCE_PX,
        longest * _MAX_COMPONENT_LENGTH_RELATIVE_DELTA,
    ):
        return False
    return (
        _descriptor_distance(reference.feature, candidate.feature)
        <= _MAX_DESCRIPTOR_DISTANCE
    )


def _orientations_compatible(first: float | None, second: float | None) -> bool:
    if first is None or second is None:
        return first is None and second is None
    delta = abs((float(first) - float(second)) % np.pi)
    delta = min(delta, np.pi - delta)
    return delta <= _MAX_ORIENTATION_DELTA_RAD


def _descriptor_distance(
    first: GeometryMaskFeature,
    second: GeometryMaskFeature,
) -> float:
    first_values = np.asarray(first.descriptor, dtype=float)
    second_values = np.asarray(second.descriptor, dtype=float)
    if first_values.shape != second_values.shape or first_values.size == 0:
        return 1.0
    return float(np.mean(np.abs(first_values - second_values) > 0.5))
