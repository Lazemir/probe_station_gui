"""Stage-consistent optical geometry fitting and coverage policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    StageFeatureObservation,
    StageGeometryCorrection,
    _positive_float,
    _stage_geometry_correct_point,
    _valid_point,
    _valid_frame_size,
    _valid_pixels_to_mm_matrix,
)


Point2D = tuple[float, float]
MIN_STAGE_GEOMETRY_FEATURES = 4
MIN_STAGE_GEOMETRY_OBSERVATIONS = 12
MIN_STAGE_GEOMETRY_FRAMES = 5
MIN_STAGE_GEOMETRY_COVERAGE_FRACTION = 0.15


@dataclass(frozen=True)
class StageGeometryCalibrationFit:
    """Result of fitting stage-consistent image geometry."""

    model: StageGeometryCorrection
    baseline_residual_mean_px: float
    baseline_residual_max_px: float
    success: bool
    evaluations: int
    optimizer: str
    message: str = ""

    @property
    def frame_size(self) -> tuple[int, int]:
        return self.model.frame_size

    @property
    def pixels_to_mm(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return self.model.pixels_to_mm

    @property
    def center_px(self) -> Point2D:
        return self.model.center_px

    @property
    def residual_mean_px(self) -> float:
        return self.model.residual_mean_px

    @property
    def residual_max_px(self) -> float:
        return self.model.residual_max_px

    @property
    def feature_count(self) -> int:
        return self.model.feature_count

    @property
    def observation_count(self) -> int:
        return self.model.observation_count

    def to_payload(self) -> dict[str, object]:
        payload = self.model.to_payload()
        payload["optimizer"] = self.optimizer
        payload["optimizer_success"] = bool(self.success)
        payload["optimizer_message"] = self.message
        payload["optimizer_evaluations"] = int(self.evaluations)
        payload["baseline_residual_mean_px"] = float(self.baseline_residual_mean_px)
        payload["baseline_residual_max_px"] = float(self.baseline_residual_max_px)
        return payload


def fit_stage_geometry_from_grid_frames(
    frames: Sequence[GridCalibrationFrame],
    *,
    frame_size: tuple[int, int],
    pixels_to_mm: Sequence[Sequence[float]],
    cluster_tolerance_px: float = 30.0,
    min_feature_observations: int = 2,
    optimize_distortion: bool = True,
    max_nfev: int = 800,
) -> StageGeometryCalibrationFit:
    """Fit stage geometry from bright-grid frames without using grid pitch."""

    from probe_station_gui.camera.bright_grid_detection import detect_bright_grid

    width, height = _valid_frame_size(frame_size)
    matrix = _valid_pixels_to_mm_matrix(pixels_to_mm)
    if matrix is None:
        raise ValueError("pixels_to_mm must be a non-singular 2x2 matrix.")
    frame_center = (float(width) * 0.5, float(height) * 0.5)
    pixel_mm = _matrix_mean_pixel_size_mm(matrix)
    tolerance_mm = max(pixel_mm * float(cluster_tolerance_px), pixel_mm * 2.0)
    clusters: list[dict[str, object]] = []
    for frame_index, frame in enumerate(frames):
        detection = detect_bright_grid(frame.frame)
        stage_xy = _valid_point(frame.stage_offset_mm, "stage_offset_mm")
        for pixel_xy in detection.intersections_px:
            world_xy = _world_xy_from_pixel(
                stage_xy=stage_xy,
                pixel_xy=pixel_xy,
                frame_center_px=frame_center,
                matrix=matrix,
                frame_size=(width, height),
                center_px=frame_center,
                k1=0.0,
                k2=0.0,
                p1=0.0,
                p2=0.0,
            )
            _add_stage_feature_cluster(
                clusters,
                frame_index=frame_index,
                stage_xy=stage_xy,
                pixel_xy=pixel_xy,
                world_xy=world_xy,
                tolerance_mm=tolerance_mm,
            )

    observations: list[StageFeatureObservation] = []
    min_count = max(2, int(min_feature_observations))
    for feature_index, cluster in enumerate(clusters):
        items = list(cluster.get("items", ()))
        if len(items) < min_count:
            continue
        feature_id = f"grid_{feature_index}"
        for item in items:
            observations.append(
                StageFeatureObservation(
                    frame_index=int(item["frame_index"]),
                    feature_id=feature_id,
                    stage_xy=item["stage_xy"],
                    pixel_xy=item["pixel_xy"],
                )
            )
    if not observations:
        raise ValueError("No repeated grid features were detected for geometry fit.")
    return fit_stage_geometry_from_observations(
        observations,
        frame_size=(width, height),
        initial_pixels_to_mm=matrix,
        optimize_distortion=optimize_distortion,
        max_nfev=max_nfev,
    )


def fit_stage_geometry_from_observations(
    observations: Sequence[StageFeatureObservation],
    *,
    frame_size: tuple[int, int],
    initial_pixels_to_mm: Sequence[Sequence[float]],
    optimize_distortion: bool = True,
    center_search_fraction: float = 0.25,
    matrix_relative_bound: float = 0.25,
    k1_bounds: tuple[float, float] = (-0.12, 0.12),
    k2_bounds: tuple[float, float] = (-0.12, 0.12),
    p_bounds: tuple[float, float] = (-0.04, 0.04),
    max_nfev: int = 800,
) -> StageGeometryCalibrationFit:
    """Fit affine pixel scale and low-order distortion by feature consistency."""

    import numpy as np

    width, height = _valid_frame_size(frame_size)
    initial_matrix = _valid_pixels_to_mm_matrix(initial_pixels_to_mm)
    if initial_matrix is None:
        raise ValueError("initial_pixels_to_mm must be a non-singular 2x2 matrix.")
    matrix0 = np.asarray(initial_matrix, dtype=float)
    prepared = _valid_stage_feature_observations(observations)
    grouped = _stage_feature_groups(prepared)
    if len(grouped) < 2:
        raise ValueError("At least two repeated features are required.")
    used_indexes = sorted(index for indexes in grouped.values() for index in indexes)
    prepared = tuple(prepared[index] for index in used_indexes)
    grouped = _stage_feature_groups(prepared)
    if optimize_distortion:
        _validate_stage_geometry_fit_coverage(
            prepared,
            grouped,
            frame_size=(width, height),
            initial_matrix=matrix0,
        )
    frame_center = (float(width) * 0.5, float(height) * 0.5)
    center_fraction = _positive_float(center_search_fraction, "center_search_fraction")
    center_fraction = min(center_fraction, 0.95)
    matrix_bound = _positive_float(matrix_relative_bound, "matrix_relative_bound")
    matrix_bound = min(matrix_bound, 2.0)
    scale_floor = _matrix_mean_pixel_size_mm(matrix0) * matrix_bound
    matrix_lower: list[float] = []
    matrix_upper: list[float] = []
    for value in matrix0.reshape(-1):
        span = max(abs(float(value)) * matrix_bound, scale_floor)
        matrix_lower.append(float(value) - span)
        matrix_upper.append(float(value) + span)
    center_x = frame_center[0]
    center_y = frame_center[1]
    center_dx = float(width) * center_fraction
    center_dy = float(height) * center_fraction
    if optimize_distortion:
        x0 = np.array(
            [
                *matrix0.reshape(-1).tolist(),
                center_x,
                center_y,
                0.0,
                0.0,
                0.0,
                0.0,
            ],
            dtype=float,
        )
        lower = np.array(
            [
                *matrix_lower,
                center_x - center_dx,
                center_y - center_dy,
                float(k1_bounds[0]),
                float(k2_bounds[0]),
                float(p_bounds[0]),
                float(p_bounds[0]),
            ],
            dtype=float,
        )
        upper = np.array(
            [
                *matrix_upper,
                center_x + center_dx,
                center_y + center_dy,
                float(k1_bounds[1]),
                float(k2_bounds[1]),
                float(p_bounds[1]),
                float(p_bounds[1]),
            ],
            dtype=float,
        )
    else:
        x0 = np.asarray(matrix0.reshape(-1), dtype=float)
        lower = np.asarray(matrix_lower, dtype=float)
        upper = np.asarray(matrix_upper, dtype=float)

    def unpack(vector: object) -> tuple[object, Point2D, float, float, float, float]:
        values = np.asarray(vector, dtype=float)
        matrix = values[:4].reshape((2, 2))
        if optimize_distortion:
            return (
                matrix,
                (float(values[4]), float(values[5])),
                float(values[6]),
                float(values[7]),
                float(values[8]),
                float(values[9]),
            )
        return (matrix, frame_center, 0.0, 0.0, 0.0, 0.0)

    def residual_vector(vector: object) -> object:
        matrix, center_px, k1, k2, p1, p2 = unpack(vector)
        residuals = _stage_geometry_residuals_mm(
            prepared,
            grouped,
            frame_size=(width, height),
            frame_center_px=frame_center,
            matrix=matrix,
            center_px=center_px,
            k1=k1,
            k2=k2,
            p1=p1,
            p2=p2,
        )
        return residuals.reshape(-1)

    baseline = residual_vector(x0)
    result = _least_squares(
        residual_vector,
        x0,
        bounds=(lower, upper),
        loss="soft_l1",
        f_scale=max(_matrix_mean_pixel_size_mm(matrix0) * 2.0, 1e-6),
        max_nfev=max(1, int(max_nfev)),
    )
    best_matrix, best_center, best_k1, best_k2, best_p1, best_p2 = unpack(result.x)
    optimized = _stage_geometry_residuals_mm(
        prepared,
        grouped,
        frame_size=(width, height),
        frame_center_px=frame_center,
        matrix=best_matrix,
        center_px=best_center,
        k1=best_k1,
        k2=best_k2,
        p1=best_p1,
        p2=best_p2,
    )
    baseline_stats = _stage_geometry_residual_stats(baseline.reshape((-1, 2)), matrix0)
    optimized_stats = _stage_geometry_residual_stats(optimized, best_matrix)
    model = StageGeometryCorrection(
        frame_size=(width, height),
        pixels_to_mm=(
            (float(best_matrix[0, 0]), float(best_matrix[0, 1])),
            (float(best_matrix[1, 0]), float(best_matrix[1, 1])),
        ),
        center_px=(float(best_center[0]), float(best_center[1])),
        k1=float(best_k1),
        k2=float(best_k2),
        p1=float(best_p1),
        p2=float(best_p2),
        residual_mean_px=optimized_stats[0],
        residual_max_px=optimized_stats[1],
        residual_mean_mm=optimized_stats[2],
        residual_max_mm=optimized_stats[3],
        feature_count=len(grouped),
        observation_count=len(prepared),
    )
    return StageGeometryCalibrationFit(
        model=model,
        baseline_residual_mean_px=baseline_stats[0],
        baseline_residual_max_px=baseline_stats[1],
        success=bool(getattr(result, "success", False)),
        evaluations=int(getattr(result, "nfev", 0)),
        optimizer="least_squares",
        message=str(getattr(result, "message", "")),
    )


def _matrix_mean_pixel_size_mm(matrix: object) -> float:
    import numpy as np

    values = np.asarray(matrix, dtype=float)
    if values.shape != (2, 2):
        return 1.0
    column_x = values[:, 0]
    column_y = values[:, 1]
    pixel_size = (
        float(np.linalg.norm(column_x)) + float(np.linalg.norm(column_y))
    ) * 0.5
    if not math.isfinite(pixel_size) or pixel_size <= 0.0:
        return 1.0
    return pixel_size


def _valid_stage_feature_observations(
    observations: Sequence[StageFeatureObservation],
) -> tuple[StageFeatureObservation, ...]:
    prepared: list[StageFeatureObservation] = []
    for observation in observations:
        stage_xy = _valid_point(observation.stage_xy, "stage_xy")
        pixel_xy = _valid_point(observation.pixel_xy, "pixel_xy")
        prepared.append(
            StageFeatureObservation(
                frame_index=int(observation.frame_index),
                feature_id=str(observation.feature_id),
                stage_xy=stage_xy,
                pixel_xy=pixel_xy,
            )
        )
    if len(prepared) < 4:
        raise ValueError("At least four feature observations are required.")
    return tuple(prepared)


def _stage_feature_groups(
    observations: Sequence[StageFeatureObservation],
) -> dict[str, tuple[int, ...]]:
    by_feature: dict[str, list[int]] = {}
    for index, observation in enumerate(observations):
        by_feature.setdefault(str(observation.feature_id), []).append(index)
    return {
        feature_id: tuple(indexes)
        for feature_id, indexes in by_feature.items()
        if len(indexes) >= 2
    }


def _validate_stage_geometry_fit_coverage(
    observations: Sequence[StageFeatureObservation],
    grouped: dict[str, tuple[int, ...]],
    *,
    frame_size: tuple[int, int],
    initial_matrix: object,
) -> None:
    import numpy as np

    def reject(reason: str) -> None:
        raise ValueError(
            f"Stage-geometry calibration has insufficient geometry coverage: {reason}."
        )

    if len(grouped) < MIN_STAGE_GEOMETRY_FEATURES:
        reject(f"need at least {MIN_STAGE_GEOMETRY_FEATURES} repeated features")
    if len(observations) < MIN_STAGE_GEOMETRY_OBSERVATIONS:
        reject(f"need at least {MIN_STAGE_GEOMETRY_OBSERVATIONS} observations")
    if 2 * (len(observations) - len(grouped)) < 10:
        reject("too few independent residuals for the ten-parameter model")

    frame_indexes = {int(observation.frame_index) for observation in observations}
    if len(frame_indexes) < MIN_STAGE_GEOMETRY_FRAMES:
        reject(f"need at least {MIN_STAGE_GEOMETRY_FRAMES} captured positions")

    stage_positions = np.unique(
        np.round(
            np.asarray(
                [observation.stage_xy for observation in observations],
                dtype=float,
            ),
            decimals=12,
        ),
        axis=0,
    )
    if stage_positions.shape[0] < MIN_STAGE_GEOMETRY_FRAMES:
        reject(f"need at least {MIN_STAGE_GEOMETRY_FRAMES} distinct stage positions")

    width, height = frame_size
    matrix = np.asarray(initial_matrix, dtype=float)
    pixel_corners = np.asarray(
        (
            (0.0, 0.0),
            (float(width), 0.0),
            (0.0, float(height)),
            (float(width), float(height)),
        ),
        dtype=float,
    )
    fov_stage_span = np.ptp(pixel_corners @ matrix.T, axis=0)
    minimum_span = fov_stage_span * float(MIN_STAGE_GEOMETRY_COVERAGE_FRACTION)
    stage_span = np.ptp(stage_positions, axis=0)
    if np.any(stage_span < minimum_span):
        reject("stage positions must span both image axes")

    frame_center = (float(width) * 0.5, float(height) * 0.5)
    world_centers: list[tuple[float, float]] = []
    for indexes in grouped.values():
        world_points = [
            _world_xy_from_pixel(
                stage_xy=observations[index].stage_xy,
                pixel_xy=observations[index].pixel_xy,
                frame_center_px=frame_center,
                matrix=matrix,
                frame_size=frame_size,
                center_px=frame_center,
                k1=0.0,
                k2=0.0,
                p1=0.0,
                p2=0.0,
            )
            for index in indexes
        ]
        center = np.mean(np.asarray(world_points, dtype=float), axis=0)
        world_centers.append((float(center[0]), float(center[1])))
    feature_span = np.ptp(np.asarray(world_centers, dtype=float), axis=0)
    if np.any(feature_span < minimum_span):
        reject("matched features must be distributed across both image axes")


def _world_xy_from_pixel(
    *,
    stage_xy: Point2D,
    pixel_xy: Point2D,
    frame_center_px: Point2D,
    matrix: object,
    frame_size: tuple[int, int],
    center_px: Point2D,
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> Point2D:
    import numpy as np

    corrected = _stage_geometry_correct_point(
        pixel_xy,
        frame_size=frame_size,
        center_px=center_px,
        k1=k1,
        k2=k2,
        p1=p1,
        p2=p2,
    )
    delta_px = np.array(
        [
            float(corrected[0]) - float(frame_center_px[0]),
            float(corrected[1]) - float(frame_center_px[1]),
        ],
        dtype=float,
    )
    stage = np.asarray(stage_xy, dtype=float)
    world = stage - np.asarray(matrix, dtype=float) @ delta_px
    return (float(world[0]), float(world[1]))


def _stage_geometry_residuals_mm(
    observations: Sequence[StageFeatureObservation],
    grouped: dict[str, tuple[int, ...]],
    *,
    frame_size: tuple[int, int],
    frame_center_px: Point2D,
    matrix: object,
    center_px: Point2D,
    k1: float,
    k2: float,
    p1: float,
    p2: float,
):
    import numpy as np

    world_points = np.asarray(
        [
            _world_xy_from_pixel(
                stage_xy=observation.stage_xy,
                pixel_xy=observation.pixel_xy,
                frame_center_px=frame_center_px,
                matrix=matrix,
                frame_size=frame_size,
                center_px=center_px,
                k1=k1,
                k2=k2,
                p1=p1,
                p2=p2,
            )
            for observation in observations
        ],
        dtype=float,
    )
    residuals: list[object] = []
    for indexes in grouped.values():
        group = world_points[list(indexes), :]
        center = np.mean(group, axis=0)
        residuals.extend(group - center)
    if not residuals:
        return np.zeros((0, 2), dtype=float)
    return np.asarray(residuals, dtype=float).reshape((-1, 2))


def _stage_geometry_residual_stats(
    residuals_mm: object, matrix: object
) -> tuple[float, float, float, float]:
    import numpy as np

    residuals = np.asarray(residuals_mm, dtype=float).reshape((-1, 2))
    if residuals.size == 0:
        return (0.0, 0.0, 0.0, 0.0)
    norms_mm = np.linalg.norm(residuals, axis=1)
    mean_mm = float(np.mean(norms_mm))
    max_mm = float(np.max(norms_mm))
    pixel_size = _matrix_mean_pixel_size_mm(matrix)
    return (
        float(mean_mm / pixel_size),
        float(max_mm / pixel_size),
        mean_mm,
        max_mm,
    )


def _add_stage_feature_cluster(
    clusters: list[dict[str, object]],
    *,
    frame_index: int,
    stage_xy: Point2D,
    pixel_xy: Point2D,
    world_xy: Point2D,
    tolerance_mm: float,
) -> None:
    import numpy as np

    world = np.asarray(world_xy, dtype=float)
    best: dict[str, object] | None = None
    best_distance = float("inf")
    for cluster in clusters:
        frame_indexes = set(cluster.get("frame_indexes", ()))
        if int(frame_index) in frame_indexes:
            continue
        center = np.asarray(cluster["center"], dtype=float)
        distance = float(np.linalg.norm(world - center))
        if distance <= float(tolerance_mm) and distance < best_distance:
            best = cluster
            best_distance = distance
    item = {
        "frame_index": int(frame_index),
        "stage_xy": (float(stage_xy[0]), float(stage_xy[1])),
        "pixel_xy": (float(pixel_xy[0]), float(pixel_xy[1])),
        "world_xy": (float(world_xy[0]), float(world_xy[1])),
    }
    if best is None:
        clusters.append(
            {
                "center": (float(world[0]), float(world[1])),
                "items": [item],
                "frame_indexes": {int(frame_index)},
            }
        )
        return
    items = list(best.get("items", ()))
    items.append(item)
    points = np.asarray([entry["world_xy"] for entry in items], dtype=float)
    center = np.mean(points, axis=0)
    best["center"] = (float(center[0]), float(center[1]))
    best["items"] = items
    frame_indexes = set(best.get("frame_indexes", ()))
    frame_indexes.add(int(frame_index))
    best["frame_indexes"] = frame_indexes


def _least_squares(fun: object, x0: object, **kwargs):
    from scipy.optimize import least_squares

    return least_squares(fun, x0, **kwargs)


__all__ = [
    "StageGeometryCalibrationFit",
    "fit_stage_geometry_from_grid_frames",
    "fit_stage_geometry_from_observations",
]
