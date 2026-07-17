"""Lens distortion correction helpers for microscope frames."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from PySide6.QtGui import QImage

from probe_station_gui.camera.imaging import MicroscopeScaleCalibration, MicroscopeScanTile


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


@dataclass(frozen=True)
class SeamRadialDistortionFit:
    """Result of fitting one radial model against seam debug tiles."""

    model: RadialDistortionModel
    baseline_score: float
    optimized_score: float
    success: bool
    evaluations: int
    optimizer: str
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "optimizer": self.optimizer,
            "success": bool(self.success),
            "message": self.message,
            "evaluations": int(self.evaluations),
            "baseline_score": float(self.baseline_score),
            "optimized_score": float(self.optimized_score),
            "improvement": float(self.baseline_score - self.optimized_score),
            "model": self.model.to_dict(),
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


def correction_from_payload(payload: object) -> DistortionCorrection | StageGeometryCorrection:
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
        raise ValueError("Distortion correction frame size does not match image frame size.")

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
            raise ValueError("Radial distortion frame size does not match image frame size.")
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
        raise ValueError("Radial distortion frame size does not match image frame size.")
    return _apply_radial_distortion_array(array, model)


def fit_seam_radial_distortion(
    tile_images: Sequence[tuple[MicroscopeScanTile, object]],
    scale: MicroscopeScaleCalibration,
    *,
    reference_label: str = "control",
    optimization_scale: float = 0.25,
    center_search_fraction: float = 0.20,
    k1_bounds: tuple[float, float] = (-0.08, 0.08),
    k2_bounds: tuple[float, float] = (-0.08, 0.08),
    maxiter: int = 80,
    seed: int | None = 0,
    no_local_search: bool = True,
) -> SeamRadialDistortionFit:
    """Fit a single radial model so seam captures match the control frame."""

    placements = _seam_tile_pixel_placements(
        tile_images,
        scale,
        reference_label=reference_label,
    )
    if len(placements) < 2:
        raise ValueError("At least one seam tile plus a reference tile is required.")
    reference = placements[0]
    width = int(reference[3].shape[1])
    height = int(reference[3].shape[0])
    opt_scale = _positive_float(optimization_scale, "optimization_scale")
    if opt_scale > 1.0:
        opt_scale = 1.0
    scaled = _scaled_seam_placements(placements, opt_scale)
    opt_width = int(scaled[0][3].shape[1])
    opt_height = int(scaled[0][3].shape[0])
    center_fraction = _positive_float(center_search_fraction, "center_search_fraction")
    center_dx = float(opt_width) * center_fraction
    center_dy = float(opt_height) * center_fraction
    center_x = float(opt_width) * 0.5
    center_y = float(opt_height) * 0.5
    bounds = (
        (center_x - center_dx, center_x + center_dx),
        (center_y - center_dy, center_y + center_dy),
        (float(k1_bounds[0]), float(k1_bounds[1])),
        (float(k2_bounds[0]), float(k2_bounds[1])),
    )
    baseline_vector = (center_x, center_y, 0.0, 0.0)

    def objective(vector: Sequence[float]) -> float:
        model = RadialDistortionModel(
            frame_size=(opt_width, opt_height),
            center_px=(float(vector[0]), float(vector[1])),
            k1=float(vector[2]),
            k2=float(vector[3]),
        )
        return _seam_radial_distortion_score(scaled, model)

    baseline_score = objective(baseline_vector)
    result = _dual_annealing(
        objective,
        bounds,
        maxiter=int(maxiter),
        seed=seed,
        no_local_search=bool(no_local_search),
    )
    result_score = float(getattr(result, "fun", math.inf))
    if not math.isfinite(result_score) or result_score >= baseline_score:
        model = RadialDistortionModel(
            frame_size=(width, height),
            center_px=(float(width) * 0.5, float(height) * 0.5),
            k1=0.0,
            k2=0.0,
        )
        return SeamRadialDistortionFit(
            model=model,
            baseline_score=float(baseline_score),
            optimized_score=float(baseline_score),
            success=False,
            evaluations=int(getattr(result, "nfev", 0)),
            optimizer="dual_annealing",
            message=(
                "identity baseline retained; optimizer did not improve score"
            ),
        )
    best = tuple(float(value) for value in result.x)
    model = RadialDistortionModel(
        frame_size=(width, height),
        center_px=(best[0] / opt_scale, best[1] / opt_scale),
        k1=best[2],
        k2=best[3],
    )
    return SeamRadialDistortionFit(
        model=model,
        baseline_score=float(baseline_score),
        optimized_score=float(result.fun),
        success=bool(getattr(result, "success", False)),
        evaluations=int(getattr(result, "nfev", 0)),
        optimizer="dual_annealing",
        message=str(getattr(result, "message", "")),
    )


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


def detect_bright_grid(
    frame: object,
    *,
    expected_spacing_px: Point2D | None = None,
) -> GridDetection:
    """Detect visible bright grid lines in a microscope calibration frame."""

    import cv2
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


def fit_distortion_from_grid_frames(
    frames: Sequence[GridCalibrationFrame],
    *,
    frame_size: tuple[int, int],
    grid_spacing_um: float = 50.0,
    pixels_to_mm: Sequence[Sequence[float]] | None = None,
) -> dict[str, object]:
    """Fit a correction payload from partial bright-grid calibration frames."""

    expected_spacing_px = _expected_axis_spacing_px(
        pixels_to_mm,
        grid_spacing_um=grid_spacing_um,
    )
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    capture_offsets: list[list[float]] = []
    frame_candidates: list[dict[str, object]] = []
    detections: list[GridDetection] = []
    for item in frames:
        detection = detect_bright_grid(
            item.frame,
            expected_spacing_px=expected_spacing_px,
        )
        if len(detection.vertical_lines_px) < 2 or len(detection.horizontal_lines_px) < 2:
            continue
        frame_source, frame_target = _grid_points_from_detection(detection)
        if len(frame_source) < 8:
            continue
        offset = [float(item.stage_offset_mm[0]), float(item.stage_offset_mm[1])]
        candidate = _axis_distortion_payload_from_detection(
            frame_size=frame_size,
            detection=detection,
            grid_spacing_um=grid_spacing_um,
            expected_spacing_px=expected_spacing_px,
        )
        candidate["capture_offsets_mm"] = [offset]
        candidate["fit_frame_count"] = 1
        frame_candidates.append(candidate)
        detections.append(detection)
        capture_offsets.append(offset)
        source_points.extend(frame_source)
        target_points.extend(frame_target)

    if len(source_points) < 8:
        raise ValueError("Grid coverage is too small.")
    scale_payload = _grid_scale_payload_from_detections(
        detections,
        stage_offsets_mm=capture_offsets,
        pixels_to_mm=pixels_to_mm,
        grid_spacing_um=grid_spacing_um,
    )
    payload = _distortion_payload_with_residuals(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    payload["capture_offsets_mm"] = capture_offsets
    payload["fit_frame_count"] = len(capture_offsets)
    payload.update(scale_payload)
    if scale_payload:
        for candidate in frame_candidates:
            candidate.update(scale_payload)
    if len(frame_candidates) == 1:
        return frame_candidates[0]
    axis_payload = _axis_distortion_payload_from_detections(
        frame_size=frame_size,
        detections=detections,
        grid_spacing_um=grid_spacing_um,
        scale_payload=scale_payload,
    )
    if axis_payload is not None:
        axis_mean = float(axis_payload.get("residual_mean_px", math.inf))
        combined_mean = float(payload.get("residual_mean_px", math.inf))
        if axis_mean <= max(2.0, combined_mean * 2.0):
            return axis_payload
    if frame_candidates:
        best_payload = min(
            frame_candidates,
            key=lambda item: float(item.get("residual_mean_px", math.inf)),
        )
        combined_mean = float(payload.get("residual_mean_px", math.inf))
        best_mean = float(best_payload.get("residual_mean_px", math.inf))
        if combined_mean > max(8.0, best_mean * 4.0):
            return best_payload
    return payload


def _axis_distortion_payload_from_detections(
    *,
    frame_size: tuple[int, int],
    detections: Sequence[GridDetection],
    grid_spacing_um: float,
    scale_payload: dict[str, object],
) -> dict[str, object] | None:
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    x_pairs: list[tuple[float, float]] = []
    y_pairs: list[tuple[float, float]] = []
    for detection in detections:
        frame_source, frame_target = _grid_points_from_detection(detection)
        source_points.extend(frame_source)
        target_points.extend(frame_target)
        x_pairs.extend(
            zip(
                (float(value) for value in detection.vertical_lines_px),
                _regularized_positions(detection.vertical_lines_px),
            )
        )
        y_pairs.extend(
            zip(
                (float(value) for value in detection.horizontal_lines_px),
                _regularized_positions(detection.horizontal_lines_px),
            )
        )
    axis_x = _consolidated_axis_pairs(x_pairs)
    axis_y = _consolidated_axis_pairs(y_pairs)
    if axis_x is None or axis_y is None or len(source_points) < 8:
        return None
    axis_source_x, axis_target_x = axis_x
    axis_source_y, axis_target_y = axis_y
    payload = distortion_payload_from_points(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    residuals = _axis_interpolation_residuals(
        axis_source_x=axis_source_x,
        axis_target_x=axis_target_x,
        axis_source_y=axis_source_y,
        axis_target_y=axis_target_y,
        source_points=source_points,
        target_points=target_points,
    )
    payload["residual_mean_px"] = (
        float(sum(residuals) / len(residuals)) if residuals else 0.0
    )
    payload["residual_max_px"] = float(max(residuals)) if residuals else 0.0
    payload["axis_source_x"] = [float(value) for value in axis_source_x]
    payload["axis_target_x"] = [float(value) for value in axis_target_x]
    payload["axis_source_y"] = [float(value) for value in axis_source_y]
    payload["axis_target_y"] = [float(value) for value in axis_target_y]
    payload["fit_frame_count"] = len(detections)
    payload.update(scale_payload)
    return payload


def _consolidated_axis_pairs(
    pairs: Sequence[tuple[float, float]],
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    values = sorted(
        (
            (float(source), float(target))
            for source, target in pairs
            if math.isfinite(float(source)) and math.isfinite(float(target))
        ),
        key=lambda item: item[0],
    )
    if len(values) < 2:
        return None
    merged: list[tuple[float, float, int]] = []
    for source, target in values:
        if not merged or abs(source - merged[-1][0]) >= 3.0:
            merged.append((source, target, 1))
            continue
        old_source, old_target, count = merged[-1]
        next_count = count + 1
        merged[-1] = (
            (old_source * count + source) / next_count,
            (old_target * count + target) / next_count,
            next_count,
        )
    source_axis: list[float] = []
    target_axis: list[float] = []
    for source, target, _count in merged:
        if source_axis and (
            source <= source_axis[-1] + 1e-6 or target <= target_axis[-1] + 1e-6
        ):
            continue
        source_axis.append(source)
        target_axis.append(target)
    if len(source_axis) < 2:
        return None
    return (tuple(source_axis), tuple(target_axis))


def _grid_scale_payload_from_detections(
    detections: Sequence[GridDetection],
    *,
    stage_offsets_mm: Sequence[Sequence[float]] = (),
    pixels_to_mm: Sequence[Sequence[float]] | None,
    grid_spacing_um: float,
) -> dict[str, object]:
    matrix = _valid_pixels_to_mm_matrix(pixels_to_mm)
    if matrix is None:
        return {}
    spacing_mm = _positive_float(grid_spacing_um, "grid_spacing_um") / 1000.0
    x_spacing_px = _median_grid_line_spacing_px(
        detection.vertical_lines_px for detection in detections
    )
    y_spacing_px = _median_grid_line_spacing_px(
        detection.horizontal_lines_px for detection in detections
    )
    if x_spacing_px is None or y_spacing_px is None:
        return {}
    calibrated = _rescale_pixel_matrix_axes(
        matrix,
        x_mm_per_px=spacing_mm / x_spacing_px,
        y_mm_per_px=spacing_mm / y_spacing_px,
    )
    payload: dict[str, object] = {
        "grid_line_spacing_px": [float(x_spacing_px), float(y_spacing_px)],
        "grid_pixel_size_um": [
            float(spacing_mm / x_spacing_px * 1000.0),
            float(spacing_mm / y_spacing_px * 1000.0),
        ],
    }
    if calibrated is not None:
        payload["grid_pixels_to_mm_estimate"] = [
            [float(calibrated[0][0]), float(calibrated[0][1])],
            [float(calibrated[1][0]), float(calibrated[1][1])],
        ]
    stage_x_spacing_mm = _median_nonzero_stage_offset_mm(stage_offsets_mm, 0)
    stage_y_spacing_mm = _median_nonzero_stage_offset_mm(stage_offsets_mm, 1)
    if stage_x_spacing_mm is None or stage_y_spacing_mm is None:
        return payload
    if not _stage_offsets_match_grid_spacing(
        stage_x_spacing_mm,
        stage_y_spacing_mm,
        expected_spacing_mm=spacing_mm,
    ):
        return payload
    stage_calibrated = _rescale_pixel_matrix_axes(
        matrix,
        x_mm_per_px=stage_x_spacing_mm / x_spacing_px,
        y_mm_per_px=stage_y_spacing_mm / y_spacing_px,
    )
    if stage_calibrated is None:
        return payload
    payload["calibrated_pixels_to_mm"] = [
        [float(stage_calibrated[0][0]), float(stage_calibrated[0][1])],
        [float(stage_calibrated[1][0]), float(stage_calibrated[1][1])],
    ]
    payload["calibrated_pixel_size_um"] = [
        float(stage_x_spacing_mm / x_spacing_px * 1000.0),
        float(stage_y_spacing_mm / y_spacing_px * 1000.0),
    ]
    payload["calibrated_stage_spacing_mm"] = [
        float(stage_x_spacing_mm),
        float(stage_y_spacing_mm),
    ]
    return payload


def _stage_offsets_match_grid_spacing(
    x_spacing_mm: float,
    y_spacing_mm: float,
    *,
    expected_spacing_mm: float,
) -> bool:
    if (
        not math.isfinite(x_spacing_mm)
        or not math.isfinite(y_spacing_mm)
        or not math.isfinite(expected_spacing_mm)
        or expected_spacing_mm <= 0.0
    ):
        return False
    tolerance = max(expected_spacing_mm * 0.25, 1e-6)
    return (
        abs(float(x_spacing_mm) - expected_spacing_mm) <= tolerance
        and abs(float(y_spacing_mm) - expected_spacing_mm) <= tolerance
    )


def _median_nonzero_stage_offset_mm(
    offsets: Sequence[Sequence[float]],
    axis_index: int,
) -> float | None:
    values: list[float] = []
    for offset in offsets:
        try:
            value = abs(float(offset[axis_index]))
        except (IndexError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 1e-9:
            values.append(value)
    if not values:
        return None
    values.sort()
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) * 0.5


def _median_grid_line_spacing_px(
    line_groups: Iterable[Sequence[float]],
) -> float | None:
    samples: list[float] = []
    for lines in line_groups:
        regularized = _regularized_positions(lines)
        for left, right in zip(regularized, regularized[1:]):
            spacing = float(right) - float(left)
            if math.isfinite(spacing) and spacing > 0.0:
                samples.append(spacing)
    if not samples:
        return None
    values = sorted(samples)
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) * 0.5


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
    if (
        not isinstance(raw_point, Iterable)
        or isinstance(raw_point, (str, bytes))
    ):
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


def _matrix_mean_pixel_size_mm(matrix: object) -> float:
    import numpy as np

    values = np.asarray(matrix, dtype=float)
    if values.shape != (2, 2):
        return 1.0
    column_x = values[:, 0]
    column_y = values[:, 1]
    pixel_size = (float(np.linalg.norm(column_x)) + float(np.linalg.norm(column_y))) * 0.5
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


def _stage_geometry_residual_stats(residuals_mm: object, matrix: object) -> tuple[float, float, float, float]:
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


def _rescale_pixel_matrix_axes(
    matrix: tuple[tuple[float, float], tuple[float, float]],
    *,
    x_mm_per_px: float,
    y_mm_per_px: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if (
        not math.isfinite(x_mm_per_px)
        or not math.isfinite(y_mm_per_px)
        or x_mm_per_px <= 0.0
        or y_mm_per_px <= 0.0
    ):
        return None
    x_norm = math.hypot(matrix[0][0], matrix[1][0])
    y_norm = math.hypot(matrix[0][1], matrix[1][1])
    if x_norm <= 0.0 or y_norm <= 0.0:
        return None
    x_scale = x_mm_per_px / x_norm
    y_scale = y_mm_per_px / y_norm
    calibrated = (
        (matrix[0][0] * x_scale, matrix[0][1] * y_scale),
        (matrix[1][0] * x_scale, matrix[1][1] * y_scale),
    )
    determinant = (
        calibrated[0][0] * calibrated[1][1]
        - calibrated[0][1] * calibrated[1][0]
    )
    if not math.isfinite(determinant) or abs(determinant) < 1e-18:
        return None
    return calibrated


def _grid_points_from_detection(
    detection: GridDetection,
    *,
    expected_spacing_px: Point2D | None = None,
) -> tuple[list[Point2D], list[Point2D]]:
    source_points: list[Point2D] = []
    target_points: list[Point2D] = []
    target_x = _regularized_positions(detection.vertical_lines_px)
    target_y = _regularized_positions(detection.horizontal_lines_px)
    for row, source_y in enumerate(detection.horizontal_lines_px):
        for column, source_x in enumerate(detection.vertical_lines_px):
            source_points.append((float(source_x), float(source_y)))
            target_points.append((float(target_x[column]), float(target_y[row])))
    return source_points, target_points


def _distortion_payload_with_residuals(
    *,
    frame_size: tuple[int, int],
    source_points: Sequence[Sequence[float]],
    target_points: Sequence[Sequence[float]],
    grid_spacing_um: float,
) -> dict[str, object]:
    payload = distortion_payload_from_points(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    correction = correction_from_payload(payload)
    residuals = _homography_residuals(
        correction.homography,
        correction.source_points,
        correction.target_points,
    )
    payload["residual_mean_px"] = (
        float(sum(residuals) / len(residuals)) if residuals else 0.0
    )
    payload["residual_max_px"] = float(max(residuals)) if residuals else 0.0
    return payload


def _axis_distortion_payload_from_detection(
    *,
    frame_size: tuple[int, int],
    detection: GridDetection,
    grid_spacing_um: float,
    expected_spacing_px: Point2D | None = None,
) -> dict[str, object]:
    source_points, target_points = _grid_points_from_detection(
        detection,
    )
    axis_source_x = tuple(float(value) for value in detection.vertical_lines_px)
    expected_x = _positive_optional_spacing(expected_spacing_px, 0)
    expected_y = _positive_optional_spacing(expected_spacing_px, 1)
    axis_target_x = _regularized_positions(axis_source_x)
    axis_source_y = tuple(float(value) for value in detection.horizontal_lines_px)
    axis_target_y = _regularized_positions(axis_source_y)
    payload = distortion_payload_from_points(
        frame_size=frame_size,
        source_points=source_points,
        target_points=target_points,
        grid_spacing_um=grid_spacing_um,
    )
    residuals = _axis_interpolation_residuals(
        axis_source_x=axis_source_x,
        axis_target_x=axis_target_x,
        axis_source_y=axis_source_y,
        axis_target_y=axis_target_y,
        source_points=source_points,
        target_points=target_points,
    )
    payload["residual_mean_px"] = (
        float(sum(residuals) / len(residuals)) if residuals else 0.0
    )
    payload["residual_max_px"] = float(max(residuals)) if residuals else 0.0
    payload["axis_source_x"] = [float(value) for value in axis_source_x]
    payload["axis_target_x"] = [float(value) for value in axis_target_x]
    payload["axis_source_y"] = [float(value) for value in axis_source_y]
    payload["axis_target_y"] = [float(value) for value in axis_target_y]
    if expected_x is not None or expected_y is not None:
        payload["axis_expected_spacing_px"] = [
            float(expected_x) if expected_x is not None else None,
            float(expected_y) if expected_y is not None else None,
        ]
    return payload


def _dual_annealing(objective: object, bounds: Sequence[tuple[float, float]], **kwargs):
    from scipy.optimize import dual_annealing

    return dual_annealing(objective, bounds=list(bounds), **kwargs)


def _least_squares(fun: object, x0: object, **kwargs):
    from scipy.optimize import least_squares

    return least_squares(fun, x0, **kwargs)


def _stage_geometry_correction_from_payload(payload: dict[str, object]) -> StageGeometryCorrection:
    frame_size = _valid_frame_size(payload.get("frame_size"))
    matrix = _valid_pixels_to_mm_matrix(payload.get("pixels_to_mm"))
    if matrix is None:
        raise ValueError("Stage-geometry correction requires a valid pixels_to_mm matrix.")
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
        raise ValueError("Distortion correction frame size does not match image frame size.")
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
    return observed_x.astype(np.float32, copy=False), observed_y.astype(np.float32, copy=False)


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
        raise ValueError("Radial distortion frame size does not match image frame size.")
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


def _seam_tile_pixel_placements(
    tile_images: Sequence[tuple[MicroscopeScanTile, object]],
    scale: MicroscopeScaleCalibration,
    *,
    reference_label: str,
) -> list[tuple[MicroscopeScanTile, float, float, object]]:
    items = list(tile_images)
    if not items:
        raise ValueError("No seam tiles were provided.")
    reference_index = 0
    for index, (tile, _image) in enumerate(items):
        label = str(getattr(tile, "label", "") or "")
        if label == reference_label:
            reference_index = index
            break
    reference_tile = items[reference_index][0]
    ordered = [items[reference_index], *items[:reference_index], *items[reference_index + 1 :]]
    reference_stage = reference_tile.stage_xy
    reference_raw = _image_array(ordered[0][1])
    reference_size = reference_raw.shape[:2]
    placements: list[tuple[MicroscopeScanTile, float, float, object]] = []
    for tile, image in ordered:
        raw = _image_array(image)
        if raw.shape[:2] != reference_size:
            raise ValueError("All seam tiles must have the same frame size.")
        center_dx, center_dy = scale.stage_delta_to_pixel(
            float(tile.stage_xy[0]) - float(reference_stage[0]),
            float(tile.stage_xy[1]) - float(reference_stage[1]),
        )
        left_px = -float(center_dx) - float(raw.shape[1]) * 0.5
        top_px = float(center_dy) - float(raw.shape[0]) * 0.5
        placements.append((tile, left_px, top_px, raw))
    return placements


def _scaled_seam_placements(
    placements: Sequence[tuple[MicroscopeScanTile, float, float, object]],
    scale: float,
) -> list[tuple[MicroscopeScanTile, float, float, object]]:
    import cv2
    import numpy as np

    if abs(float(scale) - 1.0) <= 1e-12:
        return [
            (tile, float(left), float(top), np.asarray(raw).copy())
            for tile, left, top, raw in placements
        ]
    result: list[tuple[MicroscopeScanTile, float, float, object]] = []
    for tile, left, top, raw in placements:
        array = np.asarray(raw)
        width = max(1, int(round(float(array.shape[1]) * float(scale))))
        height = max(1, int(round(float(array.shape[0]) * float(scale))))
        resized = cv2.resize(array, (width, height), interpolation=cv2.INTER_AREA)
        result.append((tile, float(left) * float(scale), float(top) * float(scale), resized))
    return result


def _seam_radial_distortion_score(
    placements: Sequence[tuple[MicroscopeScanTile, float, float, object]],
    model: RadialDistortionModel,
) -> float:
    import numpy as np

    scored: list[tuple[MicroscopeScanTile, float, float, object]] = []
    for tile, left, top, raw in placements:
        corrected = _apply_radial_distortion_array(raw, model)
        scored.append((tile, float(left), float(top), _contour_array(corrected)))
    reference = scored[0]
    weighted_error = 0.0
    total_weight = 0.0
    for current in scored[1:]:
        patches = _score_overlap_patches(reference, current)
        if patches is None:
            continue
        reference_patch, current_patch = patches
        mask = np.maximum(reference_patch, current_patch)
        active = mask >= max(0.03, float(np.percentile(mask, 75.0)) * 0.35)
        weights = 1.0 + 4.0 * mask
        if np.count_nonzero(active) >= max(16, int(active.size * 0.002)):
            diff = reference_patch[active] - current_patch[active]
            active_weights = weights[active]
        else:
            diff = reference_patch - current_patch
            active_weights = weights
        weighted_error += float(np.sum((diff * diff) * active_weights))
        total_weight += float(np.sum(active_weights))
    if total_weight <= 0.0:
        return 1e6
    return weighted_error / total_weight


def _score_overlap_patches(
    existing: tuple[MicroscopeScanTile, float, float, object],
    current: tuple[MicroscopeScanTile, float, float, object],
) -> tuple[object, object] | None:
    import numpy as np

    _existing_tile, existing_left, existing_top, existing_raw = existing
    _current_tile, current_left, current_top, current_raw = current
    first = np.asarray(existing_raw)
    second = np.asarray(current_raw)
    existing_h, existing_w = first.shape[:2]
    current_h, current_w = second.shape[:2]
    left = max(float(existing_left), float(current_left))
    top = max(float(existing_top), float(current_top))
    right = min(float(existing_left) + existing_w, float(current_left) + current_w)
    bottom = min(float(existing_top) + existing_h, float(current_top) + current_h)
    overlap_w = int(round(right - left))
    overlap_h = int(round(bottom - top))
    if overlap_w < 8 or overlap_h < 8:
        return None
    x0 = int(round(left))
    y0 = int(round(top))
    existing_x = x0 - int(round(float(existing_left)))
    existing_y = y0 - int(round(float(existing_top)))
    current_x = x0 - int(round(float(current_left)))
    current_y = y0 - int(round(float(current_top)))
    existing_patch = first[
        existing_y : existing_y + overlap_h,
        existing_x : existing_x + overlap_w,
    ]
    current_patch = second[
        current_y : current_y + overlap_h,
        current_x : current_x + overlap_w,
    ]
    if existing_patch.shape != current_patch.shape:
        return None
    return existing_patch, current_patch


def _contour_array(array: object):
    import cv2
    import numpy as np

    image = np.asarray(array)
    if image.ndim == 2:
        gray = image.astype(np.float32, copy=False)
    elif image.ndim == 3 and image.shape[2] >= 3:
        gray = cv2.cvtColor(
            image[:, :, :3].astype(np.uint8, copy=False),
            cv2.COLOR_RGB2GRAY,
        ).astype(np.float32, copy=False)
    else:
        return np.asarray([], dtype=np.float32)
    if gray.size == 0:
        return gray.astype(np.float32, copy=False)
    gray = gray / 255.0
    blurred = cv2.GaussianBlur(gray, (0, 0), 1.0)
    grad_x = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    edge = cv2.magnitude(grad_x, grad_y)
    normalizer = float(np.percentile(edge, 99.0))
    if normalizer <= 1e-9 or not math.isfinite(normalizer):
        return np.zeros_like(edge, dtype=np.float32)
    return np.clip(edge / normalizer, 0.0, 1.0).astype(np.float32, copy=False)


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


def _axis_interpolation_residuals(
    *,
    axis_source_x: Sequence[float],
    axis_target_x: Sequence[float],
    axis_source_y: Sequence[float],
    axis_target_y: Sequence[float],
    source_points: Sequence[Point2D],
    target_points: Sequence[Point2D],
) -> list[float]:
    import numpy as np

    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    if source.size == 0 or target.size == 0:
        return []
    mapped_x = _interp_with_extrapolation(
        source[:, 0],
        axis_source_x,
        axis_target_x,
    )
    mapped_y = _interp_with_extrapolation(
        source[:, 1],
        axis_source_y,
        axis_target_y,
    )
    mapped = np.column_stack((mapped_x, mapped_y))
    errors = np.linalg.norm(mapped - target, axis=1)
    return [float(value) for value in errors if math.isfinite(float(value))]


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
        return cv2.cvtColor(array[:, :, :3].astype(np.uint8, copy=False), cv2.COLOR_RGB2GRAY)
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
        return tuple(center + (float(index) - mid) * target_step_px for index in range(len(values)))
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

    largest_count = min(len(values), 5 if expected_spacing_px is not None else len(values))
    smallest_count = 2 if expected_spacing_px is not None else 3
    for count in range(largest_count, smallest_count - 1, -1):
        candidates: list[tuple[float, float, float, tuple[float, ...]]] = []
        for subset in combinations(values, count):
            step = (subset[-1] - subset[0]) / float(count - 1)
            if step <= 0.0:
                continue
            if expected_spacing_px is not None and not _regular_subset_spacing_is_expected(
                subset,
                expected_spacing_px,
            ):
                continue
            target = _regularized_positions(subset)
            errors = [abs(value - expected) for value, expected in zip(subset, target)]
            max_error = max(errors)
            tolerance = max(12.0, abs(step) * 0.12)
            if max_error <= tolerance:
                spacing_error = 0.0
                if expected_spacing_px is not None:
                    spacing_error = abs(step - expected_spacing_px) / expected_spacing_px
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


def _expected_axis_spacing_px(
    pixels_to_mm: Sequence[Sequence[float]] | None,
    *,
    grid_spacing_um: float,
) -> Point2D | None:
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
    spacing_mm = _positive_float(grid_spacing_um, "grid_spacing_um") / 1000.0
    x_mm_per_px = math.hypot(rows[0][0], rows[1][0])
    y_mm_per_px = math.hypot(rows[0][1], rows[1][1])
    if x_mm_per_px <= 0.0 or y_mm_per_px <= 0.0:
        return None
    return (spacing_mm / x_mm_per_px, spacing_mm / y_mm_per_px)


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


def _homography_residuals(
    homography: object,
    source_points: Sequence[Point2D],
    target_points: Sequence[Point2D],
) -> list[float]:
    import cv2
    import numpy as np

    source = np.asarray(source_points, dtype=np.float32).reshape((-1, 1, 2))
    projected = cv2.perspectiveTransform(source, homography).reshape((-1, 2))
    target = np.asarray(target_points, dtype=np.float32)
    errors = np.linalg.norm(projected - target, axis=1)
    return [float(value) for value in errors if math.isfinite(float(value))]


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
    "BrightFeatureBounds",
    "DistortionCorrection",
    "GridCalibrationFrame",
    "GridDetection",
    "MODEL_VERSION",
    "RadialDistortionModel",
    "SeamRadialDistortionFit",
    "StageFeatureObservation",
    "StageGeometryCalibrationFit",
    "StageGeometryCorrection",
    "apply_distortion_correction",
    "apply_radial_distortion_correction",
    "correction_from_payload",
    "detect_bright_feature_bounds",
    "detect_bright_grid",
    "distortion_payload_from_points",
    "fit_seam_radial_distortion",
    "fit_distortion_from_grid_frames",
    "fit_stage_geometry_from_grid_frames",
    "fit_stage_geometry_from_observations",
]
