"""Seam-debug radial distortion fitting and scoring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from probe_station_gui.camera.distortion import (
    RadialDistortionModel,
    _apply_radial_distortion_array,
    _image_array,
    _positive_float,
)
from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanTile,
)


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
            message=("identity baseline retained; optimizer did not improve score"),
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


def _dual_annealing(objective: object, bounds: Sequence[tuple[float, float]], **kwargs):
    from scipy.optimize import dual_annealing

    return dual_annealing(objective, bounds=list(bounds), **kwargs)


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
    ordered = [
        items[reference_index],
        *items[:reference_index],
        *items[reference_index + 1 :],
    ]
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
        result.append(
            (tile, float(left) * float(scale), float(top) * float(scale), resized)
        )
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


__all__ = [
    "SeamRadialDistortionFit",
    "fit_seam_radial_distortion",
]
